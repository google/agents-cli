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

"""Shared utility functions for agents-cli eval commands."""

import datetime
import json
import os
import re
from pathlib import Path
from typing import Any, Literal, get_args

import click
import vertexai._genai.types.common as vertex_types
import yaml
from rich.console import Console
from vertexai._genai import _evals_visualization
from vertexai._genai._evals_constant import SUPPORTED_PREDEFINED_METRICS

Execution = Literal["local", "remote"]

# Vertex eval services support only a subset of GCP regions. Default to
# `global` rather than the project deploy region (which may be unsupported);
# the service rejects an unsupported --region.
DEFAULT_EVAL_REGION = "global"


def load_eval_dotenv(start_dir: Path) -> None:
    """Load the nearest ``.env`` at or above ``start_dir`` into ``os.environ``.

    Mirrors the eval runners' ``_load_agent_dotenv``: scoring must see the same
    config the agent uses -- notably the model backend (``GEMINI_API_KEY`` for
    AI Studio, ``GOOGLE_CLOUD_*`` for Vertex) needed by local model-calling
    metrics (e.g. an LLM-judge via google-genai). Pre-existing OS env vars win
    (``override=False``), matching ADK's ``load_dotenv_for_agent``.
    """
    from dotenv import load_dotenv

    start = start_dir.resolve()
    for folder in (start, *start.parents):
        candidate = folder / ".env"
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return


def resolve_eval_region(region: str | None) -> str:
    """Return the region for Vertex eval-service calls.

    Returns ``region`` if set, otherwise ``global``. The project deploy region
    is not used.
    """
    return region or DEFAULT_EVAL_REGION


def _compile_custom_function(source: str, metric_name: str):
    """Compiles a custom_function source string into a local Python callable."""
    namespace: dict = {}
    try:
        exec(compile(source, f"<custom_metric:{metric_name}>", "exec"), namespace)
    except Exception as e:
        raise click.ClickException(
            f"Failed to compile custom_function for metric '{metric_name}': {e}"
        ) from e
    evaluate_fn = namespace.get("evaluate")
    if not callable(evaluate_fn):
        raise click.ClickException(
            f"Custom metric '{metric_name}' must define a callable named "
            "'evaluate(instance)' in custom_function."
        )
    return evaluate_fn


def _resolve_custom_function_file(
    m_dict: dict, metric_name: str, config_path: str | None
) -> None:
    """Inline a metric's ``custom_function_file`` into ``custom_function``.

    Reads the referenced Python file -- resolved relative to the eval config
    file's directory (absolute paths honored) -- so the rest of the pipeline
    treats it identically to an inline ``custom_function`` (compiled for local
    execution, or uploaded for ``execution: remote``). Mutates ``m_dict``.
    """
    file_ref = m_dict.pop("custom_function_file", None)
    if file_ref is None:
        return
    if "custom_function" in m_dict:
        raise click.ClickException(
            f"Custom metric '{metric_name}': specify either 'custom_function' or "
            "'custom_function_file', not both."
        )
    base_dir = (
        os.path.dirname(os.path.abspath(config_path)) if config_path else os.getcwd()
    )
    path = file_ref if os.path.isabs(file_ref) else os.path.join(base_dir, file_ref)
    if not os.path.isfile(path):
        raise click.ClickException(
            f"Custom metric '{metric_name}': custom_function_file not found: "
            f"{file_ref} (resolved to {path}, relative to the eval config directory)."
        )
    with open(path, encoding="utf-8") as f:
        m_dict["custom_function"] = f.read()


def load_eval_config(config_path: str) -> tuple[list[str], dict]:
    """Helper to load and validate eval configuration from a JSON/YAML file.

    Returns:
        A tuple of (metrics_to_run, dictionary of custom metric definitions mapped by name).
    """
    try:
        with open(config_path, encoding="utf-8") as f:
            content_str = f.read()

        file_extension = os.path.splitext(config_path)[1].lower()
        if file_extension in [".yaml", ".yml"]:
            data = yaml.safe_load(content_str)
        elif file_extension == ".json":
            data = json.loads(content_str)
        else:
            raise click.ClickException(
                f"Unsupported file extension: {file_extension}. Must be .yaml, .yml, or .json"
            )

        if not isinstance(data, dict):
            raise click.ClickException(
                "Configuration file must be a JSON/YAML mapping containing 'custom_metrics' and/or 'metrics_to_run' keys."
            )

        metrics_to_run = data.get("metrics_to_run", [])
        if not isinstance(metrics_to_run, list):
            raise click.ClickException("'metrics_to_run' must be a list of strings.")

        custom_metrics_pool = {}
        raw_custom_list = data.get("custom_metrics", [])
        if not isinstance(raw_custom_list, list):
            raise click.ClickException(
                "'custom_metrics' must be a list of metric objects."
            )

        for m in raw_custom_list:
            if not isinstance(m, dict):
                raise click.ClickException(
                    f"Found non-object item in 'custom_metrics': {m}"
                )
            name = m.get("name")
            if name:
                custom_metrics_pool[name] = m

        return metrics_to_run, custom_metrics_pool

    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(
            f"Failed to load eval configuration from {config_path}"
        ) from e


