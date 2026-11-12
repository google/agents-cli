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

"""Agent Runtime's `:query` and `:streamQuery` methods in `reasoning_engine.py`."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.app_utils import reasoning_engine

_USER = "user-1"
_AGENT = reasoning_engine.agent_name()
_QUERY = "/api/reasoning_engine"
_STREAM = "/api/stream_reasoning_engine"


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    reasoning_engine.add_reasoning_engine_routes(app)
    return TestClient(app)


def _stream(client: TestClient, **kwargs) -> list[dict]:
    response = client.post(
        _STREAM, json={"class_method": "async_stream_query", "input": kwargs}
    )
    assert response.status_code == 200, response.text
    return [json.loads(line) for line in response.text.splitlines()]


def _result(**fields) -> ResultMessage:
    defaults = {
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": "s",
    }
    return ResultMessage(**{**defaults, **fields})


class _FakeTurns:
    """Stands in for `stream_turn`: replays `messages` and records each call."""

    def __init__(self, messages: list) -> None:
        self.messages = messages
        self.calls: list[tuple[str, str, str | None]] = []

    async def __call__(self, prompt, options, conversation_id, user_id=None):
        self.calls.append((prompt, conversation_id, user_id))
        for message in self.messages:
            if isinstance(message, Exception):
                raise message
            yield message


def test_a_turn_streams_its_tool_calls_and_reply(client):
    turns = _FakeTurns(
        [
            AssistantMessage(
                content=[
                    ToolUseBlock(id="c1", name="get_weather", input={"city": "SF"})
                ],
                model="m",
            ),
            # A subagent's messages stay inside its tool call.
            AssistantMessage(
                content=[TextBlock(text="inner")], model="m", parent_tool_use_id="c1"
            ),
            UserMessage(content=[ToolResultBlock(tool_use_id="c1", content="Foggy")]),
            UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="c2", content="Not found", is_error=True
                    )
                ]
            ),
            StreamEvent(uuid="u", session_id="s", event={"type": "message_start"}),
            AssistantMessage(content=[TextBlock(text="It is foggy.")], model="m"),
            _result(result="It is foggy."),
        ]
    )
    with patch.object(reasoning_engine, "stream_turn", turns):
        events = _stream(client, message="Weather?", user_id=_USER, session_id="s-1")

    assert turns.calls == [("Weather?", "s-1", _USER)]
    call = {"id": "c1", "name": "get_weather", "args": {"city": "SF"}}
    found = {"id": "c1", "name": "get_weather", "response": {"result": "Foggy"}}
    missing = {"id": "c2", "name": "", "response": {"error": "Not found"}}
    assert [(e["content"]["role"], e["content"]["parts"]) for e in events] == [
        ("model", [{"function_call": call}]),
        ("user", [{"function_response": found}]),
        ("user", [{"function_response": missing}]),
        ("model", [{"text": "It is foggy."}]),
    ]
    assert len({e["invocation_id"] for e in events}) == 1
    assert {e["author"] for e in events} == {_AGENT}


def test_a_turn_without_a_session_starts_a_new_conversation(client):
    turns = _FakeTurns([])
    message = {"role": "user", "parts": [{"text": "Hello "}, {"text": "there"}]}
    with patch.object(reasoning_engine, "stream_turn", turns):
        _stream(client, message=message, user_id=_USER)

    [(prompt, conversation_id, user_id)] = turns.calls
    assert (prompt, user_id) == ("Hello there", _USER)
    assert uuid.UUID(conversation_id)


@pytest.mark.parametrize(
    "messages, error",
    [
        pytest.param([RuntimeError("CLI crashed")], "CLI crashed", id="raised"),
        pytest.param(
            # The SDK raises after an error result too.
            [_result(is_error=True, result="Rate limited"), RuntimeError("exit 1")],
            "Rate limited",
            id="error_result",
        ),
    ],
)
def test_a_failed_turn_ends_with_one_error_event(client, messages, error):
    with patch.object(reasoning_engine, "stream_turn", _FakeTurns(messages)):
        [event] = _stream(client, message="hi", user_id=_USER)

    assert event["error_code"] == "AGENT_ERROR"
    assert event["error_message"] == error
    assert "content" not in event


def test_a_structured_output_turn_replies_with_the_json(client, monkeypatch):
    weather = {"city": "SF"}
    monkeypatch.setattr(
        reasoning_engine,
        "root_agent",
        ClaudeAgentOptions(output_format={"type": "json_schema", "schema": {}}),
    )
    turns = _FakeTurns([_result(result="", structured_output=weather)])
    with patch.object(reasoning_engine, "stream_turn", turns):
        [event] = _stream(client, message="hi", user_id=_USER)

    assert json.loads(event["content"]["parts"][0]["text"]) == weather


@pytest.mark.parametrize(
    "session_input, session_id",
    [
        pytest.param({}, None, id="new"),
        pytest.param({"session_id": "mine"}, "mine", id="given"),
    ],
)
def test_create_session_returns_the_session_to_continue(
    client, session_input, session_id
):
    response = client.post(
        _QUERY,
        json={
            "class_method": "async_create_session",
            "input": {"user_id": _USER, **session_input},
        },
    )

    session = response.json()["output"]
    assert (session["user_id"], session["events"]) == (_USER, [])
    assert session["id"] == session_id or uuid.UUID(session["id"])


_BAD_CALLS = {
    "unknown_query_method": (
        _QUERY,
        {"class_method": "async_get_session", "input": {}},
        404,
        "Supported: async_create_session",
    ),
    "unknown_stream_method": (
        _STREAM,
        {"class_method": "streaming_agent_run_with_events", "input": {}},
        404,
        "Supported: async_stream_query",
    ),
    "body_not_object": (_QUERY, ["x"], 400, "class_method"),
    "input_not_object": (
        _QUERY,
        {"class_method": "async_create_session", "input": "u"},
        400,
        "must be an object",
    ),
    "input_empty_list": (
        _QUERY,
        {"class_method": "async_create_session", "input": []},
        400,
        "must be an object",
    ),
    "missing_user_id": (
        _STREAM,
        {"class_method": "async_stream_query", "input": {"message": "hi"}},
        400,
        "user_id must be a non-empty string",
    ),
    "session_id_not_string": (
        _STREAM,
        {
            "class_method": "async_stream_query",
            "input": {"user_id": "u", "message": "hi", "session_id": 7},
        },
        400,
        "session_id must be a non-empty string",
    ),
    "empty_message": (
        _STREAM,
        {
            "class_method": "async_stream_query",
            "input": {"user_id": "u", "message": " "},
        },
        400,
        "no text",
    ),
    "bad_message": (
        _STREAM,
        {"class_method": "async_stream_query", "input": {"user_id": "u", "message": 3}},
        400,
        "must be a string",
    ),
}


@pytest.mark.parametrize(
    "path, body, status, detail", _BAD_CALLS.values(), ids=_BAD_CALLS
)
def test_bad_calls_are_refused_before_a_turn_runs(client, path, body, status, detail):
    turns = _FakeTurns([])
    with patch.object(reasoning_engine, "stream_turn", turns):
        response = client.post(path, json=body)

    assert response.status_code == status
    assert detail in response.json()["detail"]
    assert turns.calls == []


def test_a_body_that_is_not_json_is_refused(client):
    response = client.post(_QUERY, content=b"{")

    assert response.status_code == 400
    assert "not JSON" in response.json()["detail"]


@pytest.mark.parametrize("engine_id, waits", [("123", True), (None, False)])
def test_on_agent_runtime_a_turn_waits_for_its_memory_ingest(
    client, monkeypatch, engine_id, waits
):
    """Agent Runtime throttles the CPU once the response ends."""
    if engine_id:
        monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", engine_id)
    else:
        monkeypatch.delenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", raising=False)
    turns = _FakeTurns([_result(result="Hi.")])
    flush = AsyncMock()
    with (
        patch.object(reasoning_engine, "stream_turn", turns),
        patch.object(reasoning_engine.memory_bank, "flush", flush),
    ):
        _stream(client, message="hi", user_id=_USER)

    assert flush.await_count == (1 if waits else 0)
