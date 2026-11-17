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

"""A2A protocol layer: the executor, the agent card, and the routes.

`fast_api_app.py` owns the app; this owns what A2A clients see.
"""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING

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
from fastapi import FastAPI
from opentelemetry import trace

from app.agent import root_agent
from app.app_utils.content import content_to_text

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph


def advertised_base_url() -> str:
    """Base URL for the agent card: APP_URL, else the host and port we bind."""
    if app_url := os.environ.get("APP_URL"):
        return app_url
    host = os.environ.get("HOST", "127.0.0.1")
    if host in ("0.0.0.0", "::", ""):  # what we bind is not what clients dial
        host = "127.0.0.1"
    return f"http://{host}:{os.environ.get('PORT', '8080')}"


_tracer = trace.get_tracer("langchain-agent")

# Mount name for the A2A endpoint. Defaults to the project's agent directory
# ("app") so it matches the `agents-cli run --mode a2a` default (--agent-name).
A2A_NAME = os.environ.get("A2A_NAME", "app")
A2A_RPC_PATH = f"/a2a/{A2A_NAME}"


class LangGraphAgentExecutor(AgentExecutor):
    """Bridge the A2A request lifecycle to the compiled LangGraph graph."""

    def __init__(self, agent: CompiledStateGraph = root_agent) -> None:
        self._agent = agent

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_input = context.get_user_input()
        task = context.current_task
        if task is None:
            # First message of a conversation. The Task itself has to reach the
            # queue before any status update, or the server rejects the stream.
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()

        # Stream the response as task artifacts (A2A clients read the reply from
        # task artifacts, not the final status message). "messages" mode yields
        # LLM token chunks; "values" mode gives the final state, used as a
        # fallback for graphs whose nodes don't stream tokens (e.g. the echo
        # demo model). The whole turn is wrapped in an `invoke_agent` span.
        artifact_id = uuid.uuid4().hex
        streamed = False
        final_reply = ""
        with _tracer.start_as_current_span("invoke_agent"):
            async for mode, data in self._agent.astream(
                {"messages": [{"role": "user", "content": user_input}]},
                config={"configurable": {"thread_id": task.context_id or task.id}},
                stream_mode=["messages", "values"],
            ):
                if mode == "messages":
                    chunk, _metadata = data
                    text = content_to_text(getattr(chunk, "content", ""))
                    if text:
                        await updater.add_artifact(
                            [Part(text=text)],
                            artifact_id=artifact_id,
                            name="response",
                            append=streamed,
                            last_chunk=False,
                        )
                        streamed = True
                elif mode == "values":
                    messages = data.get("messages") if isinstance(data, dict) else None
                    if messages:
                        final_reply = content_to_text(
                            getattr(messages[-1], "content", "")
                        )

            if not streamed and final_reply:
                # Non-streaming node: emit the final reply as a single artifact.
                await updater.add_artifact(
                    [Part(text=final_reply)],
                    artifact_id=artifact_id,
                    name="response",
                )
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("This agent does not support cancellation.")


def _agent_card() -> AgentCard:
    rpc_url = f"{advertised_base_url()}{A2A_RPC_PATH}"
    return AgentCard(
        name=A2A_NAME,
        description="A LangChain agent served over the A2A protocol.",
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
                tags=["chat", "langchain"],
            )
        ],
    )


def add_a2a_routes(app: FastAPI, agent: CompiledStateGraph = root_agent) -> None:
    """Mount the JSON-RPC endpoint and the agent card on the app."""
    agent_card = _agent_card()
    request_handler = DefaultRequestHandler(
        agent_executor=LangGraphAgentExecutor(agent=agent),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(
            agent_card, card_url=f"{A2A_RPC_PATH}{AGENT_CARD_WELL_KNOWN_PATH}"
        ),
        # v0.3 compat keeps older A2A clients working against the same endpoint.
        jsonrpc_routes=create_jsonrpc_routes(
            request_handler, rpc_url=A2A_RPC_PATH, enable_v0_3_compat=True
        ),
    )
