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

"""agents-cli run command — run agent with a single prompt."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import uuid
from pathlib import Path
from typing import NamedTuple

import click
import httpx
import requests
from a2a.client import ClientConfig
from a2a.types import (
    Message,
    Part,
    Role,
    SendMessageRequest,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
)
from a2a.utils.constants import (
    AGENT_CARD_WELL_KNOWN_PATH,
    PROTOCOL_VERSION_1_0,
    VERSION_HEADER,
    TransportProtocol,
)
from google.protobuf.json_format import MessageToDict

from google.agents.cli._adk_client import (
    DEFAULT_WS_PATH,
    create_session,
    finished_transcript,
    run_sse,
    stream_live_events,
)
from google.agents.cli._modes import (
    MODE_A2A,
    MODE_ADK,
    MODE_ADK_LIVE,
    RUN_MODES,
)
from google.agents.cli._project import (
    chdir_project_root,
    read_project_config,
    require_agent_directory,
)
from google.agents.cli._remote import (
    build_agent_runtime_passthrough_url,
    build_remote_headers,
    is_legacy_agent_runtime_url,
    parse_agent_runtime_service_url,
    resolve_agent_endpoints,
    validate_agent_runtime_url,
)
from google.agents.cli.run._a2a_pinning import (
    OriginPinnedAuth,
    create_pinned_client,
    origin_of,
)
from google.agents.cli.run._local_server import (
    SERVER_LOG_PATH,
    api_base_path,
    ensure_server,
    stop_server,
)
from google.agents.cli.run._multimodal import (
    build_a2a_parts,
    build_adk_sse_parts,
    build_agent_runtime_message,
)

_ARTIFACTS_DIR = Path(".google-agents-cli") / "artifacts"

# Fixed user id so a --session-id from one run resolves on the next.
_CLI_USER_ID = "cli-user"


class _DispatchTarget(NamedTuple):
    service_url: str
    headers: dict
    mode: str
    app_name: str
    # True only when this invocation started the local server; False when a
    # running server was reused or for remote (--url) runs.
    started_server: bool = False
    server_pid: int | None = None


class _SseEventResult(NamedTuple):
    """Outcome of rendering one SSE/NDJSON event."""

    last_author: str | None
    # True when the event rendered content or surfaced an error.
    rendered: bool
    # Text of the partial events printed since the last final one.
    streamed: str


def _resolve_dispatch_target(
    url: str | None,
    mode: str | None,
    app_name: str | None,
    custom_headers: tuple[str, ...],
    *,
    otel_to_cloud: bool = False,
    start_server: bool = False,
) -> _DispatchTarget:
    """Resolve where and how to dispatch a query.

    Remote (``--url``): validates ``--mode`` first so missing flags don't
    surface as "no pyproject.toml". Reads the local project for
    ``app_name`` only when ``--app-name`` was not provided.

    Local: starts (or reuses) a background server and points at it.
    """
    if url:
        if not mode:
            raise click.UsageError(
                "--mode is required when using --url. "
                f"Choose from: {', '.join(RUN_MODES)}"
            )
        validate_agent_runtime_url(url)
        if app_name:
            resolved = app_name
        else:
            chdir_project_root()
            cfg = read_project_config()
            require_agent_directory(cfg)
            resolved = cfg.app_name
        return _DispatchTarget(
            service_url=url,
            headers=build_remote_headers(custom_headers, url),
            mode=mode,
            app_name=resolved,
        )

    chdir_project_root()
    cfg = read_project_config()
    require_agent_directory(cfg)
    server = ensure_server(
        Path.cwd(),
        cfg.agent_directory,
        language=cfg.language,
        otel_to_cloud=otel_to_cloud,
    )
    if server.started:
        _print_local_server_banner(server.port, server.pid, keep_running=start_server)
    base_path = api_base_path(cfg.language)
    return _DispatchTarget(
        service_url=f"http://127.0.0.1:{server.port}{base_path}",
        headers={},
        mode=mode or MODE_ADK,
        app_name=app_name or cfg.app_name,
        started_server=server.started,
        server_pid=server.pid,
    )


def _print_local_server_banner(port: int, pid: int | None, *, keep_running: bool) -> None:
    """Prints a banner for a freshly started local server and how it will end.

    A persistent server (``--start-server``) outlives the run, so it advertises
    how to stop it; a one-off server tears itself down when the run finishes, so
    it says so instead of a stop hint that would mislead.
    """
    if keep_running:
        click.secho(f"Local server started on port {port} (PID {pid})", dim=True)
        click.secho("  Stop with: agents-cli run --stop-server", dim=True)
    else:
        click.secho(
            f"Starting a temporary local server on port {port} "
            "(stops automatically when done).",
            dim=True,
        )


def _handle_stop_server(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    """Eager callback for ``--stop-server``: stop and exit before argument parsing."""
    if not value:
        return
    chdir_project_root()
    if stop_server(Path.cwd()):
        ctx.exit(0)
    raise click.ClickException("No local server is running.")


@click.command("run")
@click.argument("message")
@click.option(
    "--url",
    default=None,
    help="URL of a remote agent to query. If specified, no local server is started.",
)
@click.option(
    "--mode",
    type=click.Choice(RUN_MODES, case_sensitive=False),
    default=None,
    help=(
        "Protocol used to talk to the agent: 'a2a', 'adk' (SSE), or 'adk_live' "
        "(bidi WebSocket). Required with --url; local runs default to 'adk'."
    ),
)
@click.option(
    "--app-name",
    default=None,
    help=(
        "Agent name for remote ADK SSE / A2A endpoints. "
        "Defaults to the local project's app name; "
        "specify to query a different agent or to run from outside a project."
    ),
)
@click.option(
    "--file",
    "-f",
    "files",
    multiple=True,
    type=click.Path(exists=True, readable=True),
    help="Attach a file (image, PDF, audio, video). Repeatable.",
)
@click.option(
    "--session-id",
    default=None,
    help=(
        "Resume an existing session. A local session lives in the running "
        "server's memory — keep the server alive with --start-server to "
        "resume it across runs."
    ),
)
@click.option(
    "--header",
    "-H",
    "custom_headers",
    multiple=True,
    help="Custom HTTP header (format: 'Key: Value'). Repeatable. Overrides auto-detected auth.",
)
@click.option(
    "--start-server",
    "start_server",
    is_flag=True,
    default=False,
    help=(
        "Keep the local server running after execution. The server persists until "
        "stopped with --stop-server, giving subsequent run requests less overhead "
        "and keeping in-memory sessions alive between runs."
    ),
)
@click.option(
    "--stop-server",
    "stop_server_flag",
    is_flag=True,
    default=False,
    is_eager=True,
    expose_value=False,
    callback=_handle_stop_server,
    help="Stop the local background server and exit.",
)
@click.option(
    "--otel-to-cloud",
    is_flag=True,
    default=False,
    help=(
        "Export OpenTelemetry traces/logs to Google Cloud. "
        "Takes effect when the local server starts; ignored with --url."
    ),
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Print full JSON event payloads.",
)
def cmd_run(
    message: str,
    *,
    url: str | None,
    mode: str | None,
    app_name: str | None,
    files: tuple[str, ...],
    session_id: str | None,
    custom_headers: tuple[str, ...],
    start_server: bool,
    otel_to_cloud: bool,
    verbose: bool,
):
    """Run the agent with a single prompt (non-interactive).

    MESSAGE is the prompt to send to the agent.

    \b
    Run from your project directory to query the agent locally. A plain run
    starts a one-off server and shuts it down when it finishes; pass
    --start-server to keep the server running in the background instead.
    Once a server is running, later plain runs reuse it (and leave it
    running), so repeated requests are faster. Stop a persistent server with
    --stop-server. After 30 minutes idle, the next request restarts it.

    \b
    Use --url to query a deployed agent instead. Requires
    --mode to choose the protocol:

    \b
      a2a       A2A protocol
      adk       ADK SSE (/run_sse, or :streamQuery for Agent Runtime in Gemini platform)
      adk_live  ADK bidi WebSocket (/run_live), for Live and voice agents

    \b
    Local runs default to adk. Use --mode adk_live to talk to a Live (Voice)
    agent, locally or against any deployment target:

    \b
      agents-cli run "What's the weather?" --mode adk_live
      agents-cli run "..." --mode adk_live --url https://my-live-agent.run.app --app-name app

    \b
    Supports --file for multimodal input and --session-id for
    conversation continuity. A local session lives in the running server's
    memory, so keep the server alive with --start-server to resume it
    across separate runs.

    \b
    Binary artifacts (images, audio, files) returned by the agent are
    saved under '.google-agents-cli/artifacts/' in the project root and
    listed in an 'Artifacts:' footer at the end of the response.
    File references returned by URI are not downloaded.
    """
    if url and start_server:
        logging.warning("--start-server has no effect when using --url.")
    if url and otel_to_cloud:
        logging.warning("--otel-to-cloud has no effect when using --url.")

    target = _resolve_dispatch_target(
        url=url,
        mode=mode,
        app_name=app_name,
        custom_headers=custom_headers,
        otel_to_cloud=otel_to_cloud,
        start_server=start_server,
    )
    if url:
        click.echo(f"Querying remote agent: {url} (mode: {target.mode})")

    # A remote session lives on the deployed agent, so its resume command must
    # repeat the routing/auth flags used here
    # Local runs reuse the running server and need nothing extra.
    resume_flags = _build_resume_flags(url, target.mode, app_name, custom_headers)

    # Only tear down a server this invocation started; a reused persistent
    # server (e.g. from --start-server) is left running.
    should_stop_server = not url and not start_server and target.started_server

    # A session can only be resumed if it outlives this run. A remote session
    # lives on the deployed agent; a local one lives in the local server's
    # memory and survives only when that server persists (--start-server, or a
    # reused already-running server). A one-off local server is torn down here,
    # so its session is gone — advertise --start-server instead of a resume
    # command that would 404.
    keep_server = bool(url) or not should_stop_server
    try:
        _dispatch_query(
            service_url=target.service_url,
            message=message,
            files=files,
            headers=target.headers,
            mode=target.mode,
            app_name=target.app_name,
            session_id=session_id,
            verbose=verbose,
            local_server=not url,
            resume_flags=resume_flags,
            keep_server=keep_server,
        )
    except (
        requests.ConnectionError,
        requests.Timeout,
        httpx.TransportError,
    ) as exc:
        if not url:
            # The local server is unreachable or wedged — stop it (even one we
            # reused) so a later retry starts a fresh one.
            should_stop_server = True
            raise
        raise click.ClickException(
            f"Could not reach remote agent at: {url}\n"
            f"  {exc}\n"
            "  Check that the URL is correct and the service is running."
        ) from exc
    finally:
        if should_stop_server:
            # cwd is the project root here (set by _resolve_dispatch_target).
            stop_server(Path.cwd(), pid=target.server_pid)


def _dispatch_query(
    service_url: str,
    message: str,
    files: tuple[str, ...],
    headers: dict,
    *,
    mode: str,
    app_name: str,
    session_id: str | None = None,
    verbose: bool = False,
    local_server: bool = False,
    resume_flags: str = "",
    keep_server: bool = True,
) -> None:
    """Route a query to the right protocol handler.

    Used by both local (localhost ``service_url``, empty ``headers``) and
    remote (deployed URL, auth headers) flows so the two paths can't drift.
    ``mode`` is the resolved ``--mode`` value in both cases.

    Modes:
      - ``a2a``: A2A protocol.
        If the URL points to legacy Agent Runtime, automatically constructs the passthrough URL
        which is used as the base URL for A2A queries (legacy A2A on Reasoning Engine is not supported).

        The remote agent's card location can't be known from a bare ``--url``,
        so both known layouts are probed: ``/a2a/{app_name}`` (Python ADK, which
        namespaces multiple agents) then the base root (Go and the A2A spec's
        canonical location).
      - ``adk``: ADK SSE.  Uses ``:streamQuery`` for legacy Agent Runtime
        URLs, ``/run_sse`` for everything else.
      - ``adk_live``: ADK's ``/run_live`` WebSocket bidi transport
    """
    if mode == MODE_ADK_LIVE:
        _query_adk_live(
            service_url=service_url,
            parts=build_adk_sse_parts(message, files),
            headers=headers,
            app_name=app_name,
            session_id=session_id,
            verbose=verbose,
            local_server=local_server,
            resume_flags=resume_flags,
            keep_server=keep_server,
        )
    elif mode == MODE_A2A:
        if is_legacy_agent_runtime_url(service_url):
            location, runtime_resource = parse_agent_runtime_service_url(service_url)
            service_url = build_agent_runtime_passthrough_url(location, runtime_resource)

        # Probe the namespaced Python path first (when we have an app name),
        # then fall back to the root card served by Go and the A2A spec.
        a2a_bases = []
        if app_name:
            a2a_bases.append(f"{service_url}/a2a/{app_name}")
        a2a_bases.append(service_url)

        _query_a2a(
            a2a_bases=a2a_bases,
            parts=build_a2a_parts(message, files),
            headers=headers,
            session_id=session_id,
            verbose=verbose,
            resume_flags=resume_flags,
            keep_server=keep_server,
        )
    elif mode == MODE_ADK:
        if is_legacy_agent_runtime_url(service_url):
            _query_legacy_agent_runtime_sse(
                service_url=service_url,
                message=build_agent_runtime_message(message, files),
                headers=headers,
                session_id=session_id,
                verbose=verbose,
                resume_flags=resume_flags,
                keep_server=keep_server,
            )
        else:
            _query_adk_sse(
                service_url=service_url,
                parts=build_adk_sse_parts(message, files),
                headers=headers,
                app_name=app_name,
                session_id=session_id,
                verbose=verbose,
                resume_flags=resume_flags,
                keep_server=keep_server,
            )


def _build_resume_flags(
    url: str | None,
    mode: str,
    app_name: str | None,
    custom_headers: tuple[str, ...],
) -> str:
    """Return the flags a resumed remote run needs, with a leading space."""
    if not url:
        return ""
    flags = [f'--url "{url}"', f"--mode {mode}"]
    if app_name:
        flags.append(f'--app-name "{app_name}"')
    flags.extend(f'--header "{header}"' for header in custom_headers)
    return " " + " ".join(flags)


def _print_session_id(
    session_id: str | None, resume_flags: str = "", *, keep_server: bool = True
) -> None:
    """Print the session ID footer."""
    if not session_id:
        return
    click.echo()
    click.secho(f"Session: {session_id}", dim=True)
    if keep_server:
        click.secho(
            f'  Resume with: agents-cli run "<message>"{resume_flags}'
            f" --session-id {session_id}",
            dim=True,
        )
    else:
        click.secho(
            "  One-off session — add --start-server to keep the local server "
            "(and its sessions) alive so you can resume with --session-id.",
            dim=True,
        )


def _print_artifacts(paths: list[str]) -> None:
    """Print an Artifacts footer listing binary artifacts saved to disk."""
    if not paths:
        return
    click.echo()
    click.secho("Artifacts:", dim=True)
    for path in paths:
        click.secho(f"  {path}", dim=True)


def _write_artifact(data: bytes, mime_type: str | None) -> str:
    """Write artifact bytes to the artifacts dir and return a display path."""
    mime = mime_type or "application/octet-stream"
    ext = mimetypes.guess_extension(mime) or ""
    artifacts_dir = Path.cwd() / _ARTIFACTS_DIR
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    path = artifacts_dir / f"{uuid.uuid4().hex[:8]}{ext}"
    path.write_bytes(data)
    # Prefer a relative path so terminals can cmd+click and shells can tab-complete.
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _save_inline_artifact(data_b64: str, mime_type: str | None) -> str | None:
    """Decode URL-safe base64 inline data and save it as an artifact.

    Returns the saved path, or ``None`` if decoding failed.
    """
    mime = mime_type or "application/octet-stream"
    try:
        decoded = base64.urlsafe_b64decode(data_b64)
    except ValueError as exc:
        logging.warning("Could not decode inline %s artifact: %s", mime, exc)
        return None

    return _write_artifact(decoded, mime_type)


def _print_author_tag(author: str | None, last_author: str | None) -> str | None:
    """Print ``[author]:`` tag when author changes. Returns updated last_author."""
    if author and author != last_author:
        if last_author is not None:
            click.echo()
        click.echo(f"[{author}]: ", nl=False)
        return author
    return last_author


def _print_sse_event(
    event: dict,
    last_author: str | None,
    verbose: bool,
    artifacts: list[str],
    streamed: str,
) -> _SseEventResult:
    """Process and print a single SSE/NDJSON/Live event.

    Returns an :class:`_SseEventResult` carrying the updated ``last_author`` and
    whether the event ``rendered`` anything — so the caller can tell a genuinely
    empty turn from one that just streamed nothing renderable. Appends paths of
    any saved binary artifacts to ``artifacts``.
    """
    # A bare top-level {"error": ...} is the last frame ADK sends before closing
    # the stream on a failed run (e.g. the model call was rejected). The run
    # failed, so fail the command rather than report an empty turn.
    fatal_error = event.get("error")
    if fatal_error:
        if last_author is not None:
            click.echo()  # end the partially streamed line before the error
        raise click.ClickException(f"Agent run failed: {fatal_error}")

    rendered = False
    content = event.get("content")
    parts = content.get("parts", []) if isinstance(content, dict) else []

    # ADK closes a run of partial events with a final event holding their
    # whole text (a remote A2A agent's streamed reply comes this way): print
    # only what it adds to the text already streamed.
    text = "".join(part.get("text") or "" for part in parts if not part.get("thought"))
    if event.get("partial"):
        streamed += text
    else:
        if streamed and all(
            part.get("text") and not part.get("thought") for part in parts
        ):
            if text.startswith(streamed):
                parts = [{"text": text[len(streamed) :]}]
            elif streamed.endswith(text):
                # The whole text can leave out what streamed before a tool call.
                parts = []
        streamed = ""

    if parts:
        last_author = _print_author_tag(event.get("author"), last_author)
        for part in parts:
            if _print_sse_part(part, artifacts):
                rendered = True
    else:
        # Render a finished transcript as text; skip other control frames.
        transcript = finished_transcript(event)
        if transcript is not None:
            last_author = _print_author_tag(transcript.author, last_author)
            click.echo(transcript.text, nl=False)
            rendered = True

    # ADK reports a failed turn via errorCode/errorMessage (camelCase over HTTP,
    # snake_case elsewhere) rather than content — surface it instead of dropping
    # it, which used to make an errored run look silently empty. See b/557288939.
    error_message = event.get("errorMessage") or event.get("error_message")
    error_code = event.get("errorCode") or event.get("error_code")
    if error_message or error_code:
        code = f" [{error_code}]" if error_code else ""
        click.secho(f"\n[error{code}]: {error_message or 'unknown error'}", fg="red")
        rendered = True

    if verbose:
        click.echo()
        click.secho(json.dumps(event, indent=2), dim=True)
    return _SseEventResult(last_author, rendered, streamed)


def _create_adk_session(base_url: str, app_name: str, headers: dict) -> str:
    """Create an ADK session, turning HTTP errors into actionable messages."""
    try:
        return create_session(base_url, app_name, _CLI_USER_ID, headers=headers)
    except requests.HTTPError as exc:
        response = exc.response
        status = response.status_code if response is not None else "unknown"
        body = response.text if response is not None else str(exc)
        hint = ""
        if response is not None and response.status_code in (404, 405):
            hint = "\n  If this is an A2A agent, try --mode a2a instead."
        raise click.ClickException(
            f"Failed to create session (HTTP {status}):\n  {body}{hint}"
        ) from exc


def _without_inline_audio(event: dict) -> dict:
    """Drop inline audio parts, which a spoken reply sends by the hundred.

    Saved as artifacts they bury the transcript in a file per chunk, and
    ``mimetypes`` has no extension for any PCM spelling ADK uses.
    """
    content = event.get("content")
    if not isinstance(content, dict):
        return event
    parts = content.get("parts")
    if not parts:
        return event
    kept = [
        part
        for part in parts
        if not (part.get("inlineData") or {}).get("mimeType", "").startswith("audio/")
    ]
    if len(kept) == len(parts):
        return event
    return {**event, "content": {**content, "parts": kept}}


def _query_adk_live(
    service_url: str,
    parts: list[dict],
    headers: dict,
    *,
    app_name: str,
    session_id: str | None = None,
    verbose: bool = False,
    local_server: bool = False,
    resume_flags: str = "",
    keep_server: bool = True,
) -> None:
    """Create a session and stream one Live (bidi) turn over ``/run_live``."""
    endpoints = resolve_agent_endpoints(service_url)
    if not session_id:
        session_id = _create_adk_session(endpoints.http_base, app_name, headers)

    user_text = " ".join(p.get("text", "") for p in parts if "text" in p).strip()
    if user_text:
        click.echo(f"[user]: {user_text}")

    from websockets.exceptions import ConnectionClosedError, InvalidHandshake

    last_author = None
    streamed = ""
    artifacts: list[str] = []
    # Separate from last_author: an author-less content frame prints without
    # updating it, so it can't gate the "no output" note.
    rendered = False
    try:
        for event in stream_live_events(
            endpoints.ws_base,
            app_name,
            session_id,
            user_turns=[{"role": "user", "parts": parts}],
            headers=headers,
            user_id=_CLI_USER_ID,
        ):
            if event is None:
                continue  # turn boundary; a single turn has just the one
            event = _without_inline_audio(event)
            last_author, printed, streamed = _print_sse_event(
                event, last_author, verbose, artifacts, streamed
            )
            rendered = printed or rendered
    except ConnectionClosedError as exc:
        # The close reason carries the real cause
        close = exc.rcvd
        code = close.code if close is not None else "unknown"
        reason = (close.reason if close is not None else "").strip()
        detail = f" {reason}" if reason else ""
        # Only our own server writes that log; a --url one logs elsewhere.
        hint = f"\n  Full traceback in {SERVER_LOG_PATH}." if local_server else ""
        raise click.ClickException(
            f"The Live agent closed the stream with an error "
            f"(WebSocket code {code}):{detail}{hint}"
        ) from exc
    except (OSError, InvalidHandshake) as exc:
        # A rejected handshake (any non-101) is an InvalidHandshake, not an
        # OSError; a refused connection is the reverse.
        raise click.ClickException(
            f"Live WebSocket connection failed: {exc}\n"
            f"  Endpoint: {endpoints.ws_base}{DEFAULT_WS_PATH}"
        ) from exc

    click.echo()
    # A clean turn can render nothing (audio reply, no transcript); say so.
    if not rendered and not artifacts:
        click.secho(
            "Turn completed with no text output. Live agents reply with audio; "
            "a transcript is shown only when the model emits one.",
            dim=True,
        )
    _print_artifacts(artifacts)
    _print_session_id(session_id, resume_flags, keep_server=keep_server)


def _query_adk_sse(
    service_url: str,
    parts: list[dict],
    headers: dict,
    *,
    app_name: str,
    session_id: str | None = None,
    verbose: bool = False,
    resume_flags: str = "",
    keep_server: bool = True,
) -> None:
    """Create a session and stream an SSE response from an ADK FastAPI agent.

    Thin CLI wrapper around :mod:`google.agents.cli._adk_client`: creates a
    session if none is supplied, streams events from ``/run_sse``, and
    renders each event to the terminal as it arrives.
    """
    if not session_id:
        session_id = _create_adk_session(service_url, app_name, headers)

    # Print user message (text part only for display)
    user_text = " ".join(p.get("text", "") for p in parts if "text" in p).strip()
    if user_text:
        click.echo(f"[user]: {user_text}")

    last_author = None
    streamed = ""
    artifacts: list[str] = []
    rendered_any = False
    user_message = {"role": "user", "parts": parts}
    try:
        for event in run_sse(
            service_url,
            app_name,
            session_id,
            user_message=user_message,
            headers=headers,
            user_id=_CLI_USER_ID,
        ):
            last_author, rendered, streamed = _print_sse_event(
                event, last_author, verbose, artifacts, streamed
            )
            rendered_any = rendered_any or rendered
    except requests.HTTPError as exc:
        response = exc.response
        status = response.status_code if response is not None else "unknown"
        body = response.text if response is not None else str(exc)
        # A local resume against a session the server never heard of returns 404
        # (in-memory local sessions don't survive a one-off run or an idle-out).
        # Explain that instead of surfacing a bare "HTTP 404".
        if (
            status == 404
            and session_id
            and (
                service_url.startswith("http://127.0.0.1")
                or service_url.startswith("http://localhost")
            )
        ):
            raise click.ClickException(
                f"Session {session_id} was not found on the local server.\n"
                "  In-memory local sessions don't survive a one-off run (or "
                "after the server idles out).\n"
                "  Start a fresh run without --session-id, or use "
                "--start-server to keep sessions alive across runs."
            ) from exc
        raise click.ClickException(
            f"Failed to run agent (HTTP {status}):\n  {body}"
        ) from exc

    click.echo()
    if not rendered_any:
        click.secho("(no response content)", fg="yellow")
    _print_artifacts(artifacts)
    _print_session_id(session_id, resume_flags, keep_server=keep_server)


def _create_agent_runtime_session(
    service_url: str, headers: dict, user_id: str = _CLI_USER_ID
) -> str:
    resp = requests.post(
        f"{service_url}:query",
        headers=headers,
        json={
            "class_method": "async_create_session",
            "input": {"user_id": user_id},
        },
        timeout=30,
    )
    if not resp.ok:
        raise click.ClickException(
            f"Failed to create Agent Runtime session (HTTP {resp.status_code}):\n"
            f"  {resp.text}"
        )
    session_id = resp.json().get("output", {}).get("id")
    if not session_id:
        raise click.ClickException("Agent Runtime returned a session with no ID.")
    return session_id


def _query_legacy_agent_runtime_sse(
    service_url: str,
    message: str | dict,
    headers: dict,
    *,
    session_id: str | None = None,
    verbose: bool = False,
    resume_flags: str = "",
    keep_server: bool = True,
) -> None:
    """Stream a query to an Agent Runtime via the ``:streamQuery`` HTTP endpoint.

    Uses the ``async_stream_query`` class method.  The response is
    newline-delimited JSON (one JSON object per line).  Creates a session
    via ``async_create_session`` when ``session_id`` is not provided.
    """
    if not session_id:
        session_id = _create_agent_runtime_session(service_url, headers)

    stream_url = f"{service_url}:streamQuery"

    input_payload: dict = {
        "user_id": _CLI_USER_ID,
        "session_id": session_id,
        "message": message,
    }

    payload = {
        "class_method": "async_stream_query",
        "input": input_payload,
    }

    # Print user message
    user_text = (
        message
        if isinstance(message, str)
        else " ".join(
            p.get("text", "") for p in message.get("parts", []) if "text" in p
        ).strip()
    )
    if user_text:
        click.echo(f"[user]: {user_text}")

    with requests.post(
        stream_url, headers=headers, json=payload, stream=True, timeout=120
    ) as resp:
        if not resp.ok:
            hint = ""
            if resp.status_code in (400, 404, 405, 500):
                hint = (
                    "\n  This agent may not support the ADK :streamQuery API."
                    "\n  If this is an A2A agent, try --mode a2a instead."
                )
            raise click.ClickException(
                f"Failed to query Agent Runtime (HTTP {resp.status_code}):\n"
                f"  {resp.text}{hint}"
            )
        last_author = None
        streamed = ""
        artifacts: list[str] = []
        rendered_any = False
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            last_author, rendered, streamed = _print_sse_event(
                event, last_author, verbose, artifacts, streamed
            )
            rendered_any = rendered_any or rendered

    click.echo()
    if not rendered_any:
        click.secho("(no response content)", fg="yellow")
    _print_artifacts(artifacts)
    _print_session_id(session_id, resume_flags, keep_server=keep_server)


def _query_a2a(
    *,
    a2a_bases: list[str],
    parts: list[Part],
    headers: dict,
    session_id: str | None = None,
    verbose: bool = False,
    resume_flags: str = "",
    keep_server: bool = True,
) -> None:
    """Probe candidate A2A base URLs for an agent card, then query the agent.

    ``a2a_bases`` are tried in order; the first that serves a card at
    ``{base}/.well-known/agent-card.json`` is used. If all candidates fail,
    an error is raised listing one line per path tried.
    """
    failures: list[tuple[str, int, str]] = []  # [(path, status, error)]
    for base in a2a_bases:
        card_url = f"{base}{AGENT_CARD_WELL_KNOWN_PATH}"
        resp = httpx.get(card_url, headers=headers, timeout=30)
        if resp.status_code == 200:
            asyncio.run(
                _query_a2a_async(
                    base,
                    parts,
                    headers,
                    session_id=session_id,
                    verbose=verbose,
                    resume_flags=resume_flags,
                    keep_server=keep_server,
                )
            )
            return
        failures.append((card_url, resp.status_code, resp.text))

    detail = "\n".join(
        f"  HTTP {status} from {url}:\n    {text}" for url, status, text in failures
    )
    hint = ""

    if a2a_bases and a2a_bases[-1].rstrip("/").endswith("/a2a"):
        hint += "\n  Pass the base service URL without the '/a2a' suffix."
    if any(status in (404, 405) for _, status, _ in failures):
        hint += "\n  If this is an ADK agent, try --mode adk instead."
    raise click.ClickException(f"Failed to fetch agent card:\n{detail}{hint}")


# Final states that carry no reply. A canceled task is final too, and like a failed
# one usually ends without an artifact, so it would otherwise print nothing.
_A2A_FAILED_STATES = (
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_REJECTED,
    TaskState.TASK_STATE_CANCELED,
)


def _a2a_failure_reason(status: TaskStatus) -> str | None:
    """The agent's reason when an A2A task ended failed, rejected or canceled."""
    if status.state not in _A2A_FAILED_STATES:
        return None
    reason = " ".join(p.text for p in status.message.parts if p.text)
    state = TaskState.Name(status.state).removeprefix("TASK_STATE_").lower()
    return reason or f"task {state}"


