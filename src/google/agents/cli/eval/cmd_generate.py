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

"""agents-cli eval generate command — run agent inference over dataset."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from importlib import resources
from pathlib import Path

import click
from google.genai import types as genai_types
from rich.console import Console
from vertexai import types
from vertexai._genai.types import common
from vertexai._genai.types import evals as evals_types

from google.agents.cli._adk_client import create_session, fetch_app_info, run_sse
from google.agents.cli._project import (
    ProjectConfig,
    find_project_root,
    read_project_config,
    require_agent_directory,
)
from google.agents.cli._remote import build_remote_headers
from google.agents.cli._runner import run
from google.agents.cli.eval import _paths

_INFERENCE_TIMEOUT = 600  # 10 minutes

_INFERENCE_RUNNER = "_inference_runner.py"
_INFERENCE_STAGE_DIR = ".agents-cli-scripts"

# Number of eval cases dispatched in parallel by the HTTP path.
_HTTP_CONCURRENCY = 4

# Fallback root-agent name for the rewrite_model_author_events rewrite
# when /app-info is unavailable.
_FALLBACK_ROOT_AGENT_NAME = "root_agent"


def strip_thought_signatures(events: list[evals_types.AgentEvent]) -> None:
    """Remove thought_signature from every event's content parts."""
    for event in events:
        if event.content and event.content.parts:
            for part in event.content.parts:
                part.thought_signature = None


def rewrite_model_author_events(case: common.EvalCase, root_agent_name: str) -> None:
    """Rewrite events with author=='model' to use root_agent_name."""
    if not case.agent_data:
        return
    for turn in case.agent_data.turns or []:
        for event in turn.events or []:
            if event.author == "model":
                event.author = root_agent_name


def final_response_content_from_events(
    events: list[evals_types.AgentEvent],
) -> genai_types.Content | None:
    """Extract the final agent text response from a list of events.

    Walks events in reverse looking for the most recent event whose first
    text-bearing part has a non-empty text. Returns a Content
    ({"role": "model", "parts": [{"text": ...}]}) suitable for
    EvalCase.responses[i].response, or None if no text was found.
    """
    for event in reversed(events):
        if not event.content or not event.content.parts:
            continue
        texts = [p.text for p in event.content.parts if p.text]
        if texts:
            return genai_types.Content(
                role=event.content.role or "model",
                parts=[genai_types.Part(text="".join(texts))],
            )
    return None


def split_case_history(
    case: common.EvalCase,
) -> tuple[list[evals_types.AgentEvent], genai_types.Content]:
    """Split a case into prior events and user message to send.

    Raises ValueError if the case has both `prompt` and non-empty
    `agent_data.turns` (ambiguous), or neither, or if the last
    message in `turns` is not a valid user message.
    """
    turns = (case.agent_data.turns if case.agent_data else None) or []
    if case.prompt and turns:
        raise ValueError(
            "Case has both top-level 'prompt' and agent_data.turns; ambiguous."
        )

    if case.prompt:
        return [], case.prompt

    prior_events: list[evals_types.AgentEvent] = []
    for turn in turns:
        prior_events.extend(turn.events or [])
    if not prior_events or prior_events[-1].author != "user":
        raise ValueError(
            "Case has no user message to send (missing prompt and no trailing "
            "user event in agent_data.turns)."
        )

    last = prior_events.pop()
    if last.content is None:
        raise ValueError("Trailing user event has no content to send to /run_sse.")
    return prior_events, last.content


def merge_events_into_case(
    case: common.EvalCase,
    new_events: list[evals_types.AgentEvent],
    *,
    agents_map: dict[str, evals_types.AgentConfig],
) -> common.EvalCase:
    """Merge new_events (from /run_sse) into case and return a new case.

    new_events must be non-empty.

    Does not mutate case. For multi-turn cases the merged case's last turn
    already carries the seeded user event that produced this response, so
    we extend it with the new agent events; single-turn cases start with
    no turns and get a fresh turn_0.

    Populates `responses` with the final agent text (wrapped in a
    ResponseCandidate) when the agent produced any text.

    Sets agent_data.agents from agents_map when non-empty.
    """
    assert new_events, "merge_events_into_case requires at least one event"

    merged = case.model_copy(deep=True)

    if merged.agent_data is None:
        merged = merged.model_copy(update={"agent_data": evals_types.AgentData(turns=[])})
    agent_data = merged.agent_data
    assert agent_data is not None

    if agents_map:
        agent_data.agents = agents_map

    turns: list[evals_types.ConversationTurn] = agent_data.turns or []
    if turns:
        turns[-1].events = list((turns[-1].events or []) + list(new_events))
    else:
        turns.append(
            evals_types.ConversationTurn(
                turn_index=0, turn_id="turn_0", events=list(new_events)
            )
        )
    agent_data.turns = turns

    final_response = final_response_content_from_events(new_events)
    if final_response is not None:
        if merged.responses is None:
            merged = merged.model_copy(update={"responses": []})
        assert merged.responses is not None
        merged.responses.append(common.ResponseCandidate(response=final_response))

    return merged


