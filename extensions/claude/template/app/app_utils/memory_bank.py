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

"""Long-term memory of each user, in Memory Bank, as ADK's memory tools keep it.

Enabled when MEMORY_BANK_ENABLED is true. Each turn:

- `recall` adds the user's memories that match the prompt to the system prompt
  (ADK's PreloadMemoryTool);
- `start_turn` records the main agent's events and, when the turn succeeds,
  sends them to Memory Bank in the background, which extracts memories from them;
- the `search_memory` tool lets the model search the memories (ADK's
  LoadMemoryTool).

Memories are scoped to the agent's name and the user. On Agent Runtime they live
in the agent's own engine. Elsewhere, in the engine named MEMORY_BANK_ENGINE_NAME
(default: the agent's name), which is created when missing.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import threading
import uuid
from typing import Any

import anyio
from claude_agent_sdk import (
    McpServerConfig,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from app.app_utils.conversation import current_user
from app.app_utils.replies import tool_result_text
from app.app_utils.telemetry.config import agent_name
from app.app_utils.turn_events import Event, ModelCallEnded, ToolCallEnded, TurnEnded

__all__ = [
    "allowed_tools",
    "enabled",
    "flush",
    "mcp_servers",
    "recall",
    "search_memory",
    "start_turn",
]

_PAST_CONVERSATIONS = """The following content is from your previous conversations with the user.
They may be useful for answering the user's current query.
<PAST_CONVERSATIONS>
{}
</PAST_CONVERSATIONS>
"""

_engine_name: str | None = None
_engine_lock = threading.Lock()
# Strong references, so running ingests aren't garbage collected.
_pending: set[asyncio.Task[None]] = set()


def enabled() -> bool:
    """Whether MEMORY_BANK_ENABLED turns memory on."""
    return os.environ.get("MEMORY_BANK_ENABLED", "").lower() in ("true", "1")


@tool(
    "search_memory",
    "Search your memories of earlier conversations with the user. Call it when "
    "the user's request may depend on something they told you before.",
    {"query": str},
)
async def search_memory(args: dict[str, Any]) -> dict[str, Any]:
    lines = await _search(args["query"], current_user.get())
    text = "\n".join(lines) if lines else "No memories found."
    return {"content": [{"type": "text", "text": text}]}


_server = create_sdk_mcp_server(name="memory", tools=[search_memory])


def mcp_servers() -> dict[str, McpServerConfig]:
    """The memory tool's server, keyed by name."""
    return {"memory": _server}


def allowed_tools() -> list[str]:
    """The memory tool's allow rule."""
    return ["mcp__memory__search_memory"]


async def recall(system_prompt: Any, prompt: str, user_id: str) -> Any:
    """`system_prompt` with the user's memories that match `prompt` added.

    Without memories, or when they can't be read, it is returned unchanged.
    """
    if not enabled():
        return system_prompt
    try:
        lines = await _search(prompt, user_id)
    except Exception as e:  # memory must never stop the agent
        logging.warning("Couldn't recall the user's memories: %s", e)
        return system_prompt
    if not lines:
        return system_prompt
    return _with_memories(system_prompt, _PAST_CONVERSATIONS.format("\n".join(lines)))


def start_turn(prompt: str, session_id: str, user_id: str) -> MemoryIngest | None:
    """A recorder that sends one turn to Memory Bank, or None when memory is off."""
    if not enabled():
        return None
    return MemoryIngest(prompt, session_id, user_id)


async def flush() -> None:
    """Wait until the turns' pending ingests are sent."""
    # shortcut: waits for every turn's pending ingest, not only the caller's;
    # track them per turn if a turn shouldn't wait on another one's.
    if _pending:
        await asyncio.wait(set(_pending))


