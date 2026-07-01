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

import logging
import os
import pathlib
import shlex
import subprocess
import sys
import tomllib
from typing import Any

import click
from packaging import version as pkg_version
from rich.console import Console
from rich.prompt import IntPrompt, Prompt

from google.agents.cli._project import (
    ProjectConfig,
    find_project_config,
)
from google.agents.cli._runner import run_resolved
from google.agents.cli._tools import ToolNotFoundError, require_tool

from ..utils.backup import create_project_backup
from ..utils.generation_metadata import metadata_to_cli_args
from ..utils.language import (
    find_agent_file,
    get_agent_file_hint,
    get_language_config,
    validate_agent_file,
)
from ..utils.logging import display_welcome_banner
from ..utils.merge import run_three_way_merge
from ..utils.template import (
    get_available_agents,
    get_deployment_targets,
    load_template_config,
    prompt_cicd_runner_selection,
    prompt_deployment_target,
    prompt_session_type_selection,
    resolve_agent_alias,
    validate_agent_directory_name,
)
from ..utils.upgrade import update_acli_metadata
from ..utils.version import get_current_version
from .create import (
    create,
    get_available_base_templates,
    shared_template_options,
    validate_base_template,
)

console = Console()

# Environment variable names for saved config handling
_ENV_USING_SAVED_CONFIG = "_ACLI_USING_SAVED_CONFIG"
_ENV_SKIP_VERSION_LOCK = "ACLI_SKIP_VERSION_LOCK"

# Directories to exclude when scanning for agent directories
_EXCLUDED_DIRS = {
    ".git",
    ".github",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "build",
    "dist",
    ".terraform",
}


def _should_skip_config_value(value: Any) -> bool:
    """Check if a config value should be skipped (empty, none, skip, etc.)."""
    return value is None or value is False or str(value).lower() in ("none", "skip", "")


def build_args_from_config(
    project_config: ProjectConfig,
    auto_approve: bool = False,
    cli_overrides: dict[str, str] | None = None,
) -> list[str]:
    """Build CLI arguments from project config.

    Args:
        project_config: The ProjectConfig object
        auto_approve: If True, add --auto-approve to args
        cli_overrides: Additional CLI args to merge (e.g., from original command)

    Returns:
        List of CLI arguments to pass to enhance command
    """
    # --skip-deps is added because dependencies were already installed on first run
    # --skip-welcome avoids showing the banner twice
    args = ["scaffold", "enhance", "--skip-deps", "--skip-welcome"]

    # Pass through auto-approve if it was set on the original command
    if auto_approve:
        args.append("--auto-approve")

    # Add saved config options (single source of truth for how to convert them)
    args.extend(metadata_to_cli_args(project_config, for_enhance=True))

    # Merge CLI overrides (these take precedence over saved config)
    # This ensures user-provided args like --cicd-runner are passed through
    if cli_overrides:
        for arg_name, value in cli_overrides.items():
            # Convert to CLI format
            cli_arg = f"--{arg_name.replace('_', '-')}"
            # Remove existing arg if present (to override)
            # Find and remove any existing occurrence
            i = 0
            while i < len(args):
                if args[i] == cli_arg:
                    # Remove the arg and its value if present
                    args.pop(i)
                    if i < len(args) and not args[i].startswith("--"):
                        args.pop(i)
                else:
                    i += 1
            # Add the override
            if value is True:
                args.append(cli_arg)
            elif value is not False and value is not None:
                args.extend([cli_arg, str(value)])

    return args


def get_display_params_from_config(project_config: ProjectConfig) -> dict[str, Any]:
    """Extract display-worthy parameters from project config.

    Args:
        project_config: The ProjectConfig object

    Returns:
        Dict of parameter names to values for display
    """
    display_params: dict[str, Any] = {}

    # Add top-level config values
    base_template = project_config.base_template
    if base_template:
        display_params["base_template"] = base_template

    agent_directory = project_config.agent_directory
    if agent_directory:
        display_params["agent_directory"] = agent_directory

    acli_version = project_config.acli_version
    if acli_version:
        display_params["acli_version"] = acli_version

    # Add create_params
    create_params = project_config.create_params
    for key, value in create_params.items():
        if _should_skip_config_value(value):
            continue
        display_params[key] = value

    return display_params


def _display_saved_config(
    display_params: dict[str, Any],
    project_version: str | None,
    current_version: str,
    use_different_version: bool,
) -> None:
    """Display detected saved configuration to the user."""
    console.print()
    console.print("📋 [bold]Detected saved configuration from previous setup:[/bold]")
    console.print()
    for key, value in display_params.items():
        display_key = key.replace("_", " ").title()
        console.print(f"   • {display_key}: [cyan]{value}[/cyan]")

    if use_different_version and project_version:
        console.print()
        console.print(
            f"   • Version: [cyan]{project_version}[/cyan] (current: {current_version})"
        )
    console.print()


def _should_use_different_version(
    project_version: str | None, current_version: str
) -> bool:
    """Determine if we need to switch to a different ACLI version."""
    skip_version_lock = os.environ.get(_ENV_SKIP_VERSION_LOCK) == "1"
    return (
        not skip_version_lock
        and bool(project_version)
        and current_version != "0.0.0"
        and project_version != current_version
    )