def _parse_sse_event(event: dict) -> evals_types.AgentEvent:
    """Parse a ``/run_sse`` event into an ``AgentEvent``, or raise ``ValueError``.

    Raises an exception when the event is unusable:

    * it signals a failure -- event carrying ``errorCode`` / ``errorMessage``
      or a bare top-level ``{"error": ...}`` (the final frame ADK emits before
      closing the stream on a failed run); or
    * it is missing ``author`` and/or ``content``.

    ``AgentEvent`` construction may also raise ``ValueError`` for
    otherwise-malformed content.
    """
    message = event.get("errorMessage") or event.get("error")
    code = event.get("errorCode")
    if message or code:
        detail = message or "unknown error"
        raise Exception(
            f"Agent returned an error: {detail}" + (f" ({code})" if code else "")
        )

    missing = [field for field in ("author", "content") if not event.get(field)]
    if missing:
        raise ValueError(f"Malformed agent event: missing {' and '.join(missing)}.")

    return evals_types.AgentEvent(
        author=event.get("author"), content=event.get("content")
    )


def run_case(
    case: common.EvalCase,
    *,
    base_url: str,
    app_name: str,
    headers: dict,
    root_agent_name: str,
    agents_map: dict[str, evals_types.AgentConfig],
    user_id: str = "eval-cli-user",
) -> tuple[common.EvalCase, str | None]:
    """Run one eval case against a live ADK server over HTTP.

    Returns (merged_case, None) on success, (original_case, error_msg)
    on any failure. Callers record failures without aborting the whole run.
    """
    rewrite_model_author_events(case, root_agent_name)
    try:
        prior_events, user_message = split_case_history(case)
    except Exception as exc:
        return case, str(exc)

    try:
        session_id = create_session(
            base_url,
            app_name,
            user_id,
            headers=headers,
            prior_events=(
                [e.model_dump(exclude_none=True, by_alias=True) for e in prior_events]
                or None
            ),
        )
    except Exception as exc:
        return case, f"Session create failed: {type(exc).__name__}: {exc}"

    raw_events: list[dict] = []
    try:
        for event in run_sse(
            base_url,
            app_name,
            session_id,
            user_message=user_message.model_dump(exclude_none=True, by_alias=True),
            headers=headers,
            user_id=user_id,
        ):
            raw_events.append(event)
    except Exception as exc:
        return case, f"/run_sse failed: {type(exc).__name__}: {exc}"

    if not raw_events:
        return case, ("Inference returned no agent events.")

    try:
        new_events = [_parse_sse_event(e) for e in raw_events]
    except Exception as exc:
        return case, str(exc)
    strip_thought_signatures(new_events)
    return merge_events_into_case(case, new_events, agents_map=agents_map), None


def _resolve_agents_metadata(url: str, app_name: str, headers: dict) -> tuple[str, dict]:
    """Fetch agents metadata from /app-info; warn and use fallbacks if unavailable.

    Returns (root_agent_name, agents_map). On failure both fall back to
    safe defaults (_FALLBACK_ROOT_AGENT_NAME and an empty map).
    """
    try:
        root_agent_name, raw_agents = fetch_app_info(
            base_url=url, app_name=app_name, headers=headers
        )
    except Exception as exc:
        logging.warning(
            "Could not fetch /app-info (%s: %s); traces will omit "
            "agent_data.agents -- grading will degrade.",
            type(exc).__name__,
            exc,
        )
        return _FALLBACK_ROOT_AGENT_NAME, {}

    if not root_agent_name:
        logging.warning(
            "/app-info response missing rootAgentName; falling back to %r.",
            _FALLBACK_ROOT_AGENT_NAME,
        )
        root_agent_name = _FALLBACK_ROOT_AGENT_NAME

    # ADK's /app-info only recurses into LlmAgent sub-agents, so every entry
    # is guaranteed to be an LlmAgent.
    agents_map = {
        agent_id: evals_types.AgentConfig(
            agent_id=agent_id,
            agent_type="LlmAgent",
            description=info.get("description"),
            instruction=info.get("instruction"),
            tools=info.get("tools"),
            sub_agents=info.get("sub_agents"),
        )
        for agent_id, info in raw_agents.items()
    }

    return root_agent_name, agents_map


