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

"""Upgrade command for upgrading existing projects to newer Agents CLI versions."""

import filecmp
import logging
import pathlib
import shlex
import subprocess
import tempfile
from collections.abc import Callable

import click
from rich.prompt import Prompt

from google.agents.cli import _tools
from google.agents.cli._output import Console, configure_logging
from google.agents.cli._project import find_project_config, find_project_root

from ..utils import adk_shortcut
from ..utils.backup import make_backup_pre_apply_hook
from ..utils.cli_options import InteractionMode
from ..utils.generation_metadata import metadata_to_cli_args
from ..utils.language import get_language_config
from ..utils.merge import (
    MergeResult,
    copy_file,
    display_dependency_changes,
    is_fetchable_version,
    run_create_command,
    run_three_way_merge,
)
from ..utils.template import resolve_agent_alias
from ..utils.upgrade import (
    FILE_CATEGORIES,
    UNION_DEPENDENCY_HANDLERS,
    WRITE_DEPENDENCY_HANDLERS,
    DependencyReadError,
    DependencyResolution,
    categorize_file,
    collect_all_files,
    migrate_legacy_evalsets,
    migrate_legacy_python_config,
    update_acli_metadata,
    warn_legacy_eval_config,
)
from ..utils.version import get_current_version

console = Console()

_NOT_UPDATED = "No project files were updated to the new template."
_NEEDS_OLD_TEMPLATE = (
    "The upgrade needs the template of the version that created this project "
    "to find the files you changed."
)


def _display_version_header(old_version: str, new_version: str) -> None:
    """Display the upgrade version header."""
    console.print()
    console.print(f"[bold blue]📦 Upgrading {old_version} → {new_version}[/bold blue]")
    console.print()


def _display_overwrite_plan(overwrite: list[str], protected: list[str]) -> None:
    """Display which files force mode will overwrite and which it will not."""
    console.print()
    console.print(
        "[bold yellow]Force mode: scaffolding files will be overwritten "
        "with the new template, discarding your edits to them.[/bold yellow]"
    )
    console.print()

    if overwrite:
        console.print("[bold yellow]Will overwrite:[/bold yellow]")
        for relative_path in overwrite:
            console.print(f"  [yellow]![/yellow] {relative_path}")
        console.print()

    if protected:
        console.print(
            "[dim]Not overwritten (your agent code, config, docs, evals and manifests):[/dim]"
        )
        for relative_path in protected:
            console.print(f"  [dim]-[/dim] {relative_path}")
        console.print()


def _union_merge_dependencies(
    project_dir: pathlib.Path,
    template_project: pathlib.Path,
    language: str,
) -> list[DependencyResolution]:
    """Union-merge dependencies with the new template. Returns [] if it cannot."""
    union_dependencies = UNION_DEPENDENCY_HANDLERS.get(language)
    if union_dependencies is None:
        logging.warning(
            "Dependency merging isn't supported for %s projects yet — your "
            "dependency manifest is left unchanged. Review it manually after "
            "the upgrade.",
            language,
        )
        return []

    try:
        return union_dependencies(project_dir, template_project)
    except DependencyReadError as e:
        logging.warning(
            "Skipping dependency merge — %s. Review your dependency manifest "
            "manually after the upgrade.",
            e,
        )
        return []


def _warn_force_mode_risks(agent_directory: str, agent_file: str) -> None:
    """Warn about the two things force mode cannot check for the user."""
    never_overwritten = (
        [
            pattern.replace("{agent_directory}", agent_directory)
            for pattern in FILE_CATEGORIES["agent_code"]
            if pattern.endswith(pathlib.PurePath(agent_file).suffix)
        ]
        + FILE_CATEGORIES["config_files"]
        + FILE_CATEGORIES["user_content"]
    )
    logging.warning(
        "Force mode overwrites scaffolding, including other files in %s/. "
        "Kept: %s, dependency files (merged) and files not in the template.",
        agent_directory,
        ", ".join(never_overwritten),
    )
    logging.warning(
        "Nothing is removed: dependencies are merged as a union and files are "
        "never deleted. A package or file the new template dropped may stay; "
        "remove it manually if it is unused."
    )


