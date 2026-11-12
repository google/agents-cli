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

"""Memory Bank in `app/app_utils/memory_bank.py`, with the SDK's `query` and the
Agent Platform client faked."""

from __future__ import annotations

import datetime
import json
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import fsspec
import pytest
from agentplatform import types
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
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

from app.app_utils import bq_analytics, claude, completions, memory_bank, turn_options
from app.app_utils.telemetry.config import agent_name

pytestmark = pytest.mark.usefixtures("isolated")

_UPDATED = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
_MEMORIES = [
    types.RetrieveMemoriesResponseRetrievedMemory(
        memory=types.Memory(fact="Likes green tea.", update_time=_UPDATED)
    ),
    types.RetrieveMemoriesResponseRetrievedMemory(
        memory=types.Memory(fact="Lives in Oslo.")
    ),
    # Skipped: nothing to show.
    types.RetrieveMemoriesResponseRetrievedMemory(memory=types.Memory()),
    types.RetrieveMemoriesResponseRetrievedMemory(),
]
_LINES = f"Time: {_UPDATED.isoformat()}\nuser: Likes green tea.\nuser: Lives in Oslo."
_PAST = (
    "The following content is from your previous conversations with the user.\n"
    "They may be useful for answering the user's current query.\n"
    f"<PAST_CONVERSATIONS>\n{_LINES}\n</PAST_CONVERSATIONS>\n"
)


