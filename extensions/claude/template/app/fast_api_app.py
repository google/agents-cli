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

"""FastAPI app serving the agent over A2A at ``/a2a/{name}``, and to Agent Runtime's
``:query`` and ``:streamQuery`` methods."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.app_utils import bq_analytics
from app.app_utils.telemetry import (
    FASTAPI_TELEMETRY,
    FlushTelemetryMiddleware,
    setup_telemetry,
)

setup_telemetry()


@asynccontextmanager
async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
    # Imported here so app.agent sees the .env that setup_telemetry() loaded.
    from app.app_utils.a2a import add_a2a_routes
    from app.app_utils.reasoning_engine import add_reasoning_engine_routes

    add_a2a_routes(app_instance)
    add_reasoning_engine_routes(app_instance)
    # Create the BigQuery Agent Analytics table up front, and flush rows on shutdown.
    bq_analytics.writer()
    yield
    bq_analytics.flush(5)


app = FastAPI(
    title="claude-agent",
    description="Claude Agent SDK agent (A2A protocol)",
    lifespan=lifespan,
    telemetry=FASTAPI_TELEMETRY,
)
app.add_middleware(FlushTelemetryMiddleware)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8080")),
    )
