# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A2A protocol layer: the executor, the agent card, and the routes."""

from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import aclosing

from a2a.helpers import new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, Part
from a2a.utils.constants import (
    AGENT_CARD_WELL_KNOWN_PATH,
    PROTOCOL_VERSION_0_3,
    PROTOCOL_VERSION_1_0,
)
from claude_agent_sdk import Message
from fastapi import FastAPI
from opentelemetry import trace

from app.agent import root_agent
from app.app_utils import memory_bank
from app.app_utils.claude import stream_turn
from app.app_utils.replies import TurnReply, record, starts_block, text_delta
from app.app_utils.telemetry.config import on_agent_runtime

# Matches the `agents-cli run --mode a2a --app-name` default.
_A2A_NAME = os.environ.get("A2A_NAME", "app")
_A2A_RPC_PATH = f"/a2a/{_A2A_NAME}"


def add_a2a_routes(app: FastAPI) -> None:
    """Mount the JSON-RPC endpoint and the agent card on the app."""
    agent_card = _agent_card()
    request_handler = DefaultRequestHandler(
        agent_executor=ClaudeAgentExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(
            agent_card, card_url=f"{_A2A_RPC_PATH}{AGENT_CARD_WELL_KNOWN_PATH}"
        ),
        jsonrpc_routes=create_jsonrpc_routes(
            request_handler, rpc_url=_A2A_RPC_PATH, enable_v0_3_compat=True
        ),
    )


class ClaudeAgentExecutor(AgentExecutor):
    """Bridge the A2A request lifecycle to a Claude Agent SDK turn."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is None:
            # The Task must be queued before any status update.
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()

        structured = root_agent.output_format is not None
        artifact = _ResponseArtifact(updater, stream_text=not structured)
        reply = TurnReply()
        turn_span = trace.INVALID_SPAN
        try:
            async with aclosing(
                stream_turn(
                    context.get_user_input(),
                    root_agent,
                    task.context_id,
                    # As ADK names unauthenticated callers.
                    _caller(context) or f"A2A_USER_{task.context_id}",
                )
            ) as stream:
                async for message in stream:
                    turn_span = trace.get_current_span()
                    await artifact.add(message)
                    reply = record(reply, message)
        except Exception as e:
            # Log under the ended turn span so the record links to its trace.
            with trace.use_span(turn_span):
                logging.exception("Claude agent turn failed")
            await updater.failed(
                updater.new_agent_message([Part(text=f"Agent error: {e}")])
            )
            return

        await artifact.finish(_final_reply(reply, artifact.answer, structured))
        if on_agent_runtime():
            # Agent Runtime throttles the CPU once the response ends.
            await memory_bank.flush()
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


class _ResponseArtifact:
    """The "response" artifact a turn's text deltas stream into."""

    def __init__(self, updater: TaskUpdater, *, stream_text: bool) -> None:
        self._updater = updater
        self._stream_text = stream_text
        self._id = uuid.uuid4().hex
        self._started = False
        self._separator = ""
        self._answer: list[str] = []

    @property
    def answer(self) -> str:
        """The text streamed since the last tool call."""
        return "".join(self._answer)

    async def add(self, message: Message) -> None:
        """Stream the text `message` adds, separating text blocks with a blank line."""
        if self._started and starts_block(message, "text"):
            self._separator = "\n\n"
        if starts_block(message, "tool_use"):
            self._answer = []
        if not self._stream_text or not (text := text_delta(message)):
            return
        await self._updater.add_artifact(
            [Part(text=self._separator + text)],
            artifact_id=self._id,
            name="response",
            append=self._started,
            last_chunk=False,
        )
        self._started = True
        self._answer.append((self._separator if self._answer else "") + text)
        self._separator = ""

    async def finish(self, reply: str) -> None:
        """Send `reply` as the last chunk, replacing the streamed ones.

        Some clients keep only the last chunk, so it carries the whole reply.
        """
        if reply:
            await self._updater.add_artifact(
                [Part(text=reply)],
                artifact_id=self._id,
                name="response",
                append=False,
                last_chunk=True,
            )


def _final_reply(reply: TurnReply, answer: str, structured: bool) -> str:
    """The reply a caller gets once the turn has finished.

    With `output_format` set, it's the structured output as JSON. Otherwise it's
    `answer`, the text streamed after the last tool call, so the model's narration
    before a tool call is dropped; a turn that streamed no text falls back to the
    turn's reply.
    """
    if structured and reply.result and reply.result.structured_output is not None:
        return json.dumps(reply.result.structured_output)
    return answer or reply.text


def _agent_card() -> AgentCard:
    rpc_url = f"{advertised_base_url()}{_A2A_RPC_PATH}"
    return AgentCard(
        name=_A2A_NAME,
        description="A Claude Agent SDK agent served over the A2A protocol.",
        supported_interfaces=[
            AgentInterface(
                url=rpc_url,
                protocol_binding="JSONRPC",
                protocol_version=PROTOCOL_VERSION_1_0,
            ),
            # Gemini's registration validator still requires the 0.3 card shape.
            AgentInterface(
                url=rpc_url,
                protocol_binding="JSONRPC",
                protocol_version=PROTOCOL_VERSION_0_3,
            ),
        ],
        version=os.environ.get("AGENT_VERSION", "0.1.0"),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        skills=[
            AgentSkill(
                id="chat",
                name="chat",
                description="Hold a conversation with the agent.",
                tags=["chat", "claude"],
            )
        ],
    )


def advertised_base_url() -> str:
    """Base URL for the agent card."""
    if app_url := os.environ.get("APP_URL"):
        return app_url
    # APP_URL can be unset on a first Agent Runtime deploy.
    engine_id = os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID")
    location = os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if engine_id and location and project:
        return (
            f"https://{location}-aiplatform.googleapis.com/reasoningEngines/v1"
            f"/projects/{project}/locations/{location}"
            f"/reasoningEngines/{engine_id}/api"
        )
    host = os.environ.get("HOST", "127.0.0.1")
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return f"http://{host}:{os.environ.get('PORT', '8080')}"


def _caller(context: RequestContext) -> str | None:
    """The authenticated caller's name; unauthenticated names are ignored."""
    user = context.call_context.user if context.call_context else None
    if user and user.user_name and user.is_authenticated:
        return user.user_name
    return None
