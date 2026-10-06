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

"""agents-cli eval grade command — score traces with metrics."""

import glob
import logging
import os
from pathlib import Path

import click

import google.agents.cli._gcp_project as _gcp_project
import google.agents.cli._project as _project
from google.agents.cli._agent_platform import AgentPlatformClient
from google.agents.cli._agent_platform_types import types
from google.agents.cli._output import Console
from google.agents.cli.eval import _paths
from google.agents.cli.eval.eval_utils import (
    load_eval_dotenv,
    prepare_eval_metrics,
    print_results_table,
    resolve_eval_region,
    save_evaluation_artifacts,
)

# The SDK paces every metric computation through one limiter sized for the eval
# service, including local metrics that never call it. This rate replaces it:
# enough to keep a judge that reuses its client busy, low enough that one
# building a client per case still completes.
DEFAULT_QPS = 15.0
# Reused by `eval run`, which forwards the flag.
qps_option = click.option(
    "--qps",
    type=click.FloatRange(min=0, min_open=True),
    default=DEFAULT_QPS,
    show_default=True,
    help=(
        "Metric computations dispatched per second. Raise it when your metrics "
        "are cheap or your judge reuses one client; lower it when the judge "
        "model or the eval service rate-limits you."
    ),
)


def _load_traces_eval_cases(traces_path: str) -> tuple[list, int]:
    """Load and merge evaluation cases from one or more populated trace JSON files."""
    if os.path.isfile(traces_path):
        json_files = [traces_path]
    else:
        json_files = glob.glob(os.path.join(traces_path, "*.json"))

    if not json_files:
        raise click.ClickException(f"No JSON trace files found at: {traces_path}")

    all_eval_cases = []
    for filepath in json_files:
        try:
            with open(filepath, encoding="utf-8") as f:
                dataset_part = types.EvaluationDataset.model_validate_json(f.read())
                if dataset_part.eval_cases:
                    all_eval_cases.extend(dataset_part.eval_cases)
        except Exception as e:
            raise click.ClickException(f"Failed to parse trace file {filepath}.") from e

    if not all_eval_cases:
        raise click.ClickException("No eval_cases found in the provided trace files.")

    return all_eval_cases, len(json_files)


def _warn_on_dropped_cases(result: types.EvaluationResult) -> None:
    """Name the cases that errored, which the mean and the exit status hide."""
    errored = {
        m.metric_name: m.num_cases_error
        for m in result.summary_metrics or []
        if m.num_cases_error
    }
    if not errored:
        return
    logging.warning(
        "Scores average only the cases that graded, excluding: %s. The saved "
        "results carry each error; if they are rate limits, lower --qps.",
        ", ".join(f"{name} ({count})" for name, count in errored.items()),
    )


_print_results_table = print_results_table
_save_evaluation_artifacts = save_evaluation_artifacts


def _drop_content_less_events(eval_cases: list) -> int:
    """Remove events carrying no content, returning how many were dropped.

    The eval service requires ``content`` on every event and fails the whole
    case with ``400 ... Required field is not set`` otherwise. Inference keeps
    actions-only events (a state delta and nothing else), which any ADK agent
    whose callbacks write state emits on every turn, so they are dropped here
    rather than at generate time: the trace on disk stays faithful.
    """
    dropped = 0
    for case in eval_cases:
        for turn in (case.agent_data.turns if case.agent_data else None) or []:
            events = turn.events or []
            kept = [event for event in events if event.content]
            dropped += len(events) - len(kept)
            turn.events = kept
    return dropped


