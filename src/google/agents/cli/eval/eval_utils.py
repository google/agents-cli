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
import functools
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, get_args

import backoff
import click
import yaml
from agentplatform._genai import _evals_visualization  # noqa: TID251 - b/512125999
from agentplatform._genai._evals_constant import (  # noqa: TID251 - b/512125999
    SUPPORTED_PREDEFINED_METRICS,
)

from google.agents.cli._agent_platform_types import types as vertex_types
from google.agents.cli._output import Console

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


# Status codes the eval service's own client retries (_evals_metric_handlers).
_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
# How a saturated endpoint surfaces once the wrappers are peeled off. Named
# rather than "any OSError", which would also retry a bad hostname.
_TRANSIENT_ERRORS = (ConnectionError, TimeoutError)
# Attempts are the usual limit; the elapsed budget only catches a metric slow
# enough that retrying it would sit on a pool thread for minutes. Sized so a
# judge taking up to ~20s a call still spends all five attempts, and checked
# between attempts, so a slower one overshoots it by a call.
_METRIC_MAX_TRIES = 5
_METRIC_MAX_ELAPSED_SECONDS = 120.0
# Cap on the sleep between attempts, not on the metric itself.
_RETRY_MAX_WAIT_SECONDS = 8.0


def _is_transient(exc: BaseException | None) -> bool:
    """True for failures worth retrying rather than scoring as an error.

    Both links are walked because google-auth chains the cause while httpcore
    severs it with ``raise ... from None``, leaving it on ``__context__``.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, _TRANSIENT_ERRORS):
            return True
        if _status_of(exc) in _RETRYABLE_STATUS_CODES:
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _status_of(exc: BaseException) -> int | None:
    """The HTTP status an exception reports, if it reports one as an int."""
    code = (
        getattr(exc, "code", None)
        or getattr(exc, "status_code", None)
        # requests and httpx keep it on the response they raised for.
        or getattr(getattr(exc, "response", None), "status_code", None)
    )
    return code if isinstance(code, int) else None


def with_transient_retry(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """Retry a local metric's function on transient failures.

    The SDK runs a custom function once and turns any exception into that
    case's result, so a rate-limited judge silently shrinks the scored sample.
    """

    @backoff.on_exception(
        backoff.expo,
        Exception,
        max_value=_RETRY_MAX_WAIT_SECONDS,
        giveup=lambda exc: not _is_transient(exc),
        max_tries=_METRIC_MAX_TRIES,
        max_time=_METRIC_MAX_ELAPSED_SECONDS,
        # Every worker hits the same endpoint, so a fixed schedule re-converges.
        jitter=backoff.full_jitter,
    )
    @functools.wraps(fn)
    def run(instance):
        return fn(instance)

    return run


def _compile_custom_function(source: str, metric_name: str):
    """Compiles a custom_function source string into a local Python callable."""
    namespace: dict = {}
    try:
        exec(compile(source, f"<custom_metric:{metric_name}>", "exec"), namespace)
    except Exception as e:
        raise click.ClickException(
            f"Failed to load custom_function for metric '{metric_name}': {e}"
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


def _defines_own_metric_body(custom_metric: dict[str, Any]) -> bool:
    """True when a `custom_metrics` entry defines its own judge prompt or function.

    An entry that defines neither is a parameterization of the built-in metric it
    names, not a metric of its own.
    """
    return any(
        key in custom_metric
        for key in (
            "prompt_template",
            "custom_function",
            "custom_function_file",
            "remote_custom_function",
        )
    )


def _is_sdk_computed_metric(name: str) -> bool:
    """True for the computation-based metrics the SDK dispatches on the name itself.

    `_transformers.t_metrics` special-cases `exact_match`, `bleu` and `rouge*`
    ahead of the predefined branch, so they work while being absent from
    SUPPORTED_PREDEFINED_METRICS.
    """
    lowered = name.lower()
    return lowered in ("exact_match", "bleu") or lowered.startswith("rouge")


def _resolve_predefined_metric_name(name: str) -> str | None:
    """Resolves a built-in metric name to the version the service registers.

    Every predefined name carries a version suffix, so the exact match only fires
    when the caller already wrote one (`final_response_quality_v1`); otherwise the
    bare name is matched against each registered version.
    """
    lowered = name.lower()
    if lowered in SUPPORTED_PREDEFINED_METRICS:
        return lowered
    matches = [
        match
        for match in (
            re.fullmatch(rf"{re.escape(lowered)}_v(\d+)", candidate)
            for candidate in SUPPORTED_PREDEFINED_METRICS
        )
        if match
    ]
    if not matches:
        return None
    # Candidates are already lowercase, so the match is the registered name.
    return max(matches, key=lambda m: int(m.group(1))).group(0)


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
        overlapping = {
            name
            for name in set(custom_metrics_pool.keys()) & predefined_names
            if _defines_own_metric_body(custom_metrics_pool[name])
            and name.lower() not in SUPPORTED_PREDEFINED_METRICS
        }
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
            if "rubric_group_name" in m_dict:
                raise click.ClickException(
                    f"Custom metric '{m_name}': 'rubric_group_name' is not "
                    "supported. To grade a case's 'rubric_groups', use a managed "
                    "rubric metric (e.g. 'final_response_quality') and select the "
                    "group with 'metric_spec_parameters.rubric_group_key'. To "
                    "judge with your own 'prompt_template', drop "
                    "'rubric_group_name'."
                )
            resolved_builtin = _resolve_predefined_metric_name(m_name)
            if _defines_own_metric_body(m_dict):
                if m_name.lower() == resolved_builtin:
                    raise click.ClickException(
                        f"Custom metric '{m_name}': that name is reserved by the "
                        "eval service, which would ignore your definition. Rename "
                        f"the metric (e.g. 'my_{m_name}')."
                    )
            elif resolved_builtin is None and not _is_sdk_computed_metric(m_name):
                raise click.ClickException(
                    f"Custom metric '{m_name}': needs a 'prompt_template' or "
                    "'custom_function', unless the name is a built-in metric being "
                    "parameterized. Run 'agents-cli eval metric list' for built-in "
                    "names."
                )
            try:
                _resolve_custom_function_file(m_dict, m_name, config_path)
                if "custom_function" in m_dict:
                    execution = m_dict.pop("execution", "local")
                    if execution == "local":
                        fn_value = m_dict["custom_function"]
                        if isinstance(fn_value, str):
                            fn_value = _compile_custom_function(fn_value, m_name)
                        metrics.append(
                            vertex_types.Metric(
                                name=m_name,
                                custom_function=with_transient_retry(fn_value),
                            )
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
                    if resolved_builtin and not _defines_own_metric_body(m_dict):
                        m_dict["name"] = resolved_builtin
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
