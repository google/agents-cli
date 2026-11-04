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

"""Reverse proxy from Agent Command Center to the agent's ADK server.

The browser only talks to Agent Command Center. Runtime calls (chat, sessions,
traces) are forwarded unchanged to the agent's ADK server, which keeps owning
them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketDisconnected
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI
from websockets.typing import Origin

# Headers that describe one connection, not the message, so they are never
# forwarded (RFC 9110, section 7.6.1). `host` is dropped so httpx sends the
# upstream's own host: ADK rejects requests whose Host doesn't match where it
# listens. `content-length` is recomputed by httpx for the request body.
_REQUEST_SKIP_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",  # codespell:ignore
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_RESPONSE_SKIP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",  # codespell:ignore
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
# Close codes (RFC 6455, 7.4.1): the message broke the server's policy, and the
# server hit an unexpected condition.
_WS_POLICY_VIOLATION = 1008
_WS_INTERNAL_ERROR = 1011
# No read timeout: /run_sse streams can stay quiet for as long as a model or
# tool call takes.
_UPSTREAM_TIMEOUT = httpx.Timeout(None, connect=5.0)


@dataclass(frozen=True)
class AgentServerProxy:
    """Forwards HTTP and WebSocket requests to the agent's ADK server.

    ``client`` must have ``base_url`` set to the ADK server, e.g.
    ``http://127.0.0.1:8000``.
    """

    client: httpx.AsyncClient

    async def forward_http(self, request: Request) -> Response:
        """Forward ``request`` and stream the response back unbuffered."""
        if _has_dot_segment(request):
            return JSONResponse(
                {
                    "error": "invalid_path",
                    "message": "Paths with '.' or '..' segments aren't forwarded.",
                },
                status_code=400,
            )
        upstream_request = self.client.build_request(
            request.method,
            _path_and_query(request),
            headers=_forwarded_headers(request.headers.items(), _REQUEST_SKIP_HEADERS),
            content=await request.body(),
        )
        try:
            upstream = await self.client.send(upstream_request, stream=True)
        except httpx.TransportError as err:
            logging.warning(
                "Agent server at %s did not answer %s %s: %s",
                self.client.base_url,
                request.method,
                request.url.path,
                err,
            )
            return JSONResponse(
                {
                    "error": "agent_server_unreachable",
                    "message": (
                        f"The agent server at {self.client.base_url} is not "
                        "reachable. Check that it is running."
                    ),
                },
                status_code=502,
            )
        # aiter_raw keeps the upstream encoding, so content-encoding and
        # content-length stay valid; SSE events are passed on as they arrive.
        response = StreamingResponse(
            _stream_body(upstream),
            status_code=upstream.status_code,
            background=BackgroundTask(upstream.aclose),
        )
        # Set raw headers directly: a headers mapping would collapse repeated
        # headers such as Set-Cookie.
        response.raw_headers = [
            (k, v)
            for k, v in upstream.headers.raw
            if k.decode("latin-1").lower() not in _RESPONSE_SKIP_HEADERS
        ]
        return response

    async def forward_websocket(self, websocket: WebSocket) -> None:
        """Relay a WebSocket session (e.g. ``/run_live``) in both directions."""
        if _has_dot_segment(websocket):
            # Closing before accept rejects the browser's handshake.
            await websocket.close(code=_WS_POLICY_VIOLATION)
            return
        url = str(
            self.client.base_url.copy_with(
                scheme="wss" if self.client.base_url.scheme == "https" else "ws"
            )
        ).rstrip("/") + _path_and_query(websocket)
        origin = websocket.headers.get("origin")
        try:
            upstream = await connect(
                url,
                origin=Origin(origin) if origin else None,
                subprotocols=websocket.scope.get("subprotocols") or None,
                # The agent server is local; never route through HTTPS_PROXY.
                proxy=None,
                # Live audio/video frames can exceed the 1 MiB default; the
                # agent server applies its own limit.
                max_size=None,
            )
        except (OSError, InvalidHandshake, InvalidURI, TimeoutError) as err:
            logging.warning("Agent server WebSocket %s failed to open: %s", url, err)
            # Closing before accept rejects the browser's handshake.
            await websocket.close(code=_WS_INTERNAL_ERROR)
            return

        async with upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)
            to_upstream = asyncio.create_task(_browser_to_upstream(websocket, upstream))
            to_browser = asyncio.create_task(_upstream_to_browser(upstream, websocket))
            done, pending = await asyncio.wait(
                {to_upstream, to_browser}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for task in done:
                task.result()


def upstream_client(base_url: str) -> httpx.AsyncClient:
    """An HTTP client for the agent's ADK server at ``base_url``."""
    # trust_env=False: the agent server is local, so HTTP(S)_PROXY must not apply.
    return httpx.AsyncClient(
        base_url=base_url, timeout=_UPSTREAM_TIMEOUT, trust_env=False
    )


