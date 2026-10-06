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

"""Live (bidi) transport for ``eval generate``. Plays each eval case over
one persistent ADK /run_live WebSocket.
"""

from __future__ import annotations

from google.genai import types as genai_types
from websockets.exceptions import ConnectionClosed

from google.agents.cli._adk_client import (
    DEFAULT_WS_PATH,
    create_session,
    finished_transcript,
    group_turns,
    is_transcription_event,
    stream_live_events,
)
from google.agents.cli._agent_platform_types import types
from google.agents.cli._remote import resolve_agent_endpoints
from google.agents.cli.eval._events import (
    final_response_content_from_events,
    parse_content_event,
    raise_if_error,
    rewrite_model_author_events,
    strip_thought_signatures,
    to_adk_event_payload,
)

# Inline media a Live stream carries that bloats the trace but grades nothing --
# the gradable text comes from the transcription frames instead.
_UNGRADABLE_MIME_PREFIXES = ("audio/", "video/")

# Content-less control frames the Live stream interleaves with content; skipped
# rather than treated as malformed. Live-only: /run_sse never emits these, and
# skipping a content-less SSE frame would undo the failure contract from #762.
_LIVE_CONTROL_KEYS = (
    "turnComplete",
    "interrupted",
    "usageMetadata",
    "voiceActivity",
    "liveSessionResumptionUpdate",
    "interactionStatus",
    # Not emitted by ADK yet; listed so it is skipped, not raised, when it is.
    "generationComplete",
)


def strip_media_parts(events: list[types.evals.AgentEvent]) -> None:
    """Drop inline audio/video parts from Live events, keeping any other parts."""
    for event in events:
        if not (event.content and event.content.parts):
            continue
        event.content.parts = [
            part
            for part in event.content.parts
            if not (
                part.inline_data is not None
                and (part.inline_data.mime_type or "").startswith(
                    _UNGRADABLE_MIME_PREFIXES
                )
            )
        ]


def extract_user_turns(case: types.EvalCase) -> list[genai_types.Content]:
    """Return the ordered list of user-authored turn contents for a case.

    Raises ValueError if the case has neither a prompt nor any user turn.
    """
    if case.prompt:
        return [case.prompt]

    turns = (case.agent_data.turns if case.agent_data else None) or []
    user_contents: list[genai_types.Content] = []
    for turn in turns:
        for event in turn.events or []:
            if event.author == "user" and event.content is not None:
                user_contents.append(event.content)
    if not user_contents:
        raise ValueError(
            "Case has no user message to send (missing prompt and no user "
            "event in agent_data.turns)."
        )
    return user_contents


def has_authored_agent_turns(case: types.EvalCase) -> bool:
    """True if any turn carries a non-user (agent/tool) authored event."""
    turns = (case.agent_data.turns if case.agent_data else None) or []
    for turn in turns:
        for event in turn.events or []:
            if event.author and event.author != "user":
                return True
    return False


def input_state_events(case: types.EvalCase) -> list[types.evals.AgentEvent]:
    """Content-less copies of the user-authored state deltas to seed.

    A delta the agent authored is a recording of what its callbacks wrote, and
    live re-runs every turn, so the agent writes it again -- seeding it would
    hide an agent that stopped writing state. Content is dropped because the
    turns themselves are replayed over the socket.
    """
    turns = (case.agent_data.turns if case.agent_data else None) or []
    return [
        event.model_copy(update={"content": None})
        for turn in turns
        for event in turn.events or []
        if event.state_delta and not (event.author and event.author != "user")
    ]


def _transcription_to_event(event: dict) -> types.evals.AgentEvent | None:
    """Convert a *finished* Live transcription frame into a text AgentEvent, else None."""
    transcript = finished_transcript(event)
    if transcript is None:
        return None
    return types.evals.AgentEvent(
        author=transcript.author,
        content=genai_types.Content(
            role=transcript.role, parts=[genai_types.Part(text=transcript.text)]
        ),
    )


def normalize_live_events(raw_events: list[dict]) -> list[types.evals.AgentEvent]:
    """Turn a stream of raw ``/run_live`` frames into gradable ``AgentEvent``s.

    Live-only. ``/run_sse`` calls :func:`parse_content_event` directly, which
    has no notion of the transcription frames handled here.
    """
    events: list[types.evals.AgentEvent] = []
    for event in raw_events:
        raise_if_error(event)

        if event.get("content"):
            content_event = parse_content_event(event)
            if content_event is not None:
                events.append(content_event)
            continue

        transcript_event = _transcription_to_event(event)
        if transcript_event is not None:
            events.append(transcript_event)
            continue

        # Content-less control / partial-transcription frame: skip. Presence,
        # not truthiness -- `{"interrupted": false}` is still a control frame.
        if any(key in event for key in _LIVE_CONTROL_KEYS) or is_transcription_event(
            event
        ):
            continue

        # A workflow node's routing decision, a state or artifact delta.
        if event.get("author"):
            continue

        raise ValueError(
            f"Malformed agent event: missing author and content. Keys: {sorted(event)}"
        )

    return events