async def _query_a2a_async(
    base_url: str,
    parts: list[Part],
    headers: dict,
    *,
    session_id: str | None = None,
    verbose: bool = False,
    resume_flags: str = "",
    keep_server: bool = True,
) -> None:
    """Async implementation — sends a message and prints the response (a2a-sdk 1.0).

    The client is resolved from the reachable ``base_url`` (which may differ from
    the card's advertised URL, e.g. kubectl port-forward / Agent Runtime).
    ``base_url`` — not the card — decides where the message is actually sent:
    see ``_a2a_pinning`` for why a card's self-declared endpoints are not
    trusted with the caller's credentials.
    """
    agent_name = "agent"

    # Print user message (text parts only for display).
    user_text = " ".join(p.text for p in parts if p.text)
    if user_text:
        click.echo(f"[user]: {user_text}")

    req_headers = dict(headers)
    # Note that on the 0.3 legacy compatibility path, this header gets overwritten,
    # so including it should be safely backwards compatible.
    req_headers.setdefault(VERSION_HEADER, PROTOCOL_VERSION_1_0)

    # Headers ride on an origin-pinned auth rather than as client-wide defaults,
    # which httpx would attach to every request no matter which host the agent
    # card sends us to.
    async with httpx.AsyncClient(
        auth=OriginPinnedAuth(req_headers, origin_of(base_url)), timeout=120
    ) as http_client:
        config = ClientConfig(
            httpx_client=http_client,
            # INVARIANT: keep JSONRPC here. The a2a-sdk v0.3 compat transport is
            # JSON-RPC only; dropping it silently breaks consuming v0.3 agents.
            supported_protocol_bindings=[
                TransportProtocol.JSONRPC,
                TransportProtocol.HTTP_JSON,
            ],
        )
        try:
            a2a_client, redirected = await create_pinned_client(base_url, config)
        except Exception as exc:
            raise click.ClickException(
                f"Could not resolve an A2A agent at {base_url}: {exc}\n"
                "  If this is an older agent, upgrade it with "
                "'agents-cli scaffold upgrade', or try --mode adk."
            ) from exc

        if redirected:
            logging.warning(
                "The agent card points at a different host (%s). Sending to %s "
                "instead, as given by --url.",
                ", ".join(redirected),
                base_url,
            )

        msg = Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=parts,
            context_id=session_id or "",
        )

        last_author = None
        response_session_id = None
        artifacts: list[str] = []
        rendered_any = False
        failure_shown = False

        def _render_a2a_parts(render_parts: list[Part]) -> None:
            """Print the agent tag only for parts that actually render, so an
            empty/marker-only chunk doesn't emit a bare "[agent]:" line.
            """
            nonlocal last_author, rendered_any
            for part in render_parts:
                if not _a2a_part_has_content(part):
                    continue
                last_author = _print_author_tag(agent_name, last_author)
                _print_a2a_part(part, artifacts)
                rendered_any = True

        # Text printed so far, per artifact id.
        printed: dict[str, str] = {}

        def _render_artifact_update(update: TaskArtifactUpdateEvent) -> None:
            """Print an artifact chunk; a chunk that replaces printed text (no
            append) prints only what it adds, so a reply streamed in deltas and
            then sent whole shows once.
            """
            render_parts = list(update.artifact.parts)
            artifact_id = update.artifact.artifact_id
            shown = printed.get(artifact_id, "")
            text = "".join(part.text for part in render_parts)
            full = shown + text if update.append else text
            printed[artifact_id] = full
            if not update.append and shown and all(part.text for part in render_parts):
                if full.startswith(shown):
                    render_parts = [Part(text=full[len(shown) :])]
                elif shown.endswith(full):
                    # The whole reply can leave out what streamed before a tool call.
                    render_parts = []
            _render_a2a_parts(render_parts)

        async for chunk in a2a_client.send_message(SendMessageRequest(message=msg)):
            status = None
            if chunk.HasField("artifact_update"):
                if not response_session_id and chunk.artifact_update.context_id:
                    response_session_id = chunk.artifact_update.context_id
                _render_artifact_update(chunk.artifact_update)
            elif chunk.HasField("task"):
                if not response_session_id and chunk.task.context_id:
                    response_session_id = chunk.task.context_id
                for artifact in chunk.task.artifacts:
                    _render_a2a_parts(list(artifact.parts))
                status = chunk.task.status
            elif chunk.HasField("status_update"):
                if not response_session_id and chunk.status_update.context_id:
                    response_session_id = chunk.status_update.context_id
                status = chunk.status_update.status
            elif chunk.HasField("message"):
                _render_a2a_parts(list(chunk.message.parts))

            reason = _a2a_failure_reason(status) if status is not None else None
            if reason and not failure_shown:
                click.secho(f"\n[error]: {reason}", fg="red")
                rendered_any = failure_shown = True

            if verbose:
                click.echo()
                click.secho(str(chunk), dim=True)

    click.echo()
    # Never let a run look like it silently succeeded: if nothing was rendered, say
    # so.
    if not rendered_any:
        click.secho("(no response content)", fg="yellow")
    _print_artifacts(artifacts)
    _print_session_id(response_session_id, resume_flags, keep_server=keep_server)