def _ensure_uvx_available(project_version: str) -> None:
    """Ensure uvx is installed, exit with instructions if not."""
    try:
        require_tool("uvx")
    except ToolNotFoundError:
        console.print(
            f"❌ Project requires agents-cli version {project_version}, "
            "but 'uvx' is not installed",
            style="bold red",
        )
        console.print(
            "💡 Install uv to use version-locked projects:",
            style="bold blue",
        )
        console.print("   curl -LsSf https://astral.sh/uv/install.sh | sh")
        console.print(
            "   OR visit: https://docs.astral.sh/uv/getting-started/installation/"
        )
        sys.exit(1)


def _execute_with_saved_config(
    args: list[str], project_version: str | None, use_different_version: bool
) -> bool:
    """Execute enhance command with saved config args.

    Returns:
        True if execution succeeded, False otherwise
    """
    if use_different_version and project_version:
        console.print(
            f"📦 Using agents-cli version {project_version}...",
            style="dim",
        )
        _ensure_uvx_available(project_version)
        cmd = ["uvx", f"google-agents-cli@{project_version}", *args]
    else:
        console.print("✅ Using saved configuration", style="dim")
        # Reinvoke ourselves via the module entry point: always importable when
        # the CLI is installed, regardless of how it was originally launched.
        cmd = [sys.executable, "-m", "google.agents.cli.main", *args]

    logging.debug(f"Executing command: {shlex.join(cmd)}")

    # Set env var to prevent infinite loop in nested execution
    env = os.environ.copy()
    env[_ENV_USING_SAVED_CONFIG] = "1"

    try:
        run_resolved(cmd, check=True, env=env)
        return True
    except subprocess.CalledProcessError as e:
        if use_different_version:
            console.print(
                f"❌ Failed to execute with locked version {project_version}: {e}",
                style="bold red",
            )
            console.print(
                "⚠️  Continuing with current version, but compatibility is not guaranteed",
                style="yellow",
            )
        else:
            console.print(
                f"❌ Failed to execute with saved config: {e}",
                style="bold red",
            )
        return False


def check_and_execute_with_saved_config(
    *,
    project_dir: pathlib.Path,
    auto_approve: bool = False,
    cli_overrides: dict[str, Any] | None = None,
    force: bool = False,
    dry_run: bool = False,
    interactive: bool = False,
) -> bool | dict[str, Any]:
    """Check for saved config and offer to reuse it.

    If config is found, displays it to the user and asks whether to use it.
    If yes, executes enhance with the saved parameters.
    If user chooses "customize", returns interactive overrides dict.

    Args:
        project_dir: Path to the project directory
        auto_approve: If True, skip confirmation prompt and use saved config
        cli_overrides: CLI args to pass through (e.g., cicd_runner from original command)
        force: If True, include --force in subprocess args (skipped for old versions)
        dry_run: If True, include --dry-run in subprocess args (skipped for old versions)
        interactive: If True, show interactive prompts

    Returns:
        True if config was used and executed successfully.
        False if no saved config found or execution failed.
        dict if user chose "customize" — contains only the changed parameters.
    """
    # Skip if already executing with saved config (prevents infinite loop)
    if os.environ.get(_ENV_USING_SAVED_CONFIG) == "1":
        return False

    project_config = find_project_config(project_dir)
    if not project_config:
        return False

    display_params = get_display_params_from_config(project_config)
    if not display_params:
        return False

    current_version = get_current_version()
    project_version = project_config.acli_version
    use_different_version = _should_use_different_version(
        project_version, current_version
    )

    # Show detected configuration
    _display_saved_config(
        display_params, project_version, current_version, use_different_version
    )

    if interactive:
        # Always go through interactive customization so the user can
        # configure params they haven't set yet (e.g., cicd_runner).
        # Pressing Enter on every prompt keeps current values.
        return _prompt_customize_overrides(project_config)

    # non-interactive mode: use saved config as-is via subprocess
    args = build_args_from_config(project_config, auto_approve, cli_overrides)
    # --force and --dry-run were introduced in this version; strip them
    # when re-executing against an older locked version to avoid crashes.
    is_older_version = (
        use_different_version
        and bool(project_version)
        and pkg_version.parse(project_version) < pkg_version.parse(current_version)
    )
    if not is_older_version:
        if force:
            args.append("--force")
        if dry_run:
            args.append("--dry-run")
    return _execute_with_saved_config(args, project_version, use_different_version)


