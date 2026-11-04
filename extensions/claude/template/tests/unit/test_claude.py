# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""How `app/app_utils/claude.py` drives the SDK, with the SDK's `query` faked."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    InMemorySessionStore,
    MirrorErrorMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from openinference.instrumentation import TraceConfig
from openinference.instrumentation.claude_agent_sdk import ClaudeAgentSDKInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.app_utils import (
    claude,
    replies,
    settings,
    turn_options,
    workspaces,
)
from app.app_utils.telemetry import traces
from tests.unit.helpers import workspaces_root, write_local_transcript

pytestmark = pytest.mark.usefixtures("isolated")


def _result(**overrides) -> ResultMessage:
    fields = {
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": "s-1",
        "result": "It's foggy.",
    }
    return ResultMessage(**{**fields, **overrides})


def _delta(text: str, parent: str | None = None) -> StreamEvent:
    return StreamEvent(
        uuid="u",
        session_id="s-1",
        event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
        parent_tool_use_id=parent,
    )


_TOOL_TURN = [
    AssistantMessage(
        content=[
            ToolUseBlock(id="t1", name="mcp__w__get_weather", input={"city": "SF"})
        ],
        model="m",
    ),
    UserMessage(
        content=[
            ToolResultBlock(tool_use_id="t1", content=[{"type": "text", "text": "60F"}])
        ]
    ),
    AssistantMessage(content=[TextBlock(text="It's foggy.")], model="m"),
    _result(),
]


def _fake_query(messages, seen: list[dict] | None = None):
    async def fake(*, prompt, options):
        if seen is not None:
            seen.append({"prompt": prompt, "options": options})
        for message in messages:
            yield message

    return fake


