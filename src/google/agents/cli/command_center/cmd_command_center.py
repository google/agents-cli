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

"""agents-cli command-center command — start Agent Command Center."""

import errno
import ipaddress
import logging
import os
import socket
from urllib.parse import urlsplit

import click

from google.agents.cli._project import (
    chdir_project_root,
    read_project_config,
    require_agent_directory,
)

# Seconds to wait for open requests (e.g. a streaming chat) on shutdown.
_GRACEFUL_SHUTDOWN_TIMEOUT = 3
# Errors meaning "this machine can't use that address" (e.g. ::1 with IPv6
# off), so binding moves on to the next one. Windows reports the WSA codes.
_UNUSABLE_ADDRESS_ERRNOS = frozenset(
    getattr(errno, name)
    for name in ("EAFNOSUPPORT", "EADDRNOTAVAIL", "WSAEAFNOSUPPORT", "WSAEADDRNOTAVAIL")
    if hasattr(errno, name)
)


@click.command("command-center")
@click.option("--port", default=8080, help="Port Agent Command Center listens on.")
@click.option(
    "--host",
    default="127.0.0.1",
    help=(
        "Host the server binds to. Anything other than a loopback address "
        "exposes it to your network."
    ),
)
# TODO: b/578819529 - Replace with a child ADK server started by this command.
@click.option(
    "--upstream-url",
    required=True,
    hidden=True,
    help="URL of an already running ADK server to forward runtime calls to.",
)
def cmd_command_center(port: int, host: str, upstream_url: str) -> None:
    """Start Agent Command Center (experimental).

    Serves Agent Command Center and forwards the agent's runtime API (chat,
    sessions, traces) to the agent's ADK server.
    """
    if urlsplit(upstream_url).scheme not in ("http", "https"):
        raise click.UsageError(
            f"--upstream-url must be an http:// or https:// URL, got {upstream_url!r}."
        )
    chdir_project_root()
    require_agent_directory(read_project_config())

    if not _is_loopback(host):
        logging.warning(
            "Agent Command Center is listening on %s, so other machines on your "
            "network can reach it. It runs agents-cli with your local "
            "credentials; use --host 127.0.0.1 unless you need remote access.",
            host,
        )
    sock = _bind_socket(host, port)

    # Imported here, not at module top, to keep CLI startup fast.
    import uvicorn

    from google.agents.cli.command_center._app import build_app
    from google.agents.cli.command_center._proxy import upstream_client

    app = build_app(upstream_client(upstream_url))
    # The bound port, not --port: with --port 0 the OS picks one.
    logging.info("Agent Command Center: %s", _browser_url(host, sock.getsockname()[1]))
    logging.info("Forwarding agent runtime calls to %s", upstream_url)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="warning",
            timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_TIMEOUT,
        )
    )
    server.run(sockets=[sock])


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A host name other than localhost may resolve to any interface.
        return False


def _bind_socket(host: str, port: int) -> socket.socket:
    """Bind the listening socket here, so a busy port is a clear CLI error.

    Left to uvicorn, a failed bind logs an error and calls sys.exit(1).
    """
    try:
        candidates = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as err:
        raise click.UsageError(f"Cannot resolve --host {host!r}: {err}.") from err
    unusable: OSError | None = None
    # A name like localhost can resolve to ::1 first, which a machine without
    # IPv6 can't use, so try each address in turn.
    for family, sock_type, proto, _, address in candidates:
        try:
            return _listening_socket(family, sock_type, proto, address)
        except OSError as err:
            if err.errno not in _UNUSABLE_ADDRESS_ERRNOS:
                # Any other failure, a busy port above all, stops here: on the
                # next address a browser could reach whatever holds this one.
                raise click.ClickException(
                    f"Cannot listen on {host}:{port}: {err.strerror or err}.\n"
                    "  Stop the process using that port, or pass a different --port."
                ) from err
            unusable = err
    raise click.ClickException(
        f"Cannot listen on {host}:{port}: none of its addresses can be used on this "
        f"machine ({unusable}).\n  Pass a local address, e.g. --host 127.0.0.1."
    )


def _listening_socket(
    family: socket.AddressFamily,
    sock_type: socket.SocketKind,
    proto: int,
    address: tuple,
) -> socket.socket:
    sock = socket.socket(family, sock_type, proto)
    try:
        # On Windows SO_REUSEADDR lets another process take a port that is in
        # use, so only set it on POSIX, where it just allows a quick restart.
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(address)
        # asyncio calls listen() again when uvicorn starts serving, but a failure
        # there is a traceback; some port conflicts only show up at this step.
        sock.listen()
    except OSError:
        sock.close()
        raise
    return sock


def _browser_url(host: str, port: int) -> str:
    # A wildcard address isn't something a browser can open.
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}/"
