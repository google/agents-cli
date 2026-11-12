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

import functools
import logging
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

import click
from click.core import ParameterSource
from rich.prompt import IntPrompt, Prompt

from google.agents.cli._defaults import GCP_PROJECT_PLACEHOLDER
from google.agents.cli._fs import delete_folder
from google.agents.cli._gcp_project import resolve_gcp_project
from google.agents.cli._output import (
    INTERACTIVE_FIX,
    Console,
    auto_approve_fix,
    configure_logging,
    programmatic_mode_error,
)
from google.agents.cli._project import (
    AGENT_IDENTITY,
    DEFAULT_BASE_TEMPLATE,
    find_project_config,
    read_project_config,
)
from google.agents.cli._skills_check import find_parent_workspace_skills
from google.agents.cli._symlinks import find_repo_boundary
from google.agents.cli.dev.cmd_playground import LANGUAGE_HANDLERS as PLAYGROUND_HANDLERS

from ..utils import adk_shortcut, cli_options, go_overlay, remote_template, template
from ..utils.cli_options import InteractionMode
from ..utils.command import run_gcloud_command
from ..utils.copy_files import CopyOptions, copy_dir_contents
from ..utils.gcp import verify_credentials_and_vertex
from ..utils.language import LANGUAGE_CONFIGS
from ..utils.logging import display_welcome_banner

# Export the shared decorator for use by other commands
__all__ = ["create"]

console = Console()

DEFAULT_PROJECT_NAME = "my-agent"


@dataclass
class AgentSelectionResult:
    """Result of the interactive agent selection flow."""

    agent: str
    bq_analytics: bool = False


@dataclass
class AgentSelection:
    """Outcome of resolving which agent/template to use and fetching it."""

    agent: str | None
    final_agent: str
    template_source_path: pathlib.Path | None
    temp_dir_to_clean: str | None
    remote_spec: remote_template.RemoteTemplateSpec | None
    recorded_spec: str | None
    bq_analytics: bool
    template_repo_root: pathlib.Path | None


@dataclass
class LoadedTemplateConfig:
    """Outcome of loading and merging a selected template's config."""

    config: dict
    template_path: pathlib.Path
    base_template_name: str | None
    deployment_agent_name: str
    remote_config: dict | None
    cli_overrides: dict | None


@dataclass(frozen=True)
class CreationInputs:
    """The raw ``create`` request: the CLI inputs that drive resolution."""

    agent: str | None
    deployment_target: str | None
    cicd_runner: str | None
    session_type: str | None
    region: str
    region_from_cli: bool
    prototype: bool
    agent_garden: bool
    agent_gateway: bool | None
    agent_runtime_identity_default: str | None
    cloud_run_identity_default: str | None
    agent_directory: str | None
    root_agent_name: str | None
    agent_guidance_filename: str
    base_template: str | None
    cli_overrides: dict | None
    bq_analytics: bool
    memory_bank: bool
    skip_checks: bool
    skip_deps: bool
    locked: bool


@dataclass(frozen=True)
class ProjectLocation:
    """Where the generated project lives on disk."""

    project_name: str
    destination_dir: pathlib.Path
    output_dir: str | None
    in_folder: bool

    @property
    def project_path(self) -> pathlib.Path:
        # In-folder templating writes into the destination itself; otherwise the
        # project gets its own subdirectory named after it.
        if self.in_folder:
            return self.destination_dir
        return self.destination_dir / self.project_name


@dataclass(frozen=True)
class RenderPlan:
    """The resolved choices produced by the resolver chain."""

    deployment_target: str
    cicd_runner: str
    session_type: str | None
    region: str
    google_cloud_project: str | None
    agent_runtime_identity_default: str
    cloud_run_identity_default: str