@pytest.fixture
def memory_on(isolated, monkeypatch):
    """Memory on, in an Agent Runtime deployment."""
    monkeypatch.setenv("MEMORY_BANK_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "123")
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION", "us-central1")


def _fake_client(client_class: MagicMock, memories=()) -> MagicMock:
    """Give the patched agentplatform.Client a Memory Bank; return its async client."""
    aio = client_class.return_value.aio
    aio.__aenter__.return_value = aio
    aio.memory_banks.memories.retrieve = AsyncMock(
        side_effect=lambda **_: _pager(memories)
    )
    aio.memory_banks.ingest_events = AsyncMock()
    return aio


async def _pager(items):
    for item in items:
        yield item


def _result(**overrides) -> ResultMessage:
    fields = {
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": "s-1",
        "result": "Done.",
    }
    return ResultMessage(**{**fields, **overrides})


def _turn(options: ClaudeAgentOptions | None = None) -> None:
    """One turn of alice's through stream_turn, then its pending ingest."""

    async def run() -> None:
        async for _ in claude.stream_turn(
            "Any tea?", options or ClaudeAgentOptions(), "conv-1", "alice"
        ):
            pass
        await memory_bank.flush()

    anyio.run(run)


def _system_prompt_sent(query: MagicMock, options: ClaudeAgentOptions) -> object:
    """The system prompt the CLI got for one turn run with `options`.

    `query` is the test's patched `claude_agent_sdk.query`.
    """
    seen = []

    async def record(*, prompt, options):
        seen.append(options.system_prompt)
        yield _result()

    query.side_effect = record
    _turn(options)
    [system_prompt] = seen
    return system_prompt


@pytest.mark.parametrize(
    "system_prompt, expected",
    [
        pytest.param(
            "Be brief.",
            {"type": "custom", "prompt": f"Be brief.\n\n{_PAST}", "snapshot": False},
            id="string",
        ),
        # The SDK sends no system prompt as an empty custom one.
        pytest.param(
            None, {"type": "custom", "prompt": _PAST, "snapshot": False}, id="none"
        ),
        pytest.param(
            {"type": "preset", "preset": "claude_code"},
            {
                "type": "preset",
                "preset": "claude_code",
                "append": _PAST,
                "snapshot": False,
            },
            id="preset",
        ),
        pytest.param(
            {"type": "preset", "preset": "claude_code", "append": "Be brief."},
            {
                "type": "preset",
                "preset": "claude_code",
                "append": f"Be brief.\n\n{_PAST}",
                "snapshot": False,
            },
            id="preset_with_append",
        ),
        pytest.param(
            {"type": "custom", "prompt": "Be brief."},
            {"type": "custom", "prompt": f"Be brief.\n\n{_PAST}", "snapshot": False},
            id="custom",
        ),
    ],
)
@pytest.mark.usefixtures("memory_on")
@patch.object(claude.claude_agent_sdk, "query")
@patch("agentplatform.Client")
def test_the_users_memories_are_recalled_into_the_system_prompt(
    client_class, query, system_prompt, expected
):
    aio = _fake_client(client_class, _MEMORIES)

    sent = _system_prompt_sent(query, ClaudeAgentOptions(system_prompt=system_prompt))

    assert sent == expected
    aio.memory_banks.memories.retrieve.assert_awaited_once_with(
        name="reasoningEngines/123",
        scope={"app_name": agent_name(), "user_id": "alice"},
        similarity_search_params={"search_query": "Any tea?"},
    )
    client_class.assert_called_with(location="us-central1")


_ANSWER = [
    AssistantMessage(
        content=[TextBlock(text="Green tea.")], model="m", message_id="m1"
    ),
    _result(),
]


@pytest.mark.usefixtures("memory_on")
@patch.object(claude.claude_agent_sdk, "query")
@patch("agentplatform.Client")
@patch.object(bq_analytics, "writer")
def test_bigquery_logs_the_recalled_memories_in_the_models_system_prompt(
    writer, client_class, query
):
    _fake_client(client_class, _MEMORIES)
    rows: list[dict] = []
    writer.return_value = MagicMock(put=rows.append)
    query.side_effect = _fake_query(_ANSWER)

    _turn(ClaudeAgentOptions(system_prompt="Be brief."))

    request = next(r for r in rows if r["event_type"] == "LLM_REQUEST")
    assert json.loads(request["content"])["system_prompt"] == f"Be brief.\n\n{_PAST}"
    # As ADK's, the agent's instruction leaves the memories out.
    starting = next(r for r in rows if r["event_type"] == "AGENT_STARTING")
    assert json.loads(starting["content"]) == {"text_summary": "Be brief."}


@pytest.mark.usefixtures("memory_on")
@patch.object(claude.claude_agent_sdk, "query")
@patch("agentplatform.Client")
def test_the_prompt_log_has_the_recalled_memories_in_the_system_instruction(
    client_class, query, monkeypatch
):
    _fake_client(client_class, _MEMORIES)
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK", "upload")
    monkeypatch.setenv(
        "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH", "memory://logs/completions"
    )
    events: list = []
    monkeypatch.setattr(completions.telemetry, "emit_event", events.append)
    query.side_effect = _fake_query(_ANSWER)

    _turn(ClaudeAgentOptions(system_prompt="Be brief."))

    [event] = events
    assert _uploaded(event.attributes["gen_ai.system_instructions_ref"]) == [
        {"type": "text", "content": f"Be brief.\n\n{_PAST}"}
    ]


def _uploaded(path: str) -> object:
    """The JSON file at `path`, once the hook's upload thread has written it."""
    fs, _ = fsspec.url_to_fs(path)
    deadline = time.monotonic() + 5
    # The memory filesystem creates the file, empty, when it's opened to write.
    while not fs.exists(path) or not fs.cat_file(path):
        assert time.monotonic() < deadline, f"{path} was never uploaded"
        time.sleep(0.01)
    return json.loads(fs.cat_file(path))


@pytest.mark.parametrize(
    "capture, memories, system_prompt, expected",
    [
        pytest.param(
            "true",
            _MEMORIES,
            "Be brief.",
            {
                "ClaudeAgentSDK.query": [
                    {"type": "text", "content": f"Be brief.\n\n{_PAST}"}
                ]
            },
            id="capture_on",
        ),
        pytest.param(None, _MEMORIES, "Be brief.", {}, id="capture_off"),
        pytest.param("true", [], None, {}, id="no_system_prompt"),
    ],
)
@pytest.mark.usefixtures("memory_on")
# The instrumentor wraps query() where the SDK defines it, then
# re-exports the wrapper; uninstrument() re-exports the original.
@patch.object(claude.claude_agent_sdk, "query")
@patch.object(sys.modules["claude_agent_sdk.query"], "query")
@patch("agentplatform.Client")
def test_the_turn_span_has_the_system_prompt_with_memories_only_on_capture(
    client_class,
    defined_query,
    _exported_query,
    monkeypatch,
    capture,
    memories,
    system_prompt,
    expected,
):
    _fake_client(client_class, memories)
    defined_query.side_effect = _fake_query(_SUBAGENT_TURN)
    if capture:
        monkeypatch.setenv(
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", capture
        )
    else:
        monkeypatch.delenv(
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", raising=False
        )
    monkeypatch.setenv("OPENINFERENCE_ENABLE_GENAI_SEMCONV", "true")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = ClaudeAgentSDKInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig())
    try:
        _turn(ClaudeAgentOptions(system_prompt=system_prompt))
    finally:
        instrumentor.uninstrument()

    spans = exporter.get_finished_spans()
    assert {"ClaudeAgentSDK.query", "get_weather"} <= {s.name for s in spans}
    assert {
        s.name: json.loads(s.attributes["gen_ai.system_instructions"])
        for s in spans
        if s.attributes and "gen_ai.system_instructions" in s.attributes
    } == expected


