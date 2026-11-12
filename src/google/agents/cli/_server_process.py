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

"""Start-up and shutdown helpers for local agent server processes."""

from __future__ import annotations

import collections
import contextlib
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from pathlib import Path

import click
import psutil

# Project-relative folder for local server state: PID files and logs.
SERVER_STATE_DIR = ".google-agents-cli"
# `uv run` / `go run` may sync or compile before the server listens, and both
# are slower on Windows.
DEFAULT_STARTUP_TIMEOUT = 90 if os.name == "nt" else 30
# Passed to `adk-go web --write-timeout`. Its 15s default cuts off slow /run and
# /run_sse replies and A2A calls; newer scaffolds set it in main.go too.
GO_WRITE_TIMEOUT = "10m"
# Signals whose default action ends the process at once, skipping any cleanup:
# SIGTERM (`kill`), SIGHUP (the terminal closed) and, on Windows, SIGBREAK
# (Ctrl+Break).
TERMINATION_SIGNALS = tuple(
    getattr(signal, name)
    for name in ("SIGTERM", "SIGHUP", "SIGBREAK")
    if hasattr(signal, name)
)
_LOG_TAIL_LINES = 50
_POLL_INTERVAL = 0.3


def wait_until_serving(
    port: int,
    *,
    pid: int | None,
    name: str,
    log_path: Path,
    log_hint: str,
    timeout: float = DEFAULT_STARTUP_TIMEOUT,
) -> None:
    """Wait until a server on 127.0.0.1:``port`` answers HTTP requests.

    Polls with an HTTP GET rather than a TCP connect, so a server that only
    registers its routes in its startup hook (e.g. A2A endpoints) is ready when
    this returns. Raises a ``ClickException`` naming ``log_hint`` if process
    ``pid`` exits first or the server isn't up within ``timeout`` seconds; both
    errors include the end of ``log_path``.
    """
    # The server is local: never route the check through HTTP(S)_PROXY.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid is not None and _has_exited(pid):
            raise click.ClickException(
                f"{name} process exited during startup.\n"
                f"  Check logs: {log_hint}\n"
                f"  Log content:\n{_log_tail(log_path)}"
            )
        try:
            with opener.open(f"http://127.0.0.1:{port}/", timeout=1):
                return
        except urllib.error.HTTPError as err:
            # Any HTTP response (even 404/405) means the server is ready. The
            # error carries the response; close it rather than wait for GC.
            err.close()
            return
        except (urllib.error.URLError, OSError):
            time.sleep(_POLL_INTERVAL)

    raise click.ClickException(
        f"{name} did not start within {timeout}s.\n"
        f"  Check logs: {log_hint}\n"
        f"  Log content:\n{_log_tail(log_path)}"
    )


@contextlib.contextmanager
def signals_ignored(signals: Iterable[int]) -> Iterator[None]:
    """Ignore ``signals`` while the block runs, then restore their handlers."""
    previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in signals}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def terminate_tree(pid: int, *, timeout: float = 3) -> bool:
    """Stop process ``pid`` and its descendants.

    Asks each process to terminate, then kills any still running after
    ``timeout`` seconds. Returns ``False`` if ``pid`` doesn't exist.
    """
    try:
        parent = psutil.Process(pid)
        processes = [*parent.children(recursive=True), parent]
    except psutil.NoSuchProcess:
        return False
    for process in processes:
        with contextlib.suppress(psutil.NoSuchProcess):
            process.terminate()
    _, alive = psutil.wait_procs(processes, timeout=timeout)
    for process in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            process.kill()
    return True


def terminate_process_group(pgid: int, *, timeout: float = 3) -> None:
    """Stop whatever is left of POSIX process group ``pgid``; no-op on Windows.

    Catches what terminate_tree() misses: a descendant whose parent already
    exited (e.g. the server `uv run` or `go run` started) is no longer listed
    as a descendant, but stays in the group. Only for a group this process
    started (``start_new_session``), whose leader it has already reaped.
    """
    if sys.platform == "win32":
        return
    if not _signal_group(pgid, signal.SIGTERM):
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _signal_group(pgid, 0):
            return
        time.sleep(_POLL_INTERVAL)
    _signal_group(pgid, signal.SIGKILL)


def _has_exited(pid: int) -> bool:
    # A child that exited but hasn't been reaped is a zombie on POSIX, and
    # psutil.pid_exists() (like os.kill(pid, 0)) still reports it as running.
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _log_tail(log_path: Path) -> str:
    if not log_path.exists():
        return "<Log file does not exist>"
    try:
        # The log is appended to on every start, so stream it rather than
        # reading it all; one extra line tells whether anything was cut.
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = collections.deque(f, maxlen=_LOG_TAIL_LINES + 1)
    except OSError as e:
        return f"<Failed to read log file: {e}>"
    if len(lines) > _LOG_TAIL_LINES:
        lines.popleft()
        return "... (truncated) ...\n" + "".join(lines)
    return "".join(lines)


def _signal_group(pgid: int, sig: int) -> bool:
    """Send ``sig`` to group ``pgid``; False once the group is gone."""
    if sys.platform == "win32":
        return False
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    except PermissionError as err:
        # E.g. a member changed its user ID. Called during teardown, so an
        # error here would replace the one that ended the command.
        logging.warning(
            "Could not stop the processes left in process group %s (%s); "
            "stop them yourself if they are still running.",
            pgid,
            err,
        )
        return False
    return True
