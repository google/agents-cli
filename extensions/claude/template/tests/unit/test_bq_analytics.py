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

"""BigQuery Agent Analytics rows, with BigQuery faked at the client."""

from __future__ import annotations

import json
import threading
from unittest.mock import ANY, MagicMock, patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
)
from google.api_core.exceptions import NotFound
from google.cloud.bigquery.table import TableListItem
from opentelemetry.sdk.trace import TracerProvider

from app.app_utils import bq_analytics, settings
from app.app_utils.bq_analytics import schema as agent_events
from app.app_utils.bq_analytics import tables, uploader
from app.app_utils.bq_analytics.recorder import MAX_CONTENT_LENGTH, AgentEventsRecorder
from app.app_utils.remote_agents import remote_agent
from app.app_utils.telemetry.config import agent_name
from app.app_utils.turn_events import TurnParser

# The rows ADK's plugin writes for a turn with one tool call.
ADK_TOOL_TURN = [
    "USER_MESSAGE_RECEIVED",
    "INVOCATION_STARTING",
    "AGENT_STARTING",
    "LLM_REQUEST",
    "LLM_RESPONSE",
    "TOOL_STARTING",
    "TOOL_COMPLETED",
    "LLM_REQUEST",
    "LLM_RESPONSE",
    "AGENT_RESPONSE",
    "AGENT_COMPLETED",
    "INVOCATION_COMPLETED",
]


class FakeWriter:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def put(self, row: dict) -> None:
        self.rows.append(row)


def _event(kind: str, **fields) -> StreamEvent:
    return StreamEvent(uuid="u", session_id="s", event={"type": kind, **fields})


def _model_call(message_id: str, blocks: list, output_tokens: int, stop: str) -> list:
    """A streamed model call as the CLI sends it."""
    return [
        _event("message_start", message={"usage": {"input_tokens": 100}}),
        _event("content_block_delta", delta={"type": "text_delta", "text": "x"}),
        AssistantMessage(
            content=blocks,
            model="claude-haiku-4-5",
            message_id=message_id,
            usage={"input_tokens": 100, "output_tokens": 1},
        ),
        _event(
            "message_delta",
            usage={"output_tokens": output_tokens, "cache_read_input_tokens": 40},
            delta={"stop_reason": stop},
        ),
        _event("message_stop"),
    ]


def _result(**overrides) -> ResultMessage:
    fields = {
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 2,
        "session_id": "s",
        "result": "It's foggy.",
        "total_cost_usd": 0.01,
    }
    return ResultMessage(**{**fields, **overrides})


def _play(
    parser: TurnParser, recorder, messages: list, error: BaseException | None = None
) -> None:
    """Feed `messages` through `parser` to `recorder`, as stream_turn does."""
    for message in messages:
        for event in parser.feed(message):
            recorder.handle(event)
    for event in parser.finish(error):
        recorder.handle(event)


def _record(messages: list, **kwargs) -> list[dict]:
    out = FakeWriter()
    parser = TurnParser("weather_agent")
    recorder = AgentEventsRecorder(
        out,
        prompt="Weather in SF?",
        agent_name=parser.agent_name,
        session_id="conv-1",
        user_id="A2A_USER_conv-1",
        model="claude-haiku-4-5",
        system_prompt="You report the weather.",
        instruction="You report the weather.",
        **kwargs,
    )
    _play(parser, recorder, messages)
    return out.rows


def _tool_turn(name: str, result: str = "60F", is_error: bool = False) -> list:
    return [
        SystemMessage(subtype="init", data={}),
        *_model_call(
            "m1",
            [ToolUseBlock(id="t1", name=name, input={"city": "SF"})],
            20,
            "tool_use",
        ),
        UserMessage(
            content=[
                ToolResultBlock(tool_use_id="t1", content=result, is_error=is_error)
            ]
        ),
        *_model_call("m2", [TextBlock(text="It's foggy.")], 5, "end_turn"),
        _result(),
    ]


def _types(rows: list[dict]) -> list[str]:
    return [r["event_type"] for r in rows]


def _content(row: dict):
    return json.loads(row["content"])