def _dispatch_cases(
    eval_cases: list[common.EvalCase],
    *,
    url: str,
    app_name: str,
    headers: dict,
    root_agent_name: str,
    agents_map: dict[str, evals_types.AgentConfig],
) -> tuple[list[common.EvalCase], list[tuple[int, str]]]:
    """Run all eval_cases over HTTP in parallel.

    Returns (merged_successes, failures) where merged_successes preserves
    input ordering (blanks removed) and failures is a list of
    (case_index, err_msg) tuples. Per-case errors are recorded -- one
    failing case does not abort the run.
    """
    merged: list[common.EvalCase | None] = [None] * len(eval_cases)
    failures: list[tuple[int, str]] = []

    def _submit(
        index: int, case: common.EvalCase
    ) -> tuple[int, common.EvalCase, str | None]:
        merged_case, err = run_case(
            case,
            base_url=url,
            app_name=app_name,
            headers=headers,
            root_agent_name=root_agent_name,
            agents_map=agents_map,
        )
        return index, merged_case, err

    with ThreadPoolExecutor(max_workers=_HTTP_CONCURRENCY) as pool:
        futures = [pool.submit(_submit, i, case) for i, case in enumerate(eval_cases)]
        for fut in as_completed(futures):
            try:
                index, merged_case, err = fut.result()
            except Exception as exc:
                # run_case swallows per-case errors, but if a worker itself crashes
                # (e.g. OOM) we log and continue.
                failures.append((-1, f"Worker crashed: {type(exc).__name__}: {exc}"))
                continue
            if err is not None:
                failures.append((index, err))
                print(
                    f"[generate] case[{index}] FAILED: {err}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                merged[index] = merged_case
                print(f"[generate] case[{index}] done", flush=True)

    return [c for c in merged if c is not None], failures


def _run_http(
    console: Console,
    url: str,
    app_name: str,
    eval_cases: list[dict],
    output_path: Path,
) -> None:
    """Run inference over HTTP against a running ADK server.

    Failure contract:
      * all cases succeed -> write artifact, exit 0.
      * some cases succeed -> write artifact with only the successes,
        print a partial-success summary to stderr, exit 0.
      * zero cases succeed -> do not write any artifact, print a failure
        summary to stderr, exit 1.
    """
    headers = build_remote_headers((), url)
    root_agent_name, agents_map = _resolve_agents_metadata(url, app_name, headers)
    console.print(f"[dim]Discovered root_agent_name={root_agent_name}[/dim]")

    try:
        typed_cases = [common.EvalCase.model_validate(c) for c in eval_cases]
    except Exception as exc:
        raise click.ClickException(
            f"Dataset contains a malformed eval case: {type(exc).__name__}: {exc}"
        ) from exc

    successes, failures = _dispatch_cases(
        typed_cases,
        url=url,
        app_name=app_name,
        headers=headers,
        root_agent_name=root_agent_name,
        agents_map=agents_map,
    )

    n_cases = len(eval_cases)
    n_succeeded = len(successes)

    if n_succeeded == 0:
        _print_failure_summary(failures, n_cases, n_succeeded)
        click.echo(
            f"No artifact written: 0 of {n_cases} cases produced output.",
            err=True,
        )
        raise click.ClickException(f"Inference failed: 0 of {n_cases} cases succeeded.")

    result = types.EvaluationDataset(eval_cases=successes)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        result.model_dump_json(indent=2, exclude_none=True),
        encoding="utf-8",
    )
    console.print(f"Traces saved to [green]{output_path}[/green]")

    if failures:
        _print_failure_summary(failures, n_cases, n_succeeded)
        click.echo(
            f"Artifact contains only the {n_succeeded} successful "
            f"case(s); {len(failures)} failed case(s) were dropped.",
            err=True,
        )


def _print_failure_summary(
    failures: list[tuple[int, str]], n_cases: int, n_succeeded: int
) -> None:
    """Print a human-readable per-case failure summary to stderr."""
    click.echo("", err=True)
    click.echo(
        f"Inference summary: {n_succeeded}/{n_cases} succeeded, {len(failures)} failed.",
        err=True,
    )
    click.echo("Failed cases:", err=True)
    for case_index, err in failures:
        label = f"case[{case_index}]" if case_index >= 0 else "worker"
        click.echo(f"  - {label}: {err}", err=True)


