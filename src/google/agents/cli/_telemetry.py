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

"""agents-cli usage telemetry.

Every `agents-cli <cmd>` invocation can emit one structured usage signal
(command path, exit code, error class, duration): an OTLP log record POSTed to
the Telemetry API (``telemetry.googleapis.com/v1/logs``).

Each signal is written as a log entry to the caller's GCP project
(``GOOGLE_CLOUD_PROJECT``, else the ADC default project), queryable at
``logName=projects/<id>/logs/agents-cli``, where they can analyze their own
agents-cli usage. The request headers also carry tokens for agents-cli usage
analytics, which help improve the CLI experience. The tokens hold only CLI
metadata: the CLI and Python versions, the OS and its release, the CPU
architecture, the command path and a random per-invocation id.

``record_invocation()`` runs as the command exits. For a recorded invocation it
spawns a detached child process (``python -P .../_telemetry_worker.py``) with
the signal as an argument; the CLI exits without waiting. The child
resolves the caller's project and credentials and passes the signal to a
``deliver`` callable, which POSTs it.

Telemetry prints nothing: the child's output goes to the null device and it
configures no logging, so its failures are dropped.

Opt-out: ``DO_NOT_TRACK=1`` (or any non-empty value) or
``AGENTS_CLI_TELEMETRY=0`` (or another false value: ``false``, ``no``, ``off``).
"""

from __future__ import annotations

import json
import logging
import platform
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import click

from google.agents.cli._click import (
    COMMAND_INVOKED_META_KEY,
    COMMAND_PATH_META_KEY,
    EXTENSION_PATH_MARKER,
)


@dataclass(frozen=True)
class _Signal:
    """One invocation's outcome, passed to the child and then to ``deliver``."""

    command_path: str
    exit_code: int
    duration_ms: int
    error_class: str | None
    invocation_id: str


# Telemetry (OTLP) API logs endpoint. OTLP/HTTP with JSON encoding.
_TELEMETRY_LOGS_URL = "https://telemetry.googleapis.com/v1/logs"
# Log id: entries land at logName=projects/<project>/logs/agents-cli.
_LOG_ID = "agents-cli"
# Commands we never emit signals for, matched as path prefixes: "login" also
# covers its subcommands and an extension's override of it ("login~ext").
_SKIP_COMMANDS = frozenset({"login", "setup"})

# Script the detached child runs.
_CHILD_SCRIPT = str(Path(__file__).with_name("_telemetry_worker.py"))
# The child gives up after this long, so a hung credential lookup or request
# can't keep it running. A cold run, including a gcloud project lookup, takes a
# few seconds.
_CHILD_DEADLINE_S = 10.0
# Bounds on the POST, within the child's deadline.
_CONNECT_TIMEOUT_S = 3.0
_READ_TIMEOUT_S = 5.0


def _env_opted_out() -> bool:
    """True if the user disabled telemetry via environment variables."""
    import os

    # DO_NOT_TRACK: any non-empty value means opt out.
    if os.environ.get("DO_NOT_TRACK"):
        return True
    # AGENTS_CLI_TELEMETRY: false/off/no/0 means opt out.
    val = os.environ.get("AGENTS_CLI_TELEMETRY")
    if val is not None and val.strip().lower() in ("0", "false", "no", "off"):
        return True
    return False


def is_telemetry_enabled() -> bool:
    """True if telemetry is configured to emit (not opted out).

    This reflects *configuration*, not whether a given invocation will actually
    deliver — emission additionally requires resolvable credentials and a
    project at runtime (checked in the child process).
    """
    return not _env_opted_out()


def error_class_of(exc: BaseException) -> str:
    """The ``error_class`` recorded for ``exc``, e.g. ``ClickException/CalledProcessError``.

    Commands wrap most failures as ``raise click.ClickException(...) from e``, so
    the class alone would rarely tell failures apart; the direct cause's class
    is appended when there is one.
    """
    name = type(exc).__name__
    if exc.__cause__ is not None:
        return f"{name}/{type(exc.__cause__).__name__}"
    return name


def _command_path(ctx: click.Context) -> str | None:
    """Return the dotted subcommand path for the current invocation.

    ``agents-cli scaffold create --name x`` -> ``"scaffold.create"``. The chain
    is recorded on ``ctx.meta`` by ``LazyGroup.resolve_command`` as Click dispatches
    each level (see ``COMMAND_PATH_META_KEY``), so this is just a read — no argv
    parsing. Returns ``None`` when no subcommand resolved (``agents-cli``,
    ``agents-cli --help``, an unknown command).

    Segments served by an extension carry ``EXTENSION_PATH_MARKER``: an
    overridden built-in reads ``eval.generate~ext``, and a command an extension
    adds reads ``eval.~ext``, never its name.

    Dots keep the path a single token with no spaces, as the ``command/<path>``
    header token requires.
    """
    parts = ctx.meta.get(COMMAND_PATH_META_KEY)
    return ".".join(parts) if parts else None