@pytest.mark.usefixtures("memory_on")
@patch.object(claude.claude_agent_sdk, "query")
@patch("agentplatform.Client")
def test_without_memories_the_system_prompt_is_unchanged(client_class, query):
    _fake_client(client_class, [])

    sent = _system_prompt_sent(query, ClaudeAgentOptions(system_prompt="Be brief."))

    assert sent == "Be brief."


@pytest.mark.usefixtures("memory_on")
@patch.object(claude.claude_agent_sdk, "query")
@patch("agentplatform.Client")
def test_a_failed_recall_runs_the_turn_without_memories(client_class, query, caplog):
    aio = _fake_client(client_class)
    aio.memory_banks.memories.retrieve.side_effect = RuntimeError("quota exceeded")

    sent = _system_prompt_sent(query, ClaudeAgentOptions(system_prompt="Be brief."))

    assert sent == "Be brief."
    assert "quota exceeded" in caplog.text


@pytest.mark.usefixtures("memory_on")
@patch.object(claude.claude_agent_sdk, "query")
@patch("agentplatform.Client")
def test_a_system_prompt_file_gets_no_memories(client_class, query, caplog):
    _fake_client(client_class, _MEMORIES)
    from_file = {"type": "file", "path": "prompt.md"}

    assert _system_prompt_sent(query, ClaudeAgentOptions(system_prompt=from_file)) == (
        from_file
    )
    assert "system prompt file" in caplog.text


@pytest.mark.parametrize("enabled", [None, "false"])
@patch.object(claude.claude_agent_sdk, "query")
@patch("agentplatform.Client")
def test_with_memory_off_the_turn_never_reaches_memory_bank(
    client_class, query, monkeypatch, enabled
):
    if enabled is not None:
        monkeypatch.setenv("MEMORY_BANK_ENABLED", enabled)

    sent = _system_prompt_sent(query, ClaudeAgentOptions(system_prompt="Be brief."))

    assert sent == "Be brief."
    client_class.assert_not_called()
    assert not memory_bank.enabled()


@pytest.mark.usefixtures("memory_on")
def test_with_memory_on_the_search_tool_is_offered():
    assert memory_bank.enabled()
    servers = memory_bank.mcp_servers()

    assert list(servers) == ["memory"]
    assert servers["memory"]["type"] == "sdk"
    assert memory_bank.allowed_tools() == ["mcp__memory__search_memory"]


@pytest.mark.parametrize(
    "engine_name, display_name",
    [(None, agent_name()), ("shared-memory", "shared-memory")],
)
@pytest.mark.parametrize("exists", [True, False])
@patch.object(claude.claude_agent_sdk, "query")
@patch("agentplatform.Client")
def test_off_agent_runtime_the_engine_is_found_by_name_or_created_once(
    client_class, query, monkeypatch, engine_name, display_name, exists
):
    monkeypatch.setenv("MEMORY_BANK_ENABLED", "true")
    if engine_name:
        monkeypatch.setenv("MEMORY_BANK_ENGINE_NAME", engine_name)
    name = "projects/p/locations/us-east1/reasoningEngines/9"
    engine = MagicMock(api_resource=types.ReasoningEngine(name=name))
    runtimes = client_class.return_value.runtimes
    runtimes.list.return_value = [engine] if exists else []
    runtimes.create.return_value = engine
    aio = _fake_client(client_class, _MEMORIES)

    for _ in range(2):
        _system_prompt_sent(query, ClaudeAgentOptions())

    runtimes.list.assert_called_once_with(
        config={"filter": f"display_name={display_name}"}
    )
    if exists:
        runtimes.create.assert_not_called()
    else:
        runtimes.create.assert_called_once_with(config={"display_name": display_name})
    retrieves = aio.memory_banks.memories.retrieve.call_args_list
    assert [call.kwargs["name"] for call in retrieves] == [name, name]


