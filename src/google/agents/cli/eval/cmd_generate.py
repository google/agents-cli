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
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import click
from google.genai import types as genai_types

from google.agents.cli._adk_client import (
    DEFAULT_WS_PATH,
    create_session,
    fetch_app_info,
    run_sse,
)
from google.agents.cli._agent_platform_types import types
from google.agents.cli._modes import MODE_ADK, MODE_ADK_LIVE
from google.agents.cli._output import Console
from google.agents.cli._project import (
    ProjectConfig,
    find_project_root,
    read_project_config,
    require_agent_directory,
)
from google.agents.cli._remote import build_remote_headers, resolve_agent_endpoints
from google.agents.cli.eval import _live, _paths
from google.agents.cli.eval._events import (
    final_response_content_from_events,
    parse_content_event,
    rewrite_model_author_events,
    strip_thought_signatures,
    to_adk_event_payload,
)
from google.agents.cli.run._local_server import ensure_server, stop_server

_DEFAULT_CONCURRENCY = min(32, (os.cpu_count() or 4))

# Default agent app name in the local ADK URL path (/apps/<app-name>/...).
_DEFAULT_APP_NAME = "app"

# Fallback root-agent name for the rewrite_model_author_events rewrite
# when /app-info is unavailable.
_FALLBACK_ROOT_AGENT_NAME = "root_agent"