def prepare_eval_metrics(
    config_path: str | None,
    metrics_str: str | None,
    default_config_path: str | None = None,
    default_metrics: list[str] | None = None,
    console: Console | None = None,
) -> tuple[list[Any], int, int]:
    """Loads configuration and resolves/validates metrics to evaluate.

    Returns:
        A tuple of (metrics, local_custom_count, remote_custom_count).
        ``local_custom_count`` is the number of custom metrics that will
        execute in-process via a compiled ``custom_function``;
        ``remote_custom_count`` is the number that will run server-side
        via ``CodeExecutionMetric``.
    """
    custom_metrics_pool = {}
    metrics_to_run_list = []

    if config_path and os.path.exists(config_path):
        metrics_to_run_list, custom_metrics_pool = load_eval_config(config_path)
    elif config_path and default_config_path and (config_path != default_config_path):
        raise click.ClickException(f"Configuration file not found at {config_path}.")
    elif config_path and not default_config_path:
        raise click.ClickException(f"Configuration file not found at {config_path}.")

    if custom_metrics_pool and console:
        predefined_names = set(SUPPORTED_PREDEFINED_METRICS)
        for p_name in SUPPORTED_PREDEFINED_METRICS:
            base = re.sub(r"_v\d+$", "", p_name)
            predefined_names.add(base)
        overlapping = set(custom_metrics_pool.keys()) & predefined_names
        if overlapping:
            console.print(
                f"[bold yellow]Warning:[/bold yellow] Custom metric [cyan]{', '.join(sorted(overlapping))}[/cyan] shares name with a built-in evaluation metric. The custom definition will override the built-in metric."
            )

    requested_metrics = []
    if metrics_str:
        requested_metrics = [m.strip() for m in metrics_str.split(",") if m.strip()]
    elif metrics_to_run_list:
        requested_metrics = metrics_to_run_list
    elif default_metrics is not None:
        requested_metrics = default_metrics
    else:
        raise click.ClickException(
            "No metrics specified via --metrics, and 'metrics_to_run' is empty or missing from the configuration file."
        )

    metrics = []
    local_custom_count = 0
    remote_custom_count = 0
    for m_name in requested_metrics:
        if m_name in custom_metrics_pool:
            m_dict = dict(custom_metrics_pool[m_name])
            try:
                _resolve_custom_function_file(m_dict, m_name, config_path)
                if "custom_function" in m_dict:
                    execution = m_dict.pop("execution", "local")
                    if execution == "local":
                        fn_value = m_dict["custom_function"]
                        if isinstance(fn_value, str):
                            fn_value = _compile_custom_function(fn_value, m_name)
                        metrics.append(
                            vertex_types.Metric(name=m_name, custom_function=fn_value)
                        )
                        local_custom_count += 1
                    elif execution == "remote":
                        metrics.append(
                            vertex_types.CodeExecutionMetric.model_validate(m_dict)
                        )
                        remote_custom_count += 1
                    else:
                        raise click.ClickException(
                            f"Custom metric '{m_name}': invalid 'execution' "
                            f"value '{execution}'. Expected one of "
                            f"{list(get_args(Execution))}."
                        )
                else:
                    metrics.append(vertex_types.LLMMetric.model_validate(m_dict))
            except click.ClickException:
                raise
            except Exception as e:
                raise click.ClickException(
                    f"Failed to validate custom metric '{m_name}': {e}"
                ) from e
        else:
            metrics.append(m_name)

    return metrics, local_custom_count, remote_custom_count


def print_results_table(result: vertex_types.EvaluationResult, console: Console) -> None:
    """Print the evaluation summary as plain, LLM-friendly key/value text.

    Renders one ``metric:`` block per metric with indented ``property: value``
    lines (no box-drawing) so the output is easy for both humans and coding
    agents to parse from captured CLI output.
    """
    console.print("\n[bold]Evaluation Summary[/bold]")

    metrics_list = getattr(result, "summary_metrics", None)
    if not metrics_list:
        console.print("  (no summary metrics returned)\n")
        return

    if not isinstance(metrics_list, list):
        metrics_list = [metrics_list]

    for metric_result in metrics_list:
        if not metric_result:
            continue

        m_dict = metric_result.model_dump()
        metric_name = m_dict.pop("metric_name", "Unknown")
        console.print(f"\n{metric_name}:", markup=False)

        for key, value in m_dict.items():
            if value is None:
                continue
            formatted_value = f"{value:.4f}" if isinstance(value, float) else str(value)
            console.print(f"  {key}: {formatted_value}", markup=False)

    console.print("")


def save_evaluation_artifacts(
    result: vertex_types.EvaluationResult, output_dir: str, console: Console
) -> None:
    """Creates the artifacts directory and saves JSON/HTML results."""
    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(output_dir, f"results_{timestamp}.json")
    html_path = os.path.join(output_dir, f"results_{timestamp}.html")

    dumpable = result.model_dump(mode="json")
    metadata_dataset = []
    try:
        for evaluation_dataset in result.evaluation_dataset or []:
            rows = _evals_visualization._extract_dataset_rows(evaluation_dataset)
            metadata_dataset.extend(rows)
    except Exception as e:
        console.print(
            f"[yellow]Warning: Could not extract dataset metadata: {e}[/yellow]"
        )

    if not dumpable.get("metadata"):
        dumpable["metadata"] = {}
    dumpable["metadata"]["dataset"] = metadata_dataset

    # Dump results to json
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(dumpable, f, indent=2)
        console.print(
            f"[green]Saved full results to {os.path.abspath(json_path)}[/green]"
        )
    except Exception as dump_err:
        raise click.ClickException("Failed to dump full results to json.") from dump_err

    # Dump HTML results
    try:
        html_content = _evals_visualization._get_evaluation_html(json.dumps(dumpable))

        if html_content:
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(str(html_content))
            console.print(
                f"[green]Saved HTML results to {os.path.abspath(html_path)}[/green]"
            )
        else:
            console.print(
                "[yellow]Warning: Could not generate HTML results "
                "(JSON results were saved).[/yellow]"
            )
    except Exception as e:
        console.print(
            f"[yellow]Warning: Failed to save HTML results: {e} "
            "(JSON results were saved).[/yellow]"
        )