def test_a_tool_turn_writes_adks_rows_in_adks_order():
    rows = _record(_tool_turn("mcp__weather__get_weather"))

    assert _types(rows) == ADK_TOOL_TURN
    assert {r["session_id"] for r in rows} == {"conv-1"}
    assert {r["agent"] for r in rows} == {"weather_agent"}
    assert {r["user_id"] for r in rows} == {"A2A_USER_conv-1"}
    assert len({r["invocation_id"] for r in rows}) == 1
    # The JSON paths ADK's views read.
    assert _content(rows[0]) == {"text_summary": "Weather in SF?"}
    first_response = rows[4]
    usage = _content(first_response)["usage"]
    # Final usage comes from message_delta; prompt includes cached input.
    assert usage == {"prompt": 140, "completion": 20, "total": 160}
    attributes = json.loads(first_response["attributes"])
    assert attributes["finish_reason"] == "tool_use"
    assert attributes["usage_metadata"]["cached_content_token_count"] == 40
    assert "time_to_first_token_ms" in json.loads(first_response["latency_ms"])
    assert _content(rows[5]) == {
        "tool": "mcp__weather__get_weather",
        "args": {"city": "SF"},
        "tool_origin": "MCP",
    }
    assert _content(rows[6])["result"] == "60F"
    assert _content(rows[9]) == {"response": "It's foggy."}
    assert json.loads(rows[11]["attributes"])["session_cost_usd"] == 0.01


@pytest.mark.parametrize(
    ("system_prompt", "logged"),
    [
        pytest.param("Be brief.", "Be brief.", id="string"),
        pytest.param(
            {"type": "custom", "prompt": "Be brief."}, "Be brief.", id="custom"
        ),
        # The preset's own prompt is the CLI's.
        pytest.param(
            {"type": "preset", "preset": "claude_code", "append": "Be brief."},
            "Be brief.",
            id="preset_with_append",
        ),
        pytest.param({"type": "preset", "preset": "claude_code"}, None, id="preset"),
        pytest.param({"type": "file", "path": "prompt.md"}, None, id="file"),
        pytest.param(None, None, id="none"),
    ],
)
def test_the_request_and_agent_starting_rows_have_the_system_prompts_text(
    system_prompt, logged
):
    out = FakeWriter()
    parser = TurnParser("a")
    recorder = AgentEventsRecorder(
        out,
        prompt="hi",
        agent_name=parser.agent_name,
        session_id="s",
        user_id="u",
        model=None,
        system_prompt=system_prompt,
        instruction=system_prompt,
    )
    _play(parser, recorder, _tool_turn("Bash"))

    request = next(r for r in out.rows if r["event_type"] == "LLM_REQUEST")
    assert _content(request).get("system_prompt") == logged
    starting = next(r for r in out.rows if r["event_type"] == "AGENT_STARTING")
    summary = _content(starting)["text_summary"] if starting["content"] else None
    assert summary == logged


def test_rows_join_the_turns_span_once_it_is_current():
    """The instrumentor's span is current only after query() yields."""
    out = FakeWriter()
    parser = TurnParser("a")
    recorder = AgentEventsRecorder(
        out,
        prompt="hi",
        agent_name=parser.agent_name,
        session_id="s",
        user_id="u",
        model=None,
        system_prompt=None,
        instruction=None,
    )
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("request") as request:
        with tracer.start_as_current_span("ClaudeAgentSDK.query") as turn:
            _play(parser, recorder, [SystemMessage(subtype="init", data={}), _result()])

    trace_id = format(turn.get_span_context().trace_id, "032x")
    assert {r["trace_id"] for r in out.rows} == {trace_id}
    invocation = next(r for r in out.rows if r["event_type"] == "INVOCATION_STARTING")
    assert invocation["span_id"] == format(turn.get_span_context().span_id, "016x")
    # The request span isn't a row: the root rows have no parent.
    assert request.get_span_context().is_valid
    assert invocation["parent_span_id"] is None
    assert out.rows[-1]["parent_span_id"] is None


