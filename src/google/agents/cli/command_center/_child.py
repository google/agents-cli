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

"""The agent's ADK server, run as a child process of Agent Command Center.

Agent Command Center forwards runtime calls (chat, sessions, traces) to it on an
internal loopback port; the browser never talks to it directly.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import socket
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING

from google.agents.cli._project import agents_root
from google.agents.cli._runner import popen_resolved_detached, redact_cmd
from google.agents.cli._server_process import (
    GO_WRITE_TIMEOUT,
    SERVER_STATE_DIR,
    TERMINATION_SIGNALS,
    signals_ignored,
    terminate_process_group,
    terminate_tree,
    wait_until_serving,
)
from google.agents.cli._uv_index import uv_index_env
from google.agents.cli.scaffold.utils.language import dispatch_language

if TYPE_CHECKING:
    # Type only: processes are started through the _runner helpers.
    from subprocess import Popen

_LOG_FILENAME = "command_center_agent.log"
# On Windows the server's stderr goes to a file of its own, as `run` does it.
_STDERR_LOG_FILENAME = "command_center_agent.stderr.log"
# Longer than the shared default: `go run` compiles first (about 30s on a cold
# build cache), and the user is watching and can stop it at any time.
_STARTUP_TIMEOUT = 120


@dataclass(frozen=True)
class AgentServerConfig:
    """How to run the agent's ADK server behind Agent Command Center."""

    language: str
    agent_directory: str
    # Origins the browser uses to reach Agent Command Center. The Python ADK
    # server rejects writes (e.g. creating a session) from any other Origin.
    allowed_origins: Sequence[str]
    reload_agents: bool
    otel_to_cloud: bool


@dataclass(frozen=True)
class AgentServer:
    """A running agent server: where to reach it, and its process."""

    url: str
    process: Popen


@contextlib.contextmanager
def agent_server(config: AgentServerConfig, project_root: Path) -> Iterator[AgentServer]:
    """Start the agent's ADK server, wait until it serves, and stop it on exit."""
    build = dispatch_language("command-center", LANGUAGE_HANDLERS, config.language)
    port = _free_loopback_port()
    args = build(config=config, port=port)
    logging.info("Starting the agent server: %s", redact_cmd(args))
    process = _spawn(args, config.language, project_root)
    try:
        wait_until_serving(
            port,
            pid=process.pid,
            name="Agent server",
            log_path=_startup_error_log(project_root),
            log_hint=agent_server_logs(),
            timeout=_STARTUP_TIMEOUT,
        )
        yield AgentServer(url=f"http://127.0.0.1:{port}", process=process)
    finally:
        # A second Ctrl+C or SIGTERM must not cut the teardown short: the
        # server runs in its own session, so nothing else would stop it.
        with signals_ignored((signal.SIGINT, *TERMINATION_SIGNALS)):
            # Stopping can take a few seconds; say why nothing seems to happen.
            logging.info("Stopping the agent server...")
            terminate_tree(process.pid)
            # Reap the process so it doesn't linger as a zombie on POSIX.
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1)
            # The server's own process may outlive `uv run` / `go run` (e.g.
            # when they crashed), and then terminate_tree() can't find it.
            terminate_process_group(process.pid)


def agent_server_logs() -> str:
    """The agent server's logs, project-relative, for messages to the user."""
    log = f"{SERVER_STATE_DIR}/{_LOG_FILENAME}"
    if sys.platform == "win32":
        return f"{log} and {SERVER_STATE_DIR}/{_STDERR_LOG_FILENAME}"
    return log


def _python_command(*, config: AgentServerConfig, port: int) -> list[str]:
    # `adk web`, not `adk api_server`: the /dev routes (traces, eval sets,
    # graph) only exist on the dev server it starts.
    args = [
        "uv",
        "run",
        "adk",
        "web",
        agents_root(config.agent_directory),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    for origin in config.allowed_origins:
        args += ["--allow_origins", origin]
    if config.reload_agents:
        args.append("--reload_agents")
    if config.otel_to_cloud:
        args.append("--otel_to_cloud")
    return args


def _go_command(*, config: AgentServerConfig, port: int) -> list[str]:
    # Only the REST API: Agent Command Center serves the UI. adk-go web has no
    # --host (it binds every interface) and no agent reloading. Nor does it
    # check Origin, so allowed_origins has nothing to configure: its
    # `-webui_address` only sets a CORS response header, which the browser's
    # same-origin requests to Agent Command Center don't need.
    args = ["go", "run", ".", "web", "--port", str(port)]
    args += ["--write-timeout", GO_WRITE_TIMEOUT]
    if config.otel_to_cloud:
        args.append("--otel_to_cloud")
    return [*args, "api", "-path_prefix", "/"]


# `None` = not supported yet; dispatch_language raises a clear error.
LANGUAGE_HANDLERS = {
    "python": _python_command,
    "go": _go_command,
    "java": None,
    "typescript": None,
    "kotlin": None,
}


def _free_loopback_port() -> int:
    """A port the OS reports free on 127.0.0.1."""
    # The OS picks it, so it can't collide with a fixed range such as the one
    # `agents-cli run` uses for its own server.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _spawn(args: list[str], language: str, project_root: Path) -> Popen:
    state_dir = project_root / SERVER_STATE_DIR
    state_dir.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    if language == "python":
        # Keep `uv run` from re-locking against a machine-wide mirror.
        env.update(uv_index_env(project_root) or {})
    with contextlib.ExitStack() as stack:
        log_file = stack.enter_context(_open_log(state_dir / _LOG_FILENAME, args))
        stderr: IO[str] | int = subprocess.STDOUT
        if sys.platform == "win32":
            stderr = stack.enter_context(
                _open_log(state_dir / _STDERR_LOG_FILENAME, args)
            )
        # Its own process group, so Ctrl+C reaches Agent Command Center only,
        # which then stops this server itself. The child keeps its own copies
        # of the log file handles after these close.
        return popen_resolved_detached(
            args,
            cwd=str(project_root),
            stdout=log_file,
            stderr=stderr,
            env=env,
        )


def _open_log(path: Path, args: list[str]) -> IO[str]:
    """Open a server log for appending, starting with a header for this run."""
    log_file = open(path, "a", encoding="utf-8")
    log_file.write(
        f"=== Starting agent server at {datetime.now(UTC).isoformat()} ===\n"
        f"Command: {redact_cmd(args)}\n"
    )
    log_file.flush()
    return log_file


def _startup_error_log(project_root: Path) -> Path:
    """The log a failed start's error output lands in."""
    name = _STDERR_LOG_FILENAME if sys.platform == "win32" else _LOG_FILENAME
    return project_root / SERVER_STATE_DIR / name
