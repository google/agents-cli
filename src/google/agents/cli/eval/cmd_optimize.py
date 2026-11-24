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

"""agents-cli eval optimize command — optimize agent prompts."""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from google.agents.cli._project import (
    ProjectConfig,
    chdir_project_root,
    read_project_config,
    require_agent_directory,
)
from google.agents.cli._runner import run
from google.agents.cli._uv_index import sync_failure_message, uv_index_env
from google.agents.cli.eval.optimize_utils import _prepare_adk_evalsets
from google.agents.cli.scaffold.utils.language import dispatch_language

_DEFAULT_OPTIMIZATION_CONFIG = "tests/eval/optimization_config.json"


@dataclass
class OptimizationConfigData:
    eval_config: dict
    optimizer_config: dict
    train_dataset: dict
    validation_dataset: dict
    log_level: str
    print_detailed_results: bool


def _read_json(path: str, description: str, hint: str = "") -> dict:
    """Read a JSON file that must hold an object.

    A missing file, bad JSON or a non-object top level raises a ClickException.
    """
    try:
        with open(path, encoding="utf-8") as f:
            content = json.load(f)
    except OSError as e:
        raise click.ClickException(
            f"Couldn't read the {description} '{path}': {e.strerror or e}.{hint}"
        ) from e
    except ValueError as e:  # JSONDecodeError, or a file that isn't UTF-8
        raise click.ClickException(
            f"The {description} '{path}' isn't valid JSON: {e}."
        ) from e
    if not isinstance(content, dict):
        raise click.ClickException(
            f"The {description} '{path}' must be a JSON object, "
            f"got {type(content).__name__}."
        )
    return content


def _load_dataset_entry(value: Any, field_name: str) -> dict | None:
    """Load a config dataset entry given as an inline dict or a JSON file path.

    File paths are relative to the project root.
    """
    if value is None or isinstance(value, dict):
        return value
    if isinstance(value, str):
        return _read_json(
            value,
            f"'{field_name}' file",
            hint="\n  Paths in the optimization config are relative to the project root.",
        )
    raise click.ClickException(
        f"'{field_name}' in the optimization config must be a file path or a JSON "
        f"object, got {type(value).__name__}."
    )


_TYPE_NAMES: dict[type, str] = {
    dict: "a JSON object",
    str: "a string",
    bool: "true or false",
}


def _config_value(
    config: dict, key: str, expected: type, default: Any, label: str = ""
) -> Any:
    """Return config[key], or `default` if it's missing or null.

    Any other value of the wrong type raises a ClickException.
    """
    value = config.get(key)
    if value is None:
        return default
    if not isinstance(value, expected):
        raise click.ClickException(
            f"'{label or key}' in the optimization config must be "
            f"{_TYPE_NAMES[expected]}, got {type(value).__name__}."
        )
    return value


def _load_configs_and_datasets(
    config_path: str | None, dataset_file: str | None, target_metric: str | None
) -> OptimizationConfigData:
    """Consolidates configuration parameters, defaults, and dataset validation."""
    eval_config = {}
    optimizer_config = {}
    train_dataset = None
    validation_dataset = None
    log_level = "WARNING"
    print_detailed_results = False

    if not config_path and os.path.exists(_DEFAULT_OPTIMIZATION_CONFIG):
        config_path = _DEFAULT_OPTIMIZATION_CONFIG

    eval_config = {}
    if config_path:
        combined_config = _read_json(config_path, "optimization config")
        eval_config = _config_value(combined_config, "eval_config", dict, {})
        _config_value(eval_config, "criteria", dict, None, "eval_config.criteria")
        optimizer_config = _config_value(combined_config, "optimizer_config", dict, {})
        log_level = _config_value(combined_config, "log_level", str, "WARNING")
        print_detailed_results = _config_value(
            combined_config, "print_detailed_results", bool, False
        )

        # --dataset overrides the config datasets, so don't load them.
        if not dataset_file:
            train_dataset = _load_dataset_entry(
                combined_config.get("train_dataset"), "train_dataset"
            )
            validation_dataset = _load_dataset_entry(
                combined_config.get("validation_dataset"), "validation_dataset"
            )

    # Target metric resolution
    # 1. CLI --target-metric flag overrides everything
    if target_metric:
        existing_config = (eval_config.get("criteria") or {}).get(target_metric)
        eval_config["criteria"] = {
            target_metric: existing_config if existing_config is not None else 1.0
        }

    # 2. Error if no criteria exists in eval_config
    if not eval_config.get("criteria"):
        raise click.ClickException(
            "No target metric provided. Use --target-metric or specify evaluation criteria in your config file."
        )

    # Dataset resolution
    # 1. CLI --dataset overrides everything
    if dataset_file:
        train_dataset = _read_json(dataset_file, "dataset")
        validation_dataset = None

    # 2. Error if train_dataset is missing
    if not train_dataset:
        raise click.ClickException(
            "No dataset provided. Use --dataset or specify 'train_dataset' in your config file."
        )

    if not validation_dataset:
        validation_dataset = train_dataset
        logging.warning(
            "Validation dataset not explicitly provided. Falling back to using the training dataset for validation. "
        )

    return OptimizationConfigData(
        eval_config=eval_config,
        optimizer_config=optimizer_config,
        train_dataset=train_dataset,
        validation_dataset=validation_dataset,
        log_level=log_level,
        print_detailed_results=print_detailed_results,
    )


