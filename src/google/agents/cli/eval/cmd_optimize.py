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

import click

from google.agents.cli._project import (
    chdir_project_root,
    read_project_config,
    require_agent_directory,
)
from google.agents.cli._runner import run
from google.agents.cli.eval.optimize_utils import _prepare_adk_evalsets

_DEFAULT_OPTIMIZATION_CONFIG = "tests/eval/optimization_config.json"


@dataclass
class OptimizationConfigData:
    eval_config: dict
    optimizer_config: dict
    train_dataset: dict
    validation_dataset: dict
    log_level: str
    print_detailed_results: bool


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
        with open(config_path, encoding="utf-8") as f:
            combined_config = json.load(f)
            eval_config = combined_config.get("eval_config", {})
            optimizer_config = combined_config.get("optimizer_config", {})
            train_dataset_path = combined_config.get("train_dataset")
            validation_dataset_path = combined_config.get("validation_dataset")
            log_level = combined_config.get("log_level", "WARNING")
            print_detailed_results = combined_config.get("print_detailed_results", False)

            if train_dataset_path is not None:
                with open(train_dataset_path, encoding="utf-8") as f_in:
                    train_dataset = json.load(f_in)

            if validation_dataset_path is not None:
                with open(validation_dataset_path, encoding="utf-8") as f_in:
                    validation_dataset = json.load(f_in)

    # Target metric resolution
    # 1. CLI --target-metric flag overrides everything
    if target_metric:
        existing_config = eval_config.get("criteria", {}).get(target_metric)
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
        with open(dataset_file, encoding="utf-8") as f:
            train_dataset = json.load(f)
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

        run(args, check_err_msg="Optimization failed")
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
    help="Path to a combined JSON config file for advanced settings.",
)
def cmd_optimize(dataset_file, target_metric, config_path):
    """[Experimental] Optimize agent prompts using the GEPA framework.

    This command runs 'adk optimize' under the hood to automatically improve
    your agent's instructions by iteratively refining the prompt.

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
    - Default Paths: By default, it looks for a JSON config file in
      tests/eval/optimization_config.json.
    """
    logging.warning("`eval optimize` is experimental and may change.")
    chdir_project_root()
    cfg = read_project_config()
    require_agent_directory(cfg)

    agent_path = f"./{cfg.agent_directory}"

    # Sync eval extras
    run(
        ["uv", "sync", "--dev", "--extra", "eval"],
        check_err_msg="Failed to sync eval dependencies",
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
