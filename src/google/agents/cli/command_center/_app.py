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

"""The Agent Command Center HTTP app: lifecycle API plus the runtime proxy."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Mount, Route, Router, WebSocketRoute
from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocketClose

from google.agents.cli.command_center._proxy import AgentServerProxy

# Prefix of the lifecycle API served by agents-cli itself.
_ACC_API_PREFIX = "/acc/v1"

# Runtime routes owned by the agent's ADK server. Exact paths, then prefixes
# (everything below them). `/debug/trace` is where older ADK versions serve
# traces; newer ones serve them under `/dev`.
_PROXIED_PATHS = ("/run", "/run_sse", "/list-apps", "/health", "/version")
_PROXIED_PREFIXES = ("/apps", "/dev", "/debug")
_PROXIED_WEBSOCKETS = ("/run_live",)
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def build_app(upstream: httpx.AsyncClient) -> Starlette:
    """Build the app, forwarding runtime routes through ``upstream``.

    ``upstream`` is closed when the app shuts down.
    """
    proxy = AgentServerProxy(upstream)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        yield
        await upstream.aclose()

    routes: list[BaseRoute] = [
        Mount(_ACC_API_PREFIX, app=Router(routes=[], default=_acc_api_not_found)),
        *(Route(path, proxy.forward_http, methods=_METHODS) for path in _PROXIED_PATHS),
        *(
            Route(f"{prefix}/{{path:path}}", proxy.forward_http, methods=_METHODS)
            for prefix in _PROXIED_PREFIXES
        ),
        *(WebSocketRoute(path, proxy.forward_websocket) for path in _PROXIED_WEBSOCKETS),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    # Match paths exactly: a redirect from "/apps" to "/apps/" would forward a
    # path that isn't on the list.
    app.router.redirect_slashes = False
    return app


async def _acc_api_not_found(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] == "websocket":
        await WebSocketClose()(scope, receive, send)
        return
    # A JSON 404 rather than Starlette's plain-text one: API clients parse JSON.
    request = Request(scope, receive)
    response = JSONResponse(
        {
            "error": "not_found",
            "message": f"No Agent Command Center API at {request.url.path}.",
        },
        status_code=404,
    )
    await response(scope, receive, send)