def _stage_inference_runner(dest_dir: Path) -> Path:
    """Copy the inference runner script into ``dest_dir``.

    The runner is shipped as package data inside agents-cli; this helper
    copies it into the user's project so ``uv run python <path>`` can
    execute it inside the user's virtualenv.

    Returns the destination path of the staged script.
    """
    runner_resource = resources.files("google.agents.cli.eval").joinpath(
        _INFERENCE_RUNNER
    )
    dest_path = dest_dir / _INFERENCE_RUNNER
    with resources.as_file(runner_resource) as src_path:
        shutil.copy2(src_path, dest_path)
    return dest_path


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@click.command("generate")
@click.option(
    "--dataset",
    default=None,
    help=(
        "Path to a JSON dataset file of eval cases ready for inference. "
        "Each case must provide one of: a top-level 'prompt' field "
        "(single user message), or 'agent_data' whose turns end with a "
        "user message (continued conversation; appends the next agent "
        f"response). Defaults to '{_paths.DEFAULT_INPUT_DATASET}' (the "
        "file scaffolded by `agents-cli create`)."
    ),
)
@click.option(
    "--output",
    "-o",
    default=None,
    help=(
        "Output path for the populated traces. If an existing directory "
        "is given, a timestamped file is written inside it; otherwise the "
        "value is treated as a file path. Defaults to a timestamped file "
        f"under '{_paths.ARTIFACTS_DIR}/{_paths.TRACES_SUBDIR}/' so that "
        "`agents-cli eval grade` can consume it directly."
    ),
)
@click.option(
    "--url",
    default=None,
    help=(
        "URL of a running ADK agent to run inference against, e.g. a "
        "deployed Cloud Run / GKE URL or a locally-running server. When "
        "set, agents-cli sends each case to the server over HTTP "
        "(POST /apps/{app}/users/{user}/sessions + POST /run_sse) "
        "instead of loading the agent in-process. Cases run in parallel."
    ),
)
@click.option(
    "--app-name",
    default=None,
    help=(
        "Agent app name to use in the ADK URL path "
        "(/apps/<app-name>/users/...). Required when --url is set. "
        "Ignored otherwise."
    ),
)
def cmd_generate(
    dataset: str | None,
    output: str | None,
    url: str | None,
    app_name: str | None,
):
    """Generate agent traces by running inference over eval cases.

    Reads an evaluation dataset, runs the project's local ADK agent (read from
    `agent_directory` in agents-cli-manifest.yaml) over each eval case, and writes the
    populated traces (agent responses + tool calls) ready for downstream
    scoring with `agents-cli eval grade`.

    Each eval case must provide one of:
      * a top-level ``prompt`` field (single user message), or
      * ``agent_data`` whose turns end with a user message — for continued
        conversations where the next agent response should be appended
        (the "N+1" pattern).

    When ``--url`` is set, cases are sent to the running ADK server over
    HTTP; otherwise the local agent is loaded in-process via a staged
    subprocess runner.

    \b
    Example:
      agents-cli eval generate --dataset eval_cases.json --output artifacts/traces/
      agents-cli eval generate --url https://my-agent.run.app --app-name app
    """
    console = Console()
    if url and not app_name:
        raise click.UsageError(
            "--app-name is required when --url is set. "
            "Pass the server-side ADK app name (typically the directory "
            "the agent was deployed from)."
        )
    project_root = find_project_root()
    if not project_root:
        raise click.ClickException(
            "Could not find project root: no pyproject.toml found in the "
            "current directory or any parent."
        )
    cfg = read_project_config(str(project_root))
    require_agent_directory(cfg)
    agent_path = str((project_root / cfg.agent_directory).resolve())

    if not dataset:
        default_dataset_path = project_root / _paths.DEFAULT_INPUT_DATASET
        if default_dataset_path.exists():
            dataset = str(default_dataset_path)
        else:
            raise click.ClickException(
                "No --dataset specified and default "
                f"({_paths.DEFAULT_INPUT_DATASET}) not found. "
                "Specify --dataset PATH."
            )

    dataset = str(Path(dataset).resolve())

    with open(dataset, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"Dataset file is not valid JSON: {exc}") from exc

    eval_cases = data.get("eval_cases")
    if not eval_cases:
        raise click.ClickException(
            "Dataset must contain a non-empty 'eval_cases' list.\n"
            "  Each eval_case must have either a 'prompt' field or "
            "'agent_data' whose turns end with a user message."
        )

    for i, case in enumerate(eval_cases):
        has_prompt = bool(case.get("prompt"))
        has_agent_data = bool(case.get("agent_data"))
        if not has_prompt and not has_agent_data:
            raise click.ClickException(
                f"eval_cases[{i}] is missing both 'prompt' and 'agent_data'.\n"
                "  Each eval_case must have either:\n"
                "    * a 'prompt' field (single user message), or\n"
                "    * 'agent_data' whose turns end with a user message "
                "(continued conversation)."
            )

    output_path = _paths.resolve_output_path(
        project_root,
        output,
        default_dir=project_root / _paths.ARTIFACTS_DIR / _paths.TRACES_SUBDIR,
        prefix=_paths.TRACES_FILE_PREFIX,
    )

    if url:
        assert app_name is not None
        _run_against_remote_server(
            console=console,
            eval_cases=eval_cases,
            dataset=dataset,
            url=url,
            app_name=app_name,
            output_path=output_path,
        )
    else:
        _run_in_process_evaluation(
            console=console,
            project_root=project_root,
            cfg=cfg,
            agent_path=agent_path,
            dataset=dataset,
            output_path=output_path,
        )


