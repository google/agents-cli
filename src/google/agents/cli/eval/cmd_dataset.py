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

"""agents-cli eval dataset commands — synthesize evaluation traces."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any

import click
from rich.console import Console

from google.agents.cli._click import LazyGroup
from google.agents.cli._project import (
    find_project_root,
    read_project_config,
    require_agent_directory,
)
from google.agents.cli._runner import run
from google.agents.cli.eval import _paths

_SYNTHESIZE_TIMEOUT = 600  # 10 minutes

# Name of the runner script shipped as package data alongside this module.
# It's copied into a hidden `.agents-cli-scripts/` directory under the
# user's project root and executed via `uv run python` so that it runs
# inside the user's project venv. The staged file is overwritten
# on each run and removed.
_SYNTHESIZE_RUNNER = "_synthesize_runner.py"
_SYNTHESIZE_STAGE_DIR = ".agents-cli-scripts"


def _stage_synthesize_runner(dest_dir: Path) -> Path:
    """Copy the synthesize runner script into ``dest_dir``.

    Loads the script via :mod:`importlib.resources` so it works whether
    agents-cli is installed normally, in editable mode, or as a wheel.

    Args:
        dest_dir: Existing directory to copy the script into.

    Returns:
        Absolute path to the copied script inside ``dest_dir``.
    """
    pkg = resources.files("google.agents.cli.eval")
    src = pkg.joinpath(_SYNTHESIZE_RUNNER)
    dest = dest_dir / _SYNTHESIZE_RUNNER
    with resources.as_file(src) as src_path:
        shutil.copyfile(src_path, dest)
    return dest


@click.group("dataset", cls=LazyGroup)
def dataset_group():
    """[Experimental] Manage evaluation traces."""
    pass


@click.command("synthesize")
@click.option(
    "--count",
    "-n",
    default=3,
    type=click.IntRange(min=1),
    show_default=True,
    help="Number of conversation scenarios to generate.",
)
@click.option(
    "--instruction",
    default=None,
    help=(
        "Natural-language instruction guiding scenario generation. "
        "Example: 'Generate scenarios where the user changes their mind.'"
    ),
)
@click.option(
    "--environment-context",
    default=None,
    help=(
        "Environment context injected into each scenario. "
        "Example: 'Today is Monday. Flights to Paris are available.'"
    ),
)
@click.option(
    "--model",
    default=None,
    help="Optional. Custom model used for scenario generation."
    "Example: gemini-3-flash-preview.",
)
@click.option(
    "--max-turns",
    default=5,
    type=click.IntRange(min=1),
    show_default=True,
    help="Maximum conversation turns per scenario during user simulation.",
)
@click.option(
    "--output",
    "-o",
    default=None,
    help=(
        "Output path for the synthesized traces. If an existing directory "
        "is given, a timestamped file is written inside it; otherwise the "
        "value is treated as a file path. Defaults to a timestamped file "
        f"under '{_paths.ARTIFACTS_DIR}/{_paths.TRACES_SUBDIR}/' so that "
        "`agents-cli eval grade` can consume it directly."
    ),
)
def cmd_synthesize(
    count: int,
    instruction: str | None,
    environment_context: str | None,
    *,
    max_turns: int,
    model: str | None,
    output: str | None,
):
    """[Experimental] Synthesize evaluation traces (eval cases with full agent runs).

    Generates synthetic multi-turn conversations by inspecting the project's
    ADK agent (read from `agent_directory` in agents-cli-manifest.yaml) and running it
    with a model-based user simulator.

    \b
    Examples:
      # Basic — synthesize 3 scenarios with up to 5 turns each:
      agents-cli eval dataset synthesize

      # Custom count, turns, and instruction:
      agents-cli eval dataset synthesize -n 10 --max-turns 8 \\
        --instruction "Scenarios where users change destination"

    \b
    Requires:
      - A local ADK agent project with agents-cli-manifest.yaml
      - Google Cloud authentication (`agents-cli login`)
    """
    logging.warning("`eval dataset synthesize` is experimental and may change.")
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

    config: dict[str, Any] = {"count": count}
    if instruction:
        config["generation_instruction"] = instruction
    if environment_context:
        config["environment_context"] = environment_context
    if model:
        config["model_name"] = model

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

    config_json = json.dumps(config)
    stage_dir = project_root / _SYNTHESIZE_STAGE_DIR
    stage_dir_existed = stage_dir.exists()
    stage_dir.mkdir(exist_ok=True)
    script_path = _stage_synthesize_runner(stage_dir)
    try:
        console.print(
            f"[bold]Synthesizing[/bold] [cyan]{count}[/cyan] scenarios with "
            f"[cyan]{max_turns}[/cyan]-turn user simulation..."
        )
        console.print(f"[bold]Using agent:[/bold] [cyan]{cfg.agent_directory}[/cyan]")
        if instruction:
            console.print(f"[bold]Instruction:[/bold] [cyan]{instruction}[/cyan]")

        try:
            run(
                [
                    "uv",
                    "run",
                    "python",
                    "-u",
                    str(script_path),
                    agent_path,
                    config_json,
                    str(output_path),
                    str(max_turns),
                ],
                cwd=str(project_root),
                check_err_msg="Trace synthesis failed",
                timeout=_SYNTHESIZE_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise click.ClickException(
                f"Trace synthesis timed out after {_SYNTHESIZE_TIMEOUT}s. "
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


dataset_group.add_lazy_command(
    "synthesize",
    "google.agents.cli.eval.cmd_dataset:cmd_synthesize",
    "[Experimental] Synthesize evaluation traces (eval cases with full agent runs).",
)