def _prompt_customize_overrides(project_config: ProjectConfig) -> dict[str, Any]:
    """Prompt user to customize project settings interactively.

    Uses the same rich numbered menus as the create command. Shows each
    configurable parameter with the current saved value as the default
    selection. Only shows options that are valid for the selected agent's
    template (e.g., Go agents don't have session_type).

    Args:
        project_config: The saved ProjectConfig object

    Returns:
        Dict of only the changed parameter names to new values
    """
    overrides: dict[str, Any] = {}

    # 1. Agent selection
    base_template = project_config.base_template
    new_agent = display_base_template_selection(base_template)
    if new_agent != base_template:
        overrides["base_template"] = new_agent
    effective_agent = new_agent

    # Re-load template config for the (potentially new) agent
    available_targets = get_deployment_targets(effective_agent)
    requires_session = False
    try:
        template_path = (
            pathlib.Path(__file__).parent.parent
            / "agents"
            / effective_agent
            / ".template"
        )
        template_config = load_template_config(template_path)
        requires_session = template_config.get("settings", {}).get(
            "requires_session", False
        )
    except Exception:
        pass

    # 2. Deployment target
    current_deployment = project_config.deployment_target or "cloud_run"
    if available_targets and len(available_targets) > 1:
        new_deployment = prompt_deployment_target(
            effective_agent, default_value=current_deployment
        )
        if new_deployment != current_deployment:
            overrides["deployment_target"] = new_deployment
    elif available_targets and len(available_targets) == 1:
        new_deployment = available_targets[0]
        if new_deployment != current_deployment:
            overrides["deployment_target"] = new_deployment
    else:
        new_deployment = current_deployment

    # 3. Session type — only for cloud_run AND agents that support sessions
    effective_deployment = overrides.get("deployment_target", current_deployment)
    if effective_deployment == "cloud_run" and requires_session:
        current_session = project_config.session_type or "in_memory"
        new_session = prompt_session_type_selection(default_value=current_session)
        if new_session != current_session:
            overrides["session_type"] = new_session

    # 4. CI/CD runner
    current_cicd = project_config.cicd_runner or "skip"
    new_cicd = prompt_cicd_runner_selection(default_value=current_cicd)
    if new_cicd != current_cicd:
        overrides["cicd_runner"] = new_cicd

    console.print()
    return overrides


def display_base_template_selection(current_base: str) -> str:
    """Display available base templates and prompt for selection."""
    agents = get_available_agents()

    if not agents:
        raise click.ClickException("No base templates available")

    console.print()
    console.print("🔧 [bold]Base Template Selection[/bold]")
    console.print()
    console.print(f"Your project currently inherits from: [cyan]{current_base}[/cyan]")
    console.print("Available base templates:")

    # Create a mapping of choices to agent names
    template_choices = {}
    choice_num = 1
    current_choice = None

    for agent in agents.values():
        template_choices[choice_num] = agent["name"]
        if agent["name"] == current_base:
            console.print(
                f"  {choice_num}. [bold cyan]{agent['name']}[/]"
                f" [dim]{agent['description']}[/]"
                "  [dim cyan](current)[/]"
            )
            current_choice = choice_num
        else:
            console.print(
                f"  [dim]{choice_num}. {agent['name']} - {agent['description']}[/]"
            )
        choice_num += 1

    if current_choice is None:
        current_choice = 1

    console.print()
    choice = IntPrompt.ask(
        "Select base template", default=current_choice, show_default=True
    )

    if choice in template_choices:
        return template_choices[choice]
    else:
        raise ValueError(f"Invalid base template selection: {choice}")


def display_agent_directory_selection(
    current_dir: pathlib.Path, detected_directory: str, base_template: str | None = None
) -> str:
    """Display available directories and prompt for agent directory selection."""
    while True:
        console.print()
        console.print("📁 [bold]Agent Directory Selection[/bold]")
        console.print()
        console.print("Your project needs an agent directory containing:")
        console.print(
            "  • [cyan]agent.py[/cyan] with [cyan]root_agent[/cyan] variable, or"
        )
        console.print("  • [cyan]root_agent.yaml[/cyan] (YAML config agent)")
        console.print()
        console.print("Choose where your agent code is located:")

        # Get all directories in the current path (excluding hidden and common non-agent dirs)
        available_dirs = [
            item.name
            for item in current_dir.iterdir()
            if (
                item.is_dir()
                and not item.name.startswith(".")
                and item.name not in _EXCLUDED_DIRS
            )
        ]

        # Sort directories and create choices
        available_dirs.sort()

        directory_choices = {}
        choice_num = 1
        default_choice = None

        # Only include the detected directory if it actually exists
        if detected_directory in available_dirs:
            directory_choices[choice_num] = detected_directory
            current_indicator = (
                " (detected)" if detected_directory != "app" else " (default)"
            )
            console.print(
                f"  {choice_num}. [bold]{detected_directory}[/]{current_indicator}"
            )
            default_choice = choice_num
            choice_num += 1
            # Remove from available_dirs to avoid duplication
            available_dirs.remove(detected_directory)

        # Add other available directories
        for dir_name in available_dirs:
            directory_choices[choice_num] = dir_name
            # Check if this directory might contain agent code
            hint = get_agent_file_hint(current_dir / dir_name, base_template)
            console.print(f"  {choice_num}. [bold]{dir_name}[/]{hint}")
            if (
                default_choice is None
            ):  # If no detected directory exists, use first available as default
                default_choice = choice_num
            choice_num += 1

        # Add option for custom directory
        custom_choice = choice_num
        directory_choices[custom_choice] = "__custom__"
        console.print(f"  {custom_choice}. [bold]Enter custom directory name[/]")

        # If no directories found and no default set, default to custom option
        if default_choice is None:
            default_choice = custom_choice

        console.print()
        choice = IntPrompt.ask(
            "Select agent directory", default=default_choice, show_default=True
        )

        if choice in directory_choices:
            selected = directory_choices[choice]
            if selected == "__custom__":
                console.print()
                while True:
                    custom_dir = Prompt.ask(
                        "Enter custom agent directory name", default=detected_directory
                    )
                    try:
                        validate_agent_directory_name(custom_dir)
                        return custom_dir
                    except ValueError as e:
                        console.print(f"[bold red]Error:[/] {e}", style="bold red")
                        console.print("Please try again with a valid directory name.")
            else:
                # Validate existing directory selection as well
                try:
                    validate_agent_directory_name(selected)
                    return selected
                except ValueError as e:
                    console.print(f"[bold red]Error:[/] {e}", style="bold red")
                    console.print(
                        "This directory cannot be used as an agent directory. Please select another option."
                    )
                    console.print()
                    # Continue the loop to re-prompt without recursion
                    continue
        else:
            console.print(
                f"[bold red]Error:[/] Invalid selection: {choice}", style="bold red"
            )
            console.print("Please choose a valid option from the list.")
            console.print()
            # Continue the loop to re-prompt without recursion
            continue