def test_failures_become_error_rows():
    rows = _record(_tool_turn("Bash", result="denied", is_error=True))
    tool_error = next(r for r in rows if r["event_type"] == "TOOL_ERROR")
    assert tool_error["status"] == "ERROR"
    assert tool_error["error_message"] == "denied"
    assert _content(tool_error)["tool_origin"] == "LOCAL"

    out = FakeWriter()
    parser = TurnParser("a")
    recorder = AgentEventsRecorder(
        out,
        prompt="hi",
        agent_name=parser.agent_name,
        session_id="s",
        user_id="u",
        model=None,
        system_prompt=None,
        instruction=None,
    )
    _play(parser, recorder, [], error=RuntimeError("model not available"))
    assert _types(out.rows)[-2:] == ["AGENT_ERROR", "INVOCATION_ERROR"]
    assert out.rows[-1]["error_message"] == "model not available"


def test_a_subagent_gets_its_own_agent_and_model_rows():
    messages = [
        *_model_call(
            "m1",
            [
                ToolUseBlock(
                    id="t1",
                    name="Agent",
                    input={"subagent_type": "researcher", "prompt": "Find it"},
                )
            ],
            20,
            "tool_use",
        ),
        AssistantMessage(
            content=[TextBlock(text="Found it")],
            model="claude-haiku-4-5",
            message_id="sub-1",
            parent_tool_use_id="t1",
            usage={"input_tokens": 50, "output_tokens": 7},
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="Found it")]),
        *_model_call("m2", [TextBlock(text="Done")], 5, "end_turn"),
        _result(),
    ]
    rows = _record(messages)
    sub = [r for r in rows if r["agent"] == "researcher"]

    assert _types(sub) == [
        "AGENT_STARTING",
        "LLM_REQUEST",
        "LLM_RESPONSE",
        "AGENT_COMPLETED",
    ]
    tool_start = next(r for r in rows if r["event_type"] == "TOOL_STARTING")
    assert _content(tool_start)["tool_origin"] == "SUB_AGENT"
    # The subagent runs under the subagent tool's span.
    assert sub[0]["parent_span_id"] == tool_start["span_id"]
    assert _content(sub[2])["usage"]["completion"] == 7
    # Its completion comes before the tool's.
    assert _types(rows).index("AGENT_COMPLETED") < _types(rows).index("TOOL_COMPLETED")


@pytest.mark.parametrize(
    ("parent", "agent"), [(None, "weather_agent"), ("t1", "researcher")]
)
def test_a_failed_model_call_is_an_llm_error_row(parent, agent):
    failed = AssistantMessage(
        content=[TextBlock(text="API Error: 429 rate limited")],
        model="claude-haiku-4-5",
        message_id="bad",
        parent_tool_use_id=parent,
        error="rate_limit",
    )
    if parent is None:
        messages = [failed, _result(is_error=True, result="API Error: 429")]
    else:
        delegate = ToolUseBlock(
            id="t1", name="Agent", input={"subagent_type": "researcher", "prompt": "?"}
        )
        messages = [
            *_model_call("m1", [delegate], 20, "tool_use"),
            failed,
            UserMessage(
                content=[
                    ToolResultBlock(tool_use_id="t1", content="429", is_error=True)
                ]
            ),
            *_model_call("m2", [TextBlock(text="Sorry.")], 5, "end_turn"),
            _result(),
        ]

    rows = _record(messages)

    [error] = [r for r in rows if r["event_type"] == "LLM_ERROR"]
    assert error["agent"] == agent
    assert error["status"] == "ERROR"
    assert error["error_message"] == "rate_limit: API Error: 429 rate limited"
    assert not [
        r for r in rows if r["event_type"] == "LLM_RESPONSE" and r["agent"] == agent
    ]


def _assistant(message_id: str, block, parent: str | None = None):
    return AssistantMessage(
        content=[block],
        model="claude-haiku-4-5",
        message_id=message_id,
        parent_tool_use_id=parent,
        usage={"input_tokens": 100, "output_tokens": 6},
    )


