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

"""FastAPI app serving the agent over the A2A protocol.

The agent is exposed through the Agent2Agent (A2A) JSON-RPC surface at
``/a2a/{name}``, with its Agent Card at the well-known path. This mirrors the
contract used by agents-cli's ADK A2A agents, so the deploy entrypoint
(``uvicorn app.fast_api_app:app``) and ``agents-cli run --mode a2a`` keep
working unchanged.

For the richest *local* LangGraph experience (graph inspection, streaming,
threads), ``agents-cli playground`` runs ``langgraph dev``, which also serves
the A2A protocol natively.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.agent import root_agent
from app.app_utils.a2a import add_a2a_routes
from app.app_utils.services import persistence_context
from app.app_utils.telemetry import setup_telemetry

# Configure tracing + GenAI logging before the app handles any request.
setup_telemetry()


@asynccontextmanager
async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
    # Open the Cloud SQL pool (if DB env vars are set) or fall back to in-memory
    # persistence for the lifetime of the server. The compiled graph closes over
    # its checkpointer and store, so the pool must outlive the request loop.
    async with persistence_context() as persistence:
        checkpointer = (
            persistence.checkpointer
            if persistence.has_postgres
            else (getattr(root_agent, "checkpointer", None) or persistence.checkpointer)
        )
        store = (
            persistence.store
            if persistence.has_postgres
            else (getattr(root_agent, "store", None) or persistence.store)
        )
        app_instance.state.agent = root_agent.copy(
            update={"checkpointer": checkpointer, "store": store}
        )
        add_a2a_routes(app_instance, agent=app_instance.state.agent)
        yield


app = FastAPI(
    title="langchain-agent",
    description="LangChain agent (A2A protocol)",
    lifespan=lifespan,
)


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
