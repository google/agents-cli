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

"""Turn one turn's events (see turn_events.py) into `agent_events` rows, as ADK writes them."""

from __future__ import annotations

import datetime
import json
import logging
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import ResultMessage, TextBlock, ThinkingBlock, ToolUseBlock
from opentelemetry import trace

from app.app_utils.bq_analytics.redaction import sanitize
from app.app_utils.bq_analytics.uploader import Writer
from app.app_utils.replies import result_error
from app.app_utils.turn_events import (
    SUBAGENT_TOOLS,
    Compaction,
    Event,
    ModelCallEnded,
    ModelCallStarted,
    SubagentEnded,
    SubagentStarted,
    ToolCallEnded,
    ToolCallStarted,
    TurnEnded,
    TurnResult,
    system_prompt_text,
)

MAX_CONTENT_LENGTH = 500 * 1024


class AgentEventsRecorder:
    """Maps one turn's events (see turn_events.py) to agent_events rows."""

    def __init__(
        self,
        out: Writer,
        *,
        prompt: str,
        agent_name: Callable[[str | None], str],
        session_id: str,
        user_id: str,
        model: str | None,
        # The system prompt the model gets, recalled memories included.
        system_prompt: Any,
        # The agent's own system prompt.
        instruction: Any,
        remote_agents: dict[str, str] | None = None,
        local_servers: set[str] | None = None,
    ) -> None:
        self._out = out
        self._agent_name = agent_name
        self.agent = agent_name(None)
        self.session_id = session_id
        self.user_id = user_id
        self.model = model
        self.system_prompt = system_prompt_text(system_prompt)
        self.remote_agents = remote_agents or {}
        self.local_servers = local_servers or set()
        self.invocation_id = f"e-{uuid.uuid4()}"
        self.trace_id = self.invocation_id
        self.invocation_span = _span_id()
        self.agent_span = _span_id()
        self.started = time.monotonic()
        self.finished = False
        self.inputs: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        self.llm_spans: dict[str | None, str] = {}
        self.tools: dict[str, _ToolCall] = {}
        self.subagents: dict[str, _Subagent] = {}
        self.result: ResultMessage | None = None
        # The turn's OpenTelemetry span is current only once the turn runs.
        self._held: list[dict[str, Any]] | None = []
        text, truncated = _clip(prompt)
        self._emit(
            "USER_MESSAGE_RECEIVED",
            self.invocation_span,
            None,
            content={"text_summary": text},
            content_parts=_text_parts(text),
            is_truncated=truncated,
        )
        self._emit("INVOCATION_STARTING", self.invocation_span, None)
        self._emit(
            "AGENT_STARTING",
            self.agent_span,
            self.invocation_span,
            # Where v_agent_starting reads the instruction.
            content={"text_summary": text}
            if (text := system_prompt_text(instruction))
            else None,
            attributes={"model": model} if model else None,
        )

    def handle(self, event: Event) -> None:
        """Record one event of the turn; never raises into the turn."""
        if self.finished:
            return
        try:
            self._handle(event)
        except Exception:
            logging.warning(
                "BigQuery Agent Analytics couldn't close a turn"
                if isinstance(event, TurnEnded)
                else "BigQuery Agent Analytics stopped recording a turn",
                exc_info=True,
            )
            self.finished = True

    def _handle(self, event: Event) -> None:
        if self._held is not None:
            self._release_held()
        if isinstance(event, ModelCallStarted):
            self._llm_request(event)
        elif isinstance(event, ModelCallEnded):
            self._llm_response(event)
        elif isinstance(event, ToolCallStarted):
            self._tool_started(event)
        elif isinstance(event, ToolCallEnded):
            self._tool_ended(event)
        elif isinstance(event, SubagentStarted):
            self._subagent_started(event)
        elif isinstance(event, SubagentEnded):
            self._subagent_ended(event)
        elif isinstance(event, Compaction):
            self._emit(
                "EVENT_COMPACTION",
                _span_id(),
                self.agent_span,
                content={"end_timestamp": time.time()},
                attributes={"trigger": event.trigger, "pre_tokens": event.pre_tokens},
            )
        elif isinstance(event, TurnResult):
            self.result = event.message
        elif isinstance(event, TurnEnded):
            self._turn_ended(event)

    def _release_held(self) -> None:
        """Send the opening rows, now under the turn's span when there is one."""
        held, self._held = self._held or [], None
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            # Root rows keep a NULL parent: the span's parent isn't a row.
            ours = self.invocation_span
            self.invocation_span = format(context.span_id, "016x")
            self.trace_id = format(context.trace_id, "032x")
            for row in held:
                row["trace_id"] = self.trace_id
                if row["span_id"] == ours:
                    row["span_id"] = self.invocation_span
                if row["parent_span_id"] == ours:
                    row["parent_span_id"] = self.invocation_span
        for row in held:
            self._out.put(row)

    def _llm_request(self, event: ModelCallStarted) -> None:
        span = self.llm_spans[event.agent] = _span_id()
        # Claude Code builds the real request; this row has only the new inputs.
        if event.agent is None:
            content: dict[str, Any] = {"prompt": self.inputs}
            if self.system_prompt:
                content["system_prompt"] = self.system_prompt
            self._emit(
                "LLM_REQUEST",
                span,
                self.agent_span,
                content=content,
                attributes={"model": self.model, "synthesized": True},
            )
            self.inputs = []
            return
        sub = self.subagents[event.agent]
        self._emit(
            "LLM_REQUEST",
            span,
            sub.span,
            agent=self._agent_name(event.agent),
            content={"prompt": sub.inputs},
            attributes={"synthesized": True},
        )
        sub.inputs = []

    def _llm_response(self, event: ModelCallEnded) -> None:
        parent, agent = self._parent(event.agent)
        usage = event.usage
        prompt = _prompt_tokens(usage)
        completion = int(usage.get("output_tokens") or 0)
        response, truncated = _clip(_response_text(event.blocks))
        thinking = "".join(
            b.thinking for b in event.blocks if isinstance(b, ThinkingBlock)
        )
        latency = {"total_ms": int((time.monotonic() - event.requested_at) * 1000)}
        if event.first_token_at is not None:
            latency["time_to_first_token_ms"] = int(
                (event.first_token_at - event.requested_at) * 1000
            )
        if event.error:
            # The CLI puts the API's error text in the message's text blocks.
            text = _response_text(event.blocks)
            self._emit(
                "LLM_ERROR",
                self.llm_spans.pop(event.agent),
                parent,
                agent=agent,
                status="ERROR",
                error_message=f"{event.error}: {text}" if text else event.error,
                latency_ms=latency,
            )
            return
        cache_read = usage.get("cache_read_input_tokens")
        self._emit(
            "LLM_RESPONSE",
            self.llm_spans.pop(event.agent),
            parent,
            agent=agent,
            content={
                "response": response,
                "usage": {
                    "prompt": prompt,
                    "completion": completion,
                    "total": prompt + completion,
                },
            },
            attributes={
                "model_version": event.model,
                "finish_reason": event.stop_reason,
                "cache_type": "implicit" if cache_read else "none",
                "usage_metadata": {
                    "prompt_token_count": prompt,
                    "candidates_token_count": completion,
                    "total_token_count": prompt + completion,
                    "cached_content_token_count": cache_read,
                    "cache_creation_input_tokens": usage.get(
                        "cache_creation_input_tokens"
                    ),
                    "input_tokens": usage.get("input_tokens"),
                },
                **({"thinking": _clip(thinking)[0]} if thinking else {}),
            },
            latency_ms=latency,
            is_truncated=truncated,
        )

    def _tool_started(self, event: ToolCallStarted) -> None:
        parent, agent = self._parent(event.agent)
        span = _span_id()
        self.tools[event.id] = _ToolCall(
            span=span,
            started=time.monotonic(),
            tool=event,
            agent=agent or self.agent,
        )
        self._emit(
            "TOOL_STARTING",
            span,
            parent,
            agent=agent,
            content={
                "tool": event.name,
                "args": event.input,
                "tool_origin": self._tool_origin(event.name),
            },
        )

    def _tool_ended(self, event: ToolCallEnded) -> None:
        parent, _ = self._parent(event.agent)
        call = self.tools[event.id]
        if event.agent is None:
            self.inputs.append(
                {"role": "user", "content": f"Function response: {call.tool.name}"}
            )
        else:
            self.subagents[event.agent].inputs.append(
                {"role": "user", "content": "Function response"}
            )
        origin = self._tool_origin(call.tool.name)
        result = _result_content(event.content)
        latency = {"total_ms": int((time.monotonic() - call.started) * 1000)}
        if event.is_error:
            self._emit(
                "TOOL_ERROR",
                call.span,
                parent,
                agent=call.agent,
                content={
                    "tool": call.tool.name,
                    "args": call.tool.input,
                    "tool_origin": origin,
                },
                status="ERROR",
                error_message=str(result)[:1000],
                latency_ms=latency,
            )
            return
        self._emit(
            "TOOL_COMPLETED",
            call.span,
            parent,
            agent=call.agent,
            content={"tool": call.tool.name, "result": result, "tool_origin": origin},
            latency_ms=latency,
        )
        if origin == "A2A":
            self._a2a_interaction(call, parent, result, latency)

    def _a2a_interaction(
        self, call: _ToolCall, parent: str, result: Any, latency: dict[str, int]
    ) -> None:
        server = call.tool.name.split("__")[1]
        self._emit(
            "A2A_INTERACTION",
            call.span,
            parent,
            agent=call.agent,
            content=result,
            attributes={
                "remote_agent": server,
                "a2a_metadata": {
                    "a2a:url": self.remote_agents[server],
                    "a2a:request": call.tool.input,
                    "a2a:response": result,
                },
            },
            latency_ms=latency,
        )

    def _subagent_started(self, event: SubagentStarted) -> None:
        call = self.tools[event.id]
        sub = self.subagents[event.id] = _Subagent(
            span=_span_id(),
            tool_span=call.span,
            started=call.started,
            inputs=[{"role": "user", "content": event.prompt}],
        )
        self._emit(
            "AGENT_STARTING",
            sub.span,
            sub.tool_span,
            agent=self._agent_name(event.id),
            content={"text_summary": event.prompt},
        )

    def _subagent_ended(self, event: SubagentEnded) -> None:
        sub = self.subagents[event.id]
        latency = {"total_ms": int((time.monotonic() - sub.started) * 1000)}
        self._emit(
            "AGENT_ERROR" if event.failed else "AGENT_COMPLETED",
            sub.span,
            sub.tool_span,
            agent=self._agent_name(event.id),
            status="ERROR" if event.failed else "OK",
            latency_ms=latency,
        )

    def _turn_ended(self, event: TurnEnded) -> None:
        """Close the turn: AGENT_RESPONSE and the COMPLETED, or ERROR, pair."""
        self.finished = True
        result = self.result
        latency = {"total_ms": int((time.monotonic() - self.started) * 1000)}
        stats = _turn_stats(result)
        if event.failed:
            self._close_failed(_error_detail(event.error, result), latency, stats)
            return
        if result is not None and result.result:
            text, truncated = _clip(result.result)
            self._emit(
                "AGENT_RESPONSE",
                self.agent_span,
                self.invocation_span,
                content={"response": text},
                is_truncated=truncated,
            )
        self._emit(
            "AGENT_COMPLETED",
            self.agent_span,
            self.invocation_span,
            latency_ms=latency,
        )
        self._emit(
            "INVOCATION_COMPLETED",
            self.invocation_span,
            None,
            latency_ms=latency,
            attributes=stats,
        )

    def _close_failed(
        self, detail: str, latency: dict[str, int], stats: dict[str, Any]
    ) -> None:
        self._emit(
            "AGENT_ERROR",
            self.agent_span,
            self.invocation_span,
            status="ERROR",
            error_message=detail,
            latency_ms=latency,
        )
        self._emit(
            "INVOCATION_ERROR",
            self.invocation_span,
            None,
            status="ERROR",
            error_message=detail,
            latency_ms=latency,
            attributes=stats,
        )

    def _parent(self, agent: str | None) -> tuple[str, str | None]:
        """The span and row agent name for events of `agent`."""
        if agent is None:
            return self.agent_span, None
        return self.subagents[agent].span, self._agent_name(agent)

    def _tool_origin(self, name: str) -> str:
        if name in SUBAGENT_TOOLS:
            return "SUB_AGENT"
        if name.startswith("mcp__"):
            server = name.split("__")[1]
            if server in self.remote_agents:
                return "A2A"
            return "LOCAL" if server in self.local_servers else "MCP"
        return "LOCAL"

    def _emit(
        self,
        event_type: str,
        span_id: str,
        parent_span_id: str | None,
        *,
        agent: str | None = None,
        content: Any = None,
        content_parts: list[dict[str, Any]] | None = None,
        attributes: dict[str, Any] | None = None,
        latency_ms: dict[str, int] | None = None,
        status: str = "OK",
        error_message: str | None = None,
        is_truncated: bool = False,
    ) -> None:
        # Credentials redacted and every string capped, at any depth: tool args
        # and results, A2A payloads and prompts.
        content, content_cut = sanitize(content, MAX_CONTENT_LENGTH)
        attributes, attributes_cut = sanitize(attributes, MAX_CONTENT_LENGTH)
        content_parts, parts_cut = sanitize(content_parts, MAX_CONTENT_LENGTH)
        error_message, _ = sanitize(error_message, MAX_CONTENT_LENGTH)
        row = {
            "timestamp": _now(),
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "agent": agent or self.agent,
            "session_id": self.session_id,
            "invocation_id": self.invocation_id,
            "user_id": self.user_id,
            "trace_id": self.trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "content": json.dumps(content, default=str)
            if content is not None
            else None,
            "content_parts": content_parts or [],
            "attributes": json.dumps(
                {
                    "root_agent_name": self.agent,
                    "session_metadata": {
                        "session_id": self.session_id,
                        "user_id": self.user_id,
                    },
                    "framework": "claude_agent_sdk",
                    **(attributes or {}),
                },
                default=str,
            ),
            "latency_ms": json.dumps(latency_ms) if latency_ms else None,
            "status": status,
            "error_message": error_message,
            "is_truncated": is_truncated or content_cut or attributes_cut or parts_cut,
        }
        if self._held is not None:
            self._held.append(row)
        else:
            self._out.put(row)


