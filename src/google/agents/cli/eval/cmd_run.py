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

"""agents-cli eval run command — chain generate + grade in one step."""

from __future__ import annotations

import click
from rich.console import Console

from google.agents.cli._project import find_project_root
from google.agents.cli.eval import _paths
from google.agents.cli.eval.cmd_generate import cmd_generate
from google.agents.cli.eval.cmd_grade import cmd_grade


@click.command("run")
@click.option(
    "--dataset",
    default=None,
    help=(
        "Path to a JSON dataset file of eval cases ready for inference. "
        "Forwarded to `eval generate`. Defaults to the file scaffolded "
        "by `agents-cli create`."
    ),
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(file_okay=False, dir_okay=True),
    default=None,
    help=(
        "Directory to save final evaluation results and artifacts. "
        "Forwarded to `eval grade`. Defaults to `artifacts/grade_results/`."
    ),
)
@click.option(
    "--metrics",
    "metrics_str",
    default=None,
    help=(
        "Comma-separated list of metrics to evaluate "
        "(e.g., 'final_response_quality,grounding'). Forwarded to `eval grade`."
    ),
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False),
    default=None,
    help=(
        "Path to a JSON or YAML file containing metrics to run and custom "
        "metrics configuration. Forwarded to `eval grade`."
    ),
)
@click.option(
    "--project",
    default=None,
    help="GCP project ID for grading. Overrides GOOGLE_CLOUD_PROJECT and ADC.",
)
@click.option(
    "--region",
    default=None,
    help=(
        "GCP region for the Vertex eval service (grading). Inference reads "
        "GOOGLE_CLOUD_LOCATION from the agent's .env."
    ),
)
@click.option(
    "--url",
    default=None,
    help=(
        "URL of a running ADK agent to run inference against instead of "
        "loading the local project's agent in-process. Forwarded to "
        "`eval generate`."
    ),
)
@click.option(
    "--app-name",
    default=None,
    help=(
        "Agent app name to use in the ADK URL path. Required with --url. "
        "Forwarded to `eval generate`."
    ),
)
def cmd_run(
    *,
    dataset: str | None,
    output_path: str | None,
    metrics_str: str | None,
    config_path: str | None,
    project: str | None,
    region: str | None,
    url: str | None,
    app_name: str | None,
):
    """Chain `eval generate` and `eval grade` in one command.

    Thin alias for the common path: runs inference over the dataset to produce
    traces in the default `artifacts/traces/` directory, then grades those
    traces and writes results.

    For custom intermediate trace locations, use the two-step form
    (`eval generate` then `eval grade`) instead.

    \b
    Example:
      agents-cli eval run --dataset eval_cases.json --metrics final_response_quality
    """
    console = Console()

    generate_cb = cmd_generate.callback
    grade_cb = cmd_grade.callback
    assert generate_cb is not None
    assert grade_cb is not None

    project_root = find_project_root()
    if not project_root:
        raise click.ClickException(
            "Not inside an agents-cli project (no manifest found)."
        )
    traces_file = str(_paths.default_traces_path(project_root))

    console.rule("[bold]Step 1/2: eval generate[/bold]")
    # generate trusts the agent's own .env for GCP config -- project/region are
    # only meaningful for grading (the Vertex eval service), forwarded below.
    generate_cb(
        dataset=dataset,
        output=traces_file,
        url=url,
        app_name=app_name,
    )

    console.rule("[bold]Step 2/2: eval grade[/bold]")
    grade_cb(
        traces_path=traces_file,
        output_path=output_path,
        metrics_str=metrics_str,
        config_path=config_path,
        project=project,
        region=region,
    )