def _handle_create_errors(f: Callable) -> Callable:
    """Convert create failures to Click's concise CLI errors."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except (click.ClickException, click.Abort):
            raise
        except ValueError as e:
            raise click.UsageError(str(e)) from e
        except Exception as e:
            raise click.ClickException(str(e)) from e

    return wrapper


@click.command()
@click.pass_context
@click.argument("project_name", required=False, default=None)
@click.option(
    "--root-agent-name",
    help="ADK name of the root agent. Defaults to the project name coerced to an "
    "identifier; set it when the agent must be named something else.",
)
@click.option(
    "--agent",
    "-a",
    help="Template identifier to use. Can be a local agent name (e.g., `chat_agent`), a local path (`local@/path/to/template`), or a remote Git URL. Both shorthand (e.g., `github.com/org/repo/path@main`) and full URLs from your browser (e.g., `https://github.com/org/repo/tree/main/path`) are supported. A remote template whose `manifest.yaml` declares `language: go` renders on adk_go. Lists available local templates if omitted.",
)
@click.option(
    "--output-dir",
    "-o",
    type=click.Path(),
    help="Output directory for the project (default: current directory)",
)
@click.option(
    "--skip-welcome",
    is_flag=True,
    hidden=True,
    help="Skip the welcome banner",
    default=False,
)
@click.option(
    "--quiet",
    is_flag=True,
    hidden=True,
    help="Internal flag: suppress the banner, the notices and the next steps",
    default=False,
)
@click.option(
    "--locked",
    is_flag=True,
    hidden=True,
    help="Internal flag for version-locked remote templates",
    default=False,
)
@cli_options.shared_template_options
@click.option(
    "--adk",
    is_flag=True,
    help="Quickstart mode: adk_python + agent_runtime + prototype, skips prompts",
    default=False,
)
@_handle_create_errors
def create(
    ctx: click.Context,
    project_name: str,
    *,
    agent: str | None,
    deployment_target: str | None,
    cicd_runner: str | None,
    adk: bool,
    prototype: bool,
    session_type: str | None,
    debug: bool,
    output_dir: str | None,
    auto_approve: bool,
    interactive: bool,
    region: str,
    skip_checks: bool,
    skip_deps: bool,
    in_folder: bool = False,
    agent_directory: str | None = None,
    root_agent_name: str | None = None,
    agent_garden: bool = False,
    base_template: str | None = None,
    skip_welcome: bool = False,
    quiet: bool = False,
    locked: bool = False,
    cli_overrides: dict | None = None,
    bq_analytics: bool = False,
    memory_bank: bool = False,
    agent_gateway: bool | None = None,
    agent_runtime_identity_default: str | None = None,
    cloud_run_identity_default: str | None = None,
    agent_guidance_filename: str = "GEMINI.md",
) -> None:
    """Create GCP-based AI agent projects from templates."""
    # Setup debug logging if enabled
    if debug:
        configure_logging(logging.DEBUG)
        console.print("> Debug mode enabled")
        logging.debug("Starting CLI in debug mode")

    inputs = CreationInputs(
        agent=agent,
        deployment_target=deployment_target,
        cicd_runner=cicd_runner,
        session_type=session_type,
        region=region,
        region_from_cli=(
            ctx.get_parameter_source("region") == ParameterSource.COMMANDLINE
        ),
        prototype=prototype,
        agent_garden=agent_garden,
        agent_gateway=agent_gateway,
        agent_runtime_identity_default=agent_runtime_identity_default,
        cloud_run_identity_default=cloud_run_identity_default,
        agent_directory=agent_directory,
        root_agent_name=root_agent_name,
        agent_guidance_filename=agent_guidance_filename,
        base_template=base_template,
        cli_overrides=cli_overrides,
        bq_analytics=bq_analytics,
        memory_bank=memory_bank,
        skip_checks=skip_checks,
        skip_deps=skip_deps,
        locked=locked,
    )

    # A quiet run builds a throwaway tree for the three-way merge behind
    # `scaffold enhance` and `scaffold upgrade`, so nothing it says is addressed
    # to anyone. Warnings and errors still print.
    if quiet:
        skip_welcome = True

    if not skip_welcome:
        display_welcome_banner(agent=agent, agent_garden=agent_garden, quiet=auto_approve)

    mode = InteractionMode(
        interactive=interactive, auto_approve=auto_approve, quiet=quiet
    )

    # Resolve the project name before the quickstart flips --auto-approve on, so
    # `--adk` with no name still errors in strict programmatic mode rather than
    # silently defaulting to "my-agent".
    project_name = _resolve_project_name(project_name, mode=mode, output_dir=output_dir)

    # Handle --adk quickstart flag: forces adk + agent_runtime + prototype on the
    # inputs and auto-approve on the interaction mode.
    if adk:
        inputs, mode = _apply_adk_python_quickstart(inputs, mode)

    # Convert output_dir to Path if provided, otherwise use current directory
    destination_dir = pathlib.Path(output_dir) if output_dir else pathlib.Path.cwd()
    destination_dir = destination_dir.resolve()  # Convert to absolute path

    location = _prepare_project_path(
        project_name, destination_dir, output_dir, in_folder=in_folder, mode=mode
    )

    selection = _select_agent(
        inputs.agent,
        deployment_target=inputs.deployment_target,
        interactive=mode.interactive,
        auto_approve=mode.auto_approve,
        locked=inputs.locked,
        project_name=location.project_name,
        bq_analytics=inputs.bq_analytics,
    )
    if selection is None:
        # A version-locked template executed a nested command; nothing more to do.
        return
    logging.debug("Selected agent: %s", selection.final_agent)

    try:
        loaded = _load_template_config(
            selection,
            base_template=inputs.base_template,
            cli_overrides=inputs.cli_overrides,
        )
        plan = _resolve_project(loaded, inputs, mode)
        _render_project(
            selection, loaded, location=location, plan=plan, inputs=inputs, mode=mode
        )
    finally:
        if selection.temp_dir_to_clean:
            logging.debug(
                "Cleaning up temporary directory: %s", selection.temp_dir_to_clean
            )
            delete_folder(pathlib.Path(selection.temp_dir_to_clean))

    # Everything below is the next-steps banner. A quiet run's project lives in
    # a temp directory that is deleted moments later, so telling the user to cd
    # into it would be wrong as well as noisy.
    if mode.quiet:
        return

    _print_next_steps(location, plan)


def _resolve_project_name(
    project_name: str,
    *,
    mode: InteractionMode,
    output_dir: str | None,
) -> str:
    """Resolve, validate, and normalize the project name.

    Prompts in interactive mode, defaults to "my-agent" under --auto-approve,
    and errors in strict programmatic mode when no name is supplied. Enforces
    the 26-character limit and returns the normalized name.
    """
    if not project_name:
        if mode.interactive:
            project_name = _prompt_for_project_name(output_dir=output_dir)
        elif mode.auto_approve:
            project_name = DEFAULT_PROJECT_NAME
            logging.info(
                "Project name not specified. Defaulting to '%s' in auto-approve mode.",
                project_name,
            )
        else:
            raise programmatic_mode_error(
                "PROJECT_NAME is required",
                ("<project-name>", "name the project as the first argument"),
                INTERACTIVE_FIX,
                auto_approve_fix(f"project name: {DEFAULT_PROJECT_NAME}"),
            )

    # Validate project name (for CLI-provided names)
    errors = _validate_project_name(project_name)
    if errors:
        error_text = "\n".join(err for err in errors)
        raise click.UsageError(error_text)

    return normalize_project_name(project_name)


def _prompt_for_project_name(
    output_dir: str | None,
):
    """Interactively prompts the user for a valid, unique project name.

    Args:
        output_dir: The target base directory where the project will be created.
            If None, the current working directory is used to check for collisions.

    Returns:
        The validated project name entered by the user.
    """
    project_name = None
    # Convert output_dir to Path for directory existence check
    check_dir = (pathlib.Path(output_dir) if output_dir else pathlib.Path.cwd()).resolve()
    while True:
        project_name = Prompt.ask(
            "\n> Enter a name for your project",
            default=DEFAULT_PROJECT_NAME,
            show_default=True,
        )
        errors = _validate_project_name(project_name)
        if errors:
            for error in errors:
                console.print(f"Error: {error}", style="bold red")
            continue
        # Check if directory already exists
        normalized_name = normalize_project_name(project_name)
        if (check_dir / normalized_name).exists():
            console.print(
                f"Error: Project directory '{check_dir / normalized_name}' already exists. Please choose a different name.",
                style="bold red",
            )
            continue
        break
    return project_name


def _validate_project_name(project_name: str) -> list[str]:
    """Validate a project name.

    Args:
        project_name: Name of the project to validate

    Returns:
        List of validation errors found. Empty list if project name is valid.
    """
    errors = []
    if len(project_name) > 26:
        errors.append(
            f"Project name '{project_name}' exceeds 26 characters. "
            "Please use a shorter name."
        )
    return errors


def normalize_project_name(project_name: str) -> str:
    """Normalize project name for better compatibility with cloud resources and tools."""

    needs_normalization = (
        any(char.isupper() for char in project_name) or "_" in project_name
    )

    if needs_normalization:
        normalized_name = project_name
        console.print(
            "Note: Project names are normalized (lowercase, hyphens only) for better compatibility with cloud resources and tools.",
            style="dim",
        )
        if any(char.isupper() for char in normalized_name):
            normalized_name = normalized_name.lower()
            logging.info(
                "Converting to lowercase for compatibility: '%s' -> '%s'",
                project_name,
                normalized_name,
            )

        if "_" in normalized_name:
            # Capture the name state before this specific change
            name_before_hyphenation = normalized_name
            normalized_name = normalized_name.replace("_", "-")
            logging.info(
                "Replacing underscores with hyphens for compatibility: '%s' -> '%s'",
                name_before_hyphenation,
                normalized_name,
            )

        return normalized_name

    return project_name


def _apply_adk_python_quickstart(
    inputs: CreationInputs, mode: InteractionMode
) -> tuple[CreationInputs, InteractionMode]:
    """Coerce inputs for the ``--adk`` quickstart and report any overrides.

    The quickstart forces adk + agent_runtime + prototype + auto-approve, so it
    warns when it overrides an explicit ``--agent`` / ``--deployment-target``.
    Returns updated ``(inputs, mode)`` copies with those values applied.
    """
    console.print(
        f"⚡ ADK quickstart: {DEFAULT_BASE_TEMPLATE} + Agent Runtime in Gemini platform + prototype mode\n",
        style="cyan",
    )
    # Compare after alias resolution so `-a adk --adk` is not reported as overridden.
    requested_agent = inputs.agent and template.AGENT_ALIASES.get(
        inputs.agent, inputs.agent
    )
    if requested_agent and requested_agent != DEFAULT_BASE_TEMPLATE:
        logging.info(
            "--agent '%s' ignored due to --adk flag (using %s).",
            inputs.agent,
            DEFAULT_BASE_TEMPLATE,
        )
    if inputs.deployment_target and inputs.deployment_target != "agent_runtime":
        logging.info(
            "--deployment-target '%s' ignored due to --adk flag (using agent_runtime).",
            inputs.deployment_target,
        )
    logging.debug(
        "ADK quickstart mode: agent=%s, deployment_target=agent_runtime, prototype=True, auto_approve=True",
        DEFAULT_BASE_TEMPLATE,
    )
    return (
        replace(
            inputs,
            agent=DEFAULT_BASE_TEMPLATE,
            deployment_target="agent_runtime",
            prototype=True,
        ),
        replace(mode, auto_approve=True),
    )


def _prepare_project_path(
    project_name: str,
    destination_dir: pathlib.Path,
    output_dir: str | None,
    *,
    in_folder: bool,
    mode: InteractionMode,
) -> ProjectLocation:
    """Resolve where the project lives and prepare the destination.

    In-folder mode backs up the existing directory (aborting cleanly if the user
    declines); otherwise verifies the target does not already exist. Returns the
    resolved `ProjectLocation`.
    """
    location = ProjectLocation(
        project_name=project_name,
        destination_dir=destination_dir,
        output_dir=output_dir,
        in_folder=in_folder,
    )

    if in_folder:
        # For in-folder templating, use the current directory directly. In-folder
        # mode is permissive - we assume the user wants to enhance their existing
        # repo. Back up the whole directory before writing anything.
        from ..utils.backup import create_project_backup

        try:
            create_project_backup(
                destination_dir,
                console=console,
                interactive=mode.interactive,
            )
        except click.Abort:
            console.print("✋ [red]Operation cancelled.[/red]")
            raise

        console.print()
        return location

    # Check if project would exist in output directory
    if location.project_path.exists():
        raise click.UsageError(
            f"Project directory '{location.project_path}' already exists"
        )
    return location


def _select_agent(
    agent: str | None,
    *,
    deployment_target: str | None,
    interactive: bool,
    auto_approve: bool,
    locked: bool,
    project_name: str,
    bq_analytics: bool,
) -> AgentSelection | None:
    """Resolve which agent to use and fetch its template if remote/local."""
    # Resolve agent name aliases (backwards compatibility)
    agent = template.resolve_agent_alias(agent)

    if agent:
        # None => a version-locked template executed a nested command; stop.
        return _resolve_specified_agent(
            agent,
            locked=locked,
            project_name=project_name,
            bq_analytics=bq_analytics,
        )

    # No explicit --agent: fall back to interactive / auto-approve selection.
    return _select_agent_interactively(
        agent,
        deployment_target=deployment_target,
        interactive=interactive,
        auto_approve=auto_approve,
        locked=locked,
        project_name=project_name,
        bq_analytics=bq_analytics,
    )


def _resolve_specified_agent(
    agent: str,
    *,
    locked: bool,
    project_name: str,
    bq_analytics: bool,
) -> AgentSelection | None:
    """Resolve an explicit `--agent` value into a template selection.

    Handles a local@ path (copied to a temp dir, honoring a version lock), a
    remote / adk-recipes spec, or a built-in agent name/number. Returns None when
    a version-locked local template already executed a nested command (the caller
    should stop); otherwise returns the resolved selection.
    """
    if agent.startswith("local@"):
        return _resolve_local_spec(
            agent, locked=locked, project_name=project_name, bq_analytics=bq_analytics
        )

    # Check if it's a remote template specification
    remote = _resolve_remote_spec(
        agent, locked=locked, project_name=project_name, bq_analytics=bq_analytics
    )
    if remote:
        return remote

    # Handle built-in agent selection by name or number
    agents = template.get_available_agents(include_hidden=True)
    # First check if it's a valid agent name
    if any(p["name"] == agent for p in agents.values()):
        selected_agent = agent
    else:
        # Try numeric agent selection if input is a number
        try:
            agent_num = int(agent)
            if agent_num in agents:
                selected_agent = agents[agent_num]["name"]
            else:
                raise ValueError(f"Invalid agent number: {agent_num}")
        except ValueError as err:
            raise click.UsageError(f"Invalid agent name or number: {agent}") from err

    return AgentSelection(
        agent=agent,
        final_agent=selected_agent,
        template_source_path=None,
        temp_dir_to_clean=None,
        remote_spec=None,
        recorded_spec=None,
        bq_analytics=bq_analytics,
        template_repo_root=None,
    )


def _resolve_local_spec(
    agent: str,
    *,
    locked: bool,
    project_name: str,
    bq_analytics: bool,
) -> AgentSelection | None:
    """Resolve a ``local@<path>`` spec into a template selection.

    Copies the local template into a staging dir. Symlinks are vetted as they are
    materialized against a boundary: the enclosing Git / Mercurial / Jujutsu
    working copy when the template is inside one, otherwise the template directory
    itself. An in-boundary link becomes real files; a link escaping the boundary
    fails the command.

    Returns None when a version-locked local template already executed a nested
    command (the caller should stop).
    """
    path_str = agent.split("@", 1)[1]
    local_path = pathlib.Path(path_str).resolve()
    if not local_path.is_dir():
        raise click.ClickException(
            f"Local path not found or not a directory: {local_path}"
        )

    # Record the absolute path: a relative local path would resolve against
    # wherever enhance / upgrade are later run from.
    recorded_spec = f"local@{local_path}"

    # Symlinks are bounded by the enclosing VCS working copy; with none, the
    # template directory itself is the boundary, so an in-template link is
    # materialized but one escaping it is refused.
    repo_boundary = find_repo_boundary(local_path)
    clone_root = repo_boundary or local_path
    if repo_boundary is None:
        logging.debug(
            "No Git, Mercurial, or Jujutsu repository around local template "
            "'%s'; bounding symlinks to the template directory.",
            local_path,
        )

    # Stage the template in a temp dir. The .template config must survive the
    # staging copy (it is read below); the overlay into the project drops it.
    temp_dir = tempfile.mkdtemp(prefix="acli_local_template_")
    template_source_path = pathlib.Path(temp_dir) / local_path.name
    try:
        copy_dir_contents(
            local_path,
            template_source_path,
            clone_root=clone_root,
            options=CopyOptions(overwrite=True, keep_template_dir=True),
        )
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    # Check for version lock and execute nested command if found
    if remote_template.check_and_execute_with_version_lock(
        template_source_path, agent, locked, project_name
    ):
        # If we executed with locked version, cleanup and exit
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None

    if locked:
        # In locked mode, show a nicer message
        console.print("✅ Using version-locked template", style="green")
    else:
        console.print(f"Using local template: {local_path}")
    logging.debug("Staged local template in temporary dir: %s", template_source_path)
    return AgentSelection(
        agent=agent,
        final_agent=f"local_{template_source_path.name}",
        template_source_path=template_source_path,
        temp_dir_to_clean=temp_dir,
        remote_spec=None,
        recorded_spec=recorded_spec,
        bq_analytics=bq_analytics,
        # The staged copy is already materialized/symlink-free, so the overlay
        # needs no boundary.
        template_repo_root=None,
    )


def _resolve_remote_spec(
    agent: str,
    *,
    locked: bool,
    project_name: str,
    bq_analytics: bool,
) -> AgentSelection | None:
    """Fetch a remote-template spec when ``agent`` names one.

    Shared by the CLI-input and interactive (browse) agent-selection paths, both
    of which turn a remote spec into the same selection. Returns an
    ``AgentSelection`` when ``agent`` parses as a remote / adk-recipes spec, or
    None so the caller can fall back to local agent-name resolution.
    """
    adk_shortcut.reject_adk_shortcut(agent)
    remote_spec = remote_template.parse_agent_spec(agent)
    if not remote_spec:
        return None

    fetched = _fetch_remote_template_source(
        remote_spec, agent, locked=locked, project_name=project_name
    )
    return AgentSelection(
        agent=agent,
        # Generate a unique name for the remote template.
        final_agent=f"remote_{hash(agent)}",
        template_source_path=fetched.template_dir,
        temp_dir_to_clean=str(fetched.temp_dir),
        remote_spec=remote_spec,
        recorded_spec=agent,
        bq_analytics=bq_analytics,
        template_repo_root=fetched.repo_root,
    )


def _fetch_remote_template_source(
    remote_spec: remote_template.RemoteTemplateSpec,
    agent: str,
    *,
    locked: bool,
    project_name: str,
) -> remote_template.FetchedTemplate:
    """Fetch a remote/adk-recipes template, printing progress.

    Wraps ``remote_template.fetch_remote_template`` with the user-facing
    messaging shared by the CLI and interactive (browse) agent-selection paths.

    Returns:
        The ``FetchedTemplate`` from ``fetch_remote_template`` (template
        directory, temp directory to clean up, and repository root). The caller
        keeps ownership of the ``agent``-derived values (``recorded_spec`` and
        the generated ``remote_<hash>`` name), which differ per call site.
    """
    if remote_spec.is_adk_recipes:
        console.print(
            f"> Fetching template: {remote_spec.template_path}",
            style="bold blue",
        )
    else:
        console.print(f"Fetching remote template: {agent}")

    return remote_template.fetch_remote_template(remote_spec, agent, locked, project_name)


def _select_agent_interactively(
    agent: str | None,
    *,
    deployment_target: str | None,
    interactive: bool,
    auto_approve: bool,
    locked: bool,
    project_name: str,
    bq_analytics: bool,
) -> AgentSelection:
    """Select an agent when no explicit `--agent` was given.

    Prompts interactively, defaults to the first available agent under
    --auto-approve, or errors in strict programmatic mode. A browse result that
    is itself a remote spec is fetched like CLI input.
    """
    agents = template.get_available_agents(deployment_target=deployment_target)

    if interactive:
        selection_result = display_agent_selection(deployment_target)
        final_agent = selection_result.agent
        if selection_result.bq_analytics:
            bq_analytics = True
    elif auto_approve:
        # Default to first available agent in auto-approve mode
        if not agents:
            raise click.ClickException(
                "Error: No agents available for the selected deployment target."
            )
        final_agent = next(iter(agents.values()))["name"]
        logging.info(
            "--agent not specified. Defaulting to '%s' in auto-approve mode.",
            final_agent,
        )
    else:
        names = [x["name"] for x in agents.values()]
        repo = "a template repo: <org>/<repo>[/<path>]@<tag>, local@<path>"
        if names:
            # -y takes the first agent, so it is only offered when there is one.
            fixes = [
                (
                    "-a, --agent <template>",
                    f"a built-in template ({', '.join(names)})",
                ),
                ("", f"or {repo}"),
                INTERACTIVE_FIX,
                auto_approve_fix(f"agent: {names[0]}"),
            ]
        else:
            fixes = [("-a, --agent <template>", repo), INTERACTIVE_FIX]
        raise programmatic_mode_error("--agent is required", *fixes)

    # If browse functionality returned a remote agent spec, process it like CLI input
    if final_agent:
        remote = _resolve_remote_spec(
            final_agent,
            locked=locked,
            project_name=project_name,
            bq_analytics=bq_analytics,
        )
        if remote:
            return remote

    # Non-remote selection: a built-in agent chosen via browse / auto-approve.
    return AgentSelection(
        agent=agent,
        final_agent=final_agent,
        template_source_path=None,
        temp_dir_to_clean=None,
        remote_spec=None,
        recorded_spec=None,
        bq_analytics=bq_analytics,
        template_repo_root=None,
    )


def display_agent_selection(
    deployment_target: str | None = None,
    language: str | None = None,
) -> AgentSelectionResult:
    """Display available agents grouped by language/framework and prompt for selection."""
    agents = template.get_available_agents(deployment_target=deployment_target)

    # Filter by language if specified
    if language:
        agents = {
            num: agent for num, agent in agents.items() if agent["language"] == language
        }
        # Re-number from 1
        agents = {i + 1: agent for i, agent in enumerate(agents.values())}

    if not agents:
        if deployment_target:
            raise click.ClickException(
                f"No agents available for deployment target '{deployment_target}'"
            )
        raise click.ClickException("No valid agents found")

    console.print("\n> Please select an agent to get started:")

    current_header = None
    for num, agent in agents.items():
        lang_config = LANGUAGE_CONFIGS.get(agent["language"])
        if lang_config:
            header = f"{lang_config['picker_icon']} {lang_config['display_name']}"
        else:
            header = "🌐 Other Languages"

        # Print group header when transitioning to a new group
        if header != current_header:
            current_header = header
            console.print(f"\n  [bold cyan]{header}[/]")

        # Align agent names for cleaner display (use display_name if available)
        display_name = agent.get("display_name", agent["name"])
        name_padded = display_name.ljust(14)
        console.print(
            f"     {num}. [bold]{name_padded}[/] [dim]{agent['description']}[/]"
        )

    # Add "More Options" submenu entry
    more_options_num = len(agents) + 1
    console.print("\n  [bold cyan]🔧 More Options[/]")
    label = "Browse".ljust(14)
    console.print(
        f"     {more_options_num}. [bold]{label}[/] [dim]BQ agent analytics, community agents, custom templates[/]"
    )

    choice = IntPrompt.ask(
        "\nEnter the number of your template choice", default=1, show_default=True
    )

    if choice == more_options_num:
        return display_more_options_submenu(deployment_target)
    elif choice in agents:
        return AgentSelectionResult(agent=agents[choice]["name"])
    else:
        raise ValueError(f"Invalid agent selection: {choice}")


def display_more_options_submenu(
    deployment_target: str | None = None,
) -> AgentSelectionResult:
    """Display the More Options submenu with additional agent sources."""
    console.print("\n> Select an option:")
    console.print(
        "     1. [bold]bq-analytics[/]       [dim]Log agent events to BigQuery for monitoring and evaluation[/]"
    )
    console.print(
        "     2. [bold][link=https://github.com/google/adk-recipes]google/adk-recipes[/link][/] [dim]Browse ADK recipes[/]"
    )
    console.print(
        "     3. [bold]Custom URL[/]         [dim]Enter a remote template URL[/]"
    )
    console.print("     4. [bold]← Back[/]             [dim]Return to agent selection[/]")

    choice = IntPrompt.ask(
        "\nEnter the number of your choice", default=1, show_default=True
    )

    if choice == 1:
        console.print(
            "\n[blue]BigQuery Agent Analytics will be enabled for the selected agent.[/]"
        )
        console.print(
            "[dim]Tip: You can also pass --bq-analytics directly during creation.[/]"
        )
        result = display_agent_selection(deployment_target, language="python")
        result.bq_analytics = True
        return result
    elif choice == 2:
        return display_adk_recipes_selection()
    elif choice == 3:
        url = Prompt.ask("\nEnter the remote template URL")
        try:
            adk_shortcut.reject_adk_shortcut(url)
        except click.ClickException as e:
            console.print(e.message, style="bold red")
            return display_more_options_submenu(deployment_target)
        spec = remote_template.parse_agent_spec(url)
        if spec:
            return AgentSelectionResult(agent=url)
        else:
            console.print(f"Invalid template URL: {url}", style="bold red")
            return display_more_options_submenu(deployment_target)
    elif choice == 4:
        return display_agent_selection(deployment_target)
    else:
        raise ValueError(f"Invalid selection: {choice}")


def display_adk_recipes_selection() -> AgentSelectionResult:
    """Display adk-recipes agents and prompt for selection."""

    console.print("\n> Fetching agents from [bold blue]google/adk-recipes[/]...")

    try:
        # Parse the adk-recipes repository
        spec = remote_template.parse_agent_spec(remote_template.ADK_RECIPES_REPO_URL)
        if not spec:
            raise RuntimeError("Failed to parse adk-recipes repository")

        # Fetch the repository. The spec has no template subpath, so repo_root
        # is the whole clone — exactly what discover_adk_agents scans.
        repo_path = remote_template.fetch_remote_template(spec).repo_root

        # Use shared ADK discovery function
        adk_agents = remote_template.discover_adk_agents(repo_path)

        if not adk_agents:
            console.print("No agents found in adk-recipes repository", style="yellow")
            # Fall back to local agents
            return display_agent_selection()

        console.print("\n> Available agents from [bold blue]google/adk-recipes[/]:")

        # Show explanation for inferred agents at the top
        remote_template.display_adk_caveat_if_needed(adk_agents)

        for num, agent in adk_agents.items():
            name_with_indicator = agent.name
            if not agent.has_explicit_config:
                name_with_indicator += " *"

            console.print(
                f"{num}. [bold]{name_with_indicator}[/] - [dim]{agent.description}[/]"
            )

        # Add option to go back to local agents
        back_option = len(adk_agents) + 1
        console.print(
            f"{back_option}. [bold]← Back to built-in agents[/] - [dim]Return to local agent selection[/]"
        )

        choice = IntPrompt.ask(
            "\nEnter the number of your choice", default=1, show_default=True
        )

        if choice == back_option:
            return display_agent_selection()
        elif choice in adk_agents:
            # Return the selected agent's URL spec
            selected_agent = adk_agents[choice]
            console.print(
                f"\n> Selected: [bold]{selected_agent.name}[/] from adk-recipes"
            )
            return AgentSelectionResult(agent=selected_agent.spec)
        else:
            raise ValueError(f"Invalid agent selection: {choice}")

    except Exception as e:
        console.print(f"Error fetching adk-recipes agents: {e}", style="bold red")
        console.print("Falling back to built-in agents...", style="yellow")
        return display_agent_selection()


def _load_template_config(
    selection: AgentSelection,
    *,
    base_template: str | None,
    cli_overrides: dict | None,
) -> LoadedTemplateConfig:
    """Load and merge the selected template's config.

    For remote / local-path templates, loads the remote config, merges it over
    the inherited base-template config, and derives the deployment agent name
    from the base template. For built-in agents, loads the local config and
    applies any CLI overrides.
    """
    final_agent = selection.final_agent
    template_source_path = selection.template_source_path
    remote_spec = selection.remote_spec

    # Load template configuration based on whether it's remote or local
    base_template_name = None
    if template_source_path:
        # Prepare CLI overrides for remote template config
        # Initialize cli_overrides if not provided (e.g., from enhance command)
        if cli_overrides is None:
            cli_overrides = {}

        if base_template:
            # Validate that the base template exists
            if not template.validate_base_template(base_template):
                available_templates = template.get_available_base_templates()
                raise click.UsageError(
                    f"Base template '{base_template}' not found.\n"
                    f"Available base templates: {', '.join(available_templates)}"
                )
            cli_overrides["base_template"] = template.resolve_agent_alias(base_template)

        source_config = remote_template.load_remote_template_config(
            template_source_path, cli_overrides
        )
        # Checked before the deployment and project prompts. A project
        # overlaid onto itself may predate ADK Go v2 and is still enhanced.
        if go_overlay.applies_to(
            source_config["settings"].get("language"),
            overlay_is_project=_overlays_itself(selection),
        ):
            go_overlay.check_adk_go_version(
                template_source_path, selection.template_repo_root
            )

        if (
            remote_spec
            and remote_spec.is_adk_recipes
            and not source_config.get("has_explicit_config", True)
        ):
            console.print(
                "\n[blue]ℹ️  Note: Agents CLI uses heuristics to template this ADK recipe.[/]"
            )
            console.print(
                "[dim]   Agents CLI attempts to create a working codebase, but you'll need to follow the generated README for complete setup.[/]"
            )

        # Remote templates now work even without pyproject.toml thanks to defaults
        if source_config:
            logging.debug("Final remote template config: %s", source_config)

        # Load base template config for inheritance
        base_template_name = remote_template.get_base_template_name(source_config)
        logging.debug("Using base template: %s", base_template_name)
        if not template.validate_base_template(base_template_name):
            if selection.temp_dir_to_clean:
                shutil.rmtree(selection.temp_dir_to_clean, ignore_errors=True)
            raise click.UsageError(
                f"Base template '{base_template_name}' not found.\n"
                "Available base templates: "
                f"{', '.join(template.get_available_base_templates())}"
            )

        base_template_path = (
            pathlib.Path(__file__).parent.parent
            / "agents"
            / base_template_name
            / ".template"
        )
        base_config = template.load_template_config(base_template_path)

        # Merge configs: remote inherits from and overrides base
        config = remote_template.merge_template_configs(base_config, source_config)

        # For remote templates, use the template/ subdirectory as the template source
        template_path = template_source_path / ".template"
    else:
        template_path = (
            pathlib.Path(__file__).parent.parent / "agents" / final_agent / ".template"
        )
        config = template.load_template_config(template_path)

        # Apply CLI overrides for local templates if provided (e.g., from enhance command)
        if cli_overrides:
            config = remote_template.merge_template_configs(config, cli_overrides)
            logging.debug(
                "Applied CLI overrides to local template config: %s", cli_overrides
            )
    # Warn if using a hidden (experimental) agent template
    if final_agent and not remote_spec:
        if config.get("hidden", False):
            logging.warning(
                "'%s' is an experimental template and is not fully supported for agentic development.",
                final_agent,
            )

    # Deployment target selection needs the base template name for remote templates
    deployment_agent_name = final_agent
    remote_config = None
    if template_source_path:
        # Use the base template name from remote config for deployment target selection
        deployment_agent_name = remote_template.get_base_template_name(config)
        remote_config = config

    return LoadedTemplateConfig(
        config=config,
        template_path=template_path,
        base_template_name=base_template_name,
        deployment_agent_name=deployment_agent_name,
        remote_config=remote_config,
        cli_overrides=cli_overrides,
    )


def _resolve_project(
    loaded: LoadedTemplateConfig,
    inputs: CreationInputs,
    mode: InteractionMode,
) -> RenderPlan:
    """Resolve deployment target, session type, CI/CD runner, region and GCP
    project into a `RenderPlan`.

    Runs the resolver chain in order (each step can depend on the previous one),
    enforces the agent-gateway / region constraints, and returns the resolved
    choices consumed by `_render_project`.
    """
    if inputs.memory_bank and not (loaded.config.get("settings") or {}).get(
        "supports_memory_bank"
    ):
        raise click.UsageError(
            "--memory-bank is not supported by this template. Templates that "
            "support it: the Claude Agent SDK template "
            "(--agent google/agents-cli/extensions/claude/template@<version>). "
            "For ADK agents, add Memory Bank with the cross-session-memory recipe "
            "from google/adk-recipes."
        )

    final_deployment = _resolve_deployment_target(
        deployment_target=inputs.deployment_target,
        prototype=inputs.prototype,
        deployment_agent_name=loaded.deployment_agent_name,
        remote_config=loaded.remote_config,
        mode=mode,
    )
    logging.debug("Selected deployment target: %s", final_deployment)

    if inputs.agent_gateway and final_deployment != "agent_runtime":
        raise click.UsageError(
            f"--agent-gateway is not supported for deployment target '{final_deployment}'.\n"
            "  Agent Gateway can only be bound to deployments on Agent Runtime in Gemini platform.\n"
            "  Use --deployment-target agent_runtime, or drop --agent-gateway."
        )

    final_agent_runtime_identity_default = (
        inputs.agent_runtime_identity_default or AGENT_IDENTITY
    )
    final_cloud_run_identity_default = inputs.cloud_run_identity_default or AGENT_IDENTITY

    final_session_type = _resolve_session_type(
        session_type=inputs.session_type,
        config=loaded.config,
        final_deployment=final_deployment,
        mode=mode,
    )
    if final_session_type:
        logging.debug("Selected session type: %s", final_session_type)

    final_cicd_runner = _resolve_cicd_runner(
        cicd_runner=inputs.cicd_runner,
        prototype=inputs.prototype,
        agent_garden=inputs.agent_garden,
        final_deployment=final_deployment,
        mode=mode,
    )
    logging.debug("Selected CI/CD runner: %s", final_cicd_runner)

    # Region confirmation (only in interactive mode, if not explicitly passed via CLI)
    region = _resolve_region(
        inputs.region,
        region_from_cli=inputs.region_from_cli,
        agent_garden=inputs.agent_garden,
        mode=mode,
        deployment_target=final_deployment,
    )
    logging.debug("Selected region: %s", region)

    from google.agents.cli.deploy._utils import validate_deployment_region

    validate_deployment_region(region, inputs.deployment_target)

    # GCP Setup
    logging.debug("Setting up GCP...")
    creds_info = _resolve_gcp_creds(
        skip_checks=inputs.skip_checks,
        region=region,
        agent_garden=inputs.agent_garden,
        mode=mode,
    )

    return RenderPlan(
        deployment_target=final_deployment,
        cicd_runner=final_cicd_runner,
        session_type=final_session_type,
        region=region,
        google_cloud_project=creds_info.get("project"),
        agent_runtime_identity_default=final_agent_runtime_identity_default,
        cloud_run_identity_default=final_cloud_run_identity_default,
    )


def _resolve_deployment_target(
    *,
    deployment_target: str | None,
    prototype: bool,
    deployment_agent_name: str,
    remote_config: dict | None,
    mode: InteractionMode,
) -> str:
    """Resolve the deployment target.

    Honors an explicit --deployment-target, forces 'none' in prototype mode,
    auto-selects when only one target exists or under --auto-approve, prompts
    interactively, and errors in strict programmatic mode.
    """

    if deployment_target:
        return deployment_target

    if prototype:
        logging.info("Prototype mode: using deployment_target='none'.")
        return "none"

    available_targets = template.get_deployment_targets(
        deployment_agent_name, remote_config=remote_config
    )
    if not available_targets:
        raise click.ClickException(
            f"Error: No deployment targets available for agent '{deployment_agent_name}'."
        )

    # Auto-select if only one target available, in auto-approve mode, or strict programmatic
    if len(available_targets) == 1:
        logging.info(
            "Using '%s' (only available deployment target for this agent).",
            available_targets[0],
        )
        return available_targets[0]
    elif mode.interactive:
        return template.prompt_deployment_target(
            deployment_agent_name, remote_config=remote_config
        )
    elif mode.auto_approve:
        logging.info(
            "--deployment-target not specified. Defaulting to '%s' in auto-approve mode.",
            available_targets[0],
        )
        return available_targets[0]
    else:
        raise programmatic_mode_error(
            "--deployment-target is required",
            (
                "-d, --deployment-target <target>",
                f"pick where to deploy (available: {', '.join(available_targets)})",
            ),
            (
                "-p, --prototype",
                "skip deployment for now; add it later with agents-cli scaffold enhance",
            ),
            INTERACTIVE_FIX,
            auto_approve_fix(f"deployment target: {available_targets[0]}"),
        )


def _resolve_session_type(
    *,
    session_type: str | None,
    config: dict,
    final_deployment: str,
    mode: InteractionMode,
) -> str | None:
    """Resolve the session type for the selected agent and deployment target.

    Non-Python agents and agents that don't require session management always
    use in-memory sessions. For Python agents that require sessions, the value
    depends on the deployment target (agent_runtime handles sessions itself);
    session-type selection is offered only for supported agents on cloud_run/gke,
    limited to the agent's ``session_types`` setting when it has one; its first
    entry is the default.
    """
    settings = config.get("settings", {})
    requires_session = settings.get("requires_session", False)
    supported = settings.get("session_types")
    if supported is None:
        supported = list(template.SESSION_TYPES)
    elif (
        not isinstance(supported, list)
        or not supported
        or not all(isinstance(s, str) and s in template.SESSION_TYPES for s in supported)
    ):
        raise click.ClickException(
            f"The template's settings.session_types ({supported!r}) must be a non-empty "
            f"list of session types from: {', '.join(template.SESSION_TYPES)}."
        )

    if not requires_session:
        # Agents that don't manage sessions always use in-memory sessions.
        if session_type and session_type != "in_memory":
            logging.warning(
                "Session type options are not available for this agent. "
                "Proceeding with in-memory sessions."
            )
        return "in_memory"
    elif final_deployment == "agent_runtime":
        if session_type:
            logging.warning(
                "--session-type cannot be used with agent_runtime deployment target, it will be unset. "
                "Agent Runtime in Gemini platform handles session management internally."
            )
            return "none"
    elif final_deployment in ("cloud_run", "gke"):
        if session_type and session_type not in supported:
            raise click.UsageError(
                f"--session-type {session_type} is not supported by this agent. "
                f"Valid options: {', '.join(supported)}."
            )
        if session_type:
            return session_type

        if mode.interactive:
            return template.prompt_session_type_selection(choices=supported)

        if mode.auto_approve and not mode.quiet:
            logging.info(
                "--session-type not specified. Defaulting to '%s' in auto-approve mode.",
                supported[0],
            )
        return supported[0]

    if session_type and session_type != "in_memory":
        logging.warning(
            "--session-type is only supported for cloud_run and gke deployment targets; "
            "it will be ignored and in-memory sessions will be used."
        )
    return "in_memory"


def _resolve_cicd_runner(
    *,
    cicd_runner: str | None,
    prototype: bool,
    agent_garden: bool,
    final_deployment: str,
    mode: InteractionMode,
) -> str:
    """Resolve the CI/CD runner.

    --prototype / --agent-garden and deployment_target='none' force 'skip'
    (a minimal project); otherwise honor an explicit --cicd-runner, prompt
    interactively, or default to 'skip'.
    """
    # --prototype flag or agent_garden mode defaults to "skip" (minimal project)
    if prototype or agent_garden:
        if cicd_runner and cicd_runner != "skip":
            logging.info(
                "--cicd-runner '%s' ignored due to %s flag.",
                cicd_runner,
                "--prototype" if prototype else "--agent-garden",
            )
        logging.debug("Prototype mode: setting cicd_runner to 'skip'")
        return "skip"
    elif final_deployment == "none":
        if cicd_runner and cicd_runner != "skip":
            logging.info(
                "--cicd-runner '%s' ignored for deployment_target='none'.",
                cicd_runner,
            )
        logging.debug("deployment_target='none': setting cicd_runner to 'skip'")
        return "skip"
    elif cicd_runner:
        return cicd_runner
    elif mode.interactive:
        return template.prompt_cicd_runner_selection()

    if mode.auto_approve and not mode.quiet:
        logging.info(
            "--cicd-runner not specified. Defaulting to 'skip' (simple mode) in auto-approve mode."
        )
    return "skip"


def _resolve_region(
    region: str,
    *,
    region_from_cli: bool,
    agent_garden: bool,
    mode: InteractionMode,
    deployment_target: str | None,
) -> str:
    """Confirm the deployment region interactively.

    Only prompts when interactive and the region was not passed explicitly on
    the CLI; otherwise returns the region unchanged. A deployment_target of
    'none' provisions no cloud infrastructure, so the region is irrelevant and
    the prompt is skipped.
    """
    if mode.interactive and not region_from_cli and deployment_target != "none":
        # Show Agent Runtime supported regions link if agent_garden flag is set
        if agent_garden:
            console.print(
                "\n📍 [blue]Regions supported by Agent Runtime in Gemini platform:[/blue]\n"
                "   [cyan]https://cloud.google.com/vertex-ai/generative-ai/docs/agent-engine/overview#supported-regions[/cyan]"
            )
        region = prompt_region_confirmation(region, agent_garden=agent_garden)
    return region


def prompt_region_confirmation(
    default_region: str = "us-east1", agent_garden: bool = False
) -> str:
    """Prompt user to confirm or change the default region."""
    import re

    while True:
        new_region = Prompt.ask(
            "\n🌍 Enter GCP region for deployment (Gemini model calls default to global endpoint)",
            default=default_region,
            show_default=True,
        ).strip()
        selected = new_region if new_region else default_region
        if not re.match(r"^[a-z]+-[a-z]+\d+$", selected.lower()):
            logging.warning(
                "'%s' is not a valid single regional location. Deployment infrastructure (Cloud Run, Agent Runtime, GKE) requires a single regional location (e.g., 'us-central1', 'europe-west4').",
                selected,
            )
            console.print(
                "   To route Gemini model calls to multi-region endpoints, you can set GOOGLE_CLOUD_LOCATION in your .env.\n",
                style="dim",
            )
            continue
        return selected


def _resolve_gcp_creds(
    *,
    skip_checks: bool,
    region: str,
    agent_garden: bool,
    mode: InteractionMode,
) -> dict:
    """Resolve GCP project / credentials info for the generated .env.

    Runs full GCP environment setup unless --skip-checks; on failure (or when
    skipping) falls back to whatever project ID is resolvable, degrading
    gracefully so template processing can continue.
    """
    if not skip_checks:
        try:
            return _setup_gcp_environment(
                auto_approve=mode.auto_approve,
                interactive=mode.interactive,
                region=region,
                agent_garden=agent_garden,
            )
        except Exception as e:
            logging.debug("GCP environment setup failed: %s", e)
            logging.warning("%s. Continuing with template processing...", e)

    # Checks skipped or failed (e.g. the aiplatform.googleapis.com API not enabled): still try to
    # resolve a project ID so the generated .env has a valid value for local
    # development.
    try:
        project_id = resolve_gcp_project()
        if project_id:
            logging.debug("Using project ID from env / gcloud config: %s", project_id)
            return {"project": project_id}
    except Exception as e:
        logging.debug("Could not get project ID from gcloud: %s", e)
    return {}


def _setup_gcp_environment(
    *,
    auto_approve: bool,
    region: str,
    agent_garden: bool = False,
    interactive: bool = False,
) -> dict:
    """Set up the GCP environment with proper credentials and project.

    Args:
        auto_approve: Whether to skip confirmation prompts
        region: GCP region for deployment
        agent_garden: Whether this deployment is from Agent Garden
        interactive: Whether to show interactive prompts

    Returns:
        Dictionary with credential information
    """
    logging.debug("Verifying GCP credentials...")

    context = "agent-garden" if agent_garden else None

    # Interactive mode: show prompts and allow user to change credentials
    if interactive and not agent_garden:
        creds_info = _handle_interactive_credentials(context)
    else:
        # Non-interactive mode (auto-approve or strict programmatic)
        console.print("> Verifying GCP credentials...")
        creds_info = verify_credentials_and_vertex(context=context, interactive=False)
        console.print(f"> ✓ Connected to project: {creds_info['project']}")

    return creds_info


def _handle_interactive_credentials(context: str | None = None) -> dict:
    """Handle interactive credential verification and project selection.

    Args:
        context: Optional context for user agent

    Returns:
        Dictionary with credential information
    """
    # First, get credentials to show to user
    console.print("> Verifying GCP credentials...")
    try:
        creds_info = verify_credentials_and_vertex(context=context, interactive=True)
    except Exception:
        # If verification fails, we still want to show what we can and let user fix it
        from google.agents.cli.auth import get_adc_credentials

        try:
            credentials, project = get_adc_credentials()
            account = getattr(credentials, "service_account_email", None) or getattr(
                credentials, "_account", None
            )
            if not account:
                result = run_gcloud_command(
                    ["config", "get-value", "account"],
                    check=False,
                    capture_output=True,
                )
                account = result.stdout.strip() or "Unknown"
            creds_info = {"project": project or "Unknown", "account": account}
        except Exception:
            creds_info = {"project": "Unknown", "account": "Unknown"}

    # Check if running in Cloud Shell with no project
    if os.environ.get("CLOUD_SHELL") == "true" and not creds_info.get("project"):
        console.print(
            "> It looks like you are running in Cloud Shell.", style="bold blue"
        )
        console.print(
            "> You need to set up a project ID to continue.",
            style="bold blue",
        )
        new_project = Prompt.ask("\n> Enter a project ID", default=None)
        while not new_project:
            console.print(
                "> Project ID cannot be empty. Please try again.", style="bold red"
            )
            new_project = Prompt.ask("\n> Enter a project ID", default=None)
        set_gcp_project(new_project, set_quota_project=False)
        # Re-verify with new project
        return verify_credentials_and_vertex(context=context, interactive=True)

    # Show current credentials and ask user
    console.print(f"\n> You are logged in with account: '{creds_info['account']}'")
    console.print(f"> You are using project: '{creds_info['project']}'")

    choices = ["y", "skip", "edit"]
    response = Prompt.ask(
        "> Do you want to continue? (The CLI will check if the aiplatform.googleapis.com API is enabled in this project)",
        choices=choices,
        case_sensitive=False,
        default="y",
    ).lower()

    if response == "skip":
        console.print("> Skipping verification", style="yellow")
        return creds_info

    if response == "edit":
        # Handle credential change
        console.print("\n> Initiating new login...")
        try:
            run_gcloud_command(["auth", "login", "--update-adc"], check=True)
            console.print("> Login successful.")
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logging.warning("Login failed: %s. Continuing with template processing...", e)

    # Verify credentials and the aiplatform.googleapis.com API (with interactive API enablement prompt)
    console.print("> Testing Gemini platform connection...")
    creds_info = verify_credentials_and_vertex(context=context, interactive=True)
    console.print(f"> ✓ Connected to project: {creds_info['project']}")

    return creds_info


def set_gcp_project(project_id: str, set_quota_project: bool = True) -> None:
    """Set the GCP project and optionally the application default quota project.

    Args:
        project_id: The GCP project ID to set.
        set_quota_project: Whether to set the application default quota project.
    """
    try:
        run_gcloud_command(
            ["config", "set", "project", project_id],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        console.print(f"\n> Error setting project to {project_id}:")
        console.print(e.stderr)
        raise

    if set_quota_project:
        try:
            run_gcloud_command(
                ["auth", "application-default", "set-quota-project", project_id],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            logging.debug("Setting quota project failed: %s", e.stderr)

    console.print(f"> Successfully configured project: {project_id}")


def _render_project(
    selection: AgentSelection,
    loaded: LoadedTemplateConfig,
    *,
    location: ProjectLocation,
    plan: RenderPlan,
    inputs: CreationInputs,
    mode: InteractionMode,
) -> None:
    """Render the resolved template into the destination and finalize the project.

    Processes the template (local or remote), rewrites the region when it differs
    from the default, and re-adds the base template's inherited dependencies for
    remote templates.
    """
    destination_dir = location.destination_dir

    # Built-in agents resolve their template path here; remote/local templates
    # already carry it in `loaded.template_path`.
    template_path = loaded.template_path
    if not selection.template_source_path:
        template_path = template.get_template_path(selection.final_agent)

    logging.debug("Template path: %s", template_path)
    logging.debug("Processing template for project: %s", location.project_name)

    if not destination_dir.exists():
        destination_dir.mkdir(parents=True)
    logging.debug("Output directory: %s", destination_dir)

    # Construct CLI overrides for template processing
    final_cli_overrides = loaded.cli_overrides or {}
    if inputs.agent_directory:
        if "settings" not in final_cli_overrides:
            final_cli_overrides["settings"] = {}
        final_cli_overrides["settings"]["agent_directory"] = inputs.agent_directory

    # Only a project overlaid onto itself keeps its manifest; a template's
    # manifest describes the template.
    overlay_is_project = _overlays_itself(selection)

    # An in-folder render must not rename the project's agent. The name is
    # recorded in the manifest and baked into the agent source, so re-deriving
    # it from the project name would silently undo a rename the user made.
    existing_config = find_project_config(destination_dir) if location.in_folder else None
    recorded_root_agent_name = existing_config.root_agent_name if existing_config else ""
    # Same for the app name: it is baked into the server, and the routes follow it.
    recorded_app_name = existing_config.raw_app_name if existing_config else ""

    # Enhancing a project agents-cli did not create (no manifest) with another
    # template: the files already there are the user's, so the ones the render
    # replaces are moved aside. A project with a manifest, or `local@.`, is
    # re-rendered in place.
    keep_existing_files = (
        location.in_folder and not overlay_is_project and existing_config is None
    )

    try:
        # The project records the spec it was fetched from, so enhance and
        # upgrade can fetch it again. `local@.` names the project itself, not a
        # template that can be fetched, so it records the base template it
        # rendered. A project with a manifest keeps its own: the overlay copies
        # it back.
        recorded_base_template = (
            loaded.base_template_name if overlay_is_project else selection.recorded_spec
        )
        # Process template (handles both local and remote templates)
        rendered_files = template.process_template(
            agent_name=selection.final_agent,
            template_dir=template_path,
            project_name=location.project_name,
            deployment_target=plan.deployment_target,
            cicd_runner=plan.cicd_runner,
            session_type=plan.session_type,
            output_dir=destination_dir,
            remote_template_path=selection.template_source_path,
            remote_config=loaded.config,
            template_repo_root=selection.template_repo_root,
            in_folder=location.in_folder,
            overlay_is_project=overlay_is_project,
            keep_existing_files=keep_existing_files,
            recorded_base_template=recorded_base_template,
            cli_overrides=final_cli_overrides,
            agent_garden=inputs.agent_garden,
            remote_spec=selection.remote_spec,
            google_cloud_project=plan.google_cloud_project,
            bq_analytics=selection.bq_analytics,
            memory_bank=inputs.memory_bank,
            agent_gateway=bool(inputs.agent_gateway),
            agent_runtime_identity_default=plan.agent_runtime_identity_default,
            cloud_run_identity_default=plan.cloud_run_identity_default,
            agent_guidance_filename=inputs.agent_guidance_filename,
            root_agent_name=inputs.root_agent_name or recorded_root_agent_name,
            app_name=recorded_app_name,
        )

        # Replace the region if a different one was specified, only in the
        # rendered files when the rest of the project is the user's.
        if plan.region != "us-east1":
            replace_region_in_files(location.project_path, plan.region, rendered_files)

        # Remote templates inherit base-template files (app_utils/a2a.py,
        # fast_api_app.py, the integration e2e tests) that import packages the
        # remote's own pyproject may not declare — the config merge lets the
        # remote override the base template's extra_dependencies. Re-add the
        # resolved base template's deps on the fly so the inherited code (e.g.
        # `import a2a`) resolves. Skip with --skip-deps (reusing a saved config).
        if loaded.remote_config and not inputs.skip_deps:
            if loaded.base_template_name is None:
                raise RuntimeError("remote_config set without a base template")
            base_template_path = template.get_template_path(loaded.base_template_name)
            base_config = template.load_template_config(base_template_path)
            base_deps = base_config.get("settings", {}).get("extra_dependencies", [])

            if base_deps:
                template.add_base_template_dependencies(
                    location.project_path,
                    base_deps,
                    loaded.base_template_name,
                    auto_approve=mode.auto_approve,
                    interactive=mode.interactive,
                )

            # The merged go.mod has to be tidy for the image's read-only build.
            if go_overlay.applies_to(
                (loaded.config.get("settings") or {}).get("language"),
                overlay_is_project=overlay_is_project,
            ):
                go_overlay.tidy(location.project_path)

    except ValueError as e:
        # process_template raises ValueError for input the user can fix, so it
        # gets one line. Any other exception is our bug and keeps its traceback.
        raise click.ClickException(str(e)) from e


def _overlays_itself(selection: AgentSelection) -> bool:
    return (
        isinstance(selection.agent, str)
        and selection.agent.strip().rstrip("/") == "local@."
    )


def replace_region_in_files(
    project_path: pathlib.Path,
    new_region: str,
    files: Sequence[pathlib.Path] | None,
) -> None:
    """Replace all instances of 'us-east1' with the specified region in project files.
    Also handles agent_platform_search region mapping.

    Args:
        project_path: Path to the project directory
        new_region: The new region to use
        files: Paths relative to project_path to limit the replacement to, or
            None for every file in the project
    """
    logging.debug("Replacing region 'us-east1' with '%s' in %s", new_region, project_path)

    # Define allowed file extensions
    allowed_extensions = {
        ".md",
        ".py",
        ".go",
        ".tfvars",
        ".yaml",
        ".tf",
        ".yml",
    }

    # Skip directories that shouldn't be modified
    skip_dirs = {".git", "__pycache__", "venv", ".venv", "node_modules"}

    paths = (
        project_path.rglob("*") if files is None else (project_path / f for f in files)
    )
    for file_path in paths:
        # Skip directories and files with unwanted extensions
        if (
            file_path.is_dir()
            or any(skip_dir in file_path.parts for skip_dir in skip_dirs)
            or (
                file_path.suffix not in allowed_extensions
                and file_path.name not in allowed_extensions
            )
        ):
            continue

        try:
            content = file_path.read_text()
            modified = False

            # Replace standard region references
            if "us-east1" in content:
                logging.debug("Replacing region in %s", file_path)
                content = content.replace("us-east1", new_region)
                modified = True

            if modified:
                file_path.write_text(content)

        except UnicodeDecodeError:
            # Skip files that can't be read as text
            continue


def _print_next_steps(location: ProjectLocation, plan: RenderPlan) -> None:
    """Print the post-creation success banner and next-step hints."""
    if not location.in_folder:
        cd_path = (
            (location.destination_dir / location.project_name)
            if location.output_dir
            else location.project_name
        )
    else:
        cd_path = "."

    console.print("\n[bold green]✅ Success![/] Your agent project is ready.\n")

    console.print("[bold cyan]📖 Documentation[/]")
    console.print(f"   README:    [cyan]cat {cd_path}/README.md[/]")

    # Show enhance hint for prototype mode
    if plan.deployment_target == "none":
        console.print(
            "\n[bold cyan]💡 Tip[/]\n"
            "   Add a deployment target later with: [cyan]agents-cli scaffold enhance[/]"
        )
    elif plan.cicd_runner == "skip":
        console.print(
            "\n[bold cyan]💡 Tip[/]\n"
            "   Once ready for production, run: [cyan]agents-cli scaffold enhance[/]"
        )

    if _env_has_placeholder_project(location.project_path / ".env"):
        console.print(
            "\n[bold yellow]⚠️  Set your GCP project[/]\n"
            "   No GCP project was detected, so .env still has "
            f"GOOGLE_CLOUD_PROJECT={GCP_PROJECT_PLACEHOLDER}.\n"
            f"   Edit [cyan]{cd_path}/.env[/] and set it to your project ID "
            "before running the agent."
        )

    # Languages agents-cli can't run yet still ship Makefile targets for both.
    language = read_project_config(str(location.project_path)).language
    tool = "agents-cli" if PLAYGROUND_HANDLERS.get(language) else "make"
    console.print("\n[bold cyan]🚀 Get Started[/]")
    console.print(
        f"   [bold bright_green]cd {cd_path} && {tool} install && {tool} playground[/]"
    )

    parent_skills_dir = find_parent_workspace_skills(location.project_path)
    if parent_skills_dir:
        console.print(
            "\n[bold cyan]🧩 Skills[/]\n"
            "   agents-cli skills are installed outside the project, in:\n"
            f'     [cyan]"{parent_skills_dir}"[/]\n'
            "   Start your coding agent in that directory, or install them in the "
            "project by running:\n"
            f'     [cyan]cd "{cd_path}" && agents-cli setup --workspace[/]'
        )


def _env_has_placeholder_project(env_path: pathlib.Path) -> bool:
    """Whether the generated .env still carries the placeholder project ID."""
    # A remote template may not ship a .env.
    if not env_path.is_file():
        return False
    # Tolerate an `export` prefix, spaces around `=` and a quoted value.
    placeholder = re.escape(GCP_PROJECT_PLACEHOLDER)
    pattern = (
        rf"""^\s*(?:export\s+)?GOOGLE_CLOUD_PROJECT\s*=\s*(["']?){placeholder}\1\s*$"""
    )
    return (
        re.search(pattern, env_path.read_text(encoding="utf-8"), re.MULTILINE) is not None
    )