def _run_force_upgrade(
    *,
    cli_args: list[str],
    project_dir: pathlib.Path,
    project_name: str,
    agent_directory: str,
    language: str,
    agent_file: str,
    new_version: str,
    dry_run: bool,
    interactive: bool,
    backup_hook: Callable[[pathlib.Path], bool],
    recorded_base_template: str | None,
) -> None:
    """Overwrite scaffolding from the new template instead of merging it.

    ``recorded_base_template`` is the base template name to write to the manifest.
    """
    with tempfile.TemporaryDirectory(prefix="acli_upgrade_overwrite_") as temp_base:
        output_dir = pathlib.Path(temp_base)

        console.print()
        console.print("[dim]Generating template...[/dim]")
        if not run_create_command(cli_args, output_dir, project_name):
            raise _new_template_error(new_version)

        template_project = output_dir / project_name

        overwrite: list[str] = []
        protected: list[str] = []
        for relative_path in sorted(collect_all_files([template_project])):
            category = categorize_file(relative_path, agent_directory)
            template_file = template_project / relative_path
            project_file = project_dir / relative_path
            if category not in ("scaffolding", "user_content"):
                protected.append(relative_path)
            elif not project_file.exists():
                overwrite.append(relative_path)
            elif category == "user_content":
                protected.append(relative_path)
            elif not filecmp.cmp(template_file, project_file, shallow=False):
                overwrite.append(relative_path)

        _display_overwrite_plan(overwrite, protected)

        dep_resolutions = _union_merge_dependencies(
            project_dir, template_project, language
        )
        display_dependency_changes(dep_resolutions)

        _warn_force_mode_risks(agent_directory, agent_file)

        if dry_run:
            console.print(
                "[bold yellow]Dry run complete.[/bold yellow] "
                "Run without --dry-run to apply changes."
            )
            return

        if interactive:
            proceed = Prompt.ask(
                "\nProceed with upgrade?",
                choices=["y", "n"],
                case_sensitive=False,
                default="n",
            ).lower()
            if proceed != "y":
                console.print("[yellow]Upgrade cancelled.[/yellow]")
                return

        if not backup_hook(project_dir):
            return  # user declined to continue without a backup

        overwritten = added = 0
        for relative_path in overwrite:
            project_file = project_dir / relative_path
            existed = project_file.exists()
            if copy_file(template_project / relative_path, project_file):
                if existed:
                    overwritten += 1
                else:
                    added += 1

        write_dependencies = WRITE_DEPENDENCY_HANDLERS.get(language)
        should_write_dependencies = bool(
            any(r.status not in ("unchanged", "kept") for r in dep_resolutions)
            and write_dependencies is not None
        )
        wrote_dependencies = bool(
            should_write_dependencies and write_dependencies(project_dir, dep_resolutions)
        )

        if should_write_dependencies and not wrote_dependencies:
            raise click.ClickException(
                "Project files were updated, but dependencies were not. "
                "Fix the error above and rerun."
            )

        update_acli_metadata(
            project_dir,
            {},
            acli_version=new_version,
            base_template=recorded_base_template,
        )

        if wrote_dependencies:
            console.print()
            console.print(
                "[dim] Dependencies updated — run [bold]agents-cli install[/bold] "
                "to install the new versions.[/dim]"
            )
        console.print()
        console.print(f"  Overwritten: {overwritten} files")
        console.print(f"  Added: {added} files")
        console.print()
        console.print("[bold green]✅ Upgrade complete![/bold green]")


