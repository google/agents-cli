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

"""Shared ADK FastAPI client: session create + ``/run_sse`` and ``/run_live``."""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import time
from collections.abc import (
    AsyncGenerator,
    Iterable,
    Iterator,
)
from typing import NamedTuple
from urllib.parse import urlencode

import requests

_SESSION_TIMEOUT = 30
_RUN_SSE_TIMEOUT = 120
_APP_INFO_TIMEOUT = 10

DEFAULT_WS_PATH = "/run_live"
DEFAULT_TURN_TIMEOUT = 120  # seconds to wait for a turn's turnComplete

# Live sessions where the server keeps working after a turnComplete report it
# as `interactionStatus`. IN_PROGRESS means more model output is still coming;
# anything else (IDLE, or its deprecated REQUIRES_ACTION spelling) ends the
# turn. UNSPECIFIED is the enum's zero value and carries no signal.
_STATUS_IN_PROGRESS = "IN_PROGRESS"
_STATUS_UNSPECIFIED = "INTERACTION_STATUS_UNSPECIFIED"
# An unrecognized status still ends the turn, but gets logged.
_STATUS_TERMINAL = frozenset({"IDLE", "REQUIRES_ACTION"})

# Fallback for models that don't report interactionStatus: ADK can schedule a
# NON_BLOCKING tool's response past the terminator, and on the wire that is
# indistinguishable from a call that is never answered.
DEFAULT_DEFERRED_RESPONSE_TIMEOUT = 30

# /run_live transcribes it back to text, so output stays gradable.
DEFAULT_MODALITIES = ("AUDIO",)


def create_session(
    base_url: str,
    app_name: str,
    user_id: str,
    *,
    headers: dict,
    prior_events: list[dict] | None = None,
) -> str:
    """Create an ADK session and return its ID.

    When ``prior_events`` are supplied, the ADK server seeds the fresh
    session with them.
    """
    session_url = f"{base_url}/apps/{app_name}/users/{user_id}/sessions"
    body: dict = {}
    if prior_events:
        body["events"] = prior_events

    resp = requests.post(
        session_url, headers=headers, json=body, timeout=_SESSION_TIMEOUT
    )
    resp.raise_for_status()
    return resp.json().get("id")


