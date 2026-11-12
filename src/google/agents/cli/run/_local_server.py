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

"""Background local server management for the ``run`` command."""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import click
import psutil

from google.agents.cli._project import agent_package_name, agents_root
from google.agents.cli._runner import popen_resolved_detached, redact_cmd
from google.agents.cli._server_process import (
    DEFAULT_STARTUP_TIMEOUT,
    GO_WRITE_TIMEOUT,
    SERVER_STATE_DIR,
    terminate_tree,
    wait_until_serving,
)
from google.agents.cli._uv_index import uv_index_env
from google.agents.cli.scaffold.utils.language import (
    dispatch_language,
    get_language_config,
)

_PID_FILENAME = "run_server.json"
_LOG_FILENAME = "run_server.log"
# Project-relative path to the server log, for pointing users at a traceback.
SERVER_LOG_PATH = f"{SERVER_STATE_DIR}/{_LOG_FILENAME}"
_BASE_PORT = 18080
_MAX_PORT_ATTEMPTS = 10
_DEFAULT_IDLE_TIMEOUT = 1800  # 30 minutes


class ServerInfo(NamedTuple):
    """A running local server's port, PID, and whether *this* call started it.

    ``started`` is ``True`` only when ``ensure_server`` launched a new
    process; it is ``False`` when an already-running server was reused.
    Callers use it to avoid tearing down a server someone else is keeping
    alive (e.g. one started with ``--start-server``).
    """

    port: int
    started: bool
    pid: int


def ensure_server(
    project_root: Path,
    agent_dir: str,
    *,
    language: str,
    idle_timeout: int = _DEFAULT_IDLE_TIMEOUT,
    otel_to_cloud: bool = False,
    use_in_memory_session: bool = True,
    before_start: Callable[[], None] | None = None,
) -> ServerInfo:
    """Return a running local server's port, starting one if needed.

    If an existing server has been idle longer than *idle_timeout* seconds
    (based on the ``last_activity`` timestamp in the PID file), it is
    stopped and a fresh one is started.

    Args:
        project_root: The project root directory (cwd when running).
        agent_dir: The agent directory name (e.g. ``"investment_agent"``).
        language: The project language (``"python"`` or ``"go"``). Selects the
            server launcher. Only takes effect when a new server is started.
        idle_timeout: Seconds of inactivity before the server is considered
            stale and replaced.  Defaults to 30 minutes.
        otel_to_cloud: When ``True``, export traces to Cloud Trace.
            Only takes effect when a new server is started.  Reusing a server
            started without it warns.  Legacy PID files record this as
            ``trace_to_cloud``, which is still honoured.
        use_in_memory_session: Sets ``USE_IN_MEMORY_SESSION`` in the
            server's env to ``true`` (default) or ``false``. Only takes
            effect when a new server is started.  When reusing an existing
            server, this value is compared against the mode recorded in the
            PID file; a mismatch raises a :class:`click.ClickException`
            without terminating the existing server.  Legacy PID files that
            pre-date this field are treated as ``True``.
        before_start: Called just before a new server is started, not when a
            running one is reused.

    Returns:
        A :class:`ServerInfo` with the port, PID, and whether this call
        started the server.
    """
    info = _read_pid_file(project_root)

    if info:
        if _is_server_alive(info["pid"], info["port"]):
            # Validate session mode before reuse or cleanup.  Legacy PID files that
            # pre-date this field are treated as use_in_memory_session=True
            # (the historical default).  A mismatch is a hard error; the
            # existing server is NOT terminated so the user can decide.
            existing_mode = info.get("use_in_memory_session", True)
            if existing_mode != use_in_memory_session:
                existing_str = "in-memory" if existing_mode else "persistent"
                requested_str = "in-memory" if use_in_memory_session else "persistent"
                raise click.ClickException(
                    f"Cannot reuse the existing server: it uses "
                    f"{existing_str} sessions, but {requested_str} "
                    f"sessions were requested.\n"
                    "  Run 'agents-cli run --stop-server' first, "
                    "then retry."
                )

            # Check idle timeout — stop the server if it's been idle too long.
            if _is_idle(info, idle_timeout):
                _cleanup(project_root, info)
            else:
                # Fall back to the legacy key for servers started before the rename.
                # Safe to drop once no pre-rename servers can still be running.
                otel_to_cloud_from_pid_file = info.get("otel_to_cloud") or info.get(
                    "trace_to_cloud"
                )
                if otel_to_cloud and not otel_to_cloud_from_pid_file:
                    logging.warning(
                        "Reusing existing server that was started without "
                        "--otel-to-cloud. Run 'agents-cli run --stop-server' "
                        "first to restart with tracing enabled."
                    )
                _update_activity(project_root)
                return ServerInfo(port=info["port"], started=False, pid=info["pid"])
        else:
            # Stale PID file — clean up before starting fresh.
            _cleanup(project_root, info)

    if before_start is not None:
        before_start()
    port = _find_free_port()
    pid = _start_server(
        project_root=project_root,
        agent_dir=agent_dir,
        port=port,
        language=language,
        otel_to_cloud=otel_to_cloud,
        use_in_memory_session=use_in_memory_session,
    )
    _wait_for_port(project_root, port, pid=pid)
    _write_pid_file(
        project_root,
        pid=pid,
        port=port,
        otel_to_cloud=otel_to_cloud,
        use_in_memory_session=use_in_memory_session,
    )
    return ServerInfo(port, started=True, pid=pid)


