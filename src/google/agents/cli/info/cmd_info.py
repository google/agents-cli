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

"""Show project configuration, paths, and CLI version."""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Any

import click

import google.agents.cli as _cli_pkg
from google.agents.cli import _telemetry
from google.agents.cli.__init__ import __version__
from google.agents.cli._output import emit
from google.agents.cli._project import (
    CREATE_PROJECT_HINT,
    check_cli_version,
    find_project_root,
    read_project_config,
    resolve_root_agent_name,
)
from google.agents.cli._skills_check import (
    find_parent_workspace_skills,
    get_installed_skills,
)
from google.agents.cli.extension._loader import ExtensionSet, current_extension_set

_CLI_INSTALL_PATH = str(Path(_cli_pkg.__file__).parent)


def _telemetry_status(enabled: bool) -> str:
    """Human-readable telemetry state + how to change it."""
    if enabled:
        return "enabled (opt out with AGENTS_CLI_TELEMETRY=0 or DO_NOT_TRACK=1)"
    return "disabled"


def _print_extensions(extension_set: ExtensionSet) -> None:
    rows = extension_set.command_rows()
    conflicts = extension_set.conflict_rows()
    incompatible = extension_set.incompatible_rows()
    if not rows and not conflicts and not incompatible:
        return
    click.echo()
    click.echo("Extensions (experimental):")
    for r in rows:
        line = (
            f"  {r['command']}  ({r['kind']}, {r['scope']}) "
            f"<- {r['extension']}: {' '.join(r['run'])}"
        )
        if r.get("requires"):
            line += f"  [requires agents-cli {r['requires']}]"
        if r.get("blocked"):
            line += "  [BLOCKED: out of range, this command will not run]"
        click.echo(line)
    for c in conflicts:
        click.echo(f"  ! conflict: {c} (claimed by multiple same-scope extensions)")
    for inc in incompatible:
        click.echo(
            f"  ! incompatible: {inc['extension']} requires agents-cli "
            f"{inc['requires']} (running {inc['running']})"
        )


def _print_installed_skills(
    skills: list[dict] | None,
) -> None:
    """Print installed skills summary."""
    if skills is None:
        click.echo("Installed skills:   (could not query)")
        return
    if not skills:
        click.echo("Installed skills:   none")
        return
    # Group by scope
    by_scope: dict[str, list[str]] = {}
    for s in skills:
        scope = s.get("scope", "unknown")
        by_scope.setdefault(scope, []).append(s["name"])
    for scope, names in sorted(by_scope.items()):
        click.echo(f"Installed skills:   {len(names)} ({scope})")
        for name in sorted(names):
            click.echo(f"  - {name}")


def _path_or_none(path: Path | None) -> str | None:
    return str(path) if path else None


def _print_parent_workspace_skills_hint(
    parent_skills_dir: Path | None, skills_target_dir: Path
) -> None:
    if parent_skills_dir is None:
        return
    click.echo("  Workspace skills found in a parent directory:")
    click.echo(f'    "{parent_skills_dir}"')
    click.echo("  Coding agents started in this directory may not load them.")
    click.echo("  Start your coding agent in:")
    click.echo(f'    "{parent_skills_dir}"')
    click.echo("  Or install them by running:")
    click.echo(f'    cd "{skills_target_dir}" && agents-cli setup --workspace')


@click.command()
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
def cmd_info(as_json: bool) -> None:
    """Show project configuration, paths, and CLI version."""
    installed_skills = get_installed_skills()
    project_root = find_project_root()
    # Only worth pointing at a parent's skills when none load here. Start at the
    # project root so skills installed there aren't reported as a parent's.
    skills_target_dir = project_root or Path.cwd()
    parent_skills_dir = (
        None if installed_skills else find_parent_workspace_skills(skills_target_dir)
    )
    os_info = platform.platform()
    telemetry_enabled = _telemetry.is_telemetry_enabled()
    extension_set = current_extension_set()
    if project_root is None:
        if as_json:
            payload: dict[str, Any] = {
                "cli_version": __version__,
                "cli_install_path": _CLI_INSTALL_PATH,
                "os_info": os_info,
                "installed_skills": installed_skills,
                "parent_workspace_skills_dir": _path_or_none(parent_skills_dir),
                "project": None,
                "extensions": extension_set.command_rows(),
                "extension_conflicts": extension_set.conflict_rows(),
                "extension_incompatible": extension_set.incompatible_rows(),
                "telemetry_enabled": telemetry_enabled,
            }
            emit(payload)
        else:
            click.echo(f"CLI version:        {__version__}")
            click.echo(f"CLI install path:   {_CLI_INSTALL_PATH}")
            click.echo(f"OS info:            {os_info}")
            click.echo(f"Telemetry:          {_telemetry_status(telemetry_enabled)}")
            _print_installed_skills(installed_skills)
            _print_parent_workspace_skills_hint(parent_skills_dir, skills_target_dir)
            _print_extensions(extension_set)
            click.echo()
            click.echo("No agent project found in the current directory or any parent.")
            click.echo("  Run this command from within a project, or create one:")
            click.echo(CREATE_PROJECT_HINT)
        return

    cfg = read_project_config(str(project_root))
    check_cli_version(cfg)

    info = {
        "cli_version": __version__,
        "cli_install_path": _CLI_INSTALL_PATH,
        "os_info": os_info,
        "installed_skills": installed_skills,
        "parent_workspace_skills_dir": _path_or_none(parent_skills_dir),
        "project_root": str(project_root),
        "project_name": cfg.project_name,
        "root_agent_name": resolve_root_agent_name(cfg),
        "language": cfg.language,
        "deployment_target": cfg.deployment_target,
        "agent_directory": cfg.agent_directory,
        "is_a2a": cfg.is_a2a,
        "region": cfg.region,
        "extensions": extension_set.command_rows(),
        "extension_conflicts": extension_set.conflict_rows(),
        "extension_incompatible": extension_set.incompatible_rows(),
        "telemetry_enabled": telemetry_enabled,
    }

    if as_json:
        emit(info)
        return

    click.echo(f"CLI version:        {__version__}")
    click.echo(f"CLI install path:   {_CLI_INSTALL_PATH}")
    click.echo(f"OS info:            {os_info}")
    click.echo(f"Telemetry:          {_telemetry_status(telemetry_enabled)}")
    _print_installed_skills(installed_skills)
    _print_parent_workspace_skills_hint(parent_skills_dir, skills_target_dir)
    click.echo()
    click.echo(f"Project root:       {project_root}")
    click.echo(f"Project name:       {cfg.project_name or '(not set)'}")
    click.echo(f"Root agent name:    {resolve_root_agent_name(cfg)}")
    click.echo(f"Language:           {cfg.language}")
    click.echo(f"Deployment target:  {cfg.deployment_target}")
    click.echo(f"Agent directory:    {cfg.agent_directory}")
    click.echo(f"Region:             {cfg.region}")
    if cfg.is_a2a:
        click.echo("A2A:                yes")
    _print_extensions(extension_set)
