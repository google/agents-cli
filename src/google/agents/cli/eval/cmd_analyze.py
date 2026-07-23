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

"""agents-cli eval analyze command — analyze failure clusters from results."""

from __future__ import annotations

import json
import logging
import re
import traceback
from pathlib import Path

import click
import vertexai
from rich.console import Console
from rich.table import Table
from vertexai._genai import _evals_visualization

from google.agents.cli._gcp_project import resolve_gcp_project
from google.agents.cli._project import (
    chdir_project_root,
    read_project_config,
    require_agent_directory,
)
from google.agents.cli.eval import _paths

logger = logging.getLogger(__name__)

_ALLOWED_METRICS = ["multi_turn_task_success", "multi_turn_tool_use_quality"]
_ALLOWED_METRICS_PATTERN = re.compile(
    rf"^({'|'.join(_ALLOWED_METRICS)})(_v\d+)?$",
    re.IGNORECASE,
)


@click.command("analyze")
@click.option(
    "--eval-result",
    "eval_result_path",
    type=click.Path(exists=True, dir_okay=False, resolve_path=True),
    required=True,
    help="Required. Path to the evaluation results JSON file.",
)
@click.option(
    "--top-k",
    type=int,
    default=None,
    help="Optional. Maximum number of loss clusters to identify.",
)
@click.option(
    "--metric",
    type=str,
    default=None,
    help="Optional. Evaluation metric name to run analysis against. Currently supported: multi_turn_task_success, multi_turn_tool_use_quality",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, resolve_path=True),
    default=None,
    help="Optional. Path where the results should be saved. Defaults to saving to the 'artifacts' directory.",
)
@click.option(
    "--output-format",
    type=click.Choice(["json", "html"], case_sensitive=False),
    default="json",
    show_default=True,
    help="Format of the saved output file.",
)
@click.option(
    "--project",
    default=None,
    help="GCP project ID. Overrides GOOGLE_CLOUD_PROJECT and ADC.",
)
def cmd_analyze(
    *,
    eval_result_path: str,
    top_k: int | None,
    metric: str | None,
    output_path: str | None,
    output_format: str,
    project: str | None,
):
    """[Experimental] Analyze failure clusters from an evaluation run result JSON file. Results are always saved to a file."""
    logging.warning("`eval analyze` is experimental and may change.")
    if metric and not _ALLOWED_METRICS_PATTERN.match(metric):
        raise click.ClickException(
            f"Unsupported metric: '{metric}'. Allowed metrics are {', '.join(_ALLOWED_METRICS)}."
        )

    chdir_project_root()
    cfg = read_project_config()

    try:
        eval_result_content = Path(eval_result_path).read_text(encoding="utf-8")
        eval_result_data = json.loads(eval_result_content)
        metadata = eval_result_data.get("metadata")
        if isinstance(metadata, dict):
            metadata.pop("dataset", None)
        eval_result = vertexai.types.EvaluationResult.model_validate(eval_result_data)
    except Exception as e:
        raise click.ClickException(
            f"Failed to parse evaluation results JSON file '{eval_result_path}': {e}"
        ) from e

    if not output_path:
        require_agent_directory(cfg)
        output_path = str(
            _paths.timestamped_artifact_path(
                Path(_paths.ARTIFACTS_DIR), "analysis", output_format.lower()
            )
        )

    resolved_project = resolve_gcp_project(project, required=True)
    # The only location supported by the service is global.
    resolved_location = "global"

    try:
        client = vertexai.Client(project=resolved_project, location=resolved_location)
    except Exception as e:
        raise click.ClickException(f"Failed to instantiate Vertex AI Client: {e}") from e

    config = {}
    if top_k is not None:
        config["max_top_cluster_count"] = top_k

    try:
        response = client.evals.generate_loss_clusters(
            eval_result=eval_result,
            metric=metric,
            config=config,
        )
    except Exception as e:
        traceback.print_exc()
        raise click.ClickException(
            f"Failed to execute loss clustering analysis: {e}"
        ) from e

    results = getattr(response, "results", []) or []

    # 1. Save output
    if output_format.lower() == "html":
        try:
            json_str = response.model_dump_json(exclude_none=True)
            # TODO: Use public metthods once SDK exposes them (b/512125999)
            serialized_output = _evals_visualization._get_loss_analysis_html(json_str)
        except Exception as e:
            raise click.ClickException(
                f"Failed to build HTML visualization output: {e}"
            ) from e
    else:
        try:
            serialized_output = response.model_dump_json(indent=2, exclude_none=True)
        except Exception as e:
            raise click.ClickException(
                f"Failed to serialize output JSON string: {e}"
            ) from e

    try:
        Path(output_path).write_text(serialized_output, encoding="utf-8")
    except Exception as e:
        raise click.ClickException(
            f"Failed to write output to '{output_path}': {e}"
        ) from e

    # 2. Always print summary of clusters to console
    console = Console()

    for result in results:
        res_cfg = getattr(result, "config", None)
        metric_name = getattr(res_cfg, "metric", "Unknown") if res_cfg else "Unknown"
        cand_name = getattr(res_cfg, "candidate", "") if res_cfg else ""

        title = f"Analyzed clusters for metric: {metric_name}"
        if cand_name:
            title += f" [{cand_name}]"

        table = Table(title=title, show_header=True, header_style="bold magenta")
        table.add_column("L1 Category", style="cyan")
        table.add_column("L2 Category", style="magenta")
        table.add_column("Count", justify="right", style="green")
        table.add_column("Percentage", justify="right", style="green")
        table.add_column("Description", style="yellow")

        clusters = getattr(result, "clusters", []) or []
        if not clusters:
            console.print(
                f"\n[bold yellow]No failure clusters identified for {metric_name}.[/bold yellow]\n"
            )
            continue

        total_items = sum(getattr(c, "item_count", 0) or 0 for c in clusters)

        for cluster in clusters:
            entry = getattr(cluster, "taxonomy_entry", None)
            l1 = getattr(entry, "l1_category", "General") if entry else "General"
            l2 = getattr(entry, "l2_category", "Cluster") if entry else "Cluster"
            desc = getattr(entry, "description", "") if entry else ""
            count = getattr(cluster, "item_count", 0) or 0
            pct = round((count / total_items) * 100) if total_items > 0 else 0

            table.add_row(l1, l2, str(count), f"{pct}%", desc)

        console.print(table)
        console.print()

    console.print(f"Detailed analysis results saved to [green]{output_path}[/green]")
