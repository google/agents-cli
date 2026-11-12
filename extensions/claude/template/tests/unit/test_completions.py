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

"""Prompt-response logging: what each model call uploads and logs."""

from __future__ import annotations

import json
import logging
import time
import uuid

import fsspec
import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.util.genai.completion_hook import load_completion_hook

from app.app_utils import completions
from app.app_utils.telemetry.config import agent_name
from app.app_utils.telemetry.subagent_spans import SubagentSpans
from app.app_utils.turn_events import TurnParser

MODEL = "claude-haiku-4-5"


@pytest.fixture
def events(monkeypatch):
    """The completion events sent to Cloud Logging."""
    sent: list = []
    monkeypatch.setattr(completions.telemetry, "emit_event", sent.append)
    return sent


@pytest.fixture
def upload(monkeypatch):
    """Turn on the upload hook, writing to an in-memory bucket."""
    base = f"memory://bucket-{uuid.uuid4().hex}/completions"
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK", "upload")
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH", base)
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT", "jsonl")


@pytest.fixture
def spans():
    """Where the turn's spans go: PromptLog gets this provider's tracer."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _call(message_id: str, blocks: list, parent: str | None = None, **fields):
    return AssistantMessage(
        content=blocks,
        model=MODEL,
        message_id=message_id,
        parent_tool_use_id=parent,
        **fields,
    )


def _log(messages: list, spans, system_prompt=None) -> None:
    """Observe `messages` as one turn, under a span standing in for the agent's."""
    provider, _ = spans
    tracer = provider.get_tracer(__name__)
    parser = TurnParser(agent_name())
    turn = completions.PromptLog(
        load_completion_hook(),
        tracer,
        "Weather in SF?",
        ClaudeAgentOptions(system_prompt=system_prompt),
        "conv-1",
        parser.agent_name,
        SubagentSpans(parser.agent_name),
    )
    with tracer.start_as_current_span("turn"):
        for message in messages:
            for event in parser.feed(message):
                turn.handle(event)
        for event in parser.finish():
            turn.handle(event)


def _uploaded(path: str) -> list[dict]:
    """The JSONL file at `path`, once the hook's upload thread has written it."""
    fs, _ = fsspec.url_to_fs(path)
    deadline = time.monotonic() + 5
    # The memory filesystem creates the file, empty, when it's opened to write.
    while not fs.exists(path) or not fs.cat_file(path):
        assert time.monotonic() < deadline, f"{path} was never uploaded"
        time.sleep(0.01)
    with fs.open(path) as file:
        return [json.loads(line) for line in file.read().splitlines() if line]


def _messages(path: str) -> list[tuple[str, list[dict]]]:
    """(role, parts) of each message uploaded to `path`."""
    return [(message["role"], message["parts"]) for message in _uploaded(path)]


_TOOL_TURN = [
    _call(
        "m1",
        [
            TextBlock(text="Checking."),
            ToolUseBlock(id="t1", name="get_weather", input={"city": "SF"}),
        ],
        stop_reason="tool_use",
        usage={"input_tokens": 20, "output_tokens": 5},
    ),
    UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="60F")]),
    _call("m2", [TextBlock(text="It's foggy.")], stop_reason="end_turn"),
]


