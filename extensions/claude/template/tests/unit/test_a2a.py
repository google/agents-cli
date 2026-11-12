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

"""The A2A layer in `app/app_utils/a2a.py`: its agent card and executor."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    TextBlock,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from app.app_utils import a2a
from app.app_utils.a2a import advertised_base_url
from app.app_utils.telemetry import FASTAPI_TELEMETRY


def test_agent_runtime_card_points_at_the_passthrough(monkeypatch):
    """A first deploy without Agent Identity gets no APP_URL from agents-cli."""
    monkeypatch.delenv("APP_URL", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "123")
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION", "us-central1")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")

    assert advertised_base_url() == (
        "https://us-central1-aiplatform.googleapis.com/reasoningEngines/v1"
        "/projects/my-project/locations/us-central1/reasoningEngines/123/api"
    )


_CALLER_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


def _trace_id_seen_by_a_turn(telemetry: dict) -> str | None:
    """Send one A2A message with a caller's traceparent; the turn's trace id."""
    seen = []

    async def fake_stream_turn(prompt, options, conversation_id, user_id=None):
        context = trace.get_current_span().get_span_context()
        seen.append(format(context.trace_id, "032x") if context.is_valid else None)
        return
        yield

    with patch.object(a2a, "stream_turn", fake_stream_turn):
        app = FastAPI(telemetry={**telemetry, "tracer_provider": TracerProvider()})
        a2a.add_a2a_routes(app)
        response = TestClient(app).post(
            "/a2a/app",
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "message/send",
                "params": {
                    "message": {
                        "messageId": uuid.uuid4().hex,
                        "role": "user",
                        "parts": [{"kind": "text", "text": "hi"}],
                    }
                },
            },
            headers={"traceparent": f"00-{_CALLER_TRACE_ID}-00f067aa0ba902b7-01"},
        )
    assert response.status_code == 200
    [trace_id] = seen
    return trace_id


def test_a_turn_ignores_the_callers_trace_context():
    assert _trace_id_seen_by_a_turn(FASTAPI_TELEMETRY) != _CALLER_TRACE_ID
    # FastAPI's default would continue the caller's trace.
    assert _trace_id_seen_by_a_turn({}) == _CALLER_TRACE_ID


def _block_start(kind: str) -> StreamEvent:
    return StreamEvent(
        uuid="u",
        session_id="s-1",
        event={"type": "content_block_start", "content_block": {"type": kind}},
    )


def _text(text: str) -> StreamEvent:
    return StreamEvent(
        uuid="u",
        session_id="s-1",
        event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
    )


def test_the_reply_holds_only_the_text_after_the_last_tool_call():
    stream = [
        _block_start("text"),
        _text("Asking the remote agent:"),
        _block_start("tool_use"),
        _block_start("text"),
        _text("Reykjavik is "),
        _text("cold."),
    ]

    async def fake_stream_turn(prompt, options, conversation_id, user_id=None):
        for message in stream:
            yield message

    with patch.object(a2a, "stream_turn", fake_stream_turn):
        app = FastAPI(telemetry=FASTAPI_TELEMETRY)
        a2a.add_a2a_routes(app)
        response = TestClient(app).post(
            "/a2a/app",
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "message/stream",
                "params": {
                    "message": {
                        "messageId": uuid.uuid4().hex,
                        "role": "user",
                        "parts": [{"kind": "text", "text": "weather?"}],
                    }
                },
            },
        )

    updates = [
        "".join(p["text"] for p in event["artifact"]["parts"])
        for line in response.text.splitlines()
        if line.startswith("data:")
        and (event := json.loads(line.removeprefix("data:"))["result"])["kind"]
        == "artifact-update"
    ]
    *chunks, last = updates
    assert "".join(chunks) == "Asking the remote agent:\n\nReykjavik is cold."
    assert last == "Reykjavik is cold."


@pytest.mark.parametrize(
    "stream",
    [
        [_block_start("text"), _text("The fact is "), _text("**77**")],
        # A turn that streamed no deltas still gets its reply.
        [AssistantMessage(content=[TextBlock(text="The fact is **77**")], model="m")],
    ],
    ids=["streamed", "not_streamed"],
)
def test_the_last_artifact_chunk_carries_the_whole_reply(stream):
    """ADK's RemoteA2aAgent keeps only the last chunk's parts."""

    async def fake_stream_turn(prompt, options, conversation_id, user_id=None):
        for message in stream:
            yield message

    with patch.object(a2a, "stream_turn", fake_stream_turn):
        app = FastAPI(telemetry=FASTAPI_TELEMETRY)
        a2a.add_a2a_routes(app)
        response = TestClient(app).post(
            "/a2a/app",
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "message/stream",
                "params": {
                    "message": {
                        "messageId": uuid.uuid4().hex,
                        "role": "user",
                        "parts": [{"kind": "text", "text": "fact?"}],
                    }
                },
            },
        )

    updates = [
        event
        for line in response.text.splitlines()
        if line.startswith("data:")
        and (event := json.loads(line.removeprefix("data:"))["result"])["kind"]
        == "artifact-update"
    ]
    *chunks, last = updates
    assert not any(chunk.get("lastChunk") for chunk in chunks)
    assert last["lastChunk"] is True
    assert not last.get("append")
    assert "".join(p["text"] for p in last["artifact"]["parts"]) == (
        "The fact is **77**"
    )


def test_a_structured_output_turn_replies_with_only_the_json():
    """With output_format, callers get the JSON without the model's preceding text."""
    weather = {"city": "San Francisco", "temperature_f": 60, "conditions": "foggy"}
    stream = [
        _block_start("text"),
        _text("Now let me provide the structured output:"),
        _block_start("tool_use"),
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s-1",
            result=json.dumps(weather),
            structured_output=weather,
        ),
    ]

    async def fake_stream_turn(prompt, options, conversation_id, user_id=None):
        for message in stream:
            yield message

    options = ClaudeAgentOptions(
        output_format={"type": "json_schema", "schema": {"type": "object"}}
    )
    with (
        patch.object(a2a, "stream_turn", fake_stream_turn),
        patch.object(a2a, "root_agent", options),
    ):
        app = FastAPI(telemetry=FASTAPI_TELEMETRY)
        a2a.add_a2a_routes(app)
        response = TestClient(app).post(
            "/a2a/app",
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "message/stream",
                "params": {
                    "message": {
                        "messageId": uuid.uuid4().hex,
                        "role": "user",
                        "parts": [{"kind": "text", "text": "weather?"}],
                    }
                },
            },
        )

    [update] = [
        event
        for line in response.text.splitlines()
        if line.startswith("data:")
        and (event := json.loads(line.removeprefix("data:"))["result"])["kind"]
        == "artifact-update"
    ]
    assert update["lastChunk"] is True
    [part] = update["artifact"]["parts"]
    assert json.loads(part["text"]) == weather