def test_a_background_subagent_ends_at_its_task_notification():
    """The CLI returns the tool before the stream ends; the subagent's report
    starts another round after the first result."""
    task = {"tool_use_id": "t1", "subagent_type": "forecaster", "task_id": "a9"}
    call = {"subagent_type": "forecaster", "prompt": "Weather in Tokyo?"}
    messages = [
        SystemMessage(subtype="init", data={}),
        _event("message_start", message={"usage": {"input_tokens": 3777}}),
        _assistant("m1", ToolUseBlock(id="t1", name="Agent", input=call)),
        SystemMessage(subtype="task_started", data=task),
        _event(
            "message_delta",
            usage={"output_tokens": 189},
            delta={"stop_reason": "tool_use"},
        ),
        UserMessage(
            content=[ToolResultBlock(tool_use_id="t1", content="Async agent launched")]
        ),
        _event("message_stop"),
        *_model_call("m2", [TextBlock(text="Delegated.")], 33, "end_turn"),
        _result(result="Delegated."),
        _assistant(
            "s1",
            ToolUseBlock(id="t2", name="mcp__weather__get_weather", input={}),
            parent="t1",
        ),
        SystemMessage(subtype="task_progress", data=task),
        UserMessage(
            content=[ToolResultBlock(tool_use_id="t2", content="Sunny")],
            parent_tool_use_id="t1",
        ),
        _assistant("s2", TextBlock(text="Tokyo is sunny."), parent="t1"),
        SystemMessage(subtype="task_updated", data={"task_id": "a9"}),
        SystemMessage(
            subtype="task_notification",
            data={"tool_use_id": "t1", "status": "completed", "task_id": "a9"},
        ),
        SystemMessage(subtype="init", data={}),
        *_model_call("m3", [TextBlock(text="It's sunny in Tokyo.")], 90, "end_turn"),
        _result(result="It's sunny in Tokyo."),
    ]
    rows = _record(messages)
    types = _types(rows)

    sub = [r for r in rows if r["agent"] == "forecaster"]
    assert _types(sub) == [
        "AGENT_STARTING",
        "LLM_REQUEST",
        "LLM_RESPONSE",
        "TOOL_STARTING",
        "TOOL_COMPLETED",
        "LLM_REQUEST",
        "LLM_RESPONSE",
        "AGENT_COMPLETED",
    ]
    tool_start = next(r for r in rows if r["event_type"] == "TOOL_STARTING")
    assert sub[0]["parent_span_id"] == tool_start["span_id"]
    # The launch's LLM_RESPONSE has the stream's final usage.
    assert _content(rows[types.index("LLM_RESPONSE")])["usage"]["completion"] == 189
    # Every model call, including the round after the first result, is inside
    # the turn, which closes with the last result.
    assert types.count("LLM_RESPONSE") == 5
    assert types[-3:] == ["AGENT_RESPONSE", "AGENT_COMPLETED", "INVOCATION_COMPLETED"]
    assert _content(rows[-3]) == {"response": "It's sunny in Tokyo."}


@pytest.mark.parametrize(
    ("result", "turn_end"),
    [
        (_result(), ["AGENT_RESPONSE", "AGENT_COMPLETED", "INVOCATION_COMPLETED"]),
        (_result(is_error=True), ["AGENT_ERROR", "INVOCATION_ERROR"]),
    ],
)
def test_a_subagent_still_running_at_the_end_of_the_turn_is_closed(result, turn_end):
    """A background subagent whose task_notification never came."""
    call = {"subagent_type": "researcher", "prompt": "Find it"}
    messages = [
        SystemMessage(subtype="init", data={}),
        _event("message_start", message={"usage": {"input_tokens": 100}}),
        _assistant("m1", ToolUseBlock(id="t1", name="Agent", input=call)),
        SystemMessage(subtype="task_started", data={"tool_use_id": "t1"}),
        UserMessage(
            content=[ToolResultBlock(tool_use_id="t1", content="Async agent launched")]
        ),
        _event("message_stop"),
        _assistant(
            "s1",
            ToolUseBlock(id="t2", name="mcp__weather__get_weather", input={}),
            parent="t1",
        ),
        result,
    ]
    rows = _record(messages)

    sub = [r for r in rows if r["agent"] == "researcher"]
    assert _types(sub) == [
        "AGENT_STARTING",
        "LLM_REQUEST",
        "LLM_RESPONSE",
        "TOOL_STARTING",
        turn_end[-2],
    ]
    # Closed just before the turn is, with the turn's outcome.
    assert rows[-len(turn_end) - 1] is sub[-1]
    assert _types(rows)[-len(turn_end) :] == turn_end