@click.command()
@click.argument(
    "project_path",
    type=click.Path(exists=True, path_type=pathlib.Path),
    default=".",
    required=False,
)
@click.option(
    "--dry-run",
    "--dryrun",
    is_flag=True,
    help="Preview changes without applying them",
)
@click.option(
    "--auto-approve",
    "--yes",
    "-y",
    is_flag=True,
    help="Auto-apply non-conflicting changes without prompts",
)
@click.option(
    "--interactive",
    "-i",
    is_flag=True,
    default=False,
    help="Enable interactive prompts for human use",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite scaffolding files with the new template (skip the 3-way merge)",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug logging",
)
def upgrade(
    project_path: pathlib.Path,
    *,
    dry_run: bool,
    auto_approve: bool,
    interactive: bool,
    force: bool,
    debug: bool,
) -> None:
    """Upgrade project to a newer agents-cli version.

    Applies a 3-way merge between the old template, the new template, and your
    project: unmodified files are auto-updated, your customizations are preserved,
    and conflicts are surfaced for manual resolution (with --interactive) or kept as-is.

    Use --force if the merge fails. It overwrites scaffolding files with the new
    template and keeps agent code and config files. It never removes dependencies
    or files the new template dropped.
    """
    if debug:
        configure_logging(logging.DEBUG)
        console.print("[dim]Debug mode enabled[/dim]")

    # Resolve project path
    project_dir = project_path.resolve()
    # Handle the case where we're in a subdirectory under the project root.
    project_root_dir = find_project_root(project_dir)
    if project_root_dir is not None:
        project_dir = project_root_dir
        console.print(f"[dim]Resolved project root to: {project_dir}[/dim]")

    migrate_legacy_python_config(project_dir, dry_run=dry_run)

    metadata = find_project_config(project_dir)
    if not metadata:
        raise click.ClickException(
            "No agents-cli metadata found.\n"
            "Ensure agents-cli-manifest.yaml exists in your project root."
        )

    # Get language from metadata for language-aware operations
    language = metadata.language
    # Raises for a language this agents-cli doesn't support, before any
    # template is generated.
    agent_file = get_language_config(language)["agent_file"]

    # Version is normalized to acli_version by find_project_config
    old_version = metadata.acli_version
    if not old_version:
        raise click.ClickException(
            "No acli_version found in project metadata.\n"
            "The project metadata is missing the version. "
            "Please ensure agents-cli-manifest.yaml has acli_version set."
        )

    new_version = get_current_version()

    # Check if upgrade is needed
    if old_version == new_version:
        console.print(
            f"[bold green]✅[/bold green] Project is already at version {new_version}"
        )
        return

    # --force does not build the old template, so it needs neither check.
    # Both run up front: an unpublished version fails before asking for uvx,
    # and a missing uvx gets an install hint instead of the --force hint below.
    if not force:
        if not is_fetchable_version(old_version):
            raise _not_fetchable_version_error(old_version, project_path)
        _tools.require_tool("uvx")

    _display_version_header(old_version, new_version)

    # The old CLI replayed by the merge maps adk@ to a removed path, so pass it
    # the recipe URL. The manifest is only rewritten once applied.
    migrated_base_template = adk_shortcut.resolve_recorded_adk_shortcut(
        metadata.base_template
    )
    if migrated_base_template:
        metadata.base_template = migrated_base_template
    # Once applied, the manifest records the canonical name (adk -> adk_python).
    # metadata keeps the recorded name: the old CLI replayed by the merge needs it.
    recorded_base_template = migrated_base_template or resolve_agent_alias(
        metadata.base_template
    )

    # Get project name and CLI args from metadata
    project_name = metadata.project_name or project_dir.name
    agent_directory = metadata.agent_directory or "app"
    cli_args = metadata_to_cli_args(metadata)
    old_cli_args = metadata_to_cli_args(metadata, target_version=old_version)

    mode = InteractionMode(interactive=interactive, auto_approve=auto_approve)

    # -- Pre-apply hook: back up the project before writing changes ----------
    backup_hook = make_backup_pre_apply_hook(
        console=console,
        mode=mode,
    )

    # Post-apply: stamp the new version into the manifest
    def _update_version(proj_dir: pathlib.Path, lang: str) -> None:
        update_acli_metadata(
            proj_dir,
            {},
            acli_version=new_version,
            base_template=recorded_base_template,
        )

    # Migrate before the merge so a customized evalset isn't clobbered by the
    # stock template default the merge would otherwise copy in first.
    migrate_legacy_evalsets(project_dir, dry_run=dry_run)
    warn_legacy_eval_config(project_dir)

    if force:
        _run_force_upgrade(
            cli_args=cli_args,
            project_dir=project_dir,
            project_name=project_name,
            agent_directory=agent_directory,
            language=language,
            agent_file=agent_file,
            new_version=new_version,
            dry_run=dry_run,
            interactive=interactive,
            backup_hook=backup_hook,
            recorded_base_template=recorded_base_template,
        )
        return

    result = run_three_way_merge(
        project_dir=project_dir,
        project_name=project_name,
        agent_directory=agent_directory,
        language=language,
        old_args=old_cli_args,
        new_args=cli_args,
        old_version=old_version,
        mode=mode,
        dry_run=dry_run,
        operation_label="upgrade",
        pre_apply_hook=backup_hook,
        post_apply_hook=_update_version,
    )

    if result is MergeResult.OLD_TEMPLATE_FAILED:
        raise _old_template_error(old_version, project_path)
    if result is MergeResult.NEW_TEMPLATE_FAILED:
        raise _new_template_error(new_version)


def _not_fetchable_version_error(
    old_version: str, project_path: pathlib.Path
) -> click.ClickException:
    return click.ClickException(
        f"agents-cli {old_version} (acli_version in agents-cli-manifest.yaml) "
        f"is not published on PyPI. {_NEEDS_OLD_TEMPLATE}\n"
        f"{_NOT_UPDATED}\n"
        f"To upgrade anyway, {_force_hint(project_path)}"
    )


def _force_hint(project_path: pathlib.Path) -> str:
    """Tell the user how to preview and run --force on the same project."""
    command = "agents-cli scaffold upgrade"
    path = str(project_path)
    if path != ".":
        quoted = (
            subprocess.list2cmdline([path]) if _tools.is_windows() else shlex.quote(path)
        )
        command += f" {quoted}"
    return (
        f"run `{command} --force --dry-run` to see which scaffolding files "
        "--force would overwrite, then run it without --dry-run. "
        "--force backs up the project first."
    )


def _old_template_error(
    old_version: str, project_path: pathlib.Path
) -> click.ClickException:
    return click.ClickException(
        f"Could not build the agents-cli {old_version} template. "
        f"{_NEEDS_OLD_TEMPLATE}\n"
        f"{_NOT_UPDATED}\n"
        "If the error is a network or proxy problem, fix it and run the "
        "upgrade again.\n"
        f"Otherwise, {_force_hint(project_path)}"
    )


def _new_template_error(new_version: str) -> click.ClickException:
    return click.ClickException(
        f"Could not build the agents-cli {new_version} template.\n"
        f"{_NOT_UPDATED}\n"
        "Fix the error above and run the upgrade again."
    )