class MemoryIngest:
    """Collects one turn's main-agent events, and sends them if the turn succeeds."""

    def __init__(self, prompt: str, session_id: str, user_id: str) -> None:
        self._session_id = session_id
        self._user_id = user_id
        self._turn = uuid.uuid4().hex
        self._tool_names: dict[str, str] = {}
        self._events: list[dict[str, Any]] = []
        self._add("user", [{"text": prompt}])

    def handle(self, event: Event) -> None:
        # Subagents' events are left out: the main agent's tool call and its
        # result stand for a subagent's work.
        if isinstance(event, ModelCallEnded) and event.agent is None:
            self._add("model", self._model_parts(event.blocks))
        elif isinstance(event, ToolCallEnded) and event.agent is None:
            block = ToolResultBlock(
                tool_use_id=event.id, content=event.content, is_error=event.is_error
            )
            response = {
                "id": event.id,
                "name": self._tool_names.get(event.id, ""),
                "response": {
                    "error" if event.is_error else "result": tool_result_text(block)
                },
            }
            self._add("user", [{"function_response": response}])
        elif isinstance(event, TurnEnded) and not event.failed:
            task = asyncio.create_task(
                _ingest(self._session_id, self._user_id, self._events)
            )
            _pending.add(task)
            task.add_done_callback(_pending.discard)

    def _model_parts(self, blocks: tuple[Any, ...]) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        for block in blocks:
            if isinstance(block, TextBlock) and block.text:
                parts.append({"text": block.text})
            elif isinstance(block, ToolUseBlock):
                self._tool_names[block.id] = block.name
                call = {"id": block.id, "name": block.name, "args": block.input}
                parts.append({"function_call": call})
        return parts

    def _add(self, role: str, parts: list[dict[str, Any]]) -> None:
        if not parts:
            return
        self._events.append(
            {
                "content": {"role": role, "parts": parts},
                "event_id": f"{self._turn}-{len(self._events)}",
                "event_time": datetime.datetime.now(datetime.UTC),
            }
        )


async def _ingest(session_id: str, user_id: str, events: list[dict[str, Any]]) -> None:
    try:
        async with _client().aio as client:
            await client.memory_banks.ingest_events(
                name=await _engine(),
                scope=_scope(user_id),
                stream_id=session_id,
                direct_contents_source={"events": events},
            )
    except Exception as e:  # memory must never stop the agent
        logging.warning("Couldn't send the turn to Memory Bank: %s", e)


async def _search(query: str, user_id: str) -> list[str]:
    """The user's memories that match `query`, as ADK's memory tools show them."""
    lines: list[str] = []
    async with _client().aio as client:
        retrieved = await client.memory_banks.memories.retrieve(
            name=await _engine(),
            scope=_scope(user_id),
            similarity_search_params={"search_query": query},
        )
        async for item in retrieved:
            memory = item.memory
            if memory is None or not memory.fact:
                continue
            if memory.update_time:
                lines.append(f"Time: {memory.update_time.isoformat()}")
            # Memory Bank's memories are the user's, so ADK shows each as "user: <fact>".
            lines.append(f"user: {memory.fact}")
    return lines


def _with_memories(system_prompt: Any, memories: str) -> Any:
    """`system_prompt` with `memories` appended, rebuilt on every request.

    snapshot=False makes the CLI send this turn's memories, not the system
    prompt the session recorded on its first request.
    """
    if system_prompt is None:
        # The SDK sends no system prompt as an empty custom one.
        return {"type": "custom", "prompt": memories, "snapshot": False}
    if isinstance(system_prompt, str):
        return {
            "type": "custom",
            "prompt": _joined(system_prompt, memories),
            "snapshot": False,
        }
    if system_prompt.get("type") == "preset":
        append = _joined(system_prompt.get("append", ""), memories)
        return {**system_prompt, "append": append, "snapshot": False}
    if system_prompt.get("type") == "custom":
        prompt = _joined(system_prompt["prompt"], memories)
        return {**system_prompt, "prompt": prompt, "snapshot": False}
    logging.warning(
        "Memories are not added to a system prompt file; use a string system "
        "prompt to get them."
    )
    return system_prompt


def _joined(text: str, memories: str) -> str:
    return f"{text}\n\n{memories}" if text else memories


def _scope(user_id: str) -> dict[str, str]:
    return {"app_name": agent_name(), "user_id": user_id}


def _client() -> Any:
    """A client per call: its async client is bound to the event loop it first ran in."""
    import agentplatform

    return agentplatform.Client(location=_location())


def _location() -> str:
    """The engine's region; Agent Runtime doesn't support "global"."""
    if location := os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION"):
        return location
    location = os.environ.get("GOOGLE_CLOUD_LOCATION")
    if location and location != "global":
        return location
    # `scaffold create --region` rewrites this default to the project's region.
    return "us-east1"


async def _engine() -> str:
    """The engine whose Memory Bank holds the memories, resolved once per process."""
    return await anyio.to_thread.run_sync(_resolve_engine)


def _resolve_engine() -> str:
    global _engine_name
    with _engine_lock:
        if _engine_name is None:
            _engine_name = _find_or_create_engine()
        return _engine_name


def _find_or_create_engine() -> str:
    if engine_id := os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID"):
        return f"reasoningEngines/{engine_id}"
    name = os.environ.get("MEMORY_BANK_ENGINE_NAME") or agent_name()
    client = _client()
    found = list(client.runtimes.list(config={"filter": f"display_name={name}"}))
    if found:
        return found[0].api_resource.name
    logging.info("Creating Agent Runtime %r for Memory Bank", name)
    return client.runtimes.create(config={"display_name": name}).api_resource.name