def test_a_tool_result_before_the_stream_ends_waits_for_the_final_usage():
    """A denied call's result comes before the stream's message_delta."""
    messages = [
        SystemMessage(subtype="init", data={}),
        _event("message_start", message={"usage": {"input_tokens": 3772}}),
        _assistant(
            "m1", ToolUseBlock(id="t1", name="mcp__weather__get_time", input={})
        ),
        SystemMessage(subtype="permission_denied", data={"tool_use_id": "t1"}),
        UserMessage(
            content=[ToolResultBlock(tool_use_id="t1", content="denied", is_error=True)]
        ),
        _event(
            "message_delta",
            usage={"output_tokens": 101},
            delta={"stop_reason": "tool_use"},
        ),
        _event("message_stop"),
        *_model_call("m2", [TextBlock(text="I can't.")], 122, "end_turn"),
        _result(result="I can't."),
    ]
    rows = _record(messages)

    assert _types(rows)[3:7] == [
        "LLM_REQUEST",
        "LLM_RESPONSE",
        "TOOL_STARTING",
        "TOOL_ERROR",
    ]
    assert _content(rows[4])["usage"]["completion"] == 101
    assert json.loads(rows[4]["attributes"])["finish_reason"] == "tool_use"


def test_a_turn_stopped_at_an_error_result_records_the_result():
    out = FakeWriter()
    parser = TurnParser("a")
    recorder = AgentEventsRecorder(
        out,
        prompt="hi",
        agent_name=parser.agent_name,
        session_id="s",
        user_id="u",
        model=None,
        system_prompt=None,
        instruction=None,
    )
    # run_turn stopped reading at the result.
    _play(
        parser,
        recorder,
        [_result(is_error=True, result="Reached max turns")],
        error=GeneratorExit(),
    )
    assert out.rows[-1]["event_type"] == "INVOCATION_ERROR"
    assert out.rows[-1]["error_message"] == "Reached max turns"


def test_a_remote_agent_call_adds_an_a2a_interaction():
    rows = _record(
        _tool_turn("mcp__weather__ask", result="Sunny"),
        remote_agents={"weather": "https://weather.example/a2a/app"},
    )
    started = next(r for r in rows if r["event_type"] == "TOOL_STARTING")
    assert _content(started)["tool_origin"] == "A2A"
    interaction = next(r for r in rows if r["event_type"] == "A2A_INTERACTION")
    metadata = json.loads(interaction["attributes"])["a2a_metadata"]
    assert metadata["a2a:url"] == "https://weather.example/a2a/app"
    assert metadata["a2a:response"] == "Sunny"


def test_a_compaction_is_recorded():
    rows = _record(
        [
            SystemMessage(
                subtype="compact_boundary",
                data={"compact_metadata": {"trigger": "auto", "pre_tokens": 150000}},
            ),
            _result(),
        ]
    )
    compaction = next(r for r in rows if r["event_type"] == "EVENT_COMPACTION")
    assert json.loads(compaction["attributes"])["trigger"] == "auto"


def test_a_malformed_subagent_call_is_still_recorded():
    odd = AssistantMessage(
        content=[ToolUseBlock(id="t1", name="Agent", input=["not", "a", "dict"])],
        model="m",
    )
    rows = _record([odd, _event("message_stop"), _result()])  # must not raise
    assert "INVOCATION_COMPLETED" in _types(rows)
    started = next(
        r
        for r in rows
        if r["event_type"] == "AGENT_STARTING" and r["agent"] != "weather_agent"
    )
    assert started["agent"] == "subagent"


def test_tool_rows_are_redacted_and_capped():
    rows = _record(
        _tool_turn("mcp__weather__ask", result="x" * (MAX_CONTENT_LENGTH + 10)),
        remote_agents={"weather": "https://weather.example/a2a/app"},
    )

    completed = next(r for r in rows if r["event_type"] == "TOOL_COMPLETED")
    assert completed["is_truncated"] is True
    assert _content(completed)["result"].endswith("...[TRUNCATED]")
    interaction = next(r for r in rows if r["event_type"] == "A2A_INTERACTION")
    metadata = json.loads(interaction["attributes"])["a2a_metadata"]
    assert interaction["is_truncated"] is True
    assert metadata["a2a:response"].endswith("...[TRUNCATED]")


def test_tool_arguments_have_credentials_redacted():
    turn = _tool_turn("mcp__crm__lookup")
    for message in turn:
        for block in getattr(message, "content", None) or []:
            if isinstance(block, ToolUseBlock):
                block.input["api_key"] = "sk-live-123"
    rows = _record(turn)

    started = next(r for r in rows if r["event_type"] == "TOOL_STARTING")
    assert _content(started)["args"] == {"city": "SF", "api_key": "[REDACTED]"}
    assert "sk-live-123" not in json.dumps(rows)