def _is_skipped(command_path: str) -> bool:
    """True if ``command_path`` is, or is under, one of ``_SKIP_COMMANDS``.

    Segments are compared with the extension marker removed, so an extension's
    override of a skipped command is skipped too.
    """
    segments = [s.removesuffix(EXTENSION_PATH_MARKER) for s in command_path.split(".")]
    for skip in _SKIP_COMMANDS:
        prefix = skip.split(".")
        if segments[: len(prefix)] == prefix:
            return True
    return False


def _python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def _cli_version() -> str:
    # __init__ is cheap (no rich); version.py would pull rich in via its module
    # top-level Console, so don't import get_current_version here.
    from google.agents.cli import __version__

    return __version__


def _build_request_headers(signal: _Signal) -> dict[str, str]:
    cli_version = _cli_version()
    py = _python_version()
    system = platform.system().lower()
    release = platform.release()
    arch = platform.machine().lower()

    shared_tokens = (
        f"google-agents-cli/{cli_version} "
        f"command/{signal.command_path} "
        f"invocation-id/{signal.invocation_id} "
        f"python/{py}"
    )

    user_agent_os_tokens = (
        f"client-os/{system.upper()} client-os-ver/{release} client-pltf-arch/{arch}"
    )
    x_goog_api_client_os_token = f"{system}/{release}"

    user_agent = f"{user_agent_os_tokens} {shared_tokens}"
    x_goog_api_client = f"gl-python/{py} {x_goog_api_client_os_token} {shared_tokens}"

    return {
        "Content-Type": "application/json",
        "User-Agent": user_agent,
        "X-Goog-Api-Client": x_goog_api_client,
    }


def _any_value(value: str | int) -> dict:
    """Wrap a scalar as an OTLP AnyValue (int64 as a string, per proto3 JSON)."""
    if isinstance(value, int):
        return {"intValue": str(value)}
    return {"stringValue": value}


def _build_log_body(signal: _Signal) -> dict:
    """The invocation fields as an OTLP kvlist body.

    The Telemetry API turns a kvlist body into the entry's ``jsonPayload``
    (a string body would become ``textPayload``, and record attributes become
    string-only ``labels``). ``message`` gives Logs Explorer a readable summary
    line.
    """
    message = (
        f"agents-cli {signal.command_path} exited {signal.exit_code}"
        f" in {signal.duration_ms}ms"
    )
    if signal.error_class:
        message += f" ({signal.error_class})"
    fields: dict[str, str | int] = {
        "message": message,
        "command": signal.command_path,
        "exit_code": signal.exit_code,
        "duration_ms": signal.duration_ms,
        "agents_cli_version": _cli_version(),
        "python_version": _python_version(),
        "os": sys.platform,
        "invocation_id": signal.invocation_id,
    }
    if signal.error_class:
        fields["error_class"] = signal.error_class
    return {
        "kvlistValue": {
            "values": [{"key": k, "value": _any_value(v)} for k, v in fields.items()]
        }
    }