def _execute_optimization_run(
    *,
    full_agent_path: str,
    sampler_config: dict,
    optimizer_config: dict,
    log_level: str,
    print_detailed_results: bool,
    rel_train_id: str,
    rel_val_id: str,
) -> None:
    """Builds persistent and temporary evaluation files and launches the ADK engine."""
    tmpdir = os.path.join(full_agent_path, ".tmp")
    os.makedirs(tmpdir, exist_ok=True)

    tmp_sampler_path = os.path.join(tmpdir, "sampler_config.json")
    tmp_optimizer_path = os.path.join(tmpdir, "optimizer_config.json")

    try:
        with open(tmp_sampler_path, "w", encoding="utf-8") as tmp:
            json.dump(sampler_config, tmp)

        args = [
            "uv",
            "run",
            "adk",
            "optimize",
            full_agent_path,
            "--sampler_config_file_path",
            tmp_sampler_path,
            "--log_level",
            log_level,
        ]

        if print_detailed_results:
            args.append("--print_detailed_results")

        if optimizer_config:
            with open(tmp_optimizer_path, "w", encoding="utf-8") as tmp:
                json.dump(optimizer_config, tmp)
            args.extend(["--optimizer_config_file_path", tmp_optimizer_path])

        run(args, env=uv_index_env(Path.cwd()), check_err_msg="Optimization failed")
    finally:
        # Clean up temp configs
        for p in [tmp_sampler_path, tmp_optimizer_path]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

        # Clean up temp evalsets from hidden subdir
        for rel_id in [rel_train_id, rel_val_id]:
            tpath = os.path.join(full_agent_path, rel_id + ".evalset.json")
            if os.path.exists(tpath):
                try:
                    os.remove(tpath)
                except OSError:
                    pass
        # Remove hidden subdir if empty
        if os.path.exists(tmpdir) and not os.listdir(tmpdir):
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass


@click.command("optimize")
@click.option(
    "--dataset",
    "dataset_file",
    required=False,
    type=click.Path(exists=True, dir_okay=False, resolve_path=True),
    help="Path to an EvaluationDataset JSON file. Overrides datasets in the config file.",
)
@click.option(
    "--target-metric",
    "target_metric",
    default=None,
    help="The evaluation metric to optimize for. Overrides evaluation settings in the config file.",
)
@click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, resolve_path=True),
    help="Path to a combined JSON config file for advanced settings.",
)
def cmd_optimize(dataset_file, target_metric, config_path):
    """[Experimental] Optimize agent prompts using the GEPA framework.

    This command runs 'adk optimize' under the hood to automatically improve
    your agent's instructions by iteratively refining the prompt. Supported
    for ADK Python projects only.

    \b
    How it works:
    - --dataset: Path to a JSON file in EvaluationDataset format. If not provided,
      it uses values from your config file.
    - --target-metric: The name of the evaluation metric to optimize for.
      If not provided, it uses values from your config file.
    - --config: Optional JSON file for advanced configuration. It can include:
        - eval_config (ADK EvalConfig)
        - train_dataset (Path to EvaluationDataset JSON file or dict in EvaluationDataset format)
        - validation_dataset (Path to EvaluationDataset JSON file or dict in EvaluationDataset format)
        - optimizer_config (ADK GEPARootAgentPromptOptimizerConfig)
        - log_level (string, e.g., 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'). Default is 'WARNING'.
        - print_detailed_results (boolean). Set to true to enable printing detailed results. Default is false.
      Dataset file paths inside the config are relative to the project root.
    - Default Paths: By default, it looks for a JSON config file in
      tests/eval/optimization_config.json.
    """
    logging.warning("`eval optimize` is experimental and may change.")
    chdir_project_root()
    cfg = read_project_config()
    handler = dispatch_language("eval optimize", LANGUAGE_HANDLERS, cfg.language)
    handler(
        cfg=cfg,
        dataset_file=dataset_file,
        target_metric=target_metric,
        config_path=config_path,
    )


def _optimize_python(
    *,
    cfg: ProjectConfig,
    dataset_file: str | None,
    target_metric: str | None,
    config_path: str | None,
) -> None:
    """Optimize the agent's prompt by running ADK Python's `adk optimize`."""
    require_agent_directory(cfg)

    agent_path = f"./{cfg.agent_directory}"

    # Sync eval extras
    uv_env = uv_index_env(Path.cwd())
    run(
        ["uv", "sync", "--dev", "--extra", "eval"],
        env=uv_env,
        check_err_msg=sync_failure_message("Failed to sync eval dependencies", uv_env),
    )

    opt_data = _load_configs_and_datasets(config_path, dataset_file, target_metric)

    full_agent_path = os.path.abspath(agent_path)
    app_name = os.path.basename(full_agent_path)

    rel_train_id, rel_val_id = _prepare_adk_evalsets(
        opt_data.train_dataset, opt_data.validation_dataset, full_agent_path, app_name
    )

    sampler_config = {
        "eval_config": opt_data.eval_config,
        "app_name": app_name,
        "train_eval_set": rel_train_id,
        "validation_eval_set": rel_val_id,
    }

    _execute_optimization_run(
        full_agent_path=full_agent_path,
        sampler_config=sampler_config,
        optimizer_config=opt_data.optimizer_config,
        log_level=opt_data.log_level,
        print_detailed_results=opt_data.print_detailed_results,
        rel_train_id=rel_train_id,
        rel_val_id=rel_val_id,
    )


# `adk optimize` is only available in ADK Python, so other languages aren't supported yet.
LANGUAGE_HANDLERS = {
    "python": _optimize_python,
    "go": None,
    "java": None,
    "typescript": None,
    "kotlin": None,
}