def fetch_app_info(
    *,
    base_url: str,
    app_name: str,
    headers: dict,
) -> tuple[str | None, dict]:
    """Fetch agent metadata from ADK's /apps/{app_name}/app-info endpoint.

    Returns (root_agent_name, agents), where agents is the raw ADK agents
    map keyed by agent id. root_agent_name may be None if the server omits
    it; agents may be empty.

    Raises requests.RequestException (or a subclass: ConnectionError,
    HTTPError, JSONDecodeError) if the endpoint isn't reachable, returns a
    non-2xx status, or returns a body that isn't valid JSON.
    """
    url = f"{base_url}/apps/{app_name}/app-info"
    resp = requests.get(url, headers=headers, timeout=_APP_INFO_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    # Accept both camelCase and snake_case for cross language compatibility
    root_agent_name = payload.get("rootAgentName", payload.get("root_agent_name"))
    agents = payload.get("agents") or {}
    return root_agent_name, agents


def run_sse(
    base_url: str,
    app_name: str,
    session_id: str,
    *,
    user_message: dict,
    headers: dict,
    user_id: str,
) -> Iterator[dict]:
    """Stream ADK events for a single user turn.

    Yields each ``data:`` line's decoded JSON dict as it arrives. Blank
    lines, non-``data:`` lines, and payloads that fail to JSON-decode are
    silently skipped.
    """
    run_url = f"{base_url}/run_sse"
    payload = {
        "appName": app_name,
        "userId": user_id,
        "sessionId": session_id,
        "newMessage": user_message,
    }

    with requests.post(
        run_url, headers=headers, json=payload, stream=True, timeout=_RUN_SSE_TIMEOUT
    ) as resp:
        resp.raise_for_status()
        # Honor the server's explicit charset if provided; otherwise,
        # fall back to UTF-8 rather than latin-1.
        if "charset" not in resp.headers.get("Content-Type", "").lower():
            resp.encoding = "utf-8"
        for line in resp.iter_lines(decode_unicode=True):
            # The space after "data:" is optional; ADK Java's server omits it.
            if not isinstance(line, str) or not line.startswith("data:"):
                continue
            data_str = line[len("data:") :]
            try:
                yield json.loads(data_str)
            except json.JSONDecodeError:
                continue


def _event_parts(event: dict) -> list:
    """Return event's content parts, or an empty list when it carries none."""
    content = event.get("content")
    if not isinstance(content, dict):
        return []
    parts = content.get("parts")
    return parts if isinstance(parts, list) else []


def _event_has_function_call(event: dict) -> bool:
    """Return True if event carries a model function call."""
    return any(
        isinstance(part, dict) and part.get("functionCall")
        for part in _event_parts(event)
    )


def _event_has_function_response(event: dict) -> bool:
    """Return True if event carries a tool's function response."""
    return any(
        isinstance(part, dict) and part.get("functionResponse")
        for part in _event_parts(event)
    )


def _event_has_answer_content(event: dict) -> bool:
    """Return true if event carries gradable model answer content."""
    transcription = event.get("outputTranscription")
    if isinstance(transcription, dict) and transcription.get("text"):
        return True
    return any(
        isinstance(part, dict) and (part.get("text") or part.get("inlineData"))
        for part in _event_parts(event)
    )


class FinishedTranscript(NamedTuple):
    """A completed Live transcription frame."""

    # "user" for input transcription, else the event's author (or "model").
    author: str
    # "user" or "model" -- the genai Content role for the rendered text.
    role: str
    text: str


# Live transcription keys mapped to the Content role they represent.
_TRANSCRIPTION_KEYS = (
    ("inputTranscription", "user"),
    ("outputTranscription", "model"),
)


def finished_transcript(event: dict) -> FinishedTranscript | None:
    """Return the finished transcript carried by event, else None"""
    for key, role in _TRANSCRIPTION_KEYS:
        transcription = event.get(key)
        if not isinstance(transcription, dict):
            continue
        if not transcription.get("finished"):
            return None  # partial chunk; the finished aggregate carries the text
        text = transcription.get("text")
        if not text:
            return None
        author = "user" if role == "user" else (event.get("author") or "model")
        return FinishedTranscript(author=author, role=role, text=text)
    return None


def is_transcription_event(event: dict) -> bool:
    """True if event is a Live transcription frame (finished or partial)."""
    # An empty transcription is still a transcription frame, not a malformed one.
    return any(key in event for key, _ in _TRANSCRIPTION_KEYS)


def stream_live_events(
    ws_base: str,
    app_name: str,
    session_id: str,
    *,
    user_turns: Iterable[dict],
    headers: dict,
    user_id: str,
) -> Iterator[dict | None]:
    """Stream ADK events for a Live conversation over one /run_live socket."""
    return _iter_async(
        _stream_live_events(
            ws_base,
            app_name,
            session_id,
            user_turns=user_turns,
            headers=headers,
            user_id=user_id,
        )
    )


def group_turns(stream: Iterable[dict | None]) -> Iterator[list[dict]]:
    """Batch a stream_live_events stream into one event list per turn."""
    events: list[dict] = []
    for item in stream:
        if item is None:
            yield events
            events = []
        else:
            events.append(item)


def build_run_live_url(
    ws_base: str,
    app_name: str,
    session_id: str,
    *,
    user_id: str,
) -> str:
    """Build the ADK /run_live WebSocket URL from a ws-base."""
    base = ws_base.rstrip("/")
    query = [
        ("app_name", app_name),
        ("user_id", user_id),
        ("session_id", session_id),
    ]
    query.extend(("modalities", m) for m in DEFAULT_MODALITIES)
    return f"{base}{DEFAULT_WS_PATH}?{urlencode(query)}"


async def _stream_live_events(
    ws_base: str,
    app_name: str,
    session_id: str,
    *,
    user_turns: Iterable[dict],
    headers: dict,
    user_id: str,
) -> AsyncGenerator[dict | None, None]:
    """Play user_turns over one socket, yielding each event as it arrives."""
    from websockets.asyncio.client import connect as ws_connect
    from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

    ws_url = build_run_live_url(ws_base, app_name, session_id, user_id=user_id)
    user_turns = list(user_turns)
    expected_turns = len(user_turns)
    extra_headers = {
        k: v for k, v in (headers or {}).items() if k.lower() != "content-type"
    }

    async with ws_connect(ws_url, additional_headers=extra_headers or None) as ws:
        try:
            for turn_index, content in enumerate(user_turns):
                # One `content` frame per turn; ADK validates each frame.
                await ws.send(json.dumps({"content": content}))

                boundary = _TurnBoundary()
                # Set once a turnComplete arrives with a tool call still open.
                deferred_deadline: float | None = None
                while True:
                    if deferred_deadline is None:
                        timeout = float(DEFAULT_TURN_TIMEOUT)
                    else:
                        timeout = max(0.0, deferred_deadline - time.monotonic())
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    except TimeoutError:
                        if deferred_deadline is not None:
                            # Nothing followed the provisional terminator, so it
                            # was the real end of the turn. An ordinary ending
                            # for a fire-and-forget tool -- not a stall.
                            break
                        logging.warning(
                            "Turn %d: no event for %ds, giving up on the turn.",
                            turn_index,
                            DEFAULT_TURN_TIMEOUT,
                        )
                        break
                    except ConnectionClosedOK:
                        if turn_index + 1 < expected_turns:
                            logging.warning(
                                "Turn %d: the agent closed the stream; "
                                "%d later turn(s) were never sent.",
                                turn_index,
                                expected_turns - turn_index - 1,
                            )
                        yield None  # end of turn
                        return

                    event = _decode_ws_frame(raw)
                    if event is None:
                        continue
                    yield event

                    state = boundary.classify(event)
                    if state is _Boundary.TERMINAL:
                        break
                    if state is _Boundary.PROVISIONAL:
                        deferred_deadline = (
                            time.monotonic() + DEFAULT_DEFERRED_RESPONSE_TIMEOUT
                        )
                    elif deferred_deadline is not None:
                        # Only the answer extends the wait: metadata frames
                        # routinely follow a turnComplete and would hold it open.
                        if _event_has_function_response(event):
                            deferred_deadline = None  # full turn budget again
                        elif _event_has_answer_content(event):
                            deferred_deadline = (
                                time.monotonic() + DEFAULT_DEFERRED_RESPONSE_TIMEOUT
                            )

                yield None  # end of turn
        finally:
            try:
                await ws.send(json.dumps({"close": True}))
            except ConnectionClosed:
                pass


class _Boundary(enum.Enum):
    """What one event means for the end of the current logical turn."""

    CONTINUE = "continue"
    PROVISIONAL = "provisional"
    TERMINAL = "terminal"


class _TurnBoundary:
    """Detect the end of one logical live turn.

    Some live models answer one prompt with several turns and report
    ``interactionStatus``; models that omit it use the heuristic below.
    """

    def __init__(self) -> None:
        self._tool_call_open = False
        self._tool_response_seen = False

    def classify(self, event: dict) -> _Boundary:
        """Feed one event; report what it means for the current logical turn."""
        if _event_has_function_call(event):
            # Anything spoken before the call is not the post-tool answer.
            self._tool_call_open = True
            self._tool_response_seen = False
        elif _event_has_function_response(event):
            if self._tool_call_open:
                self._tool_response_seen = True
        elif _event_has_answer_content(event):
            self._tool_call_open = False
            self._tool_response_seen = False

        if not event.get("turnComplete"):
            return _Boundary.CONTINUE

        status = str(event.get("interactionStatus") or "").upper()
        # UNSPECIFIED carries no signal, so it falls through to the heuristic
        # below rather than being read as an authoritative end of turn.
        if status and status != _STATUS_UNSPECIFIED:
            if status == _STATUS_IN_PROGRESS:
                return _Boundary.CONTINUE
            if status not in _STATUS_TERMINAL:
                logging.debug(
                    "Unrecognized interactionStatus %r; ending the turn.", status
                )
            return _Boundary.TERMINAL

        if self._tool_call_open and self._tool_response_seen:
            # Terminator for the tool round-trip; keep reading for the answer.
            self._tool_call_open = False
            self._tool_response_seen = False
            return _Boundary.CONTINUE
        if self._tool_call_open:
            # A terminator while the call is still unanswered. Either the
            # response was scheduled past it and the answer is still coming,
            # or the call is never answered and this really is the end. The
            # two shapes are identical here, so the caller waits it out.
            return _Boundary.PROVISIONAL
        return _Boundary.TERMINAL


def _decode_ws_frame(raw) -> dict | None:
    """Decode a /run_live frame into an ADK event dict, or None for audio."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            return json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _iter_async(agen) -> Iterator:
    """Drive an async generator from sync code on a dedicated event loop."""
    loop = asyncio.new_event_loop()
    try:
        while True:
            try:
                yield loop.run_until_complete(agen.__anext__())
            except StopAsyncIteration:
                break
    finally:
        loop.run_until_complete(agen.aclose())
        # Finalize websockets' own async generators before closing the loop.
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
