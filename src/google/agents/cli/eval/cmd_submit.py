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

import logging

import click
import vertexai
from rich.console import Console
from rich.table import Table
from vertexai._genai.types.common import (
    EvaluationDataset,
)

import google.agents.cli._project as _project
from google.agents.cli.eval import _paths
from google.agents.cli.eval.eval_utils import (
    prepare_eval_metrics,
    print_results_table,
    resolve_eval_region,
    save_evaluation_artifacts,
)


def _get_eval_client(project: str | None, region: str | None) -> vertexai.Client:
    """Resolves GCP project and region, then initializes the Vertex AI Client."""
    resolved_project = _project.resolve_gcp_project(project, required=True)
    return vertexai.Client(project=resolved_project, location=resolve_eval_region(region))


@click.command("submit")
@click.option(
    "--resource-name",
    help="Agent engine resource name (e.g. projects/.../locations/.../reasoningEngines/...).",
)
@click.option(
    "--dataset",
    type=click.Path(exists=True, dir_okay=False),
    required=True,
    help="Path to evaluation dataset file (JSON trace etc.).",
)
@click.option(
    "--dest",
    required=True,
    help="GCS output bucket URI prefix staging path (e.g., gs://my-bucket).",
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
def cmd_submit(
    dataset: str,
    dest: str,
    *,
    resource_name: str | None = None,
    metrics_str: str | None = None,
    config_path: str | None = None,
    project: str | None = None,
    region: str | None = None,
) -> None:
    """[Experimental] Submit an E2E cloud-side evaluation run on Vertex AI Eval Service."""
    logging.warning("`eval submit` is experimental and may change.")
    console = Console()

    client_metrics, _, _ = prepare_eval_metrics(
        config_path=config_path,
        metrics_str=metrics_str,
        default_metrics=["final_response_quality"],
        console=console,
    )

    console.print(f"Loading dataset from [cyan]{dataset}[/cyan]...")
    try:
        with open(dataset, encoding="utf-8") as f:
            data = f.read()
            ds = EvaluationDataset.model_validate_json(data)
    except Exception as e:
        raise click.ClickException("Failed to load evaluation dataset.") from e

    console.print(f"Submitting cloud evaluation run to [cyan]{dest}[/cyan]...")
    try:
        client = _get_eval_client(project, region)
        run_res = client.evals.create_evaluation_run(
            dataset=ds,
            dest=dest,
            metrics=client_metrics,
            agent=resource_name,
        )
        run_name = getattr(run_res, "name", None)
        console.print("\n[green]Evaluation run submitted successfully![/green]")
        if run_name:
            console.print(f"Resource Name: [bold]{run_name}[/bold]")
            console.print(
                f"View results using: [bold]agents-cli eval results --run-id {run_name}[/bold]"
            )
        else:
            console.print("Resource Name: [bold]Unknown[/bold]")
            console.print(
                "[yellow]Could not retrieve the evaluation run resource name. Please check the Google Cloud Console to track the status of the run.[/yellow]"
            )
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException("Failed to submit cloud evaluation run.") from e


@click.command("results")
@click.option(
    "--run-id",
    required=True,
    help="Evaluation run resource ID/name.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(file_okay=False, dir_okay=True),
    help="Directory to save evaluation results and artifacts.",
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
def cmd_results(
    run_id: str,
    output_path: str | None = None,
    project: str | None = None,
    region: str | None = None,
) -> None:
    """[Experimental] Fetch results from a completed cloud evaluation run."""
    logging.warning("`eval results` is experimental and may change.")
    console = Console()
    console.print(f"Retrieving results for evaluation run [cyan]{run_id}[/cyan]...")
    try:
        client = _get_eval_client(project, region)
        run = client.evals.get_evaluation_run(name=run_id, include_evaluation_items=True)

        table = Table(
            title="Evaluation Run Status",
            show_header=True,
            header_style="bold magenta",
        )
        table.add_column("Property", style="cyan")
        table.add_column("Value", style="green")

        properties = {
            "Name": getattr(run, "name", "N/A"),
            "Display Name": getattr(run, "display_name", "N/A"),
            "State": getattr(run, "state", "N/A"),
        }
        for k, v in properties.items():
            table.add_row(str(k), str(v))

        console.print("\n", table, "\n")

        results_obj = getattr(run, "evaluation_item_results", None)
        if results_obj and hasattr(results_obj, "evaluation_dataset"):
            for ds in getattr(results_obj, "evaluation_dataset", []):
                if hasattr(ds, "eval_dataset_df"):
                    ds.eval_dataset_df = None

        results_obj = getattr(run, "evaluation_item_results", None)
        if results_obj:
            if not output_path:
                project_root = _project.find_project_root()
                if not project_root:
                    raise click.ClickException(
                        "Must be in a valid agent project directory unless --output is specified."
                    )
                output_path = str(_paths.default_grade_results_dir(project_root))

            save_evaluation_artifacts(results_obj, output_path, console)
            print_results_table(results_obj, console)
        else:
            state = getattr(run, "state", "UNKNOWN")
            console.print(
                f"[yellow]Run does not have summary metrics yet (current state: {state}).[/yellow]"
            )
            console.print(
                f"If the run is still in progress, check back later using: [bold]agents-cli eval results --run-id {run_id}[/bold]"
            )

    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException("Failed to retrieve evaluation run results.") from e