def _build_enhance_create_args(
    project_config: ProjectConfig,
    cli_overrides: dict[str, Any] | None = None,
) -> list[str]:
    """Build CLI args for create command from project config and enhance overrides.

    Merges saved metadata with any CLI overrides provided by the user.

    Args:
        project_config: The saved ProjectConfig object
        cli_overrides: CLI args from the enhance command to merge in

    Returns:
        List of CLI arguments for the create command
    """
    # Start with metadata-based args
    args = metadata_to_cli_args(project_config)

    if not cli_overrides:
        return args

    # Merge CLI overrides (these take precedence over saved config).
    # ``deployment_target`` is exempt from the skip-sentinel check because
    # users legitimately switch targets via ``enhance --deployment-target X``
    # and the value happens to share a name with sentinel-skip patterns
    # used elsewhere (e.g. "none", "skip").
    for key, value in cli_overrides.items():
        if key != "deployment_target" and _should_skip_config_value(value):
            continue

        # base_template maps to --agent in the create command
        if key == "base_template":
            arg_name = "--agent"
        else:
            arg_name = f"--{key.replace('_', '-')}"

        # Remove existing arg if present (to override)
        while arg_name in args:
            i = args.index(arg_name)
            # Remove the arg name
            args.pop(i)
            # If it had a value (i.e., the next item is not a flag), remove that too
            if i < len(args) and not args[i].startswith("--"):
                args.pop(i)

        # Add the override
        if value is True:
            args.append(arg_name)
        elif value is not False and value is not None:
            args.extend([arg_name, str(value)])

    # Strip --session-type when deploying to agent_runtime (it handles sessions internally)
    if "--deployment-target" in args:
        dt_idx = args.index("--deployment-target")
        if dt_idx + 1 < len(args) and args[dt_idx + 1] == "agent_runtime":
            while "--session-type" in args:
                i = args.index("--session-type")
                args.pop(i)
                if i < len(args) and not args[i].startswith("--"):
                    args.pop(i)

    return args


def _stale_manifest_keys_for_target(
    cli_overrides: dict[str, Any],
    project_config: ProjectConfig,
) -> list[str]:
    """Return manifest keys that should be removed for the resolved target.

    agent_runtime handles sessions through Agent Platform internally, so any
    stored session_type is meaningless there. cloud_run and gke both consume
    session_type in their templates, so the value must carry through for them.
    """
    effective_deployment = cli_overrides.get(
        "deployment_target",
        project_config.deployment_target,
    )
    if effective_deployment == "agent_runtime":
        return ["session_type"]
    return []


def _backfill_create_params_from_config(
    current_dir: pathlib.Path,
    cli_params: dict[str, Any],
) -> dict[str, Any]:
    """Merge CLI create_params with saved project config, filling in gaps.

    Args:
        current_dir: Project directory to read config from
        cli_params: Create param names to CLI-provided values (None if not provided)

    Returns:
        Dict with None values filled from saved create_params. CLI values
        always take precedence.
    """
    config = find_project_config(current_dir)
    if not config:
        return cli_params

    saved = config.create_params
    if not saved:
        return cli_params

    result = cli_params.copy()
    for key in result:
        if result[key] is None and key in saved and saved[key] is not None:
            # "none"/"skip" are sentinel values, except for deployment_target
            if key != "deployment_target" and _should_skip_config_value(saved[key]):
                continue
            result[key] = saved[key]

    # agent_runtime handles sessions via Vertex AI Session Service
    if result.get("deployment_target") == "agent_runtime":
        result["session_type"] = None

    return result


def _run_smart_merge(
    *,
    project_dir: pathlib.Path,
    project_config: ProjectConfig,
    cli_overrides: dict[str, Any] | None,
    auto_approve: bool,
    dry_run: bool,
    prefer_new: bool = False,
    interactive: bool = False,
) -> bool:
    """Run smart-merge using 3-way comparison.

    Generates the "old" template (what was originally generated) and the "new"
    template (with enhance params), then does a 3-way comparison to only
    overwrite files the user hasn't modified.

    Args:
        project_dir: Path to the current project
        project_config: Saved ProjectConfig object from metadata
        cli_overrides: CLI arguments from the enhance command
        auto_approve: If True, auto-apply non-conflicting changes
        dry_run: If True, preview changes without applying
        prefer_new: If True, resolve conflicts in favor of new template

    Returns:
        True if smart-merge completed successfully, False otherwise
    """
    project_name = project_config.project_name or project_dir.name
    agent_directory = project_config.agent_directory
    language = project_config.language

    # Build args for the "old" template (original generation params)
    old_args = metadata_to_cli_args(project_config)

    # Build args for the "new" template (with enhance overrides merged)
    new_args = _build_enhance_create_args(project_config, cli_overrides)

    # -- Pre-apply hook: back up the project before writing changes ----------
    def _backup(proj_dir: pathlib.Path) -> bool:
        try:
            create_project_backup(
                proj_dir,
                console=console,
                auto_approve=auto_approve,
                interactive=interactive,
            )
            return True
        except click.Abort:
            return False  # user cancelled

    # -- Post-apply hook: update manifest with new config --------------------
    def _update_metadata(proj_dir: pathlib.Path, lang: str) -> None:
        if not cli_overrides:
            return
        metadata_updates = {
            k: v
            for k, v in cli_overrides.items()
            if isinstance(v, str)
            and (k == "deployment_target" or not _should_skip_config_value(v))
        }
        stale_keys = _stale_manifest_keys_for_target(cli_overrides, project_config)

        if metadata_updates or stale_keys:
            update_acli_metadata(
                proj_dir,
                metadata_updates,
                acli_version=get_current_version(),
                language=lang,
                remove_keys=stale_keys or None,
            )

    return run_three_way_merge(
        project_dir=project_dir,
        project_name=project_name,
        agent_directory=agent_directory,
        language=language,
        old_args=old_args,
        new_args=new_args,
        auto_approve=auto_approve,
        dry_run=dry_run,
        prefer_new=prefer_new,
        interactive=interactive,
        operation_label="enhancement",
        pre_apply_hook=_backup,
        post_apply_hook=_update_metadata,
    )