@click.command("grade")
@click.option(
    "--traces",
    "traces_path",
    type=click.Path(file_okay=True, dir_okay=True),
    help=(
        "File or directory of populated traces JSON (output of "
        "`eval generate` or `eval dataset synthesize`)."
    ),
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(file_okay=False, dir_okay=True),
    help="Directory to save evaluation results and artifacts.",
)
@click.option(
    "--metrics",
    "metrics_str",
    help="Comma-separated list of metrics to evaluate (e.g., 'final_response_quality,grounding').",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False),
    help="Path to a JSON or YAML file containing metrics to run and custom metrics configuration.",
)
@click.option(
    "--project",
    default=None,
    help="GCP project ID. Overrides GOOGLE_CLOUD_PROJECT and ADC.",
)
@click.option(
    "--region",
    default=None,
    help="GCP region for the Vertex eval service. Defaults to 'global'.",
)
@qps_option
def cmd_grade(
    *,
    traces_path: str | None = None,
    output_path: str | None = None,
    metrics_str: str | None = None,
    config_path: str | None = None,
    project: str | None = None,
    region: str | None = None,
    qps: float = DEFAULT_QPS,
) -> None:
    """Score populated agent traces against one or more metrics."""
    console = Console()
    project_root = _project.find_project_root()
    # Load the project's .env (same approach as the eval runners) so local,
    # model-calling metrics (e.g. an LLM-judge via google-genai) pick up the
    # configured backend -- GEMINI_API_KEY (AI Studio) or GOOGLE_CLOUD_* (Vertex).
    load_eval_dotenv(project_root or Path.cwd())

    # Default config path in case one is not explicitly supplied
    default_config_path = None

    if not traces_path or not output_path or not config_path:
        try:
            if not project_root:
                raise FileNotFoundError("No pyproject.toml found.")
            cfg = _project.read_project_config(str(project_root))
            _project.require_agent_directory(cfg)
        except Exception as e:
            raise click.ClickException(
                "Must be in a valid agent project directory unless both --traces and --output are specified."
            ) from e

        if not traces_path:
            traces_path = str(_paths.default_traces_dir(project_root))
        if not output_path:
            output_path = str(_paths.default_grade_results_dir(project_root))
        if not config_path:
            config_path = str(_paths.default_eval_config(project_root))
            default_config_path = config_path

    metrics, local_custom_count, remote_custom_count = prepare_eval_metrics(
        config_path=config_path,
        metrics_str=metrics_str,
        default_config_path=default_config_path,
        console=console,
    )

    if local_custom_count:
        console.print(
            f"[yellow]Loaded {local_custom_count} local custom metric(s). "
            "These execute in-process; user-supplied code runs with the "
            "CLI's privileges.[/yellow]"
        )
    if remote_custom_count:
        console.print(
            f"[yellow]Loaded {remote_custom_count} remote custom metric(s) "
            "(CodeExecutionMetric). These run server-side in a Vertex AI "
            "sandbox and require a configured GCP project + region.[/yellow]"
        )

    console.print(f"Loading trace file(s) from [cyan]{traces_path}[/cyan]...")
    all_eval_cases, file_count = _load_traces_eval_cases(traces_path)
    console.print(
        f"Loaded {len(all_eval_cases)} total eval cases from {file_count} file(s)."
    )

    uses_eval_service = len(metrics) > local_custom_count

    metric_names = [m.name if hasattr(m, "name") else str(m) for m in metrics]
    console.print(
        f"Running evaluation for metrics: [cyan]{', '.join(metric_names)}[/cyan] "
        f"at {qps:g}/s (--qps to change)..."
    )

    dropped = _drop_content_less_events(all_eval_cases)
    if dropped:
        console.print(
            f"[dim]Dropped {dropped} event(s) with no content; the eval service "
            "rejects them.[/dim]"
        )

    merged_dataset = types.EvaluationDataset(eval_cases=all_eval_cases)

    try:
        if uses_eval_service:
            resolved_project = _gcp_project.resolve_gcp_project(project, required=True)
            resolved_region = resolve_eval_region(region)
            client = AgentPlatformClient(
                project=resolved_project, location=resolved_region
            )
        else:
            client = AgentPlatformClient(project=None, location=None)
        result = client.evals.evaluate(
            dataset=merged_dataset,
            metrics=metrics,
            config={"evaluation_service_qps": qps},
        )

        _print_results_table(result, console)
        _save_evaluation_artifacts(result, output_path, console)
        # After saving, so the warning can point at the artifacts it mentions.
        _warn_on_dropped_cases(result)

    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException("Evaluation failed.") from e