def test_long_content_is_truncated_at_adks_limit():
    rows = _record([_result(result="x" * (MAX_CONTENT_LENGTH + 10))])
    response = next(r for r in rows if r["event_type"] == "AGENT_RESPONSE")
    assert response["is_truncated"] is True
    assert _content(response)["response"].endswith("...[TRUNCATED]")


@pytest.fixture
def fresh_writer(monkeypatch):
    monkeypatch.setattr(bq_analytics, "_writer", None)
    monkeypatch.setattr(bq_analytics, "_writer_failed", False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")


def test_no_dataset_means_no_writer(fresh_writer, monkeypatch):
    monkeypatch.delenv("BQ_ANALYTICS_DATASET_ID", raising=False)
    with patch("google.cloud.bigquery.Client") as client:
        assert bq_analytics.writer() is None
    client.assert_not_called()


@patch("google.cloud.bigquery.Client")
def test_the_writer_creates_adks_table_and_views_then_streams_rows(
    client_cls, fresh_writer, monkeypatch
):
    monkeypatch.setenv("BQ_ANALYTICS_DATASET_ID", "telemetry")
    client = client_cls.return_value
    client.get_table.side_effect = NotFound("no table")
    client.list_tables.return_value = []
    client.insert_rows_json.return_value = []

    out = bq_analytics.writer()
    assert out is not None
    out.put({"event_id": "e-1", "event_type": "INVOCATION_STARTING"})
    assert out.flush(5)

    table, *views = [c.args[0] for c in client.create_table.call_args_list]
    assert table.table_id == agent_events.TABLE
    assert table.time_partitioning.field == "timestamp"
    assert table.clustering_fields == agent_events.CLUSTERING_FIELDS
    assert [f.name for f in table.schema] == [
        f["name"] for f in agent_events.EVENTS_SCHEMA
    ]
    assert len(views) == len(agent_events.EVENT_VIEW_DEFS)
    llm_response = next(v for v in views if v.table_id == "v_llm_response")
    assert "FROM\n  `proj.telemetry.agent_events`" in llm_response.view_query
    assert "event_type = 'LLM_RESPONSE'" in llm_response.view_query
    client.insert_rows_json.assert_called_once_with(
        "proj.telemetry.agent_events",
        [{"event_id": "e-1", "event_type": "INVOCATION_STARTING"}],
        row_ids=["e-1"],
        retry=ANY,
        timeout=ANY,
    )


def _listed(views) -> list[TableListItem]:
    """What list_tables returns for these views."""
    return [
        TableListItem(
            {
                "tableReference": {
                    "projectId": "proj",
                    "datasetId": "telemetry",
                    "tableId": v.table_id,
                },
                "type": "VIEW",
                "labels": v.labels,
            }
        )
        for v in views
    ]


@patch("google.cloud.bigquery.Client")
def test_only_missing_or_outdated_views_are_written(
    client_cls, fresh_writer, monkeypatch
):
    monkeypatch.setenv("BQ_ANALYTICS_DATASET_ID", "telemetry")
    client = client_cls.return_value
    client.list_tables.return_value = []
    assert bq_analytics.writer().flush(5)
    created = [c.args[0] for c in client.create_table.call_args_list]

    # The next start finds them all current except one ADK changed, and one
    # missing.
    monkeypatch.setattr(bq_analytics, "_writer", None)
    client.reset_mock()
    current, outdated, _missing = created[:-2], created[-2], created[-1]
    outdated.labels = {**outdated.labels, tables._VIEW_HASH_LABEL: "old"}
    client.list_tables.return_value = _listed([*current, outdated])
    assert bq_analytics.writer().flush(5)

    assert [c.args[0].table_id for c in client.create_table.call_args_list] == [
        created[-1].table_id
    ]
    (update,) = client.update_table.call_args_list
    assert update.args[0].table_id == outdated.table_id
    assert update.args[1] == ["view_query", "labels"]


@patch("google.cloud.bigquery.Client")
def test_a_table_it_cant_set_up_is_reported_once(
    client_cls, fresh_writer, monkeypatch, caplog
):
    monkeypatch.setenv("BQ_ANALYTICS_DATASET_ID", "telemetry")
    client = client_cls.return_value
    client.get_table.side_effect = NotFound("Not found: Dataset")
    client.create_table.side_effect = NotFound("Not found: Dataset")
    client.insert_rows_json.side_effect = NotFound("Not found: Table")

    out = bq_analytics.writer()
    for n in range(3):  # one batch each
        out.put({"event_id": f"e-{n}"})
        assert out.flush(5)

    assert client.insert_rows_json.call_count == 3
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1
    client.list_tables.assert_not_called()


@patch("google.cloud.bigquery.Client")
def test_rows_still_queued_at_shutdown_are_reported(
    client_cls, fresh_writer, monkeypatch, caplog
):
    monkeypatch.setenv("BQ_ANALYTICS_DATASET_ID", "telemetry")
    release = threading.Event()
    client = client_cls.return_value
    client.get_table.side_effect = lambda _: release.wait(5)

    out = bq_analytics.writer()
    out.put({"event_id": "e-1"})
    assert not out.flush(0.1)
    release.set()

    assert "1 rows weren't written" in caplog.text


@patch("google.cloud.bigquery.Client")
def test_rows_past_the_queue_bound_are_dropped_not_held(
    client_cls, fresh_writer, monkeypatch, caplog
):
    monkeypatch.setenv("BQ_ANALYTICS_DATASET_ID", "telemetry")
    monkeypatch.setattr(uploader, "_MAX_QUEUED_ROWS", 2)
    release = threading.Event()
    client = client_cls.return_value
    client.get_table.side_effect = lambda _: release.wait(5)
    client.insert_rows_json.return_value = []

    out = bq_analytics.writer()
    for n in range(4):
        out.put({"event_id": f"e-{n}"})
    release.set()
    assert out.flush(5)

    rows = [r for c in client.insert_rows_json.call_args_list for r in c.args[1]]
    assert [r["event_id"] for r in rows] == ["e-0", "e-1"]
    assert caplog.text.count("new rows are dropped") == 1


@patch("google.cloud.bigquery.Client")
def test_a_batch_is_split_under_the_request_size_limit(
    client_cls, fresh_writer, monkeypatch
):
    monkeypatch.setenv("BQ_ANALYTICS_DATASET_ID", "telemetry")
    release = threading.Event()
    client = client_cls.return_value
    client.get_table.side_effect = lambda _: release.wait(5)
    client.insert_rows_json.return_value = []

    out = bq_analytics.writer()
    # Queued together, as a busy agent's rows are: 30 turns of 450 KB prompts.
    for n in range(60):
        out.put({"event_id": f"e-{n}", "content": "x" * 450 * 1024})
    release.set()
    assert out.flush(5)

    calls = client.insert_rows_json.call_args_list
    assert len(calls) > 1
    for call in calls:
        body = {"rows": [{"insertId": r["event_id"], "json": r} for r in call.args[1]]}
        assert len(json.dumps(body)) < 10 * 1024 * 1024  # insertAll's limit
    rows = [r["event_id"] for c in calls for r in c.args[1]]
    assert rows == [f"e-{n}" for n in range(60)]


@patch("google.cloud.bigquery.Client")
def test_a_write_gives_up_after_a_bounded_time(client_cls, fresh_writer, monkeypatch):
    """A slow BigQuery mustn't hold the writer for the default 10 minutes."""
    monkeypatch.setenv("BQ_ANALYTICS_DATASET_ID", "telemetry")
    client = client_cls.return_value
    client.insert_rows_json.return_value = []

    out = bq_analytics.writer()
    out.put({"event_id": "e-1"})
    assert out.flush(5)

    kwargs = client.insert_rows_json.call_args.kwargs
    assert 0 < kwargs["timeout"] <= 60
    assert 0 < kwargs["retry"].timeout <= 60


@patch("google.cloud.bigquery.Client")
def test_the_agents_own_tools_are_local(client_cls, fresh_writer, monkeypatch):
    """In-process @tool functions are ADK's function tools; others are MCP."""
    monkeypatch.setenv("BQ_ANALYTICS_DATASET_ID", "telemetry")
    client = client_cls.return_value
    client.insert_rows_json.return_value = []
    options = ClaudeAgentOptions(
        mcp_servers={
            "weather": create_sdk_mcp_server(name="weather", tools=[]),
            "docs": {"type": "http", "url": "https://docs.example/mcp"},
        }
    )

    for name in ("mcp__weather__get_weather", "mcp__docs__search"):
        parser = TurnParser(agent_name())
        recorder = bq_analytics.start_turn(
            "hi", options, "conv-1", "conv-1", None, parser.agent_name, instruction=None
        )
        assert recorder is not None
        _play(parser, recorder, _tool_turn(name))
    assert bq_analytics.writer().flush(5)

    rows = [r for c in client.insert_rows_json.call_args_list for r in c.args[1]]
    origins = [
        _content(r)["tool_origin"] for r in rows if r["event_type"] == "TOOL_STARTING"
    ]
    assert origins == ["LOCAL", "MCP"]


@patch("google.cloud.bigquery.Client")
def test_a_remote_agent_servers_tool_is_an_a2a_call(
    client_cls, fresh_writer, monkeypatch
):
    monkeypatch.setenv("BQ_ANALYTICS_DATASET_ID", "telemetry")
    client = client_cls.return_value
    client.insert_rows_json.return_value = []
    url = "https://billing.example/a2a/app"
    options = ClaudeAgentOptions(
        mcp_servers={
            "billing": remote_agent("billing", url),
            "docs": {"type": "http", "url": "https://docs.example/mcp"},
        }
    )

    parser = TurnParser(agent_name())
    recorder = bq_analytics.start_turn(
        "hi", options, "conv-1", "conv-1", None, parser.agent_name, instruction=None
    )
    assert recorder is not None
    _play(parser, recorder, _tool_turn("mcp__billing__ask"))
    assert bq_analytics.writer().flush(5)

    rows = [r for c in client.insert_rows_json.call_args_list for r in c.args[1]]
    interaction = next(r for r in rows if r["event_type"] == "A2A_INTERACTION")
    assert json.loads(interaction["attributes"])["a2a_metadata"]["a2a:url"] == url


@patch("google.cloud.bigquery.Client")
def test_an_unexpected_write_error_doesnt_stop_the_writer(
    client_cls, fresh_writer, monkeypatch, caplog
):
    monkeypatch.setenv("BQ_ANALYTICS_DATASET_ID", "telemetry")
    client = client_cls.return_value
    client.insert_rows_json.return_value = []
    circular: dict = {"event_id": "e-1"}
    circular["self"] = circular

    out = bq_analytics.writer()
    out.put(circular)
    assert out.flush(5)
    out.put({"event_id": "e-2"})
    assert out.flush(5)

    rows = [r for c in client.insert_rows_json.call_args_list for r in c.args[1]]
    assert [r["event_id"] for r in rows] == ["e-2"]
    assert "Circular reference" in caplog.text


@patch("google.cloud.bigquery.Client")
def test_rows_name_the_agent_after_the_setting(client_cls, fresh_writer, monkeypatch):
    monkeypatch.setenv("BQ_ANALYTICS_DATASET_ID", "telemetry")
    monkeypatch.setattr(settings, "AGENT_NAME", "weather_agent")
    client_cls.return_value.insert_rows_json.return_value = []

    parser = TurnParser(agent_name())
    recorder = bq_analytics.start_turn(
        "hi",
        ClaudeAgentOptions(),
        "conv-1",
        "conv-1",
        None,
        parser.agent_name,
        instruction=None,
    )
    assert recorder is not None
    _play(parser, recorder, _tool_turn("mcp__weather__get_weather"))
    assert bq_analytics.writer().flush(5)

    calls = client_cls.return_value.insert_rows_json.call_args_list
    rows = [r for c in calls for r in c.args[1]]
    assert rows and {r["agent"] for r in rows} == {"weather_agent"}


def test_missing_credentials_turn_analytics_off(fresh_writer, monkeypatch):
    monkeypatch.setenv("BQ_ANALYTICS_DATASET_ID", "telemetry")
    with patch(
        "google.cloud.bigquery.Client", MagicMock(side_effect=OSError("no ADC"))
    ):
        assert bq_analytics.writer() is None
        assert bq_analytics.writer() is None  # not retried every turn