def test_each_call_reads_the_turn_so_far(upload, events, spans):
    _log(_TOOL_TURN, spans, system_prompt="You report the weather.")

    first, second = events
    attributes = first.attributes
    # The log sink matches on this label.
    assert attributes["event.name"] == completions.EVENT_NAME
    assert attributes["gen_ai.request.model"] == MODEL
    assert attributes["gen_ai.conversation.id"] == "conv-1"
    assert attributes["gen_ai.response.finish_reasons"] == ["tool_use"]
    assert attributes["gen_ai.usage.input_tokens"] == 20
    prompt = ("user", [{"type": "text", "content": "Weather in SF?"}])
    assert _messages(attributes["gen_ai.input.messages_ref"]) == [prompt]
    [(_, output)] = _messages(attributes["gen_ai.output.messages_ref"])
    tool_call = {
        "type": "tool_call",
        "arguments": {"city": "SF"},
        "name": "get_weather",
        "id": "t1",
    }
    assert output == [{"type": "text", "content": "Checking."}, tool_call]
    [instruction] = _uploaded(attributes["gen_ai.system_instructions_ref"])
    assert instruction["content"] == "You report the weather."
    # The last call alone holds the whole turn.
    assert _messages(second.attributes["gen_ai.input.messages_ref"]) == [
        prompt,
        ("assistant", [{"type": "text", "content": "Checking."}, tool_call]),
        (
            "tool",
            [{"type": "tool_call_response", "response": {"result": "60F"}, "id": "t1"}],
        ),
    ]


def test_a_custom_system_prompt_is_the_system_instruction(upload, events, spans):
    _log(
        [_call("m1", [TextBlock(text="It's foggy.")])],
        spans,
        system_prompt={"type": "custom", "prompt": "You report the weather."},
    )

    [event] = events
    [instruction] = _uploaded(event.attributes["gen_ai.system_instructions_ref"])
    assert instruction["content"] == "You report the weather."


def test_each_call_gets_a_chat_span_carrying_its_entry(upload, events, spans):
    _log(_TOOL_TURN, spans)

    _, exporter = spans
    *calls, turn = exporter.get_finished_spans()
    assert [span.name for span in calls] == [f"chat {MODEL}"] * 2
    assert {span.parent.span_id for span in calls} == {turn.context.span_id}
    for event, span in zip(events, calls, strict=True):
        assert event.context is not None
        span_context = trace.get_current_span(event.context).get_span_context()
        assert span_context.span_id == span.context.span_id
        assert (
            span.attributes["gen_ai.output.messages_ref"]
            == (event.attributes["gen_ai.output.messages_ref"])
        )
    assert calls[0].attributes["gen_ai.usage.input_tokens"] == 20


def test_a_failed_model_call_marks_its_chat_span_as_an_error(upload, events, spans):
    _log([_call("bad", [TextBlock(text="API Error: 429")], error="rate_limit")], spans)

    _, exporter = spans
    chat, _ = exporter.get_finished_spans()
    assert chat.status.status_code == trace.StatusCode.ERROR
    assert chat.attributes["error.type"] == "rate_limit"


def test_a_subagent_is_logged_from_its_own_prompt(upload, events, spans):
    task = ToolUseBlock(id="task-1", name="Task", input={"prompt": "Summarize"})
    _log(
        [
            # The main agent's calls stream, so this one ends before the subagent runs.
            _event("message_start", message={}),
            _call("m1", [task]),
            _event("message_stop"),
            UserMessage(content="Summarize the forecast.", parent_tool_use_id="task-1"),
            _call("s1", [TextBlock(text="Foggy all day.")], parent="task-1"),
            UserMessage(
                content=[ToolResultBlock(tool_use_id="task-1", content="Foggy.")]
            ),
            _call("m2", [TextBlock(text="It's foggy.")]),
        ],
        spans,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": "Be brief.",
        },
    )

    agent_call, subagent_call, final_call = events
    assert _messages(subagent_call.attributes["gen_ai.input.messages_ref"]) == [
        ("user", [{"type": "text", "content": "Summarize the forecast."}])
    ]
    assert "gen_ai.system_instructions_ref" not in subagent_call.attributes
    # Only what the agent adds to a preset prompt is its own.
    [instruction] = _uploaded(agent_call.attributes["gen_ai.system_instructions_ref"])
    assert instruction["content"] == "Be brief."
    roles = [
        role
        for role, _ in _messages(final_call.attributes["gen_ai.input.messages_ref"])
    ]
    assert roles == ["user", "assistant", "tool"]