@pytest.mark.parametrize(
    "env, location",
    [
        pytest.param(
            {
                "GOOGLE_CLOUD_AGENT_ENGINE_LOCATION": "europe-west4",
                "GOOGLE_CLOUD_LOCATION": "us-central1",
            },
            "europe-west4",
            id="agent_runtime",
        ),
        pytest.param(
            {"GOOGLE_CLOUD_LOCATION": "us-central1"}, "us-central1", id="regional"
        ),
        # Agent Runtime has no global location.
        pytest.param({"GOOGLE_CLOUD_LOCATION": "global"}, "us-east1", id="global"),
    ],
)
@patch.object(claude.claude_agent_sdk, "query")
@patch("agentplatform.Client")
def test_the_engine_is_in_an_agent_runtime_region(
    client_class, query, monkeypatch, env, location
):
    monkeypatch.setenv("MEMORY_BANK_ENABLED", "true")
    monkeypatch.delenv("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    client_class.return_value.runtimes.list.return_value = [
        MagicMock(api_resource=types.ReasoningEngine(name="reasoningEngines/9"))
    ]
    _fake_client(client_class)

    _system_prompt_sent(query, ClaudeAgentOptions())

    assert {call.kwargs["location"] for call in client_class.call_args_list} == {
        location
    }


_DELEGATE = {"subagent_type": "forecaster", "prompt": "Weather in Oslo?"}
# The main agent starts a background subagent, which calls a tool of its own.
_SUBAGENT_TURN = [
    AssistantMessage(
        content=[
            TextBlock(text="Asking the forecaster."),
            ToolUseBlock(id="a1", name="Agent", input=_DELEGATE),
        ],
        model="m",
        message_id="m1",
    ),
    UserMessage(content=[ToolResultBlock(tool_use_id="a1", content="Started.")]),
    AssistantMessage(
        content=[ToolUseBlock(id="t1", name="get_weather", input={"city": "Oslo"})],
        model="m",
        message_id="s1",
        parent_tool_use_id="a1",
    ),
    UserMessage(
        content=[ToolResultBlock(tool_use_id="t1", content="Sunny.")],
        parent_tool_use_id="a1",
    ),
    AssistantMessage(
        content=[TextBlock(text="It's sunny.")], model="m", message_id="m2"
    ),
    _result(),
]


def _fake_query(messages):
    async def query(*, prompt, options):
        for message in messages:
            yield message

    return query


@pytest.mark.usefixtures("memory_on")
@patch.object(claude.claude_agent_sdk, "query")
@patch("agentplatform.Client")
def test_a_finished_turn_is_sent_to_memory_bank_without_its_subagents(
    client_class, query
):
    aio = _fake_client(client_class)
    query.side_effect = _fake_query(_SUBAGENT_TURN)

    # run_turn returns once the ingest is sent: its process may end next.
    anyio.run(claude.run_turn, "Weather in Oslo?", ClaudeAgentOptions(), "conv-1")

    aio.memory_banks.ingest_events.assert_awaited_once()
    request = aio.memory_banks.ingest_events.call_args.kwargs
    events = request.pop("direct_contents_source")["events"]
    assert request == {
        "name": "reasoningEngines/123",
        # A turn without a caller is scoped like an unauthenticated A2A one.
        "scope": {"app_name": agent_name(), "user_id": "A2A_USER_conv-1"},
        "stream_id": turn_options.session_id_for("conv-1"),
    }
    call = {"id": "a1", "name": "Agent", "args": _DELEGATE}
    started = {"id": "a1", "name": "Agent", "response": {"result": "Started."}}
    assert [event["content"] for event in events] == [
        {"role": "user", "parts": [{"text": "Weather in Oslo?"}]},
        {
            "role": "model",
            "parts": [{"text": "Asking the forecaster."}, {"function_call": call}],
        },
        {"role": "user", "parts": [{"function_response": started}]},
        {"role": "model", "parts": [{"text": "It's sunny."}]},
    ]
    ids = [event["event_id"] for event in events]
    assert len(set(ids)) == len(ids)
    assert all(event["event_time"].tzinfo for event in events)


