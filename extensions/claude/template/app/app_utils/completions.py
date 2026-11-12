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

"""Prompt-response logging through OpenTelemetry's completion hook.

Enabled by OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK. A call's logged inputs
cover only the current turn, not earlier ones.
"""

from __future__ import annotations

import functools
import logging
import os
from collections.abc import Callable
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)
from opentelemetry import trace
from opentelemetry._logs import LogRecord
from opentelemetry.util.genai.completion_hook import (
    CompletionHook,
    load_completion_hook,
)
from opentelemetry.util.genai.types import (
    InputMessage,
    MessagePart,
    OutputMessage,
    TextPart,
    ToolCallRequestPart,
    ToolCallResponsePart,
)

from app.app_utils import telemetry
from app.app_utils.telemetry.subagent_spans import SubagentSpans
from app.app_utils.turn_events import (
    Event,
    ModelCallEnded,
    SubagentPrompt,
    ToolCallEnded,
    system_prompt_text,
)

EVENT_NAME = "gen_ai.client.inference.operation.details"


def start_turn(
    prompt: str,
    options: ClaudeAgentOptions,
    session_id: str,
    agent_name: Callable[[str | None], str],
    subagent_spans: SubagentSpans,
) -> PromptLog | None:
    """A recorder for one turn's model calls, or None when logging is off."""
    if not os.environ.get("OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK"):
        return None
    return PromptLog(
        _hook(),
        trace.get_tracer(__name__),
        prompt,
        options,
        session_id,
        agent_name,
        subagent_spans,
    )


@functools.cache
def _hook() -> CompletionHook:
    # Built once: the upload hook test-writes the bucket and starts threads.
    return load_completion_hook()


class PromptLog:
    """Logs each model call of one turn, with the inputs that led to it."""

    def __init__(
        self,
        hook: CompletionHook,
        tracer: trace.Tracer,
        prompt: str,
        options: ClaudeAgentOptions,
        session_id: str,
        agent_name: Callable[[str | None], str],
        subagent_spans: SubagentSpans,
    ) -> None:
        self._hook = hook
        self._tracer = tracer
        self._system = _system_instruction(options.system_prompt)
        self._attributes = {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "gcp.vertex_ai",
            # OpenTelemetry's conversation is the Claude session.
            "gen_ai.conversation.id": session_id,
        }
        # Per agent: the messages of earlier calls, and those since the last call.
        self._history: dict[str | None, list[InputMessage]] = {}
        self._new: dict[str | None, list[InputMessage]] = {
            None: [InputMessage(role="user", parts=[TextPart(content=prompt)])]
        }
        # Per agent: the SDK message the last tool result in _new came in.
        self._results_from: dict[str | None, int] = {}
        self._agent_name = agent_name
        self._subagent_spans = subagent_spans

    def handle(self, event: Event) -> None:
        try:
            if isinstance(event, ModelCallEnded):
                self._log(event)
            elif isinstance(event, ToolCallEnded):
                self._add_result(event)
            elif isinstance(event, SubagentPrompt):
                self._new.setdefault(event.agent, []).append(
                    InputMessage(role="user", parts=[TextPart(content=event.text)])
                )
        except Exception:  # logging must never fail a turn
            logging.warning("Couldn't log a model call", exc_info=True)

    def _add_result(self, event: ToolCallEnded) -> None:
        part = ToolCallResponsePart(
            response=_response(event.content, event.is_error), id=event.id
        )
        new = self._new.setdefault(event.agent, [])
        if new and self._results_from.get(event.agent) == event.message_index:
            new[-1].parts.append(part)
        else:
            new.append(InputMessage(role="tool", parts=[part]))
        self._results_from[event.agent] = event.message_index

    def _log(self, event: ModelCallEnded) -> None:
        agent = event.agent
        history = self._history.setdefault(agent, [])
        history.extend(self._new.pop(agent, []))
        self._results_from.pop(agent, None)
        inputs = list(history)
        parts = _parts(event.blocks)
        history.append(InputMessage(role="assistant", parts=parts))

        subagent = (
            self._subagent_spans.subagent_span(agent) if agent is not None else None
        )
        span = self._tracer.start_span(
            f"chat {event.model}",
            context=trace.set_span_in_context(subagent)
            if subagent is not None
            else None,
            kind=trace.SpanKind.CLIENT,
            start_time=event.started_ns,
        )
        attributes: dict[str, Any] = {
            **self._attributes,
            "gen_ai.request.model": event.model,
            "gen_ai.response.model": event.model,
            "gen_ai.agent.name": self._agent_name(agent),
        }
        if event.stop_reason:
            attributes["gen_ai.response.finish_reasons"] = [event.stop_reason]
        for key in ("input_tokens", "output_tokens"):
            if event.usage.get(key) is not None:
                attributes[f"gen_ai.usage.{key}"] = event.usage[key]
        if event.error:
            attributes["error.type"] = event.error
            span.set_status(trace.Status(trace.StatusCode.ERROR, event.error))
        try:
            span.set_attributes(attributes)
            record = LogRecord(
                attributes={"event.name": EVENT_NAME, **attributes},
                context=trace.set_span_in_context(span),
            )
            self._hook.on_completion(
                inputs=inputs,
                outputs=[OutputMessage(role="assistant", parts=parts)],
                system_instruction=self._system if agent is None else [],
                span=span,
                log_record=record,
            )
            telemetry.emit_event(record)
        finally:
            span.end()


def _parts(blocks: tuple[Any, ...]) -> list[MessagePart]:
    parts: list[MessagePart] = []
    for block in blocks:
        if isinstance(block, TextBlock):
            parts.append(TextPart(content=block.text))
        elif isinstance(block, ThinkingBlock):
            # As text: the Console's message viewer rejects "reasoning" parts.
            parts.append(TextPart(content=block.thinking))
        elif isinstance(block, ToolUseBlock):
            parts.append(
                ToolCallRequestPart(arguments=block.input, name=block.name, id=block.id)
            )
    return parts


def _response(content: Any, is_error: bool) -> dict[str, Any]:
    """A tool result as an object: the Console's viewer rejects other shapes."""
    if isinstance(content, list) and all(
        isinstance(item, dict) and item.get("type") == "text" for item in content
    ):
        content = "".join(item.get("text", "") for item in content)
    return {"error" if is_error else "result": content}


def _system_instruction(system_prompt: Any) -> list[MessagePart]:
    """The system prompt the model got; with a preset, only what it appends."""
    text = system_prompt_text(system_prompt)
    return [TextPart(content=text)] if text else []
