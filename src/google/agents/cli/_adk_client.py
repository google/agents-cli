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

"""Shared ADK FastAPI HTTP client: session create + ``/run_sse`` stream."""

from __future__ import annotations

import json
from collections.abc import Iterator

import requests

_SESSION_TIMEOUT = 30
_RUN_SSE_TIMEOUT = 120
_APP_INFO_TIMEOUT = 10


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
    return payload.get("rootAgentName"), payload.get("agents") or {}


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
        "app_name": app_name,
        "user_id": user_id,
        "session_id": session_id,
        "new_message": user_message,
    }

    with requests.post(
        run_url, headers=headers, json=payload, stream=True, timeout=_RUN_SSE_TIMEOUT
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
            if not isinstance(line, str) or not line.startswith("data: "):
                continue
            data_str = line[len("data: ") :]
            try:
                yield json.loads(data_str)
            except json.JSONDecodeError:
                continue