def _build_otlp_logs(project: str, signal: _Signal) -> dict:
    """OTLP/HTTP JSON body for ``telemetry.googleapis.com/v1/logs``.

    One ``LogRecord`` whose body carries the invocation fields.
    ``severityNumber`` 9 is INFO in the OTLP severity scale.
    """
    now_ns = str(time.time_ns())
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        # Required by the Telemetry API: the destination project
                        # (distinct from the X-Goog-User-Project quota header).
                        {"key": "gcp.project_id", "value": {"stringValue": project}},
                        {"key": "service.name", "value": {"stringValue": _LOG_ID}},
                        {
                            "key": "service.version",
                            "value": {"stringValue": _cli_version()},
                        },
                    ]
                },
                "scopeLogs": [
                    {
                        "scope": {"name": _LOG_ID, "version": _cli_version()},
                        "logRecords": [
                            {
                                "timeUnixNano": now_ns,
                                "observedTimeUnixNano": now_ns,
                                "severityNumber": 9,
                                "severityText": "INFO",
                                "body": _build_log_body(signal),
                                "attributes": [
                                    # The Telemetry API derives the Cloud Logging
                                    # logName from this attribute, so entries
                                    # land at logs/agents-cli.
                                    {
                                        "key": "gcp.log_name",
                                        "value": {"stringValue": _LOG_ID},
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _deliver(project: str, token: str, signal: _Signal) -> None:
    """POST a single OTLP log record for one invocation."""
    import requests

    headers = _build_request_headers(signal)
    headers["Authorization"] = f"Bearer {token}"
    # Quota project; the Telemetry API requires it (the caller needs
    # serviceusage.serviceUsageConsumer on this project).
    headers["X-Goog-User-Project"] = project
    response = requests.post(
        _TELEMETRY_LOGS_URL,
        headers=headers,
        json=_build_otlp_logs(project, signal),
        timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S),
    )
    if not response.ok:
        # The body names the cause, e.g. a disabled API or a missing role.
        logging.debug(
            "Telemetry API rejected the signal (%s): %s",
            response.status_code,
            response.text,
        )
    response.raise_for_status()


def _warm_up() -> tuple[str, str] | None:
    """Resolve the caller's project and an access token.

    The project is ``GOOGLE_CLOUD_PROJECT``, else the ADC default project;
    signals are written there. Returns ``(project, token)``, or ``None`` if the
    signal can't be delivered.
    """
    try:
        import os

        import google.auth
        from google.auth.transport.requests import Request

        credentials, adc_project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        project = os.environ.get("GOOGLE_CLOUD_PROJECT") or adc_project
        if not project:
            logging.debug("Telemetry skipped: no project to write the signal to")
            return None

        credentials.refresh(Request())
        token = credentials.token
        if not token:
            return None
        return project, token
    except Exception:
        # No credentials means nothing to deliver; telemetry prints nothing.
        logging.debug("Telemetry skipped: no usable credentials", exc_info=True)
        return None


def run_child(
    payload: str, deliver: Callable[[str, str, _Signal], None] = _deliver
) -> None:
    """Body of the detached child process (see ``_telemetry_worker``).

    Parses the signal from ``payload`` (JSON), resolves the caller's project
    and an access token, and passes the signal to ``deliver``. The work runs on
    a daemon thread so the child can give up after ``_CHILD_DEADLINE_S``.
    """
    try:
        signal = _Signal(**json.loads(payload))
    except Exception:
        return  # a malformed signal is dropped, like any telemetry failure

    def _warm_up_and_deliver() -> None:
        warmed = _warm_up()
        if warmed is None:
            return
        project, token = warmed
        try:
            deliver(project, token, signal)
        except Exception:
            # Telemetry prints nothing, so a failed delivery is dropped.
            logging.debug("Telemetry delivery failed", exc_info=True)

    thread = threading.Thread(target=_warm_up_and_deliver, daemon=True)
    thread.start()
    thread.join(_CHILD_DEADLINE_S)


def _spawn_child(signal: _Signal) -> None:
    """Start the detached child with ``signal``, without waiting for it."""
    import subprocess
    import sys
    import tempfile

    from google.agents.cli._runner import popen_resolved_detached

    # The detached helper keeps the child out of the terminal's process group
    # (so Ctrl+C doesn't reach it) with stdin closed, so the signal, which is
    # only CLI metadata, goes as an argument. With stdout and stderr on the
    # null device, the child never writes to or holds open the terminal.
    # -P keeps the script's folder off sys.path, so neither a module in the
    # user's project (e.g. a json.py) nor one of ours can shadow what the child
    # imports. The neutral cwd keeps the child from holding the project folder
    # open, which on Windows blocks deleting it.
    popen_resolved_detached(
        [sys.executable, "-P", _CHILD_SCRIPT, json.dumps(asdict(signal))],
        resolve_executable=False,
        cwd=tempfile.gettempdir(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def record_invocation(
    ctx: click.Context,
    exit_code: int,
    duration_ms: int,
    error_class: str | None,
) -> None:
    """Hand this invocation's outcome to a detached child that delivers it.

    Called from the CLI's exit path. It is a no-op when telemetry is disabled,
    the command is one we never instrument, or the invocation only showed help;
    otherwise it spawns the child (see :func:`run_child`) and returns without
    waiting for it. It never raises and never prints. The dotted command path
    (e.g. ``eval.dataset.synthesize``) is read from ``ctx`` and matched against
    ``_SKIP_COMMANDS``.
    """
    try:
        if not is_telemetry_enabled():
            return
        command_path = _command_path(ctx)
        if command_path is None or _is_skipped(command_path):
            return
        if exit_code == 0 and not ctx.meta.get(COMMAND_INVOKED_META_KEY):
            # Exited cleanly without the command running: --help, or a group
            # given no subcommand. Help views aren't runs.
            return
        _spawn_child(
            _Signal(
                command_path=command_path,
                exit_code=exit_code,
                duration_ms=duration_ms,
                error_class=error_class,
                invocation_id=uuid.uuid4().hex,
            )
        )
    except Exception:  # pragma: no cover - defensive catch-all
        # A failed spawn only means no signal for this invocation.
        pass