class TestRunTurn:
    def test_collects_reply_session_and_tool_calls(self):
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(_TOOL_TURN)):
            reply = anyio.run(claude.run_turn, "weather?", ClaudeAgentOptions(), "c")

        assert reply.text == "It's foggy."
        assert reply.session_id == "s-1"
        assert reply.tool_calls == [
            {
                "id": "t1",
                "name": "mcp__w__get_weather",
                "args": {"city": "SF"},
                "result": "60F",
            }
        ]

    def test_a_subagents_tool_calls_are_kept_apart_under_its_name(self):
        delegate = {"subagent_type": "forecaster", "prompt": "Weather in SF?"}
        messages = [
            AssistantMessage(
                content=[ToolUseBlock(id="a1", name="Agent", input=delegate)],
                model="m",
            ),
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="get_weather", input={})],
                model="m",
                parent_tool_use_id="a1",
            ),
            UserMessage(
                content=[ToolResultBlock(tool_use_id="t1", content="60F")],
                parent_tool_use_id="a1",
            ),
            UserMessage(content=[ToolResultBlock(tool_use_id="a1", content="Foggy")]),
            _result(),
        ]
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(messages)):
            reply = anyio.run(claude.run_turn, "weather?", ClaudeAgentOptions(), "c")

        assert [call["name"] for call in reply.tool_calls] == ["Agent"]
        assert reply.subagent_calls == [
            {
                "id": "t1",
                "name": "get_weather",
                "args": {},
                "agent": "forecaster",
                "parent": "a1",
                "result": "60F",
            }
        ]

    def test_error_result_raises(self):
        failing = [_result(is_error=True, result="model not available")]
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(failing)):
            with pytest.raises(claude.AgentError, match="model not available"):
                anyio.run(claude.run_turn, "hi", ClaudeAgentOptions(), "c")

    # The SDK reports a dropped write mid-turn, or just before the result.
    @pytest.mark.parametrize("at", [0, len(_TOOL_TURN) - 1])
    def test_a_failed_session_store_write_fails_the_turn_once_it_ends(self, at):
        dropped = MirrorErrorMessage(
            subtype="mirror_error", data={}, error="connection refused"
        )
        messages = [*_TOOL_TURN[:at], dropped, *_TOOL_TURN[at:]]
        seen = []

        async def read() -> None:
            async for message in claude.stream_turn("hi", ClaudeAgentOptions(), "c"):
                seen.append(message)

        with patch.object(claude.claude_agent_sdk, "query", _fake_query(messages)):
            with pytest.raises(claude.AgentError, match="connection refused"):
                anyio.run(read)
        # Read to the end, so analytics still get the result's cost and turns.
        assert seen == _TOOL_TURN

    @pytest.mark.parametrize("capture", [None, "true"])
    def test_a_denied_call_is_logged_with_its_arguments_only_on_capture(
        self, monkeypatch, caplog, capture
    ):
        if capture:
            monkeypatch.setenv(
                "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", capture
            )
        else:
            monkeypatch.delenv(
                "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", raising=False
            )
        denial = {
            "tool_name": "Bash",
            "tool_use_id": "t1",
            "tool_input": {"cmd": "ls ~"},
        }
        denied = [_result(permission_denials=[denial])]
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(denied)):
            anyio.run(claude.run_turn, "hi", ClaudeAgentOptions(), "c")

        [warning] = [
            r.getMessage() for r in caplog.records if "denied" in r.getMessage()
        ]
        assert "Bash" in warning
        assert ("ls ~" in warning) == bool(capture)

    def test_an_error_result_closes_the_sdk_stream(self):
        closed = []

        async def query(*, prompt, options):
            try:
                yield _result(is_error=True, result="max turns")
                yield _result()  # never reached: the turn stops at the error
            finally:
                closed.append(True)

        async def turn_then_check():
            with pytest.raises(claude.AgentError):
                await claude.run_turn("hi", ClaudeAgentOptions(), "c")
            # Checked while the loop runs: the stream must close when the turn ends.
            return list(closed)

        with patch.object(claude.claude_agent_sdk, "query", query):
            assert anyio.run(turn_then_check) == [True]

    @pytest.mark.parametrize(
        ("cli_path", "skipped"), [(None, "1"), ("/usr/local/bin/claude", None)]
    )
    def test_the_cli_version_check_is_skipped_only_for_the_bundled_cli(
        self, monkeypatch, cli_path, skipped
    ):
        # setenv first so monkeypatch removes what the turn sets.
        monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "")
        monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK")
        with patch.object(claude.claude_agent_sdk, "query", _fake_query([_result()])):
            anyio.run(claude.run_turn, "hi", ClaudeAgentOptions(cli_path=cli_path), "c")
        assert os.environ.get("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK") == skipped

    def test_least_recently_used_workspaces_are_evicted(self, monkeypatch):
        monkeypatch.setattr(workspaces, "MAX_WORKSPACES", 2)
        # Another process serving the same agent shares its directory.
        (workspaces_root() / "other-service").mkdir(parents=True)
        with patch.object(claude.claude_agent_sdk, "query", _fake_query([_result()])):
            for conversation in ["old", "mid", "new", "old", "newest"]:
                anyio.run(claude.run_turn, "hi", ClaudeAgentOptions(), conversation)

        kept = {d.name for d in workspaces_root().iterdir()}
        assert kept == {"other-service"} | {
            turn_options.session_id_for(c) for c in ("old", "newest")
        }

    def test_a_workspace_with_a_turn_running_is_not_evicted(self, monkeypatch):
        monkeypatch.setattr(workspaces, "MAX_WORKSPACES", 1)
        cwd_existed = []

        async def query(*, prompt, options):
            if prompt == "long":
                events["started"].set()
                await events["finish"].wait()
                cwd_existed.append(pathlib.Path(options.cwd).is_dir())
            yield _result()

        async def overlapping_turns():
            events.update(started=anyio.Event(), finish=anyio.Event())
            async with anyio.create_task_group() as tg:
                tg.start_soon(claude.run_turn, "long", ClaudeAgentOptions(), "a")
                await events["started"].wait()
                await claude.run_turn("short", ClaudeAgentOptions(), "b")
                events["finish"].set()

        events: dict[str, anyio.Event] = {}
        with patch.object(claude.claude_agent_sdk, "query", query):
            anyio.run(overlapping_turns)

        assert cwd_existed == [True]

    def test_turns_are_recorded_for_bigquery_only_when_it_is_on(self, monkeypatch):
        rows: list[dict] = []
        writer = MagicMock(put=rows.append)
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(_TOOL_TURN)):
            with patch.object(claude.bq_analytics, "writer", return_value=None):
                anyio.run(claude.run_turn, "weather?", ClaudeAgentOptions(), "c")
            assert rows == []
            with patch.object(claude.bq_analytics, "writer", return_value=writer):
                anyio.run(claude.run_turn, "weather?", ClaudeAgentOptions(), "conv-1")

        assert rows[0]["event_type"] == "USER_MESSAGE_RECEIVED"
        assert rows[-1]["event_type"] == "INVOCATION_COMPLETED"
        assert {r["session_id"] for r in rows} == {
            turn_options.session_id_for("conv-1")
        }
        assert {r["user_id"] for r in rows} == {"A2A_USER_conv-1"}

    def test_a_failing_observer_never_fails_the_turn_or_hides_its_error(
        self, monkeypatch, caplog
    ):
        class Broken:
            def __init__(self, agent_name):
                pass

            def handle(self, event):
                raise ValueError("observer bug")

        async def failing_query(*, prompt, options):
            yield _TOOL_TURN[0]
            raise RuntimeError("CLI exited")

        monkeypatch.setattr(claude, "CallCounts", Broken)
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(_TOOL_TURN)):
            reply = anyio.run(claude.run_turn, "weather?", ClaudeAgentOptions(), "c")
        with patch.object(claude.claude_agent_sdk, "query", failing_query):
            with pytest.raises(RuntimeError, match="CLI exited"):
                anyio.run(claude.run_turn, "weather?", ClaudeAgentOptions(), "c")

        assert reply.text == "It's foggy."
        assert "Broken failed on TurnEnded" in caplog.text

    def test_model_calls_are_logged_when_the_upload_hook_is_on(self, monkeypatch):
        monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK", "upload")
        monkeypatch.setenv(
            "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH", "memory://logs/completions"
        )
        events: list = []
        monkeypatch.setattr(claude.completions.telemetry, "emit_event", events.append)
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(_TOOL_TURN)):
            anyio.run(claude.run_turn, "weather?", ClaudeAgentOptions(), "conv-1")

        # The tool call and the answer.
        assert len(events) == 2
        assert {e.attributes["gen_ai.conversation.id"] for e in events} == {
            turn_options.session_id_for("conv-1")
        }

    def test_turns_of_one_conversation_run_one_after_another(self):
        events: list[str] = []
        resumed: dict[str, str | None] = {}

        async def query(*, prompt, options):
            events.append(f"start {prompt}")
            resumed[prompt] = options.resume
            if prompt != "c":
                write_local_transcript("ctx-1")
            await anyio.sleep(0.01)
            events.append(f"end {prompt}")
            yield _result(result=prompt)

        async def overlapping_turns():
            async with anyio.create_task_group() as tg:
                for prompt, conversation in [
                    ("a", "ctx-1"),
                    ("b", "ctx-1"),
                    ("c", "ctx-2"),
                ]:
                    tg.start_soon(
                        claude.run_turn, prompt, ClaudeAgentOptions(), conversation
                    )

        with patch.object(claude.claude_agent_sdk, "query", query):
            anyio.run(overlapping_turns)

        # The second turn resumes the first's transcript instead of starting a
        # parallel one, and another conversation doesn't wait for either.
        assert events.index("end a") < events.index("start b")
        assert events.index("start c") < events.index("end a")
        assert resumed == {
            "a": None,
            "b": turn_options.session_id_for("ctx-1"),
            "c": None,
        }
        assert not claude._turn_locks

    # Without its prompt first, the subagent's first message is its tool call.
    @pytest.mark.parametrize("prompt_first", [True, False])
    def test_a_subagents_spans_name_it_and_hold_its_model_calls(
        self, monkeypatch, prompt_first
    ):
        monkeypatch.setenv("OPENINFERENCE_ENABLE_GENAI_SEMCONV", "true")
        monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK", "upload")
        monkeypatch.setenv(
            "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH", "memory://logs/completions"
        )
        monkeypatch.setattr(claude.completions.telemetry, "emit_event", lambda _: None)
        monkeypatch.setattr(settings, "AGENT_NAME", "weather_agent")
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(traces.AgentNameProcessor("weather_agent"))
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        delegate = ToolUseBlock(
            id="agent-1",
            name="Agent",
            input={"subagent_type": "weather_researcher", "prompt": "Paris?"},
        )
        lookup = ToolUseBlock(id="t1", name="get_weather", input={"city": "Paris"})
        # The CLI runs the subagent in the background: the Agent call returns
        # before the subagent's messages arrive.
        turn = [
            AssistantMessage(content=[delegate], model="m", message_id="m1"),
            UserMessage(content=[ToolResultBlock(tool_use_id="agent-1", content="")]),
            *(
                [UserMessage(content="Paris?", parent_tool_use_id="agent-1")]
                if prompt_first
                else []
            ),
            AssistantMessage(
                content=[lookup],
                model="m",
                message_id="s1",
                parent_tool_use_id="agent-1",
            ),
            UserMessage(
                content=[ToolResultBlock(tool_use_id="t1", content="Sunny.")],
                parent_tool_use_id="agent-1",
            ),
            AssistantMessage(
                content=[TextBlock(text="Sunny.")],
                model="m",
                message_id="s2",
                parent_tool_use_id="agent-1",
            ),
            AssistantMessage(
                content=[TextBlock(text="It's sunny.")], model="m", message_id="m2"
            ),
            _result(),
        ]
        instrumentor = ClaudeAgentSDKInstrumentor()
        # The instrumentor wraps query() where the SDK defines it, then
        # re-exports the wrapper; uninstrument() re-exports the original.
        query_module = sys.modules["claude_agent_sdk.query"]
        with (
            patch.object(query_module, "query", _fake_query(turn)),
            patch.object(claude.claude_agent_sdk, "query"),
        ):
            instrumentor.instrument(tracer_provider=provider, config=TraceConfig())
            try:
                with patch.object(
                    claude.completions.trace, "get_tracer", provider.get_tracer
                ):
                    anyio.run(claude.run_turn, "weather?", ClaudeAgentOptions(), "c")
            finally:
                instrumentor.uninstrument()

        spans = exporter.get_finished_spans()
        [root] = [s for s in spans if s.name == "ClaudeAgentSDK.query"]
        [subagent] = [s for s in spans if s.parent and s.name.startswith("ClaudeAgent")]
        [tool] = [s for s in spans if s.name == "get_weather"]
        [call] = [s for s in spans if s.name == "Agent"]
        chats = [s for s in spans if s.name == "chat m"]

        def agent_of(span):
            return span.attributes["gen_ai.agent.name"]

        assert subagent.name == "ClaudeAgentSDK.weather_researcher"
        assert agent_of(subagent) == "weather_researcher"
        assert tool.parent.span_id == subagent.context.span_id
        assert agent_of(tool) == "weather_researcher"
        assert agent_of(call) == "weather_agent"
        nested: dict[int, list[str]] = {}
        for chat in chats:
            nested.setdefault(chat.parent.span_id, []).append(agent_of(chat))
        assert nested == {
            root.context.span_id: ["weather_agent"] * 2,
            subagent.context.span_id: ["weather_researcher"] * 2,
        }

    def test_missing_mcp_command_fails_with_a_fix(self):
        options = ClaudeAgentOptions(
            mcp_servers={"fs": {"command": "no-such-binary-xyz", "args": []}}
        )
        with pytest.raises(claude.AgentError, match="nodejs"):
            anyio.run(claude.run_turn, "hi", options, "c")

    def test_no_model_fails_with_a_fix_before_calling_the_model(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_MODEL")
        # Turns always go to Gemini platform, so an API key names no model.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        query = MagicMock()
        with patch.object(claude.claude_agent_sdk, "query", query):
            with pytest.raises(claude.AgentError, match="model="):
                anyio.run(claude.run_turn, "hi", ClaudeAgentOptions(), "c")
        query.assert_not_called()

    @pytest.mark.parametrize(
        "options",
        [
            ClaudeAgentOptions(model="claude-haiku-4-5"),
            ClaudeAgentOptions(env={"ANTHROPIC_MODEL": "claude-haiku-4-5"}),
            # Settings files may name the model.
            ClaudeAgentOptions(setting_sources=["project"]),
        ],
    )
    def test_a_model_from_elsewhere_runs(self, monkeypatch, options):
        monkeypatch.delenv("ANTHROPIC_MODEL")
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(_TOOL_TURN)):
            reply = anyio.run(claude.run_turn, "hi", options, "c")
        assert reply.text == "It's foggy."

    def test_project_settings_may_name_the_model(self, monkeypatch, isolated):
        monkeypatch.delenv("ANTHROPIC_MODEL")
        (isolated / ".claude").mkdir()
        (isolated / ".claude" / "settings.json").write_text('{"model": "m"}')
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(_TOOL_TURN)):
            reply = anyio.run(claude.run_turn, "hi", ClaudeAgentOptions(), "c")
        assert reply.text == "It's foggy."

    @pytest.mark.parametrize("setting_sources", [None, ["project"]])
    def test_missing_project_mcp_command_fails_with_a_fix(
        self, isolated, setting_sources
    ):
        (isolated / ".mcp.json").write_text(
            '{"mcpServers": {"fs": {"command": "no-such-binary-xyz"}}}'
        )
        options = ClaudeAgentOptions(setting_sources=setting_sources)
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(_TOOL_TURN)):
            with pytest.raises(
                claude.AgentError, match="'fs' runs 'no-such-binary-xyz'"
            ):
                anyio.run(claude.run_turn, "hi", options, "c")

    @pytest.mark.parametrize(
        ("setting_sources", "command"),
        [
            # The CLI doesn't read .mcp.json without the project source.
            (["user"], "no-such-binary-xyz"),
            # The CLI expands variables in commands itself.
            (None, "${NO_SUCH_DIR}/no-such-binary-xyz"),
        ],
    )
    def test_project_mcp_commands_left_to_the_cli_are_not_checked(
        self, isolated, setting_sources, command
    ):
        (isolated / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"fs": {"command": command}}})
        )
        options = ClaudeAgentOptions(setting_sources=setting_sources)
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(_TOOL_TURN)):
            assert anyio.run(claude.run_turn, "hi", options, "c").text == "It's foggy."

    @pytest.mark.parametrize("content", ["{not json", "[]"])
    def test_malformed_project_mcp_json_fails_with_a_fix(self, isolated, content):
        (isolated / ".mcp.json").write_text(content)
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(_TOOL_TURN)):
            with pytest.raises(claude.AgentError, match=r"\.mcp\.json is not"):
                anyio.run(claude.run_turn, "hi", ClaudeAgentOptions(), "c")