def stop_server(project_root: Path, pid: int | None = None) -> bool:
    """Stop the background server.

    If `pid` is provided, the server is only stopped if the PID in the
    metadata matches. This prevents a race where an invocation's teardown
    stops a newly-started replacement server.

    Returns:
        ``True`` if a server was found and stopped.
    """
    info = _read_pid_file(project_root)
    if not info:
        return False
    if pid is not None and info.get("pid") != pid:
        return False
    _cleanup(project_root, info)
    click.secho("Local server stopped.", dim=True)
    return True


def get_server_port(project_root: Path) -> int | None:
    """Return the port of the running local server, or ``None`` if absent.

    Returns ``None`` when no server has been started for *project_root*,
    or when the recorded process is no longer alive.
    """
    info = _read_pid_file(project_root)
    if not info:
        return None
    if not _is_server_alive(info["pid"], info["port"]):
        return None
    return info["port"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_free_port(
    base: int = _BASE_PORT, max_attempts: int = _MAX_PORT_ATTEMPTS
) -> int:
    """Find a free local port starting from *base*."""
    for offset in range(max_attempts):
        port = base + offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise click.ClickException(
        f"No free port found in range {base}–{base + max_attempts - 1}.\n"
        "  Stop other servers or use --url to query a remote agent."
    )


def _has_fast_api_app(project_root: Path, agent_dir: str) -> bool:
    return (project_root / agent_dir / "fast_api_app.py").is_file()


def _build_serve_command(
    *,
    project_root: Path,
    agent_dir: str,
    port: int,
    language: str,
    otel_to_cloud: bool = False,
) -> list[str]:
    """Return the command list for booting the local server for ``language``.

    Dispatches to the per-language builder in ``LANGUAGE_HANDLERS`` and
    raises a clear error for a language with no launcher.
    """
    builder = dispatch_language("run", LANGUAGE_HANDLERS, language)
    return builder(
        project_root=project_root,
        agent_dir=agent_dir,
        port=port,
        otel_to_cloud=otel_to_cloud,
    )


def _python_serve_command(
    *,
    project_root: Path,
    agent_dir: str,
    port: int,
    otel_to_cloud: bool = False,
) -> list[str]:
    """Booting command for a Python agent.

    Prefers running this project's `fast_api_app.py` under uvicorn when the
    file exists, otherwise falls back to `adk api_server`.
    """
    root = agents_root(agent_dir)
    if _has_fast_api_app(project_root, agent_dir):
        if otel_to_cloud:
            logging.warning(
                "--otel-to-cloud is ignored when booting %s/fast_api_app.py; "
                "your fast_api_app.py controls telemetry via its own setup. "
                "Remove fast_api_app.py to fall back to `adk api_server` with "
                "the flag applied.",
                agent_dir,
            )
        cmd = [
            "uv",
            "run",
            "uvicorn",
            f"{agent_package_name(agent_dir)}.fast_api_app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
        # A nested package (src/my_agent) imports from its parent folder.
        return [*cmd, "--app-dir", root] if root != "." else cmd

    cmd = [
        "uv",
        "run",
        "adk",
        "api_server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--reload_agents",
        "--no-reload",
    ]
    if otel_to_cloud:
        cmd.append("--otel_to_cloud")
    cmd.append(root)
    return cmd


def _go_serve_command(
    *,
    port: int,
    otel_to_cloud: bool = False,
    **_,
) -> list[str]:
    """Booting command for a Go agent: `go run . web ... api -path_prefix / a2a`.

    Mirrors the template Makefile's local-backend target. ADK Go serves the
    ADK REST API at the root (-path_prefix /) and A2A under /a2a on this port.
    """
    cmd = [
        "go",
        "run",
        ".",
        "web",
        "--port",
        str(port),
        "--write-timeout",
        GO_WRITE_TIMEOUT,
    ]
    if otel_to_cloud:
        cmd.append("--otel_to_cloud")
    return [*cmd, "api", "-path_prefix", "/", "a2a", "appinfo"]


# `None` = not supported yet; dispatch_language raises a clear error.
LANGUAGE_HANDLERS = {
    "python": _python_serve_command,
    "go": _go_serve_command,
    "java": None,
    "typescript": None,
    "kotlin": None,
}


def api_base_path(language: str) -> str:
    """Return the URL sub-path the local ADK API is served under for ``language``."""
    return get_language_config(language).get("api_base_path", "")


def _start_server(
    *,
    project_root: Path,
    agent_dir: str,
    port: int,
    language: str,
    otel_to_cloud: bool = False,
    use_in_memory_session: bool = True,
) -> int:
    """Start the local server as a detached background process.

    Returns the PID.

    `use_in_memory_session` sets `USE_IN_MEMORY_SESSION` to `true` or `false`
    in the child's env. `true` lets the server start without cloud dependencies
    (e.g. the Agent Runtime session type); `false` makes it use the agent's real
    session service.
    """
    adk_dir = project_root / SERVER_STATE_DIR
    adk_dir.mkdir(exist_ok=True)
    log_path = adk_dir / _LOG_FILENAME

    cmd = _build_serve_command(
        project_root=project_root,
        agent_dir=agent_dir,
        port=port,
        language=language,
        otel_to_cloud=otel_to_cloud,
    )

    env = os.environ.copy()
    env["USE_IN_MEMORY_SESSION"] = "true" if use_in_memory_session else "false"
    # The A2A agent card advertises APP_URL, so it must name this server, not an
    # inherited value or the template's port-8000 fallback.
    env["APP_URL"] = f"http://127.0.0.1:{port}"
    env.setdefault("PYTHONUNBUFFERED", "1")
    if language == "python":
        # Keep `uv run` from re-locking against a machine-wide mirror.
        env.update(uv_index_env(project_root) or {})

    log_file = open(log_path, "a", encoding="utf-8")
    stderr_file = None
    try:
        log_file.write(f"=== Starting server at {datetime.now(UTC).isoformat()} ===\n")
        log_file.write(f"Command: {redact_cmd(cmd)}\n")
        log_file.write(f"CWD: {project_root}\n")
        log_file.flush()

        if sys.platform == "win32":
            stderr_path = adk_dir / "run_server.stderr.log"
            stderr_file = open(stderr_path, "a", encoding="utf-8")
            stderr_file.write(
                f"=== Starting server stderr at {datetime.now(UTC).isoformat()} ===\n"
            )
            stderr_file.write(f"Command: {redact_cmd(cmd)}\n")
            stderr_file.flush()
            child_stdout = log_file
            child_stderr = stderr_file
        else:
            child_stdout = log_file
            child_stderr = log_file

        proc = popen_resolved_detached(
            cmd,
            cwd=str(project_root),
            stdout=child_stdout,
            stderr=child_stderr,
            env=env,
        )
        log_file.write(f"=== Started server process {proc.pid} ===\n")
        log_file.flush()
    finally:
        # Close the parent's copy of the fd — the child inherits its own.
        log_file.close()
        if stderr_file:
            stderr_file.close()
    return proc.pid


def _wait_for_port(
    project_root: Path,
    port: int,
    timeout: int = DEFAULT_STARTUP_TIMEOUT,
    pid: int | None = None,
) -> None:
    """Wait until the local server on ``port`` is ready to handle HTTP requests."""
    if sys.platform == "win32":
        log_path = project_root / SERVER_STATE_DIR / "run_server.stderr.log"
    else:
        log_path = project_root / SERVER_STATE_DIR / _LOG_FILENAME
    wait_until_serving(
        port,
        pid=pid,
        name="Local server",
        log_path=log_path,
        log_hint=SERVER_LOG_PATH,
        timeout=timeout,
    )


# --- PID file helpers ---


def _pid_file_path(project_root: Path) -> Path:
    return project_root / SERVER_STATE_DIR / _PID_FILENAME


def _read_pid_file(project_root: Path) -> dict | None:
    path = _pid_file_path(project_root)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_pid_file(
    project_root: Path,
    *,
    pid: int,
    port: int,
    otel_to_cloud: bool = False,
    use_in_memory_session: bool = True,
) -> None:
    now = datetime.now(UTC).isoformat()
    data = {
        "pid": pid,
        "port": port,
        "started_at": now,
        "last_activity": now,
        "otel_to_cloud": otel_to_cloud,
        "use_in_memory_session": use_in_memory_session,
    }
    path = _pid_file_path(project_root)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _update_activity(project_root: Path) -> None:
    """Stamp ``last_activity`` so idle detection resets."""
    path = _pid_file_path(project_root)
    try:
        data = json.loads(path.read_text())
        data["last_activity"] = datetime.now(UTC).isoformat()
        path.write_text(json.dumps(data, indent=2) + "\n")
    except (json.JSONDecodeError, OSError):
        pass


def _is_idle(info: dict, idle_timeout: int) -> bool:
    """Return ``True`` if the server has been idle longer than *idle_timeout*."""
    try:
        last = datetime.fromisoformat(info["last_activity"])
        idle = (datetime.now(UTC) - last).total_seconds()
        return idle > idle_timeout
    except (KeyError, ValueError):
        # Treat missing or unparseable timestamps as stale.
        return True


def _is_server_alive(pid: int, port: int) -> bool:
    """Return ``True`` if the process exists AND the port is open."""
    # os.kill(pid, 0) is unreliable on Windows, so use psutil.pid_exists.
    if not psutil.pid_exists(pid):
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _cleanup(project_root: Path, info: dict) -> None:
    """Terminate the server process and remove the PID file."""
    pid = info.get("pid")
    if pid and not terminate_tree(pid):
        logging.warning(
            "Local server process with PID %d not found, skipping termination.", pid
        )
    path = _pid_file_path(project_root)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logging.warning("Failed to remove PID file %s: %s", path, exc)
