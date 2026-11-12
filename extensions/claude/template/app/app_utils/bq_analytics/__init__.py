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

"""Write each turn's events to BigQuery Agent Analytics (`agent_events`).

Enabled when BQ_ANALYTICS_DATASET_ID is set. Rows are written on a background
thread, so a turn never waits on BigQuery.

- recorder.py turns a turn's events into rows.
- uploader.py streams the rows to BigQuery; tables.py creates the table and views.
- schema.py is ADK's table schema and view definitions.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from typing import Any

from app.app_utils.bq_analytics.recorder import AgentEventsRecorder
from app.app_utils.bq_analytics.uploader import Writer
from app.app_utils.remote_agents import remote_agent_url

__all__ = ["flush", "start_turn", "writer"]

_writer: Writer | None = None
_writer_failed = False
_writer_lock = threading.Lock()


def writer() -> Writer | None:
    """The process's writer, or None when BQ_ANALYTICS_DATASET_ID is unset."""
    global _writer, _writer_failed
    dataset = os.environ.get("BQ_ANALYTICS_DATASET_ID")
    if not dataset or _writer_failed:
        return None
    with _writer_lock:
        if _writer is None and not _writer_failed:
            try:
                _writer = Writer(_project(), dataset)
            except Exception as e:  # analytics must never stop the agent
                logging.warning(
                    "BigQuery Agent Analytics is off: %s. It needs Google Cloud "
                    "credentials and a project (GOOGLE_CLOUD_PROJECT).",
                    e,
                )
                _writer_failed = True
        return _writer


def flush(timeout: float) -> None:
    """Wait for queued rows, when the writer is running."""
    if _writer is not None:
        _writer.flush(timeout)


def start_turn(
    prompt: str,
    options: Any,
    session_id: str,
    conversation_id: str,
    user_id: str | None,
    agent_name: Callable[[str | None], str],
    *,
    instruction: Any,
) -> AgentEventsRecorder | None:
    """A recorder for one turn run with `options`, or None when analytics is off.

    Args:
        instruction: The agent's own system prompt, without recalled memories.
    """
    out = writer()
    if out is None:
        return None
    servers = options.mcp_servers if isinstance(options.mcp_servers, dict) else {}
    remote = {
        name: url
        for name, config in servers.items()
        if isinstance(config, dict) and (url := remote_agent_url(config)) is not None
    }
    # In-process @tool servers count as the agent's own (LOCAL) tools.
    local = {
        name
        for name, config in servers.items()
        if isinstance(config, dict)
        and config.get("type") == "sdk"
        and name not in remote
    }
    return AgentEventsRecorder(
        out,
        prompt=prompt,
        agent_name=agent_name,
        session_id=session_id,
        user_id=user_id or f"A2A_USER_{conversation_id}",
        model=options.model,
        system_prompt=options.system_prompt,
        instruction=instruction,
        remote_agents=remote,
        local_servers=local,
    )


def _project() -> str:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        import google.auth

        _, project = google.auth.default()
    if not project:
        raise ValueError("no Google Cloud project found")
    return project