class TestCallerScopedConversations:
    """With an authenticated caller, a conversation is that caller's alone."""

    CONTEXT_ID = "3e3a0114-1fa0-463c-a119-b599a1beb275"

    def _turn(self, user_id: str | None, options=None) -> ClaudeAgentOptions:
        """One turn through stream_turn; the fake CLI saves its transcript."""
        seen: list[dict] = []

        async def query(*, prompt, options):
            seen.append(options)
            session = options.resume or options.session_id
            # Keyed by the CLI's working directory, on disk and in the store.
            project_key = claude.claude_agent_sdk.project_key_for_directory(options.cwd)
            transcripts = (
                pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / project_key
            )
            transcripts.mkdir(parents=True, exist_ok=True)
            (transcripts / f"{session}.jsonl").write_text('{"type": "user"}\n')
            if options.session_store is not None:
                key = {"project_key": project_key, "session_id": session}
                await options.session_store.append(key, [{"type": "user"}])
            yield _result(session_id=session)

        async def run() -> None:
            async for _ in claude.stream_turn(
                "hi", options or ClaudeAgentOptions(), self.CONTEXT_ID, user_id
            ):
                pass

        with patch.object(claude.claude_agent_sdk, "query", query):
            anyio.run(run)
        return seen[0]

    def test_another_caller_does_not_resume_the_conversation(self):
        alice = self._turn("alice@example.com")
        bob = self._turn("bob@example.com")
        assert alice.cwd != bob.cwd
        assert (bob.session_id, bob.resume) == (self.CONTEXT_ID, None)

    def test_the_same_caller_resumes_their_conversation(self):
        first = self._turn("alice@example.com")
        later = self._turn("alice@example.com")
        assert later.cwd == first.cwd
        assert (first.session_id, later.resume) == (self.CONTEXT_ID, self.CONTEXT_ID)

    def test_callers_are_kept_apart_in_the_session_store(self):
        store = InMemorySessionStore()
        self._turn("alice@example.com", ClaudeAgentOptions(session_store=store))
        # Only the store has the transcript now, as on another instance.
        shutil.rmtree(pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects")

        bob = self._turn("bob@example.com", ClaudeAgentOptions(session_store=store))
        back = self._turn("alice@example.com", ClaudeAgentOptions(session_store=store))
        assert bob.resume is None
        assert back.resume == self.CONTEXT_ID

    def test_without_a_caller_the_context_id_alone_keys_it(self):
        anyone = self._turn(None)
        again = self._turn(None)
        assert anyone.cwd == str(workspaces_root() / self.CONTEXT_ID)
        assert (again.cwd, again.resume) == (anyone.cwd, self.CONTEXT_ID)
        # A caller doesn't pick up the unauthenticated conversation either.
        assert self._turn("alice@example.com").resume is None


class TestTextDelta:
    def test_top_level_text_delta(self):
        assert replies.text_delta(_delta("Hello")) == "Hello"

    def test_subagent_and_non_text_events_are_skipped(self):
        assert replies.text_delta(_delta("x", parent="t1")) == ""
        other = StreamEvent(uuid="u", session_id="s", event={"type": "message_start"})
        assert replies.text_delta(other) == ""
        assert replies.text_delta(_result()) == ""


class TestModelAccess:
    def test_defaults_to_vertex_with_the_given_project(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX", raising=False)
        monkeypatch.delenv("CLOUD_ML_REGION", raising=False)
        monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "from-env")

        claude.configure_model_access()

        import os

        assert os.environ["CLAUDE_CODE_USE_VERTEX"] == "1"
        assert os.environ["CLOUD_ML_REGION"] == "global"
        assert os.environ["ANTHROPIC_VERTEX_PROJECT_ID"] == "from-env"

    def test_an_api_key_does_not_skip_vertex(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        claude.configure_model_access()

        import os

        assert os.environ["CLAUDE_CODE_USE_VERTEX"] == "1"

    def test_existing_values_win(self, monkeypatch):
        monkeypatch.setenv("CLOUD_ML_REGION", "us-east5")
        claude.configure_model_access()

        import os

        assert os.environ["CLOUD_ML_REGION"] == "us-east5"
        assert os.environ["ANTHROPIC_VERTEX_PROJECT_ID"] == "test-project"


class TestEvalAgentsMetadata:
    def test_declares_the_agents_mcp_tools(self):
        import importlib.util
        from pathlib import Path

        script = Path(__file__).resolve().parents[2] / "scripts" / "eval_generate.py"
        spec = importlib.util.spec_from_file_location("eval_generate", script)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        from claude_agent_sdk import create_sdk_mcp_server, tool

        @tool("get_weather", "Get the weather.", {"city": str})
        async def get_weather(args):
            return {"content": []}

        # Its own agent, so the test holds whatever app/agent.py declares.
        options = ClaudeAgentOptions(
            system_prompt="Be brief.",
            mcp_servers={"w": create_sdk_mcp_server(name="w", tools=[get_weather])},
            tools=["WebFetch"],
        )

        agents = mod.agents_metadata(options, "weather")

        declarations = agents["weather"]["tools"][0]["function_declarations"]
        assert [d["name"] for d in declarations] == ["mcp__w__get_weather", "WebFetch"]
        assert declarations[0]["parameters"]["properties"] == {
            "city": {"type": "string"}
        }
        assert agents["weather"]["instruction"] == "Be brief."

    def test_declares_the_agents_subagents(self):
        import importlib.util
        from pathlib import Path

        script = Path(__file__).resolve().parents[2] / "scripts" / "eval_generate.py"
        spec = importlib.util.spec_from_file_location("eval_generate", script)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        from claude_agent_sdk import AgentDefinition

        options = ClaudeAgentOptions(
            tools=["Agent", "WebFetch"],
            agents={
                "researcher": AgentDefinition(
                    description="Looks things up.", prompt="Search.", tools=["WebFetch"]
                ),
                "writer": AgentDefinition(description="Writes.", prompt="Write."),
            },
        )

        agents = mod.agents_metadata(options, "root")

        assert agents["root"]["sub_agents"] == ["researcher", "writer"]
        assert agents["researcher"] == {
            "agent_id": "researcher",
            "agent_type": "ClaudeAgent",
            "description": "Looks things up.",
            "instruction": "Search.",
            "tools": [{"function_declarations": [{"name": "WebFetch"}]}],
        }
        # Without its own tool list, a subagent inherits the agent's tools.
        assert agents["writer"]["tools"] == agents["root"]["tools"]


def _fake_pool() -> MagicMock:
    """An asyncpg pool whose table setup succeeds and whose queries find nothing."""
    conn = MagicMock(execute=AsyncMock())
    conn.transaction.return_value.__aenter__ = AsyncMock()
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock(fetchval=AsyncMock(return_value=False))
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


_KEY = {"project_key": "p", "session_id": "s"}


class TestPostgresSessionStoreConnection:
    @patch("asyncpg.create_pool", new_callable=AsyncMock)
    def test_password_connects_over_the_cloudsql_socket(self, create_pool, monkeypatch):
        from app.app_utils.session_store import PostgresSessionStore

        monkeypatch.setenv("INSTANCE_CONNECTION_NAME", "proj:us-east1:db")
        monkeypatch.setenv("DB_PASS", "secret")
        monkeypatch.setenv("DB_NAME", "sessions")
        create_pool.return_value = _fake_pool()

        anyio.run(PostgresSessionStore.from_env().exists, _KEY)

        assert create_pool.call_args.kwargs == {
            "min_size": 1,
            "max_size": 5,
            "host": "/cloudsql/proj:us-east1:db",
            "user": "postgres",
            "password": "secret",
            "database": "sessions",
        }

    def test_needs_a_password(self, monkeypatch):
        from app.app_utils.session_store import PostgresSessionStore

        monkeypatch.setenv("INSTANCE_CONNECTION_NAME", "proj:us-east1:db")
        assert PostgresSessionStore.from_env() is None


@pytest.mark.skipif(
    not os.environ.get("SESSION_STORE_POSTGRES_URL"),
    reason="set SESSION_STORE_POSTGRES_URL to a Postgres database to run",
)
class TestPostgresSessionStore:
    @pytest.fixture
    def make_store(self):
        """Stores on fresh tables, dropped afterwards."""
        import asyncpg

        from app.app_utils.session_store import PostgresSessionStore

        dsn = os.environ["SESSION_STORE_POSTGRES_URL"]
        tables: list[str] = []

        def make() -> PostgresSessionStore:
            tables.append(f"test_session_store_{os.getpid()}_{len(tables)}")
            return PostgresSessionStore(tables[-1], dsn=dsn)

        yield make

        async def drop() -> None:
            conn = await asyncpg.connect(dsn)
            for table in tables:
                await conn.execute(f"DROP TABLE IF EXISTS {table}")
            await conn.close()

        anyio.run(drop)

    def test_meets_the_sdk_session_store_contract(self, make_store):
        from claude_agent_sdk.testing.session_store_conformance import (
            run_session_store_conformance,
        )

        # Each contract gets its own table, so they don't see each other's rows.
        anyio.run(run_session_store_conformance, make_store)

    def test_exists_without_loading(self, make_store):
        store = make_store()
        main = {"project_key": "p", "session_id": "s"}
        sub = {**main, "subpath": "subagents/agent-1"}

        async def run() -> list[bool]:
            before = await store.exists(main)
            await store.append(sub, [{"type": "user"}])
            only_sub = await store.exists(main)
            await store.append(main, [{"type": "user"}])
            return [before, only_sub, await store.exists(main)]

        assert anyio.run(run) == [False, False, True]