def _has_dot_segment(connection: Request | WebSocket) -> bool:
    """Whether the decoded path has a '.' or '..' segment.

    Starlette routes the path as sent, so /apps/../builder/save matches the
    /apps prefix, and httpx would resolve the '..' when joining it to the agent
    server URL: the request would reach a route that isn't forwarded. The
    decoded path also catches encoded forms such as %2e%2e and ..%2F.
    """
    return any(segment in (".", "..") for segment in connection.url.path.split("/"))


def _path_and_query(connection: Request | WebSocket) -> str:
    """The request target as the browser sent it, still percent-encoded."""
    # raw_path keeps encoded characters (e.g. %2F in an ID) that `path` decodes.
    raw_path = connection.scope.get("raw_path")
    path = raw_path.decode("latin-1") if raw_path else connection.url.path
    query = connection.url.query
    return f"{path}?{query}" if query else path


def _forwarded_headers(
    headers: Iterable[tuple[str, str]], skip: frozenset[str]
) -> list[tuple[str, str]]:
    return [(k, v) for k, v in headers if k.lower() not in skip]


async def _stream_body(upstream: httpx.Response):
    try:
        async for chunk in upstream.aiter_raw():
            yield chunk
    except httpx.TransportError as err:
        # Headers are already sent, so the status can't change; end the stream.
        logging.warning("Agent server response was cut off: %s", err)


async def _browser_to_upstream(websocket: WebSocket, upstream: ClientConnection) -> None:
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                await upstream.close(code=_sendable_close_code(message.get("code"), 1000))
                return
            if message.get("text") is not None:
                await upstream.send(message["text"])
            elif message.get("bytes") is not None:
                await upstream.send(message["bytes"])
    except (WebSocketDisconnect, WebSocketDisconnected, ConnectionClosed):
        return


async def _upstream_to_browser(upstream: ClientConnection, websocket: WebSocket) -> None:
    try:
        async for message in upstream:
            if isinstance(message, str):
                await websocket.send_text(message)
            else:
                await websocket.send_bytes(message)
    except ConnectionClosed:
        pass
    except (WebSocketDisconnect, WebSocketDisconnected):
        # The browser went away while a message was on its way to it; the
        # other direction closes the agent server's side.
        return
    with contextlib.suppress(WebSocketDisconnect, WebSocketDisconnected):
        await websocket.close(
            code=_sendable_close_code(upstream.close_code, _WS_INTERNAL_ERROR),
            reason=upstream.close_reason or None,
        )


def _sendable_close_code(code: int | None, fallback: int) -> int:
    """``code`` if it may appear in a close frame, else ``fallback``.

    1005, 1006 and 1015 only report that a peer sent no code, dropped the
    connection, or failed TLS (RFC 6455, 7.4.1); they can't be sent.
    """
    return fallback if code is None or code in (1005, 1006, 1015) else code