@pytest.mark.usefixtures("memory_on")
@patch.object(claude.claude_agent_sdk, "query")
@patch("agentplatform.Client")
def test_every_memory_bank_client_a_turn_opens_is_closed(client_class, query):
    aio = _fake_client(client_class, _MEMORIES)
    query.side_effect = _fake_query(_SUBAGENT_TURN)

    anyio.run(claude.run_turn, "Weather in Oslo?", ClaudeAgentOptions(), "conv-1")

    # One for the recall, one for the ingest; an unclosed one leaks its HTTP session.
    assert aio.__aenter__.await_count == 2
    assert aio.__aexit__.await_count == 2


@pytest.mark.usefixtures("memory_on")
@patch.object(claude.claude_agent_sdk, "query")
@patch("agentplatform.Client")
def test_a_model_call_with_nothing_to_remember_is_left_out(client_class, query):
    aio = _fake_client(client_class)
    thinking_only = [
        AssistantMessage(
            content=[ThinkingBlock(thinking="Hmm.", signature="s")], model="m"
        ),
        _result(),
    ]
    query.side_effect = _fake_query(thinking_only)

    anyio.run(claude.run_turn, "hi", ClaudeAgentOptions(), "conv-1")

    events = aio.memory_banks.ingest_events.call_args.kwargs["direct_contents_source"][
        "events"
    ]
    assert [event["content"]["role"] for event in events] == ["user"]


@pytest.mark.usefixtures("memory_on")
@patch.object(claude.claude_agent_sdk, "query")
@patch("agentplatform.Client")
def test_a_failed_turn_is_not_sent_to_memory_bank(client_class, query):
    aio = _fake_client(client_class)
    failing = [_result(is_error=True, result="max turns")]
    query.side_effect = _fake_query(failing)

    with pytest.raises(claude.AgentError, match="max turns"):
        anyio.run(claude.run_turn, "hi", ClaudeAgentOptions(), "conv-1")

    aio.memory_banks.ingest_events.assert_not_awaited()


@pytest.mark.usefixtures("memory_on")
@patch.object(claude.claude_agent_sdk, "query")
@patch("agentplatform.Client")
def test_a_failed_ingest_is_logged_and_never_fails_the_turn(
    client_class, query, caplog
):
    aio = _fake_client(client_class)
    aio.memory_banks.ingest_events.side_effect = RuntimeError("permission denied")
    query.side_effect = _fake_query([_result()])

    reply = anyio.run(claude.run_turn, "hi", ClaudeAgentOptions(), "conv-1")

    assert reply.text == "Done."
    assert "permission denied" in caplog.text


@pytest.mark.parametrize(
    "memories, text", [(_MEMORIES, _LINES), ([], "No memories found.")]
)
@pytest.mark.usefixtures("memory_on")
@patch.object(claude.claude_agent_sdk, "query")
@patch("agentplatform.Client")
def test_the_search_tool_searches_the_running_turns_user(
    client_class, query, memories, text
):
    aio = _fake_client(client_class, memories)
    results = []

    async def search_in_turn(*, prompt, options):
        # As the CLI calls the in-process tool, within the turn.
        results.append(await memory_bank.search_memory.handler({"query": "tea"}))
        yield _result()

    query.side_effect = search_in_turn
    _turn()

    assert results == [{"content": [{"type": "text", "text": text}]}]
    assert aio.memory_banks.memories.retrieve.call_args.kwargs == {
        "name": "reasoningEngines/123",
        "scope": {"app_name": agent_name(), "user_id": "alice"},
        "similarity_search_params": {"search_query": "tea"},
    }