def test_cancel_ends_the_task_canceled_and_stops_the_turn():
    """Stopping the turn closes the SDK stream and its CLI subprocess."""
    started = threading.Event()
    closed = []

    async def fake_stream_turn(prompt, options, conversation_id, user_id=None):
        try:
            yield _text("Once upon a time")
            started.set()
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    def rpc(client: TestClient, method: str, params: dict) -> dict:
        response = client.post(
            "/a2a/app",
            json={"jsonrpc": "2.0", "id": "1", "method": method, "params": params},
        )
        return response.json()

    with patch.object(a2a, "stream_turn", fake_stream_turn):
        app = FastAPI(telemetry=FASTAPI_TELEMETRY)
        a2a.add_a2a_routes(app)
        with TestClient(app) as client:
            sent = rpc(
                client,
                "message/send",
                {
                    "message": {
                        "messageId": uuid.uuid4().hex,
                        "role": "user",
                        "parts": [{"kind": "text", "text": "a long story"}],
                    },
                    "configuration": {"blocking": False},
                },
            )
            task_id = sent["result"]["id"]
            assert started.wait(timeout=10)

            canceled = rpc(client, "tasks/cancel", {"id": task_id})
            task = rpc(client, "tasks/get", {"id": task_id})

    assert "error" not in canceled
    assert canceled["result"]["status"]["state"] == "canceled"
    assert task["result"]["status"]["state"] == "canceled"
    assert closed == [True]


