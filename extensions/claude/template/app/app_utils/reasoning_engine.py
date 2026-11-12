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

"""Agent Runtime's `:query` and `:streamQuery` methods.

Agent Runtime forwards them to `/api/reasoning_engine` and
`/api/stream_reasoning_engine` with a `{"class_method": ..., "input": {...}}` body.
An ingress Agent Gateway governs only these two methods. They serve ADK's
`async_create_session` and `async_stream_query`, with ADK's inputs and events, so
ADK clients such as `agents-cli run --mode adk` work. A session id is a
conversation id.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    Message,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from fastapi import FastAPI, HTTPException, Request, responses
from opentelemetry import trace

from app.agent import root_agent
from app.app_utils import memory_bank
from app.app_utils.claude import stream_turn
from app.app_utils.replies import result_error, tool_result_text
from app.app_utils.telemetry.config import agent_name, on_agent_runtime


def add_reasoning_engine_routes(app: FastAPI) -> None:
    """Mount the routes Agent Runtime forwards `:query` and `:streamQuery` to."""

    @app.post("/api/reasoning_engine")
    async def query(request: Request) -> dict[str, Any]:
        args = await _input(request, "async_create_session")
        user_id = _string(args, "user_id")
        session_id = _session_id(args)
        session = {
            "id": session_id,
            "app_name": agent_name(),
            "user_id": user_id,
            "state": {},
            "events": [],
            "last_update_time": time.time(),
        }
        return {"output": session}

    @app.post("/api/stream_reasoning_engine")
    async def stream_query(request: Request) -> responses.StreamingResponse:
        args = await _input(request, "async_stream_query")
        prompt = _prompt(args.get("message"))
        events = _turn(prompt, _string(args, "user_id"), _session_id(args))

        async def lines() -> AsyncIterator[str]:
            async with aclosing(events):
                async for event in events:
                    yield json.dumps(event) + "\n"

        return responses.StreamingResponse(lines(), media_type="application/json")


async def _input(request: Request, class_method: str) -> dict[str, Any]:
    """The `input` of a call to `class_method`, the one method the route serves."""
    try:
        body = await request.json()
    except ValueError as err:
        raise HTTPException(400, "The request body is not JSON.") from err
    if not isinstance(body, dict):
        raise HTTPException(400, 'Send {"class_method": ..., "input": {...}}.')
    if body.get("class_method") != class_method:
        raise HTTPException(
            404,
            f"Unsupported reasoning_engine method: {body.get('class_method')!r}. "
            f"Supported: {class_method}.",
        )
    args = body.get("input")
    if args is None:
        return {}
    if not isinstance(args, dict):
        raise HTTPException(400, '"input" must be an object.')
    return args


def _string(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise HTTPException(400, f"{key} must be a non-empty string.")
    return value


def _session_id(args: dict[str, Any]) -> str:
    """The session to continue, or a new one."""
    if args.get("session_id") is None:
        return str(uuid.uuid4())
    return _string(args, "session_id")


def _prompt(message: Any) -> str:
    """The text of a message: a string, or a Content object's text parts."""
    if isinstance(message, str):
        text = message
    elif isinstance(message, dict) and isinstance(message.get("parts"), list):
        text = "".join(
            part["text"]
            for part in message["parts"]
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    else:
        raise HTTPException(
            400,
            'message must be a string or {"role": "user", "parts": [{"text": ...}]}.',
        )
    if not text.strip():
        raise HTTPException(400, "message has no text.")
    return text


async def _turn(
    prompt: str, user_id: str, session_id: str
) -> AsyncGenerator[dict[str, Any], None]:
    """Run one turn and yield its events; a failed turn ends with an error event."""
    invocation_id = f"e-{uuid.uuid4()}"
    tool_names: dict[str, str] = {}
    failed = False
    turn_span = trace.INVALID_SPAN
    try:
        async with aclosing(
            stream_turn(prompt, root_agent, session_id, user_id)
        ) as stream:
            async for message in stream:
                turn_span = trace.get_current_span()
                if isinstance(message, ResultMessage) and message.is_error:
                    failed = True
                    yield _error(invocation_id, result_error(message))
                elif event := _event(invocation_id, message, tool_names):
                    yield event
        if on_agent_runtime():
            # Agent Runtime throttles the CPU once the response ends.
            await memory_bank.flush()
    except Exception as e:
        # Log under the ended turn span so the record links to its trace.
        with trace.use_span(turn_span):
            logging.exception("Claude agent turn failed")
        # The SDK raises after an error result too, which already ended the turn.
        if not failed:
            yield _error(invocation_id, str(e))


def _event(
    invocation_id: str, message: Message, tool_names: dict[str, str]
) -> dict[str, Any] | None:
    """The event for a message: the agent's text and tool calls, or tool results.

    A subagent's messages are left out; its work shows as the tool call that ran it.
    """
    if isinstance(message, ResultMessage):
        # With output_format set, the reply is the structured output.
        if root_agent.output_format is None or message.structured_output is None:
            return None
        parts = [{"text": json.dumps(message.structured_output)}]
        return _adk_event(invocation_id, "model", parts)
    if not isinstance(message, AssistantMessage | UserMessage):
        return None
    if message.parent_tool_use_id or isinstance(message.content, str):
        return None
    parts: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, TextBlock) and block.text:
            parts.append({"text": block.text})
        elif isinstance(block, ToolUseBlock):
            tool_names[block.id] = block.name
            call = {"id": block.id, "name": block.name, "args": block.input}
            parts.append({"function_call": call})
        elif isinstance(block, ToolResultBlock):
            key = "error" if block.is_error else "result"
            response = {
                "id": block.tool_use_id,
                "name": tool_names.get(block.tool_use_id, ""),
                "response": {key: tool_result_text(block)},
            }
            parts.append({"function_response": response})
    if not parts:
        return None
    role = "user" if isinstance(message, UserMessage) else "model"
    return _adk_event(invocation_id, role, parts)


def _error(invocation_id: str, message: str) -> dict[str, Any]:
    return {
        **_adk_event(invocation_id, "model", []),
        "error_code": "AGENT_ERROR",
        "error_message": message,
    }


def _adk_event(
    invocation_id: str, role: str, parts: list[dict[str, Any]]
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "invocation_id": invocation_id,
        "author": agent_name(),
        "actions": {
            "state_delta": {},
            "artifact_delta": {},
            "requested_auth_configs": {},
            "requested_tool_confirmations": {},
        },
        "id": str(uuid.uuid4()),
        "timestamp": time.time(),
    }
    if parts:
        event["content"] = {"parts": parts, "role": role}
    return event