def _event(kind: str, **fields) -> StreamEvent:
    return StreamEvent(uuid="u", session_id="s", event={"type": kind, **fields})


def test_a_streamed_call_takes_its_final_usage_from_the_stream(upload, events, spans):
    _log(
        [
            _event("message_start"),
            _call(
                "m1",
                [ToolUseBlock(id="t1", name="get_weather", input={"city": "SF"})],
                usage={"input_tokens": 20, "output_tokens": 1},
            ),
            # The CLI runs the tool before the call's stream has ended.
            UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="60F")]),
            _event(
                "message_delta",
                usage={"output_tokens": 12},
                delta={"stop_reason": "tool_use"},
            ),
            _event("message_stop"),
            _call("m2", [TextBlock(text="It's foggy.")]),
        ],
        spans,
    )

    first, second = events
    assert first.attributes["gen_ai.usage.input_tokens"] == 20
    assert first.attributes["gen_ai.usage.output_tokens"] == 12
    assert first.attributes["gen_ai.response.finish_reasons"] == ["tool_use"]
    [(role, _)] = _messages(first.attributes["gen_ai.input.messages_ref"])
    assert role == "user"
    # The tool's result, which came before message_stop, follows the reply.
    roles = [
        role for role, _ in _messages(second.attributes["gen_ai.input.messages_ref"])
    ]
    assert roles == ["user", "assistant", "tool"]


@pytest.mark.parametrize(
    ("content", "is_error", "response"),
    [
        (
            [{"type": "text", "text": "60"}, {"type": "text", "text": "F"}],
            None,
            {"result": "60F"},
        ),
        ("city not found", True, {"error": "city not found"}),
    ],
)
def test_tool_results_go_up_as_objects(
    upload, events, spans, content, is_error, response
):
    _log(
        [
            _call("m1", [ToolUseBlock(id="t1", name="get_weather", input={})]),
            UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="t1", content=content, is_error=is_error
                    )
                ]
            ),
            _call("m2", [TextBlock(text="Done.")]),
        ],
        spans,
    )

    *_, (role, parts) = _messages(events[1].attributes["gen_ai.input.messages_ref"])
    assert (role, parts[0]["response"]) == ("tool", response)


def test_one_model_call_in_several_messages_is_one_event(upload, events, spans):
    _log(
        [
            _call("m1", [ThinkingBlock(thinking="SF is foggy.", signature="s")]),
            _call("m1", [TextBlock(text="It's foggy.")]),
        ],
        spans,
    )

    [event] = events
    [(_, output)] = _messages(event.attributes["gen_ai.output.messages_ref"])
    assert output == [
        {"type": "text", "content": "SF is foggy."},
        {"type": "text", "content": "It's foggy."},
    ]


def test_off_unless_the_upload_hook_is_configured(monkeypatch):
    monkeypatch.delenv("OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK", raising=False)
    names = TurnParser(agent_name()).agent_name
    spans = SubagentSpans(names)
    assert (
        completions.start_turn("hi", ClaudeAgentOptions(), "conv-1", names, spans)
        is None
    )


@pytest.mark.parametrize(
    "calls",
    [
        pytest.param(["m1"], id="at the end of the turn"),
        pytest.param(["m1", "m2"], id="when the next call starts"),
    ],
)
def test_a_failure_never_fails_the_turn(upload, spans, monkeypatch, caplog, calls):
    def refuse(record):
        raise ConnectionError("logging.googleapis.com unreachable")

    monkeypatch.setattr(completions.telemetry, "emit_event", refuse)
    with caplog.at_level(logging.WARNING):
        _log([_call(message_id, [TextBlock(text="hi")]) for message_id in calls], spans)

    assert [r.getMessage() for r in caplog.records] == ["Couldn't log a model call"]
    _, exporter = spans
    assert [s.name for s in exporter.get_finished_spans()] == [f"chat {MODEL}", "turn"]