def test_a_failed_turn_is_logged_under_its_trace():
    """The turn's span has ended when the failure is logged; the log keeps its trace."""
    tracer = TracerProvider().get_tracer(__name__)
    turn_trace = []

    async def fake_stream_turn(prompt, options, conversation_id, user_id=None):
        span = tracer.start_span("turn")
        turn_trace.append(span.get_span_context().trace_id)
        with trace.use_span(span, end_on_exit=True):
            yield _text("partial")
            raise RuntimeError("model not available")

    traces = _TraceOfRecords()
    logging.getLogger().addHandler(traces)
    with patch.object(a2a, "stream_turn", fake_stream_turn):
        app = FastAPI(telemetry=FASTAPI_TELEMETRY)
        a2a.add_a2a_routes(app)
        TestClient(app).post(
            "/a2a/app",
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "message/send",
                "params": {
                    "message": {
                        "messageId": uuid.uuid4().hex,
                        "role": "user",
                        "parts": [{"kind": "text", "text": "hi"}],
                    }
                },
            },
        )

    logging.getLogger().removeHandler(traces)

    assert traces.trace_ids["Claude agent turn failed"] == turn_trace[0]


class _TraceOfRecords(logging.Handler):
    """The trace each record was logged under, by message."""

    def __init__(self):
        super().__init__()
        self.trace_ids: dict[str, int] = {}

    def emit(self, record):
        context = trace.get_current_span().get_span_context()
        self.trace_ids[record.getMessage()] = context.trace_id


def _send_message(app: FastAPI) -> None:
    response = TestClient(app).post(
        "/a2a/app",
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": uuid.uuid4().hex,
                    "role": "user",
                    "parts": [{"kind": "text", "text": "hi"}],
                }
            },
        },
    )
    assert response.status_code == 200


def test_an_unauthenticated_caller_is_named_after_the_conversation():
    """As ADK names them, so sessions and memories have a user."""
    seen = []

    async def fake_stream_turn(prompt, options, conversation_id, user_id=None):
        seen.append((conversation_id, user_id))
        return
        yield

    with patch.object(a2a, "stream_turn", fake_stream_turn):
        app = FastAPI(telemetry=FASTAPI_TELEMETRY)
        a2a.add_a2a_routes(app)
        _send_message(app)

    [(conversation_id, user_id)] = seen
    assert user_id == f"A2A_USER_{conversation_id}"


@pytest.mark.parametrize("engine_id, waits", [("123", True), (None, False)])
def test_on_agent_runtime_a_turn_waits_for_its_memory_ingest(
    monkeypatch, engine_id, waits
):
    """Agent Runtime throttles the CPU once the response ends."""
    if engine_id:
        monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", engine_id)
    else:
        monkeypatch.delenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", raising=False)

    async def fake_stream_turn(prompt, options, conversation_id, user_id=None):
        yield _text("Hello.")

    flush = AsyncMock()
    with (
        patch.object(a2a, "stream_turn", fake_stream_turn),
        patch.object(a2a.memory_bank, "flush", flush),
    ):
        app = FastAPI(telemetry=FASTAPI_TELEMETRY)
        a2a.add_a2a_routes(app)
        _send_message(app)

    assert flush.await_count == (1 if waits else 0)
