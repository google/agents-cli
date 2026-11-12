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

"""Group one turn's SDK messages into model calls, tool calls and subagent runs.

BigQuery Agent Analytics, prompt-response logging and the call-count metrics all
read a turn through this parser. Every turn ends with one TurnEnded event, even
when the parser stopped reading the turn's messages. It handles the CLI's message
quirks once:

- one AssistantMessage per content block of a model call, all with its message_id;
- the final usage and stop reason arrive later, in a message_delta stream event;
- tool results can arrive before message_stop;
- subagent messages carry parent_tool_use_id and interleave with the main agent's;
- a background subagent ends at its task_notification, not at its tool result.

An agent is None for the main agent, else the id of the tool call that started
the subagent. `TurnParser.agent_name` names either.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

# "Task" in older Claude Code CLI versions.
SUBAGENT_TOOLS = ("Agent", "Task")


@dataclass(frozen=True)
class ModelCallStarted:
    agent: str | None


@dataclass(frozen=True)
class ModelCallEnded:
    agent: str | None
    blocks: tuple[Any, ...]
    usage: Mapping[str, Any]
    model: str | None
    stop_reason: str | None
    # time.monotonic() of the last send to the model.
    requested_at: float
    first_token_at: float | None
    # time.time_ns() when the call started.
    started_ns: int
    # Why the call failed (AssistantMessage.error, e.g. "rate_limit"), or None.
    error: str | None


@dataclass(frozen=True)
class ToolCallStarted:
    agent: str | None
    id: str
    name: str
    input: Any


@dataclass(frozen=True)
class ToolCallEnded:
    agent: str | None
    id: str
    content: Any
    is_error: bool
    # Which SDK message delivered the result: results from one message belong together.
    message_index: int


@dataclass(frozen=True)
class SubagentStarted:
    id: str
    prompt: str | None


@dataclass(frozen=True)
class SubagentEnded:
    id: str
    failed: bool


@dataclass(frozen=True)
class SubagentPrompt:
    agent: str
    text: str


@dataclass(frozen=True)
class Compaction:
    trigger: Any
    pre_tokens: Any


@dataclass(frozen=True)
class TurnResult:
    message: ResultMessage


@dataclass(frozen=True)
class TurnEnded:
    failed: bool
    # None when the turn ended without an exception, or ended at its result.
    error: BaseException | None


Event = (
    ModelCallStarted
    | ModelCallEnded
    | ToolCallStarted
    | ToolCallEnded
    | SubagentStarted
    | SubagentEnded
    | SubagentPrompt
    | Compaction
    | TurnResult
    | TurnEnded
)


@dataclass
class _Call:
    agent: str | None
    requested_at: float
    started_ns: int = field(default_factory=time.time_ns)
    seen: bool = False
    message_id: str | None = None
    blocks: list[Any] = field(default_factory=list)
    message_usage: dict[str, Any] = field(default_factory=dict)
    stream_usage: dict[str, Any] = field(default_factory=dict)
    model: str | None = None
    stop_reason: str | None = None
    stream_stop_reason: str | None = None
    first_token_at: float | None = None
    error: str | None = None


@dataclass
class _Subagent:
    prompt: str | None
    boundary: float = field(default_factory=time.monotonic)
    started: bool = False
    call: _Call | None = None


class TurnParser:
    """Turns one turn's SDK messages into events; never raises."""

    def __init__(self, main_agent: str) -> None:
        self.result: ResultMessage | None = None
        self._main_agent = main_agent
        self._names: dict[str, str] = {}
        self._tools: dict[str, ToolCallStarted] = {}
        self._subagents: dict[str, _Subagent] = {}
        # Subagents run as background tasks: they end at their task_notification.
        self._tasks: set[str] = set()
        self._call: _Call | None = None
        self._streaming = False
        # Messages that arrive while the main agent's call streams wait for its end.
        self._held: list[tuple[int, Any]] = []
        self._boundary = time.monotonic()
        self._index = 0
        self._broken = False

    def agent_name(self, agent: str | None) -> str:
        """The display name of `agent`: the main agent, or a subagent."""
        if agent is None:
            return self._main_agent
        return self._names.get(agent, "subagent")

    def feed(self, message: Any) -> list[Event]:
        """The events this message completes, in order."""
        self._index += 1
        return self._guard(self._feed, self._index, message)

    def finish(self, error: BaseException | None = None) -> list[Event]:
        """Close what is still open when the stream ends, then end the turn."""
        out = self._guard(self._end_stream)
        if isinstance(error, GeneratorExit) and self.result is not None:
            # The caller stopped reading at the result: that is how the turn ended.
            error = None
        failed = error is not None or (self.result is not None and self.result.is_error)
        out += self._guard(self._end_subagents, failed)
        out.append(TurnEnded(failed, error))
        return out

    def _guard(self, step: Any, *args: Any) -> list[Event]:
        if self._broken:
            return []
        out: list[Event] = []
        try:
            step(*args, out)
        except Exception:
            logging.warning("Stopped reading the turn's messages", exc_info=True)
            self._broken = True
        return out

    def _feed(self, index: int, message: Any, out: list[Event]) -> None:
        if self._streaming and not isinstance(message, StreamEvent):
            if not isinstance(message, AssistantMessage) or message.parent_tool_use_id:
                self._held.append((index, message))
                return
        if isinstance(message, SystemMessage):
            self._system(message, out)
        elif isinstance(message, StreamEvent):
            self._stream(message, out)
        elif isinstance(message, (AssistantMessage, UserMessage)):
            if message.parent_tool_use_id:
                self._subagent_message(index, message, out)
            elif isinstance(message, AssistantMessage):
                if self._call is None:
                    self._call = self._start_call(None, self._boundary, out)
                self._call = self._add_message(self._call, message)
            else:
                self._end_main_call(out)
                self._boundary = time.monotonic()
                self._tool_results(index, message, out)
        elif isinstance(message, ResultMessage):
            self._end_main_call(out)
            self.result = message
            out.append(TurnResult(message))

    def _system(self, message: SystemMessage, out: list[Event]) -> None:
        data = message.data if isinstance(message.data, dict) else {}
        tool_use_id = str(data.get("tool_use_id") or "")
        if message.subtype == "init":
            self._boundary = time.monotonic()
        elif message.subtype == "task_started":
            # Can arrive before its tool call; the subagent starts with it.
            self._tasks.add(tool_use_id)
            sub = self._subagents.get(tool_use_id)
            if sub is not None:
                sub = self._start_subagent(tool_use_id, sub, out)
        elif message.subtype == "task_notification":
            sub = self._subagents.pop(tool_use_id, None)
            if sub is not None:
                failed = data.get("status") != "completed"
                self._end_subagent(tool_use_id, sub, failed, out)
        elif message.subtype == "compact_boundary":
            metadata = data.get("compact_metadata") or {}
            out.append(Compaction(metadata.get("trigger"), metadata.get("pre_tokens")))

    def _stream(self, message: StreamEvent, out: list[Event]) -> None:
        # Only the main agent's calls are streamed.
        if message.parent_tool_use_id:
            return
        event = message.event
        kind = event.get("type")
        if kind == "message_start" and self._call is None:
            self._call = self._start_call(None, self._boundary, out)
            self._streaming = True
            usage = (event.get("message") or {}).get("usage") or {}
            self._call.stream_usage = dict(usage)
        elif kind == "message_stop":
            self._streaming = False
            self._end_main_call(out)
            held, self._held = self._held, []
            for index, held_message in held:
                self._feed(index, held_message, out)
        elif self._call is None:
            return
        elif kind == "content_block_delta" and self._call.first_token_at is None:
            self._call.first_token_at = time.monotonic()
        elif kind == "message_delta":
            usage = event.get("usage") or {}
            self._call.stream_usage.update(
                {k: v for k, v in usage.items() if v is not None}
            )
            self._call.stream_stop_reason = (event.get("delta") or {}).get(
                "stop_reason"
            )

    def _subagent_message(
        self, index: int, message: AssistantMessage | UserMessage, out: list[Event]
    ) -> None:
        agent = message.parent_tool_use_id or ""
        sub = self._subagents.get(agent)
        if sub is None:
            return
        sub = self._start_subagent(agent, sub, out)
        if isinstance(message, AssistantMessage):
            if sub.call is not None and sub.call.message_id not in (
                None,
                message.message_id,
            ):
                sub = self._end_subagent_call(sub, out)
            if sub.call is None:
                sub.call = self._start_call(agent, sub.boundary, out)
            sub.call = self._add_message(sub.call, message)
            return
        sub = self._end_subagent_call(sub, out)
        if isinstance(message.content, str):
            out.append(SubagentPrompt(agent, message.content))
        self._tool_results(index, message, out)

    def _start_call(
        self, agent: str | None, requested_at: float, out: list[Event]
    ) -> _Call:
        out.append(ModelCallStarted(agent))
        return _Call(agent, requested_at)

    def _add_message(self, call: _Call, message: AssistantMessage) -> _Call:
        call.seen = True
        call.message_id = message.message_id
        call.blocks.extend(message.content)
        call.message_usage = dict(message.usage or {})
        call.model = message.model
        call.stop_reason = message.stop_reason
        call.error = message.error or call.error
        for block in message.content:
            if isinstance(block, ToolUseBlock) and block.name in SUBAGENT_TOOLS:
                # Named as soon as the call is seen, not when it ends: its
                # subagent's span is claimed while the call is still open.
                self._names[block.id] = subagent_name(block.input)
        return call

    def _end_main_call(self, out: list[Event]) -> None:
        call, self._call = self._call, None
        if call is not None:
            self._end_call(call, out)

    def _end_subagent_call(self, sub: _Subagent, out: list[Event]) -> _Subagent:
        call, sub.call = sub.call, None
        if call is not None:
            self._end_call(call, out)
            sub.boundary = time.monotonic()
        return sub

    def _end_call(self, call: _Call, out: list[Event]) -> None:
        if not call.seen:
            return
        agent = call.agent
        out.append(
            ModelCallEnded(
                agent=agent,
                blocks=tuple(call.blocks),
                usage={**call.message_usage, **call.stream_usage},
                model=call.model,
                stop_reason=call.stream_stop_reason or call.stop_reason,
                requested_at=call.requested_at,
                first_token_at=call.first_token_at,
                started_ns=call.started_ns,
                error=call.error,
            )
        )
        for block in call.blocks:
            if not isinstance(block, ToolUseBlock):
                continue
            started = ToolCallStarted(agent, block.id, block.name, block.input)
            self._tools[block.id] = started
            out.append(started)
            if block.name in SUBAGENT_TOOLS:
                prompt = (
                    block.input.get("prompt") if isinstance(block.input, dict) else None
                )
                sub = _Subagent(prompt)
                self._subagents[block.id] = sub
                if block.id in self._tasks:  # its task_started came first
                    self._start_subagent(block.id, sub, out)

    def _tool_results(self, index: int, message: UserMessage, out: list[Event]) -> None:
        if not isinstance(message.content, list):
            return
        for block in message.content:
            if not isinstance(block, ToolResultBlock):
                continue
            started = self._tools.pop(block.tool_use_id, None)
            if started is None:
                continue
            sub = self._subagents.get(block.tool_use_id)
            # A subagent that hasn't sent anything yet runs in the background:
            # its result only says it started.
            if (
                sub is not None
                and block.tool_use_id not in self._tasks
                and (sub.started or block.is_error)
            ):
                del self._subagents[block.tool_use_id]
                self._end_subagent(block.tool_use_id, sub, bool(block.is_error), out)
            out.append(
                ToolCallEnded(
                    agent=started.agent,
                    id=started.id,
                    content=block.content,
                    is_error=bool(block.is_error),
                    message_index=index,
                )
            )

    def _start_subagent(
        self, tool_use_id: str, sub: _Subagent, out: list[Event]
    ) -> _Subagent:
        if not sub.started:
            sub.started = True
            out.append(SubagentStarted(tool_use_id, sub.prompt))
        return sub

    def _end_subagent(
        self, tool_use_id: str, sub: _Subagent, failed: bool, out: list[Event]
    ) -> None:
        sub = self._start_subagent(tool_use_id, sub, out)
        self._end_subagent_call(sub, out)
        out.append(SubagentEnded(tool_use_id, failed))

    def _end_stream(self, out: list[Event]) -> None:
        if self._streaming or self._held:
            self._streaming = False
            held, self._held = self._held, []
            self._end_main_call(out)
            for index, message in held:
                self._feed(index, message, out)
        self._end_main_call(out)

    def _end_subagents(self, failed: bool, out: list[Event]) -> None:
        # Background subagents whose task never reported back end with the turn.
        for tool_use_id, sub in list(self._subagents.items()):
            self._end_subagent(tool_use_id, sub, failed, out)
        self._subagents.clear()


def subagent_name(tool_input: Any) -> str:
    """The name of the subagent an Agent tool call with `tool_input` starts."""
    if isinstance(tool_input, dict) and tool_input.get("subagent_type"):
        return str(tool_input["subagent_type"])
    return "subagent"


def system_prompt_text(system_prompt: Any) -> str:
    """The text of a `ClaudeAgentOptions.system_prompt`, or "" when it has none.

    A preset's own prompt is the CLI's, so only what it appends counts; a system
    prompt file isn't read.
    """
    if isinstance(system_prompt, str):
        return system_prompt
    if isinstance(system_prompt, dict):
        if system_prompt.get("type") == "preset":
            return system_prompt.get("append", "")
        if system_prompt.get("type") == "custom":
            return system_prompt.get("prompt", "")
    return ""
