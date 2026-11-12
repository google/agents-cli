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

import contextlib
import errno
import ipaddress
import logging
import os
import signal
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, NoReturn

import click

from google.agents.cli._click import is_set_on_command_line
from google.agents.cli._project import (
    chdir_project_root,
    read_project_config,
    require_agent_directory,
)
from google.agents.cli._server_process import TERMINATION_SIGNALS
from google.agents.cli.command_center._child import (
    AgentServerConfig,
    agent_server,
    agent_server_logs,
)

if TYPE_CHECKING:
    # Type only: processes are started through the _runner helpers.
    from subprocess import Popen

    import uvicorn

# Seconds to wait for open requests (e.g. a streaming chat) on shutdown.
_GRACEFUL_SHUTDOWN_TIMEOUT = 3
# Seconds between checks that the agent server is still running.
_AGENT_SERVER_CHECK_INTERVAL = 1
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
@click.option(
    "--reload_agents/--no-reload_agents",
    default=True,
    help="Enable / disable live reload when agent code changes (Python agents).",
)
@click.option(
    "--otel-to-cloud",
    is_flag=True,
    default=False,
    help="Export OpenTelemetry traces/logs to Google Cloud.",
)
def cmd_command_center(
    port: int, host: str, reload_agents: bool, otel_to_cloud: bool
) -> None:
    """Start Agent Command Center (experimental).

    Serves Agent Command Center, starts the agent's ADK server, and forwards the
    agent's runtime API (chat, sessions, traces) to it.
    """
    chdir_project_root()
    cfg = read_project_config()
    require_agent_directory(cfg)

    # Warn only when the user asked for reloading, not for the default.
    if cfg.language == "go" and reload_agents and is_set_on_command_line("reload_agents"):
        logging.warning("--reload_agents is not supported by `adk-go web`; ignoring it.")
    if not _is_loopback(host):
        logging.warning(
            "Agent Command Center is listening on %s, so other machines on your "
            "network can reach it. It runs agents-cli with your local "
            "credentials; use --host 127.0.0.1 unless you need remote access.",
            host,
        )
    # Bind before starting the agent server, so a busy port fails fast.
    sock = _bind_socket(host, port)
    # The bound port, not --port: with --port 0 the OS picks one.
    acc_port = sock.getsockname()[1]
    config = AgentServerConfig(
        language=cfg.language,
        agent_directory=cfg.agent_directory,
        allowed_origins=_browser_origins(host, acc_port),
        reload_agents=reload_agents,
        otel_to_cloud=otel_to_cloud,
    )

    # Imported here, not at module top, to keep CLI startup fast.
    import uvicorn

    from google.agents.cli.command_center._app import build_app
    from google.agents.cli.command_center._proxy import upstream_client

    with _interrupt_on_termination(), sock, agent_server(config, Path.cwd()) as agent:
        server = uvicorn.Server(
            uvicorn.Config(
                build_app(upstream_client(agent.url)),
                log_level="warning",
                timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_TIMEOUT,
            )
        )
        logging.info("Agent Command Center: %s", _browser_url(host, acc_port))
        with _stop_when_exited(agent.process, server):
            server.run(sockets=[sock])
        exit_code = agent.process.poll()
        if exit_code is not None:
            raise click.ClickException(
                f"The agent server stopped unexpectedly (exit code {exit_code}).\n"
                f"  Check logs: {agent_server_logs()}"
            )


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


def _browser_origins(host: str, port: int) -> list[str]:
    """Origins a browser on this machine can use to open Agent Command Center."""
    origins = [f"http://127.0.0.1:{port}", f"http://localhost:{port}"]
    # e.g. http://[::1]:8080 when started with --host ::1.
    bound = _browser_url(host, port).rstrip("/")
    return origins if bound in origins else [*origins, bound]


@contextlib.contextmanager
def _interrupt_on_termination() -> Iterator[None]:
    """Turn the termination signals into KeyboardInterrupt while the block runs.

    So the agent server is stopped, as on Ctrl+C, instead of left running.
    While serving, uvicorn handles SIGTERM itself and raises it again once it
    has shut down, which then lands here.
    """
    previous = {sig: signal.signal(sig, _raise_interrupt) for sig in TERMINATION_SIGNALS}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _raise_interrupt(_signum: int, _frame: FrameType | None) -> NoReturn:
    raise KeyboardInterrupt


def _browser_url(host: str, port: int) -> str:
    # A wildcard address isn't something a browser can open.
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}/"


@contextlib.contextmanager
def _stop_when_exited(process: "Popen", server: "uvicorn.Server") -> Iterator[None]:
    """Stop ``server`` if ``process`` exits while the block runs."""
    done = threading.Event()

    def watch() -> None:
        while not done.wait(_AGENT_SERVER_CHECK_INTERVAL):
            if process.poll() is not None:
                server.should_exit = True
                return

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        yield
    finally:
        done.set()
        watcher.join()