def _a2a_part_has_content(part: Part) -> bool:
    """True if an A2A part carries something renderable (text, file, data)."""
    return bool(part.text or part.url or part.raw or part.HasField("data"))


def _print_a2a_part(part: Part, artifacts: list[str]) -> None:
    """Print an A2A 1.0 response part. Appends saved artifact paths to ``artifacts``."""
    if part.text:
        click.echo(part.text, nl=False)
    elif part.url:
        click.echo(f"\n[file: {part.url}]", nl=False)
    elif part.raw:
        artifacts.append(_write_artifact(part.raw, part.media_type))
    elif part.HasField("data"):
        click.echo(f"\n{json.dumps(MessageToDict(part.data), indent=2)}", nl=False)


def _print_sse_part(part: dict, artifacts: list[str]) -> bool:
    """Print an ADK SSE response part. Appends saved artifact paths to
    ``artifacts``. Returns True if it rendered something."""
    text = part.get("text")
    if text:
        if part.get("thought"):
            # Extended-thinking models narrate their reasoning as ordinary text
            # parts, so unlabelled it reads as the answer -- and runs together
            # with the answer that follows it.
            click.secho(f"\n[thinking] {text}\n", dim=True, nl=False)
            return True
        click.echo(text, nl=False)
        return True
    inline_data = part.get("inlineData")
    if inline_data:
        path = _save_inline_artifact(inline_data["data"], inline_data.get("mimeType"))
        if path is not None:
            artifacts.append(path)
        # A failed decode still printed a warning, so the turn isn't silently
        # empty — report it as rendered to avoid a redundant "(no response
        # content)" note.
        return True
    file_data = part.get("fileData")
    if file_data:
        uri = file_data.get("fileUri")
        if uri:
            click.echo(f"\n[file: {uri}]", nl=False)
            return True
        return False
    function_call = part.get("functionCall")
    if function_call:
        name = function_call.get("name", "")
        args = function_call.get("args", {})
        click.echo(f"\n[tool_call: {name}({json.dumps(args)})]", nl=False)
        return True
    function_response = part.get("functionResponse")
    if function_response:
        name = function_response.get("name", "")
        response = function_response.get("response", {})
        click.echo(f"\n[tool_response: {name} -> {json.dumps(response)}]", nl=False)
        return True
    return False
