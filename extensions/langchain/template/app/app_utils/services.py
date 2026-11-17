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

"""Session checkpointer and long-term memory store for the LangGraph graph.

When the deployment provides Cloud SQL / Postgres credentials (either a direct
``SESSION_DB_URI`` or the ``INSTANCE_CONNECTION_NAME`` / ``DB_USER`` /
``DB_PASS`` / ``DB_NAME`` quartet that ``agents-cli`` wires up for Cloud Run
and GKE with ``--session-type cloud_sql``), build an ``AsyncPostgresSaver`` and
``AsyncPostgresStore`` sharing a single connection pool so the compiled graph
persists thread checkpoints and cross-thread store items across restarts.
Absent credentials, fall back to ``InMemorySaver`` and ``InMemoryStore`` so
multi-turn conversations and store-backed tools work locally out of the box.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.store.base import BaseStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PersistenceServices:
    """Short-term thread checkpointer and long-term cross-thread store."""

    checkpointer: BaseCheckpointSaver
    store: BaseStore
    has_postgres: bool = False


def _session_db_uri() -> str | None:
    """Return a psycopg-compatible URI, or ``None`` when DB creds are absent."""
    if uri := os.environ.get("SESSION_DB_URI"):
        return uri
    instance = os.environ.get("INSTANCE_CONNECTION_NAME")
    password = os.environ.get("DB_PASS")
    if not (instance and password):
        return None
    user = quote(os.environ.get("DB_USER") or "postgres", safe="")
    pwd = quote(password, safe="")
    name = quote(os.environ.get("DB_NAME") or "postgres", safe="")
    # Cloud Run and the GKE Cloud SQL Auth Proxy mount the Unix socket under /cloudsql/<instance>.
    socket_dir = (os.environ.get("CLOUD_SQL_SOCKET_DIR") or "/cloudsql").rstrip("/")
    return f"postgresql://{user}:{pwd}@/{name}?host={socket_dir}/{instance}"


@asynccontextmanager
async def persistence_context() -> AsyncIterator[PersistenceServices]:
    """Yield Postgres-backed ``(checkpointer, store)`` when DB creds are set, else in-memory."""
    uri = _session_db_uri()
    if uri is None:
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.store.memory import InMemoryStore

        logger.info(
            "Postgres persistence not configured; using InMemorySaver and InMemoryStore. "
            "Set SESSION_DB_URI or INSTANCE_CONNECTION_NAME and DB_PASS to persist to Cloud SQL."
        )
        yield PersistenceServices(
            checkpointer=InMemorySaver(),
            store=InMemoryStore(),
            has_postgres=False,
        )
        return

    # Lazy imports keep the module importable without the Postgres extras.
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres.aio import AsyncPostgresStore
    from psycopg_pool import AsyncConnectionPool

    # autocommit is required by the saver/store DDL on first use (.setup()),
    # which runs CREATE INDEX CONCURRENTLY outside a transaction block.
    pool = AsyncConnectionPool(
        conninfo=uri,
        min_size=1,
        max_size=4,
        kwargs={"autocommit": True, "prepare_threshold": 0},
        open=False,
    )
    try:
        await pool.open()
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        store = AsyncPostgresStore(pool)
        await store.setup()
        logger.info("LangGraph Postgres checkpointer and store initialised.")
        yield PersistenceServices(
            checkpointer=checkpointer,
            store=store,
            has_postgres=True,
        )
    finally:
        await pool.close()