@dataclass(frozen=True)
class _ToolCall:
    span: str
    started: float
    tool: ToolCallStarted
    agent: str


@dataclass
class _Subagent:
    span: str
    tool_span: str
    started: float
    inputs: list[dict[str, Any]]


def _span_id() -> str:
    return uuid.uuid4().hex[:16]


def _clip(text: str) -> tuple[str, bool]:
    if len(text) > MAX_CONTENT_LENGTH:
        return text[:MAX_CONTENT_LENGTH] + "...[TRUNCATED]", True
    return text, False


def _text_parts(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    return [
        {
            "mime_type": "text/plain",
            "text": text,
            "part_index": 0,
            "storage_mode": "INLINE",
        }
    ]


def _prompt_tokens(usage: Mapping[str, Any]) -> int:
    """All input tokens, cached or not."""
    return sum(
        int(usage.get(key) or 0)
        for key in (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    )


def _response_text(blocks: Sequence[Any]) -> str:
    """The model's text, or the tools it called when it wrote none."""
    texts = [b.text for b in blocks if isinstance(b, TextBlock)]
    if texts:
        return "\n".join(texts)
    calls = [b.name for b in blocks if isinstance(b, ToolUseBlock)]
    return f"call: {', '.join(calls)}" if calls else ""


def _result_content(content: Any) -> Any:
    if isinstance(content, str) or content is None:
        return content
    return [c.get("text") for c in content if isinstance(c, dict)]


def _turn_stats(result: ResultMessage | None) -> dict[str, Any]:
    if result is None:
        return {}
    return {
        # Covers the whole session, including resumed turns: don't sum it.
        "session_cost_usd": result.total_cost_usd,
        "num_turns": result.num_turns,
        "result_subtype": result.subtype,
        # Tool calls outside allowed_tools; each is also a TOOL_ERROR.
        "permission_denials": result.permission_denials or None,
    }


def _error_detail(error: BaseException | None, result: ResultMessage | None) -> str:
    if error is not None:
        # A cancelled turn (a client that disconnects) has no message.
        detail = str(error) or type(error).__name__
    elif result is not None:
        detail = result_error(result)
    else:
        detail = "unknown error"
    return detail[:1000]


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()