@click.command()
@click.pass_context
@click.argument(
    "template_path",
    type=click.Path(path_type=pathlib.Path),
    default=".",
    required=False,
)
@click.option(
    "--name",
    "-n",
    help="Project name for templating (defaults to current directory name)",
)
@shared_template_options
@click.option(
    "--adk",
    is_flag=True,
    help="Shortcut for --base-template adk",
    default=False,
)
@click.option(
    "--force",
    is_flag=True,
    help="Force overwrite all files (skip smart-merge comparison)",
    default=False,
)
@click.option(
    "--dry-run",
    "--dryrun",
    is_flag=True,
    help="Preview changes without applying them (requires saved metadata)",
    default=False,
)
@click.option(
    "--prefer-new",
    is_flag=True,
    help="Resolve conflicts in favor of the new template version",
    default=False,
)
@click.option(
    "--skip-welcome",
    is_flag=True,
    hidden=True,
    help="Skip the welcome banner (used by nested commands)",
    default=False,
)
def enhance(
    ctx: click.Context,
    template_path: pathlib.Path,
    *,
    name: str | None,
    deployment_target: str | None,
    cicd_runner: str | None,
    prototype: bool,
    session_type: str | None,
    debug: bool,
    auto_approve: bool,
    interactive: bool,
    region: str,
    skip_checks: bool,
    skip_deps: bool,
    agent_garden: bool,
    base_template: str | None,
    adk: bool,
    force: bool,
    dry_run: bool,
    prefer_new: bool,
    agent_directory: str | None,
    skip_welcome: bool = False,
    bq_analytics: bool = False,
    agent_guidance_filename: str = "GEMINI.md",
) -> None:
    """Enhance your existing project with deployment, CI/CD, or RAG scaffolding.

    Applies agents-cli templates in-place to an existing project directory,
    adding infrastructure files without touching your agent logic.

    Run from inside your project directory (pass . as the path) or point to it
    explicitly. Use --dry-run to preview changes before applying them.
    """

    # Display welcome banner for enhance command (unless skipped by nested command)
    if not skip_welcome:
        display_welcome_banner(enhance_mode=True, quiet=auto_approve)

    # Check for saved config and offer to reuse it
    # This handles both version locking AND reusing previous settings
    current_dir = pathlib.Path.cwd()

    # Build CLI overrides from explicitly provided args to pass through
    cli_override_args: dict[str, Any] = {}
    if cicd_runner:
        cli_override_args["cicd_runner"] = cicd_runner
    if deployment_target:
        cli_override_args["deployment_target"] = deployment_target
    if session_type:
        cli_override_args["session_type"] = session_type
    if base_template:
        cli_override_args["base_template"] = base_template
    if agent_directory:
        cli_override_args["agent_directory"] = agent_directory
    if prototype:
        cli_override_args["prototype"] = prototype
    if agent_guidance_filename != "GEMINI.md":
        cli_override_args["agent_guidance_filename"] = agent_guidance_filename

    # Smart-merge is the default when saved config exists (unless --force).
    # Skip if running in subprocess with saved config (subprocess re-execution
    # replays the same params, so smart-merge would compare identical templates).
    is_saved_config_subprocess = os.environ.get(_ENV_USING_SAVED_CONFIG) == "1"
    has_cli_overrides = any(
        k == "deployment_target" or not _should_skip_config_value(v)
        for k, v in cli_override_args.items()
    )

    if dry_run and force:
        console.print(
            "[bold red]Error:[/bold red] --dry-run is not compatible with --force mode."
        )
        return

    if not force and not is_saved_config_subprocess:
        project_config = find_project_config(current_dir)
        if project_config:
            # Determine overrides source: CLI flags or interactive customize
            overrides: dict[str, Any] | None = None
            if has_cli_overrides:
                overrides = cli_override_args
            elif interactive:
                # Show saved config, prompt y/customize
                saved_config_result = check_and_execute_with_saved_config(
                    project_dir=current_dir,
                    auto_approve=auto_approve,
                    cli_overrides=cli_override_args,
                    dry_run=dry_run,
                    interactive=interactive,
                )
                if saved_config_result is True:
                    return  # "y" → subprocess executed
                elif isinstance(saved_config_result, dict):
                    overrides = saved_config_result
                    # Even if no overrides (user kept all defaults), run
                    # smart-merge so the file comparison is displayed.
                # saved_config_result is False → no saved config (shouldn't happen
                # since we already checked project_config above)
            else:
                # auto_approve with no CLI overrides
                if dry_run:
                    # Can't do anything non-interactively without overrides
                    console.print(
                        "[bold red]Error:[/bold red] --dry-run requires specifying what to change "
                        "(e.g. --deployment-target cloud_run) or interactive customization."
                    )
                    return
                # Use saved config subprocess
                if check_and_execute_with_saved_config(
                    project_dir=current_dir,
                    auto_approve=auto_approve,
                    cli_overrides=cli_override_args,
                    interactive=interactive,
                ):
                    return

            # Run smart-merge: with overrides if provided, or same-config
            # comparison if user kept all defaults (overrides is empty dict)
            effective_overrides = overrides if overrides else None
            if _run_smart_merge(
                project_dir=current_dir,
                project_config=project_config,
                cli_overrides=effective_overrides,
                auto_approve=auto_approve,
                dry_run=dry_run,
                prefer_new=prefer_new,
                interactive=interactive,
            ):
                return
            # If smart-merge returned False, fall through to brute-force
            console.print(
                "[yellow]⚠️  Smart-merge failed, falling back to standard mode.[/yellow]"
            )
        elif dry_run:
            console.print(
                "[bold red]Error:[/bold red] --dry-run requires saved project metadata "
                "(agents-cli-manifest.yaml file)."
            )
            return
        elif has_cli_overrides:
            console.print(
                "[dim]No saved metadata found - using standard overwrite mode.[/dim]"
            )
    else:
        # --force or subprocess re-execution
        if not is_saved_config_subprocess:
            # --force: try saved config subprocess
            saved_config_result = check_and_execute_with_saved_config(
                project_dir=current_dir,
                auto_approve=auto_approve,
                cli_overrides=cli_override_args,
                force=force,
                interactive=interactive,
            )
            if saved_config_result is True:
                return
            # If customize dict returned here (force mode), ignore it —
            # force means brute-force overwrite
        elif not force:
            # Subprocess re-execution without --force: route through smart-merge
            # so file changes are displayed and confirmation is asked
            project_config = find_project_config(current_dir)
            if project_config:
                if _run_smart_merge(
                    project_dir=current_dir,
                    project_config=project_config,
                    cli_overrides=None,
                    auto_approve=auto_approve,
                    dry_run=False,
                    prefer_new=prefer_new,
                    interactive=interactive,
                ):
                    return

    # Setup debug logging if enabled
    if debug:
        logging.basicConfig(level=logging.DEBUG, force=True)
        console.print("> Debug mode enabled")
        logging.debug("Starting enhance command in debug mode")

    # Default cicd_runner to "skip" for non-interactive invocation
    if not interactive and not cicd_runner:
        if auto_approve:
            console.print(
                "[yellow]Warning: --cicd-runner not specified with --auto-approve. "
                "Defaulting to 'skip'. Use --cicd-runner to configure CI/CD.[/yellow]"
            )
        cicd_runner = "skip"

    # Handle --adk shortcut
    if adk:
        if base_template:
            raise click.ClickException(
                "Cannot use --adk with --base-template. Use one or the other."
            )
        base_template = "adk"

    # Resolve base template aliases (backwards compatibility)
    base_template = resolve_agent_alias(base_template)

    # Validate base template if provided
    if base_template and not validate_base_template(base_template):
        available_templates = get_available_base_templates()
        console.print(
            f"Error: Base template '{base_template}' not found.", style="bold red"
        )
        console.print(
            f"Available base templates: {', '.join(available_templates)}",
            style="yellow",
        )
        return

    # Determine project name
    if name:
        project_name = name
    else:
        # Use current directory name as default
        current_dir = pathlib.Path.cwd()
        project_name = current_dir.name
        console.print(
            f"Using current directory name as project name: {project_name}", style="dim"
        )

    # Show confirmation prompt for enhancement only in interactive mode
    if interactive:
        current_dir = pathlib.Path.cwd()
        console.print()
        console.print(
            "🚀 [blue]Ready to enhance your project with deployment capabilities[/blue]"
        )
        console.print(f"📂 {current_dir}")
        console.print()
        console.print("[bold]What will happen:[/bold]")
        console.print("• New template files will be added to this directory")
        console.print("• Your existing files will be preserved")
        console.print("• A backup will be created in ~/.agents-cli/backups/")
        console.print()

        if not click.confirm(
            f"Continue with enhancement? {click.style('[Y/n]: ', fg='blue', bold=True)}",
            default=True,
            show_default=False,
        ):
            console.print("✋ [yellow]Enhancement cancelled.[/yellow]")
            return
        console.print()

    # Determine agent specification based on template_path
    if template_path == pathlib.Path("."):
        # Current directory - use local@ syntax
        agent_spec = "local@."
    elif template_path.is_dir():
        # Other local directory
        agent_spec = f"local@{template_path.resolve()}"
    else:
        # Assume it's an agent name or remote spec
        agent_spec = str(template_path)

    # Show base template inheritance info early for local projects
    if agent_spec.startswith("local@"):
        from ..utils.remote_template import (
            get_base_template_name,
            load_remote_template_config,
        )

        # Prepare CLI overrides for base template and agent directory
        cli_overrides: dict[str, Any] = {}
        if base_template:
            cli_overrides["base_template"] = base_template
        if agent_directory:
            cli_overrides["settings"] = cli_overrides.get("settings", {})
            cli_overrides["settings"]["agent_directory"] = agent_directory

        # Load config from current directory for inheritance info
        current_dir = pathlib.Path.cwd()
        source_config = load_remote_template_config(current_dir, cli_overrides)
        original_base_template_name = get_base_template_name(source_config)

        # Interactive base template selection if not provided via CLI and in interactive mode
        if not base_template and interactive:
            selected_base_template = display_base_template_selection(
                original_base_template_name
            )
            # Always set base_template to the selected value (even if unchanged)
            base_template = selected_base_template
            if selected_base_template != original_base_template_name:
                # Update CLI overrides with the selected base template
                cli_overrides["base_template"] = selected_base_template
                # Preserve agent_directory override if it was set
                if agent_directory:
                    cli_overrides["settings"] = cli_overrides.get("settings", {})
                    cli_overrides["settings"]["agent_directory"] = agent_directory
                console.print(
                    f"✅ Selected base template: [cyan]{selected_base_template}[/cyan]"
                )
                console.print()
        elif not base_template:
            # Auto-select the detected base template in non-interactive mode
            base_template = original_base_template_name

        # Reload config with potential base template override
        if cli_overrides.get("base_template"):
            source_config = load_remote_template_config(current_dir, cli_overrides)

        base_template_name = get_base_template_name(source_config)

        # Show current inheritance info
        if interactive or base_template:
            console.print()
            console.print(
                f"Template inherits from base: [cyan][link=https://github.com/google/agents-cli/tree/main/agents/{base_template_name}]{base_template_name}[/link][/cyan]"
            )
            console.print()

    # Validate project structure when using current directory template
    if template_path == pathlib.Path("."):
        current_dir = pathlib.Path.cwd()

        # Detect if this is a Go, Java, or TypeScript project from base_template or config
        is_go_project = base_template and base_template.endswith("_go")
        is_java_project = base_template and base_template.endswith("_java")
        is_ts_project = base_template and base_template.endswith("_ts")
        acli_config = find_project_config(current_dir)
        if acli_config:
            if acli_config.language == "go":
                is_go_project = True
            elif acli_config.language == "java":
                is_java_project = True
            elif acli_config.language == "typescript":
                is_ts_project = True

        # Determine agent directory: CLI param > config detection > language default
        if is_go_project:
            detected_agent_directory = "agent"
        elif is_java_project:
            detected_agent_directory = "src/main/java"
        else:
            detected_agent_directory = "app"
        if not agent_directory:  # Only try to detect if not provided via CLI
            # First check .acli.toml/pyproject.toml config
            config_agent_dir = acli_config.agent_directory if acli_config else None

            if config_agent_dir and isinstance(config_agent_dir, str):
                detected_agent_directory = config_agent_dir
            elif not is_go_project and not is_java_project:
                # For Python, also try to detect from hatch config
                pyproject_path = current_dir / "pyproject.toml"
                if pyproject_path.exists():
                    try:
                        with open(pyproject_path, "rb") as f:
                            pyproject_data = tomllib.load(f)
                        packages = (
                            pyproject_data.get("tool", {})
                            .get("hatch", {})
                            .get("build", {})
                            .get("targets", {})
                            .get("wheel", {})
                            .get("packages", [])
                        )
                        if packages:
                            # Find the first package that isn't 'frontend'
                            for pkg in packages:
                                if isinstance(pkg, str) and pkg != "frontend":
                                    detected_agent_directory = pkg
                                    break
                    except Exception as e:
                        if debug:
                            console.print(
                                f"[dim]Could not auto-detect agent directory: {e}[/dim]"
                            )
                        pass  # Fall back to default

        # Interactive agent directory selection if not provided via CLI and in interactive mode
        if not agent_directory and interactive:
            selected_agent_directory = display_agent_directory_selection(
                current_dir, detected_agent_directory, base_template
            )
            final_agent_directory = selected_agent_directory
            console.print(
                f"✅ Selected agent directory: [cyan]{selected_agent_directory}[/cyan]"
            )
            console.print()
        else:
            final_agent_directory = agent_directory or detected_agent_directory

        # Show info about agent directory selection
        if agent_directory:
            console.print(
                f"ℹ️  Using CLI-specified agent directory: [cyan]{agent_directory}[/cyan]"
            )
        elif detected_agent_directory != "app":
            console.print(
                f"ℹ️  Auto-detected agent directory: [cyan]{detected_agent_directory}[/cyan]"
            )

        agent_folder = current_dir / final_agent_directory

        if not agent_folder.exists() or not agent_folder.is_dir():
            console.print()
            console.print(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            console.print("⚠️  [bold yellow]PROJECT STRUCTURE WARNING[/bold yellow] ⚠️")
            console.print(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            console.print()
            console.print(
                f"📁 [bold]Expected Structure:[/bold] [cyan]/{final_agent_directory}[/cyan] folder containing your agent code"
            )
            console.print(f"📍 [bold]Current Directory:[/bold] {current_dir}")
            console.print(
                f"❌ [bold red]Missing:[/bold red] /{final_agent_directory} folder"
            )
            console.print()
            console.print(
                f"The enhance command can still proceed, but for best compatibility"
                f" your agent code should be organized in a /{final_agent_directory} folder structure."
            )
            console.print()

            # Ask for confirmation after showing the structure warning
            console.print("💡 Options:")
            console.print(
                f"   • Create a /{final_agent_directory} folder and move your agent code there"
            )
            if final_agent_directory == "app":
                console.print(
                    "   • Use [cyan]--agent-directory <custom_name>[/cyan] if your agent code is in a different directory"
                )
            else:
                console.print(
                    "   • Use [cyan]--agent-directory <custom_name>[/cyan] to specify your existing agent directory"
                )
            console.print()

            if interactive:
                if not click.confirm(
                    f"Continue with enhancement despite missing /{final_agent_directory} folder?",
                    default=True,
                ):
                    console.print("✋ [yellow]Enhancement cancelled.[/yellow]")
                    return
        else:
            # Detect language for proper agent file handling
            language = "python"  # default
            if is_go_project:
                language = "go"
            elif is_java_project:
                language = "java"
            elif is_ts_project:
                language = "typescript"

            lang_config = get_language_config(language)
            required_var = lang_config.get("agent_variable", "root_agent")

            # Find agent file using shared utility
            agent_file = find_agent_file(current_dir, language, final_agent_directory)

            if agent_file and agent_file.name == "root_agent.yaml":
                # YAML config agent detected
                console.print(
                    f"✅ Found [cyan]{agent_file.relative_to(current_dir)}[/cyan] (YAML config agent)"
                )
                console.print(
                    "   An agent.py shim will be generated automatically for deployment compatibility."
                )
                console.print(
                    "   📖 Learn more: [cyan][link=https://google.github.io/adk-docs/agents/agent-config/]ADK Agent Config guide[/link][/cyan]"
                )
            elif agent_file:
                # Agent file found
                console.print(
                    f"✅ Found [cyan]{agent_file.relative_to(current_dir)}[/cyan]"
                )

                # Validate the agent file contains the required variable
                is_valid, error_msg = validate_agent_file(agent_file, language)
                if is_valid:
                    console.print(
                        f"✅ Found '{required_var}' definition in {agent_file.name}"
                    )
                else:
                    console.print(f"⚠️  [yellow]{error_msg}[/yellow]")
                    console.print(
                        "   This variable should contain your main agent instance for deployment."
                    )
                    console.print(
                        f"   Example: [cyan]{required_var} = YourAgentClass()[/cyan]"
                    )
                    console.print(
                        "   📖 Learn more: [cyan][link=https://google.github.io/adk-docs/get-started/quickstart/#agentpy]ADK agent.py guide[/link][/cyan]"
                    )
                    console.print()
                    if interactive:
                        if not click.confirm(
                            f"Continue enhancement? (You can add '{required_var}' later)",
                            default=True,
                        ):
                            console.print("✋ [yellow]Enhancement cancelled.[/yellow]")
                            return
            else:
                # No agent file found - suggest creating one
                expected_file = lang_config.get("agent_file", "agent.py")
                console.print(
                    f"⚠️  [yellow]Warning: {expected_file} not found in {final_agent_directory}/[/yellow]"
                )
                console.print(
                    f"   Create {final_agent_directory}/{expected_file} with your agent logic"
                )
                if language == "python":
                    console.print(
                        f"   and define: [cyan]{required_var} = your_agent_instance[/cyan]"
                    )
                console.print()
                if interactive:
                    if not click.confirm(
                        f"Continue enhancement? (An example {expected_file} will be created for you)",
                        default=True,
                    ):
                        console.print("✋ [yellow]Enhancement cancelled.[/yellow]")
                        return

    # Prepare CLI overrides to pass to create command
    final_cli_overrides: dict[str, Any] = {}
    if base_template:
        final_cli_overrides["base_template"] = base_template

    # For current directory templates, ensure agent_directory is included in cli_overrides
    # final_agent_directory is set from interactive selection or CLI/detection
    if template_path == pathlib.Path(".") and final_agent_directory:
        final_cli_overrides["settings"] = final_cli_overrides.get("settings", {})
        final_cli_overrides["settings"]["agent_directory"] = final_agent_directory

    # Merge CLI params with saved config to avoid resetting params
    # not explicitly passed on the CLI (e.g. --session-type).
    effective_create_params = _backfill_create_params_from_config(
        pathlib.Path.cwd(),
        {
            "deployment_target": deployment_target,
            "cicd_runner": cicd_runner,
            "session_type": session_type,
        },
    )

    # Call the create command with in-folder mode enabled
    ctx.invoke(
        create,
        project_name=project_name,
        agent=agent_spec,
        deployment_target=effective_create_params["deployment_target"],
        cicd_runner=effective_create_params["cicd_runner"],
        prototype=prototype,
        session_type=effective_create_params["session_type"],
        debug=debug,
        output_dir=None,  # Use current directory
        auto_approve=auto_approve,
        interactive=interactive,
        region=region,
        skip_checks=skip_checks,
        skip_deps=skip_deps,
        in_folder=True,  # Always use in-folder mode for enhance
        agent_directory=final_agent_directory
        if template_path == pathlib.Path(".")
        else agent_directory,
        agent_garden=agent_garden,
        base_template=base_template,
        skip_welcome=True,  # Skip welcome message since enhance shows its own
        cli_overrides=final_cli_overrides if final_cli_overrides else None,
        bq_analytics=bq_analytics,
        agent_guidance_filename=agent_guidance_filename,
    )