def _run_against_remote_server(
    *,
    console: Console,
    eval_cases: list[dict],
    dataset: str,
    url: str,
    app_name: str,
    output_path: Path,
) -> None:
    console.print(f"[bold]Target:[/bold] [cyan]{url}[/cyan]")
    console.print(f"[bold]Running inference on dataset:[/bold] [cyan]{dataset}[/cyan]")
    _run_http(console, url, app_name, eval_cases, output_path)


def _run_in_process_evaluation(
    *,
    console: Console,
    project_root: Path,
    cfg: ProjectConfig,
    agent_path: str,
    dataset: str,
    output_path: Path,
) -> None:
    # In-process evaluation
    console.print("[bold]Syncing eval dependencies...[/bold]")
    # Capture (hide) the verbose uv output; run() folds it into the error if the
    # sync fails, so the failure reason is still surfaced.
    run(
        ["uv", "sync", "--dev", "--extra", "eval"],
        cwd=str(project_root),
        check_err_msg="Failed to sync eval dependencies",
        capture=True,
        print_cmd=False,
    )

    console.print(f"[bold]Running inference on dataset:[/bold] [cyan]{dataset}[/cyan]")
    console.print(f"[bold]Using agent:[/bold] [cyan]{cfg.agent_directory}[/cyan]")

    # Env for the inference subprocess. TQDM_DISABLE / LITELLM_LOG are already in
    # os.environ (set by eval/__init__.py) and are inherited by the subprocess, so
    # they aren't repeated here. Beyond unbuffering, we add PYTHONWARNINGS: the
    # Vertex eval SDK leaves semaphores for its resource_tracker child to reap at
    # shutdown, so we silence that separate child process's benign "leaked
    # semaphore" warning. Appended so any user-set PYTHONWARNINGS is preserved.
    resource_tracker_filter = "ignore::UserWarning:multiprocessing.resource_tracker"
    existing_warnings = os.environ.get("PYTHONWARNINGS")
    inference_env = {
        "PYTHONUNBUFFERED": "1",
        "PYTHONWARNINGS": (
            f"{existing_warnings},{resource_tracker_filter}"
            if existing_warnings
            else resource_tracker_filter
        ),
    }

    stage_dir = project_root / _INFERENCE_STAGE_DIR
    stage_dir_existed = stage_dir.exists()
    stage_dir.mkdir(exist_ok=True)
    script_path = _stage_inference_runner(stage_dir)
    try:
        try:
            run(
                [
                    "uv",
                    "run",
                    "python",
                    "-u",
                    str(script_path),
                    agent_path,
                    dataset,
                    str(output_path),
                ],
                cwd=str(project_root),
                check_err_msg="Inference failed",
                timeout=_INFERENCE_TIMEOUT,
                env=inference_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise click.ClickException(
                f"Inference timed out after {_INFERENCE_TIMEOUT}s. "
                "The Vertex AI call may be hanging; check "
                "GOOGLE_CLOUD_LOCATION in your .env."
            ) from exc
    finally:
        if not stage_dir_existed:
            try:
                shutil.rmtree(stage_dir)
            except OSError as exc:
                console.print(
                    f"[yellow]Warning:[/yellow] could not clean up stage dir "
                    f"{stage_dir}: {exc}"
                )
        else:
            try:
                script_path.unlink()
            except OSError as exc:
                console.print(
                    f"[yellow]Warning:[/yellow] could not remove staged script "
                    f"{script_path}: {exc}"
                )

    console.print(f"[bold green]Traces saved to:[/bold green] {output_path}")