def split_case_history(
    case: types.EvalCase,
) -> tuple[list[types.evals.AgentEvent], genai_types.Content]:
    """Split a case into prior events and user message to send.

    A state delta on the trailing event is kept as a content-less prior event,
    since the message itself is forwarded as bare content.

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

    prior_events: list[types.evals.AgentEvent] = []
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
    if last.state_delta:
        prior_events.append(last.model_copy(update={"content": None}))
    return prior_events, last.content


def merge_events_into_case(
    case: types.EvalCase,
    new_events: list[types.evals.AgentEvent],
    *,
    agents_map: dict[str, types.evals.AgentConfig],
) -> types.EvalCase:
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
        merged = merged.model_copy(update={"agent_data": types.evals.AgentData(turns=[])})
    agent_data = merged.agent_data
    assert agent_data is not None

    if agents_map:
        agent_data.agents = agents_map

    turns: list[types.evals.ConversationTurn] = agent_data.turns or []
    if turns:
        turns[-1].events = list((turns[-1].events or []) + list(new_events))
    else:
        turns.append(
            types.evals.ConversationTurn(
                turn_index=0, turn_id="turn_0", events=list(new_events)
            )
        )
    agent_data.turns = turns

    final_response = final_response_content_from_events(new_events)
    if final_response is not None:
        if merged.responses is None:
            merged = merged.model_copy(update={"responses": []})
        assert merged.responses is not None
        merged.responses.append(types.ResponseCandidate(response=final_response))

    return merged


def run_case(
    *,
    case: types.EvalCase,
    base_url: str,
    app_name: str,
    headers: dict,
    root_agent_name: str,
    agents_map: dict[str, types.evals.AgentConfig],
    user_id: str = "eval-cli-user",
    live: bool,
) -> tuple[types.EvalCase, str | None]:
    """Run one eval case against a running ADK server.

    Returns (merged_case, None) on success, (original_case, error_msg)
    on any failure. Callers record failures without aborting the whole run.
    """
    rewrite_model_author_events(case, root_agent_name)

    if live:
        return _live.run_case_live(
            case=case,
            base_url=base_url,
            app_name=app_name,
            headers=headers,
            root_agent_name=root_agent_name,
            agents_map=agents_map,
            user_id=user_id,
        )

    # ---- Default transport: POST /run_sse ----
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
            prior_events=[to_adk_event_payload(e) for e in prior_events] or None,
        )
    except Exception as exc:
        return case, f"Session create failed: {type(exc).__name__}: {exc}"

    raw_events: list[dict] = []
    try:
        for event in run_sse(
            base_url,
            app_name,
            session_id,
            user_message=user_message.model_dump(
                exclude_none=True, by_alias=True, mode="json"
            ),
            headers=headers,
            user_id=user_id,
        ):
            raw_events.append(event)
    except Exception as exc:
        return case, f"/run_sse failed: {type(exc).__name__}: {exc}"

    if not raw_events:
        return case, ("Inference returned no agent events.")

    try:
        new_events = [
            event for event in map(parse_content_event, raw_events) if event is not None
        ]
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
        agent_id: types.evals.AgentConfig(
            agent_id=agent_id,
            agent_type="LlmAgent",
            description=info.get("description"),
            instruction=info.get("instruction"),
            tools=info.get("tools"),
            # Accept both snake_case and camelCase keys for cross language compatibility.
            sub_agents=info.get("sub_agents", info.get("subAgents")),
        )
        for agent_id, info in raw_agents.items()
    }

    return root_agent_name, agents_map


def _dispatch_cases(
    *,
    eval_cases: list[types.EvalCase],
    base_url: str,
    app_name: str,
    headers: dict,
    root_agent_name: str,
    agents_map: dict[str, types.evals.AgentConfig],
    concurrency: int,
    live: bool,
) -> tuple[list[types.EvalCase], list[tuple[int, str]]]:
    """Run all eval_cases in parallel over the selected transport.

    Returns (merged_successes, failures) where merged_successes preserves
    input ordering (blanks removed) and failures is a list of
    (case_index, err_msg) tuples. Per-case errors are recorded -- one
    failing case does not abort the run.
    """
    merged: list[types.EvalCase | None] = [None] * len(eval_cases)
    failures: list[tuple[int, str]] = []

    def _submit(
        index: int, case: types.EvalCase
    ) -> tuple[int, types.EvalCase, str | None]:
        merged_case, err = run_case(
            case=case,
            base_url=base_url,
            app_name=app_name,
            headers=headers,
            root_agent_name=root_agent_name,
            agents_map=agents_map,
            live=live,
        )
        return index, merged_case, err

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
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


def _run_cases(
    *,
    console: Console,
    base_url: str,
    app_name: str,
    eval_cases: list[dict],
    output_path: Path,
    concurrency: int,
    custom_headers: tuple[str, ...],
    live: bool,
) -> None:
    """Run inference against a running ADK server.

    Failure contract:
      * all cases succeed -> write artifact, exit 0.
      * some cases succeed -> write artifact with only the successes,
        print a partial-success summary to stderr, exit 0.
      * zero cases succeed -> do not write any artifact, print a failure
        summary to stderr, exit 1.
    """
    headers = build_remote_headers(custom_headers, base_url)
    root_agent_name, agents_map = _resolve_agents_metadata(base_url, app_name, headers)
    console.print(f"[dim]Discovered root_agent_name={root_agent_name}[/dim]")
    if live:
        console.print(
            f"[dim]Transport: Live WebSocket "
            f"({resolve_agent_endpoints(base_url).ws_base}{DEFAULT_WS_PATH}, "
            f"text in, audio + transcript out)[/dim]"
        )

    try:
        typed_cases = [types.EvalCase.model_validate(c) for c in eval_cases]
    except Exception as exc:
        raise click.ClickException(
            f"Dataset contains a malformed eval case: {type(exc).__name__}: {exc}"
        ) from exc

    # Once for the dataset: run_case_live runs per case, on worker threads.
    if live and any(_live.has_authored_agent_turns(c) for c in typed_cases):
        logging.warning(
            "Live eval ignores pre-authored agent replies: the agent generates "
            "every turn over the live session, and they are not seeded as "
            "history. Author user-only turns for live datasets "
            "(see /google-agents-cli-eval)."
        )

    successes, failures = _dispatch_cases(
        eval_cases=typed_cases,
        base_url=base_url,
        app_name=app_name,
        headers=headers,
        root_agent_name=root_agent_name,
        agents_map=agents_map,
        concurrency=concurrency,
        live=live,
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
        f"response). Defaults to '{' or '.join(_paths.DEFAULT_INPUT_DATASETS)}' "
        "(the file scaffolded by `agents-cli create`)."
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
        "omitted, agents-cli runs the agent in a local server. Eval "
        "cases run in parallel."
    ),
)
@click.option(
    "--mode",
    type=click.Choice([MODE_ADK, MODE_ADK_LIVE], case_sensitive=False),
    default=MODE_ADK,
    show_default=True,
    help=(
        "Protocol used to run each case: 'adk' or 'adk_live'. "
        "Works locally and with --url."
    ),
)
@click.option(
    "--app-name",
    default=_DEFAULT_APP_NAME,
    help=(
        "Agent app name to use in the ADK URL path "
        "(/apps/<app-name>/users/...). Only used when --url is set. "
        "Defaults to 'app'."
    ),
)
@click.option(
    "--concurrency",
    type=click.IntRange(min=1),
    default=_DEFAULT_CONCURRENCY,
    show_default="number of CPU cores",
    help=(
        "Number of eval cases dispatched in parallel. Each case runs in its "
        "own session. Defaults to min(32, number of CPU cores), falling back "
        "to 4 if that cannot be determined."
    ),
)
@click.option(
    "--header",
    "-H",
    "custom_headers",
    multiple=True,
    help=(
        "Custom HTTP header (format: 'Key: Value'). Repeatable. Overrides auto-detected auth."
    ),
)
def cmd_generate(
    *,
    dataset: str | None,
    output: str | None,
    url: str | None,
    mode: str,
    app_name: str,
    concurrency: int,
    custom_headers: tuple[str, ...],
):
    """Generate agent traces by running inference over eval cases.

    Reads an evaluation dataset and runs the ADK agent over each case,
    writing populated traces (agent responses + tool calls) ready for
    downstream scoring with `agents-cli eval grade`. Eval cases run in parallel.

    Each eval case must provide one of:
      * a top-level ``prompt`` field (single user message), or
      * ``agent_data`` whose turns end with a user message — for continued
        conversations where the next agent response should be appended
        (the "N+1" pattern).

    By default, tries to run the agent in a local HTTP server (project's `fast_api_app.py` if it exists, or `adk api_server`).
    Pass `--url` to run against an already-running or deployed agent instead.

    For Live (bidi) agents, pass `--mode adk_live` to run each case over ADK's
    `/run_live` WebSocket. Author user-only turns: the agent generates every
    reply. See `/google-agents-cli-eval` for details.

    \b
    Example:
      agents-cli eval generate --dataset eval_cases.json --output artifacts/traces/
      agents-cli eval generate --url https://my-agent.run.app --app-name app
      agents-cli eval generate --mode adk_live
      agents-cli eval generate --mode adk_live --url https://my-live-agent.run.app --app-name app
    """
    console = Console()
    live = mode == MODE_ADK_LIVE
    project_root = find_project_root()
    if not project_root:
        raise click.ClickException(
            "Could not find project root: no pyproject.toml found in the "
            "current directory or any parent."
        )
    cfg = read_project_config(str(project_root))
    require_agent_directory(cfg)

    dataset = _paths.resolve_input_dataset(project_root, dataset)
    if not dataset:
        raise click.ClickException(
            "No --dataset specified and default "
            f"({' or '.join(_paths.DEFAULT_INPUT_DATASETS)}) not found. "
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
        _run_against_remote_server(
            console=console,
            eval_cases=eval_cases,
            dataset=dataset,
            url=url,
            app_name=app_name,
            output_path=output_path,
            concurrency=concurrency,
            custom_headers=custom_headers,
            live=live,
        )
    else:
        _run_against_local_server(
            console=console,
            project_root=project_root,
            cfg=cfg,
            dataset=dataset,
            eval_cases=eval_cases,
            output_path=output_path,
            concurrency=concurrency,
            custom_headers=custom_headers,
            live=live,
        )


def _run_against_remote_server(
    *,
    console: Console,
    eval_cases: list[dict],
    dataset: str,
    url: str,
    app_name: str,
    output_path: Path,
    concurrency: int,
    custom_headers: tuple[str, ...],
    live: bool,
) -> None:
    console.print(f"[bold]Target:[/bold] [cyan]{url}[/cyan]")
    console.print(f"[bold]Running inference on dataset:[/bold] [cyan]{dataset}[/cyan]")
    _run_cases(
        console=console,
        base_url=resolve_agent_endpoints(url).http_base,
        app_name=app_name,
        eval_cases=eval_cases,
        output_path=output_path,
        concurrency=concurrency,
        custom_headers=custom_headers,
        live=live,
    )


def _run_against_local_server(
    *,
    console: Console,
    project_root: Path,
    cfg: ProjectConfig,
    dataset: str,
    eval_cases: list[dict],
    output_path: Path,
    concurrency: int,
    custom_headers: tuple[str, ...],
    live: bool,
) -> None:
    """Run inference against a local ADK server booted for this command."""
    local_app_name = cfg.agent_directory
    console.print(f"[bold]Booting local ADK server (app_name={local_app_name})[/bold]")
    console.print(f"[bold]Running inference on dataset:[/bold] [cyan]{dataset}[/cyan]")
    server_info = ensure_server(
        project_root,
        cfg.agent_directory,
        language=cfg.language,
        use_in_memory_session=False,
    )
    local_url = f"http://127.0.0.1:{server_info.port}"
    try:
        console.print(f"[dim]Server ready at {local_url}[/dim]")
        _run_cases(
            console=console,
            base_url=resolve_agent_endpoints(local_url).http_base,
            app_name=local_app_name,
            eval_cases=eval_cases,
            output_path=output_path,
            concurrency=concurrency,
            custom_headers=custom_headers,
            live=live,
        )
    finally:
        if server_info.started:
            stop_server(project_root, pid=server_info.pid)
