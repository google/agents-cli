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
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import click
from rich.console import Console

from google.agents.cli._project import (
    find_project_root,
    read_project_config,
    require_agent_directory,
)
from google.agents.cli._runner import run
from google.agents.cli.eval import _paths

_INFERENCE_TIMEOUT = 600  # 10 minutes

_INFERENCE_RUNNER = "_inference_runner.py"
_INFERENCE_STAGE_DIR = ".agents-cli-scripts"


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
def cmd_generate(
    dataset: str | None,
    output: str | None,
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

    \b
    Example:
      agents-cli eval generate --dataset eval_cases.json --output artifacts/traces/
    """
    console = Console()
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

    console.print("[bold]Syncing eval dependencies...[/bold]")
    run(
        ["uv", "sync", "--dev", "--extra", "eval"],
        cwd=str(project_root),
        check_err_msg="Failed to sync eval dependencies",
    )

    console.print(f"[bold]Running inference on dataset:[/bold] [cyan]{dataset}[/cyan]")
    console.print(f"[bold]Using agent:[/bold] [cyan]{cfg.agent_directory}[/cyan]")

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
                env={"PYTHONUNBUFFERED": "1"},
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
