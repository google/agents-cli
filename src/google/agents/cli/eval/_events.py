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

"""Event/case shaping shared by every ``eval generate`` transport."""

from __future__ import annotations

from google.genai import types as genai_types

from google.agents.cli._agent_platform_types import types


def strip_thought_signatures(events: list[types.evals.AgentEvent]) -> None:
    """Remove thought_signature from every event's content parts."""
    for event in events:
        if event.content and event.content.parts:
            for part in event.content.parts:
                part.thought_signature = None


def rewrite_model_author_events(case: types.EvalCase, root_agent_name: str) -> None:
    """Rewrite events with author=='model' to use root_agent_name."""
    if not case.agent_data:
        return
    for turn in case.agent_data.turns or []:
        for event in turn.events or []:
            if event.author == "model":
                event.author = root_agent_name


def final_response_content_from_events(
    events: list[types.evals.AgentEvent],
) -> genai_types.Content | None:
    """Extract the final agent text response from a list of events.

    Walks events in reverse looking for the most recent event whose first
    text-bearing part has a non-empty text. Returns a Content
    ({"role": "model", "parts": [{"text": ...}]}) suitable for
    EvalCase.responses[i].response, or None if no text was found.
    """
    for event in reversed(events):
        if not event.content or not event.content.parts:
            continue
        texts = [p.text for p in event.content.parts if p.text]
        if texts:
            return genai_types.Content(
                role=event.content.role or "model",
                parts=[genai_types.Part(text="".join(texts))],
            )
    return None


def raise_if_error(event: dict) -> None:
    """Raise when *event* signals a failure.

    A bare top-level ``{"error": ...}`` is the last frame ADK emits before
    closing the stream on a failed run.
    """
    message = event.get("errorMessage") or event.get("error")
    code = event.get("errorCode")
    if message or code:
        detail = message or "unknown error"
        raise ValueError(
            f"Agent returned an error: {detail}" + (f" ({code})" if code else "")
        )


def parse_content_event(event: dict) -> types.evals.AgentEvent | None:
    """Parse an agent event into an ``AgentEvent``, or raise.

    Used by both transports. Raises when the event signals a failure, is
    missing ``author``, or is otherwise malformed. Returns None for events
    carrying neither content nor a state delta.
    """
    raise_if_error(event)

    if not event.get("author"):
        raise ValueError(f"Malformed agent event: missing author. Keys: {sorted(event)}")

    content = event.get("content")
    state_delta = event.get("actions", {}).get("stateDelta")
    if not content and not state_delta:
        return None

    return types.evals.AgentEvent(
        author=event.get("author"),
        content=content or None,
        event_time=event.get("timestamp") or None,
        state_delta=state_delta or None,
    )


def to_adk_event_payload(event: types.evals.AgentEvent) -> dict:
    """Serialize a seeded prior event into ADK's ``Event`` wire shape.

    ADK reads a state delta from ``actions.state_delta`` and ignores unknown
    top-level fields, so an unnested delta leaves the case graded against an
    agent that never saw the state. JSON mode because ``event_time`` is a
    ``datetime`` and ``requests`` cannot encode it.
    """
    payload = event.model_dump(exclude_none=True, by_alias=True, mode="json")
    state_delta = payload.pop("stateDelta", None)
    if state_delta:
        payload["actions"] = {"stateDelta": state_delta}
    return payload