def _user_event_for_turn(sent: genai_types.Content) -> types.evals.AgentEvent | None:
    """Build the user event for one Live turn from the turn we authored.

    That is ground truth for what was asked. Turns are sent as text, so
    stripping media normally leaves it intact.
    """
    authored = types.evals.AgentEvent(author="user", content=sent)
    strip_media_parts([authored])
    return authored if authored.content and authored.content.parts else None


def _clean_live_events(raw_events: list[dict]) -> list[types.evals.AgentEvent]:
    """Normalize + strip one Live turn's raw frames into gradable agent events."""
    events = normalize_live_events(raw_events)
    strip_thought_signatures(events)
    strip_media_parts(events)
    return [e for e in events if e.content and e.content.parts]


def _build_turn(
    turn_idx: int, sent: genai_types.Content, raw_events: list[dict]
) -> types.evals.ConversationTurn:
    """Assemble one conversation turn from the frames the agent sent back."""
    # Some agents echo the user turn back as an input transcription. Drop those
    # so the turn doesn't record the user twice; the authored turn is used.
    agent_events = [e for e in _clean_live_events(raw_events) if e.author != "user"]
    user_event = _user_event_for_turn(sent)
    return types.evals.ConversationTurn(
        turn_index=turn_idx,
        turn_id=f"turn_{turn_idx}",
        events=[e for e in (user_event, *agent_events) if e is not None],
    )


def run_case_live(
    *,
    case: types.EvalCase,
    base_url: str,
    app_name: str,
    headers: dict,
    root_agent_name: str,
    agents_map: dict[str, types.evals.AgentConfig],
    user_id: str,
) -> tuple[types.EvalCase, str | None]:
    """Play every user turn of a case over one persistent ``/run_live`` socket.

    Same contract as :func:`cmd_generate.run_case`: (merged_case, None) on
    success, (original_case, error_msg) on any failure.

    Turns are sent as text and the agent replies in audio, so ADK's output
    transcription is what gets graded. History accrues in the live session
    rather than being seeded over HTTP. A conversation the *server* cuts short
    fails the case, whether the socket errored or closed cleanly with turns
    still unsent; no partial trajectory is kept. A turn that merely stalls
    fails it too: ``stream_live_events`` gives up after its per-turn timeout
    and yields an empty turn, which fails the case on that turn.
    """
    try:
        user_turns = extract_user_turns(case)
    except Exception as exc:
        return case, str(exc)

    endpoints = resolve_agent_endpoints(base_url)

    # Conversation history accrues over the live socket; only seeded input
    # state has to be in place before the first turn.
    seed_events = [to_adk_event_payload(e) for e in input_state_events(case)]
    try:
        session_id = create_session(
            endpoints.http_base,
            app_name,
            user_id,
            headers=headers,
            prior_events=seed_events or None,
        )
    except Exception as exc:
        return case, f"Session create failed: {type(exc).__name__}: {exc}"

    source = [
        c.model_dump(mode="json", exclude_none=True, by_alias=True) for c in user_turns
    ]

    built_turns: list[types.evals.ConversationTurn] = []
    try:
        for turn_idx, events in enumerate(
            group_turns(
                stream_live_events(
                    endpoints.ws_base,
                    app_name,
                    session_id,
                    user_turns=source,
                    headers=headers,
                    user_id=user_id,
                )
            )
        ):
            turn = _build_turn(turn_idx, user_turns[turn_idx], events)
            if not any(e.author != "user" for e in turn.events or []):
                # Grading a turn the agent never answered scores a trajectory
                # that never happened, so fail rather than keep it.
                return case, (
                    f"Turn {turn_idx} returned no agent events. Check that the "
                    f"server accepted the turn on {DEFAULT_WS_PATH} and that "
                    "output transcription is enabled for a native-audio agent."
                )
            built_turns.append(turn)
    except ConnectionClosed as exc:
        # Truncated conversations are not kept: grading a partial trajectory as
        # if it were complete produces a silently wrong score, which is worse
        # than a reported failure.
        return case, (
            f"Live stream closed mid-conversation after turn {len(built_turns)}: {exc}"
        )
    except Exception as exc:
        return case, f"{DEFAULT_WS_PATH} failed: {type(exc).__name__}: {exc}"

    if len(built_turns) != len(user_turns):
        # A clean close raises nothing, so the answered turns would otherwise
        # be graded as a complete, shorter conversation.
        return case, (
            f"Live stream ended after {len(built_turns)} of {len(user_turns)} turns."
        )

    all_agent_events = [
        e for t in built_turns for e in (t.events or []) if e.author != "user"
    ]

    merged = case.model_copy(deep=True)
    agent_data = merged.agent_data or types.evals.AgentData(turns=[])
    if agents_map:
        agent_data.agents = agents_map
    agent_data.turns = built_turns
    merged = merged.model_copy(update={"agent_data": agent_data})
    # Author-less transcription frames default to "model", which matches no key
    # in `agents`. Must run after the turns are installed: `run_case` normalized
    # the input case, whose turns are replaced above.
    rewrite_model_author_events(merged, root_agent_name)

    final_response = final_response_content_from_events(all_agent_events)
    if final_response is not None:
        responses = list(merged.responses or [])
        responses.append(types.ResponseCandidate(response=final_response))
        merged = merged.model_copy(update={"responses": responses})

    return merged, None
