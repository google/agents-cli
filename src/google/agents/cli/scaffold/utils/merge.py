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

"""Shared merge utilities for upgrade and enhance commands.

Functions for generating templates, displaying comparison results,
resolving conflicts, and applying file changes.  The central
``run_three_way_merge`` orchestrator is used by both the *upgrade* and
*enhance* commands to avoid duplicating the merge pipeline.
"""

import difflib
import enum
import logging
import pathlib
import shutil
import tempfile
import textwrap
from collections.abc import Callable

import click
from rich.markup import escape
from rich.prompt import Prompt

from google.agents.cli._output import Console
from google.agents.cli.extension._manifest import (
    EXTENSIONS_FILE,
    ExtensionEntry,
    ExtensionListError,
    check_extension_list,
    read_extension_entries,
    upsert_extension_entry,
)

from .cli_options import InteractionMode
from .upgrade import (
    MERGE_DEPENDENCY_HANDLERS,
    WRITE_DEPENDENCY_HANDLERS,
    DependencyReadError,
    DependencyResolution,
    FileCompareResult,
    compare_all_files,
    group_results_by_action,
)

console = Console()

# Maximum characters to display when showing diffs
MAX_DIFF_DISPLAY_CHARS = 2000


class MergeResult(enum.Enum):
    """How ``run_three_way_merge`` ended."""

    DONE = "done"
    OLD_TEMPLATE_FAILED = "old_template_failed"
    NEW_TEMPLATE_FAILED = "new_template_failed"


def is_fetchable_version(version: str) -> bool:
    """True if ``version`` could be a release published on PyPI.

    Rejects the unknown-version sentinel, unparseable strings and PEP 440
    local versions (``1.0.0+build``), which PyPI never hosts. Whether the
    release actually exists is confirmed only by the fetch in
    ``_run_pinned_create``.
    """
    from packaging import version as pkg_version

    from .version import UNKNOWN_VERSION

    if version == UNKNOWN_VERSION:
        return False

    try:
        return pkg_version.parse(version).local is None
    except pkg_version.InvalidVersion:
        return False


def _run_vendored_create(
    args: list[str],
    output_dir: pathlib.Path,
    project_name: str,
) -> bool:
    """Generate the snapshot in-process with the bundled create command."""
    from click import Context

    from ..commands.create import create

    cli_args = [project_name]
    cli_args.extend(["--output-dir", str(output_dir)])
    # This runs in-process, so its output lands in the user's terminal.
    # --quiet keeps it silent. The pinned path deliberately does not pass it:
    # older published releases would reject it, and uvx output is captured.
    cli_args.extend(["--auto-approve", "--skip-deps", "--skip-checks", "--quiet"])
    cli_args.extend(args)

    logging.debug("Running vendored create command with args: %s", cli_args)

    try:
        ctx = Context(create, info_name="create")
        create.parse_args(ctx, list(cli_args))
        ctx.invoke(create, **ctx.params)

        # Verify the project was actually created (create command may
        # silently return without generating output on validation errors)
        expected_dir = output_dir / project_name
        if not expected_dir.exists():
            logging.error(
                f"Create command succeeded but project directory not found: {expected_dir}"
            )
            return False

        return True
    except SystemExit as e:
        if e.code == 0:
            return True
        logging.error(f"Create command failed with exit code {e.code}")
        return False
    except Exception as e:
        logging.error("Could not run the create command: %s", e)
        return False


def _run_pinned_create(
    args: list[str],
    output_dir: pathlib.Path,
    project_name: str,
    version: str,
) -> bool:
    """Generate the snapshot via ``uvx google-agents-cli@<version> scaffold create``.

    Returns true if the snapshot was generated, false otherwise.
    """
    from google.agents.cli._runner import run_resolved

    from .version import PACKAGE_NAME

    cmd = [
        "uvx",
        f"{PACKAGE_NAME}@{version}",
        "scaffold",
        "create",
        project_name,
        "--output-dir",
        str(output_dir),
        "--auto-approve",
        "--skip-deps",
        "--skip-checks",
        *args,
    ]

    logging.debug("Running pinned create via uvx: %s", cmd)

    try:
        result = run_resolved(cmd, capture_output=True, text=True)
    except Exception as e:
        logging.error("Could not run uvx to generate the %s template: %s", version, e)
        return False

    if result.returncode != 0:
        # Not parsed: uv changes its error layout between releases.
        logging.error(
            "uvx could not build the agents-cli %s template (exit %s):\n%s",
            version,
            result.returncode,
            textwrap.indent((result.stderr or "").strip(), "  "),
        )
        return False

    expected_dir = output_dir / project_name
    if not expected_dir.exists():
        logging.error(
            "uvx create for %s reported success but %s is missing.",
            version,
            expected_dir,
        )
        return False

    return True


def run_create_command(
    args: list[str],
    output_dir: pathlib.Path,
    project_name: str,
    version: str | None = None,
) -> bool:
    """Generate a template snapshot for the 3-way upgrade/enhance merge.

    A ``version`` that differs from the running CLI is re-created by that exact
    release, so the snapshot matches what it shipped. With no version, or the
    current one, the bundled create command is used.

    A version that cannot be fetched or built (the unknown-version sentinel, an
    unparseable string, a local version, a release missing from PyPI, a uvx or
    network failure) fails.

    Returns True if the template was generated, False otherwise.
    """
    from .version import get_current_version

    if not version or version == get_current_version():
        return _run_vendored_create(args, output_dir, project_name)

    if not is_fetchable_version(version):
        logging.error("agents-cli %s is not a version published on PyPI.", version)
        return False

    return _run_pinned_create(args, output_dir, project_name, version)


def display_results(
    groups: dict[str, list[FileCompareResult]],
    dep_resolutions: list[DependencyResolution],
    extension_entries: list[ExtensionEntry],
    dry_run: bool = False,
) -> None:
    """Display the comparison results grouped by action."""
    if groups["auto_update"]:
        console.print("[bold green]Will auto-update (unchanged by you):[/bold green]")
        for result in groups["auto_update"]:
            console.print(f"  [green]✓[/green] {result.path}")
        console.print()

    preserved_user_modified = [
        r for r in groups["preserve"] if r.preserve_type == "acli_unchanged"
    ]
    if preserved_user_modified:
        console.print(
            "[bold cyan]Will preserve (you modified, template unchanged):[/bold cyan]"
        )
        for result in preserved_user_modified:
            console.print(f"  [cyan]✓[/cyan] {result.path}")
        console.print()

    skipped = [r for r in groups["skip"] if r.category in ("agent_code", "config_files")]
    if skipped:
        console.print("[dim]Skipping (your code):[/dim]")
        for result in skipped:
            console.print(f"  [dim]-[/dim] {result.path}")
        console.print()

    if groups["new"]:
        console.print("[bold yellow]Files to add:[/bold yellow]")
        for result in groups["new"]:
            console.print(f"  [yellow]+[/yellow] {result.path}")
        console.print()

    if groups["removed"]:
        console.print("[bold yellow]Files to remove:[/bold yellow]")
        for result in groups["removed"]:
            console.print(f"  [yellow]-[/yellow] {result.path}")
        console.print()

    if groups["conflict"]:
        console.print("[bold red]Conflicts (both changed):[/bold red]")
        for result in groups["conflict"]:
            console.print(f"  [red]⚠[/red]  {result.path}")
        if not dry_run:
            console.print("[dim]  You'll be prompted to resolve each conflict.[/dim]")
        console.print()

    display_dependency_changes(dep_resolutions)
    display_extension_changes(extension_entries)


def display_dependency_changes(dep_resolutions: list[DependencyResolution]) -> None:
    """Display dependency resolutions, omitting the unchanged ones."""
    changed = [r for r in dep_resolutions if r.status != "unchanged"]
    if not changed:
        return

    console.print("[bold]Dependency changes:[/bold]")
    for resolution in changed:
        dep_name = escape(resolution.name)
        old_ver = escape(resolution.old_version or "")
        new_ver = escape(resolution.new_version or "")
        if resolution.status == "updated":
            console.print(f"  [green]✓[/green] Update: {dep_name} {old_ver} → {new_ver}")
        elif resolution.status == "added":
            console.print(f"  [green]+[/green] Add: {dep_name}{new_ver}")
        elif resolution.status == "kept":
            console.print(f"  [cyan]✓[/cyan] Keep (yours): {dep_name}{old_ver}")
        elif resolution.status == "removed":
            console.print(f"  [yellow]-[/yellow] Remove: {dep_name}{old_ver}")
    console.print()


def pending_template_extensions(
    project_dir: pathlib.Path, template_project: pathlib.Path
) -> list[ExtensionEntry]:
    """Return the template's extension entries to be merged into the project.

    Raises click.ClickException when the template has entries and the project's
    extension list cannot be read.
    """
    template_entries = read_extension_entries(template_project / EXTENSIONS_FILE)
    if not template_entries:
        return []
    manifest = project_dir / EXTENSIONS_FILE
    try:
        check_extension_list(manifest)
    except (OSError, ExtensionListError) as e:
        raise click.ClickException(
            f"Cannot record template extensions in {manifest}: {e}.\n"
            "No project files were changed. Fix the file, then rerun."
        ) from e
    recorded = set(read_extension_entries(manifest))
    return [entry for entry in template_entries if entry not in recorded]


def display_extension_changes(entries: list[ExtensionEntry]) -> None:
    """Display the extension entries to record, if any."""
    if not entries:
        return

    console.print(f"[bold]Extension changes ({EXTENSIONS_FILE}):[/bold]")
    for entry in entries:
        console.print(
            f"  [green]+[/green] Record: {escape(entry.name)} ({escape(entry.source)})"
        )
    console.print()


def record_template_extensions(
    project_dir: pathlib.Path, entries: list[ExtensionEntry]
) -> None:
    """Upsert ``entries`` into the project's extension list."""
    manifest = project_dir / EXTENSIONS_FILE
    for entry in entries:
        try:
            upsert_extension_entry(manifest, entry)
        except (OSError, ExtensionListError) as e:
            raise click.ClickException(
                f"Project files were updated, but template extensions were not "
                f"recorded in {manifest}: {e}. Fix the cause and rerun."
            ) from e


def handle_conflict(
    *,
    result: FileCompareResult,
    project_dir: pathlib.Path,
    new_template_dir: pathlib.Path,
    auto_approve: bool,
    prefer_new: bool = False,
    interactive: bool = False,
) -> str:
    """Handle a file conflict interactively.

    Args:
        result: The conflict result
        project_dir: Path to current project
        new_template_dir: Path to new template
        auto_approve: If True, keep user's version (unless prefer_new is set)
        prefer_new: If True with auto_approve, use new template version instead
        interactive: If True, show interactive conflict resolution menu

    Returns:
        Action taken: "kept", "kept_all", "updated", "updated_all", or "skipped"
    """
    if not interactive:
        # Non-interactive (auto-approve or strict programmatic): auto-keep user version
        if prefer_new:
            console.print(f"  [green]Using new version: {result.path}[/green]")
            return "updated"
        logging.warning(
            "Conflict: %s — keeping your version (use --prefer-new to override)",
            result.path,
        )
        return "kept"

    console.print(f"\n[bold yellow]Conflict: {result.path}[/bold yellow]")
    console.print(f"  Reason: {result.reason}")

    choice = Prompt.ask(
        "  (v)iew diff, (k)eep yours, (K)eep all, (u)se new, (U)se all, (s)kip",
        choices=["v", "k", "K", "u", "U", "s"],
        default="k",
    )

    if choice == "v":
        # Show diff using Python's difflib (cross-platform)
        current_file = project_dir / result.path
        new_file = new_template_dir / result.path

        try:
            current_lines = current_file.read_text(encoding="utf-8").splitlines(
                keepends=True
            )
            new_lines = new_file.read_text(encoding="utf-8").splitlines(keepends=True)

            diff_lines = list(
                difflib.unified_diff(
                    current_lines,
                    new_lines,
                    fromfile=f"Your version: {result.path}",
                    tofile=f"New version: {result.path}",
                )
            )
            diff_output = "".join(diff_lines)

            console.print()
            if diff_output:
                # Limit output to a reasonable length
                if len(diff_output) > MAX_DIFF_DISPLAY_CHARS:
                    console.print(diff_output[:MAX_DIFF_DISPLAY_CHARS])
                    console.print("[dim]... (truncated)[/dim]")
                else:
                    console.print(diff_output)
            else:
                console.print("[dim]No differences found[/dim]")
        except Exception as e:
            console.print(f"[red]Could not show diff: {e}[/red]")

        # Ask again after viewing
        choice = Prompt.ask(
            "  (k)eep yours, (K)eep all, (u)se new, (U)se all, (s)kip",
            choices=["k", "K", "u", "U", "s"],
            default="k",
        )

    if choice == "K":
        console.print("  [cyan]Keeping your version for all conflicts[/cyan]")
        return "kept_all"
    elif choice == "U":
        console.print("  [green]Using new version for all conflicts[/green]")
        return "updated_all"
    elif choice == "k":
        console.print("  [cyan]Keeping your version[/cyan]")
        return "kept"
    elif choice == "u":
        return "updated"
    else:
        return "skipped"


def copy_file(src: pathlib.Path, dst: pathlib.Path) -> bool:
    """Copy a file, creating parent directories as needed."""
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def apply_changes(
    *,
    groups: dict[str, list[FileCompareResult]],
    project_dir: pathlib.Path,
    new_template_dir: pathlib.Path,
    auto_approve: bool,
    dry_run: bool,
    prefer_new: bool = False,
    interactive: bool = False,
) -> dict[str, int]:
    """Apply file changes to the project."""
    counts = {
        "updated": 0,
        "added": 0,
        "removed": 0,
        "skipped": 0,
        "conflicts_kept": 0,
        "conflicts_updated": 0,
    }

    if dry_run:
        console.print("[bold yellow]Dry run - no changes made[/bold yellow]")
        return counts

    for result in groups["auto_update"]:
        if copy_file(new_template_dir / result.path, project_dir / result.path):
            counts["updated"] += 1

    for result in groups["new"]:
        if copy_file(new_template_dir / result.path, project_dir / result.path):
            counts["added"] += 1

    for result in groups["removed"]:
        file_path = project_dir / result.path
        if file_path.exists():
            file_path.unlink()
            counts["removed"] += 1

    if groups["conflict"]:
        console.print()
        console.print("[bold]Resolving conflicts:[/bold]")

    bulk_action = None  # "keep" or "update" when user chooses K or U
    for result in groups["conflict"]:
        if bulk_action == "keep":
            console.print(f"  [dim]Keeping your version: {result.path}[/dim]")
            counts["conflicts_kept"] += 1
            continue
        elif bulk_action == "update":
            if copy_file(new_template_dir / result.path, project_dir / result.path):
                console.print(f"  [green]Updated: {result.path}[/green]")
                counts["conflicts_updated"] += 1
            continue

        action = handle_conflict(
            result=result,
            project_dir=project_dir,
            new_template_dir=new_template_dir,
            auto_approve=auto_approve,
            prefer_new=prefer_new,
            interactive=interactive,
        )
        if action == "kept_all":
            counts["conflicts_kept"] += 1
            bulk_action = "keep"
        elif action == "updated_all":
            if copy_file(new_template_dir / result.path, project_dir / result.path):
                counts["conflicts_updated"] += 1
            bulk_action = "update"
        elif action == "updated":
            if copy_file(new_template_dir / result.path, project_dir / result.path):
                counts["conflicts_updated"] += 1
        elif action == "kept":
            counts["conflicts_kept"] += 1
        else:
            counts["skipped"] += 1

    return counts


def run_three_way_merge(
    *,
    project_dir: pathlib.Path,
    project_name: str,
    agent_directory: str,
    language: str,
    old_args: list[str],
    new_args: list[str],
    old_version: str | None = None,
    mode: InteractionMode,
    dry_run: bool,
    prefer_new: bool = False,
    operation_label: str = "upgrade",
    pre_apply_hook: Callable[[pathlib.Path], bool] | None = None,
    post_apply_hook: Callable[[pathlib.Path, str], None] | None = None,
    post_apply_no_changes_hook: Callable[[pathlib.Path, str], None] | None = None,
    rendered_manifest_hook: Callable[[pathlib.Path], None] | None = None,
) -> MergeResult:
    """Shared 3-way merge pipeline used by both *upgrade* and *enhance*.

    Generates old and new template snapshots, compares every file against
    the current project, displays the diff summary, and applies changes.

    Args:
        project_dir: Resolved path to the user's project.
        project_name: Name used when generating the template.
        agent_directory: Subdirectory containing agent code (e.g. "app").
        language: Project language (e.g. "python").
        old_args: CLI args for re-creating the *old* template snapshot.
        new_args: CLI args for re-creating the *new* template snapshot.
        old_version: If set, passed to ``run_create_command`` for the old
            template (used by *upgrade* to re-template at a prior version).
        mode: Interaction mode; ``interactive`` gates the confirm and
            conflict-resolution prompts, ``auto_approve`` the silent path.
        dry_run: Preview changes without writing anything.
        prefer_new: Resolve conflicts in favour of the new template.
        operation_label: Human-readable verb for prompt/log text
            (``"upgrade"`` or ``"enhancement"``).
        pre_apply_hook: Optional callback invoked *before* files are
            written.  Receives ``project_dir``.  Return ``False`` to abort.
        post_apply_hook: Optional callback invoked *after* files are
            written (and deps merged).  Receives ``(project_dir, language)``.
        post_apply_no_changes_hook: Optional callback invoked in place of
            ``post_apply_hook`` when no project file or dependency needs
            changing -- for a caller that records something the merge never
            writes itself, such as the manifest.
        rendered_manifest_hook: Optional callback invoked with the newly
            generated template directory right after it is rendered, so the
            caller can inspect what ``create`` resolved into its manifest before
            the temporary directory is deleted.

    Returns:
        ``MergeResult.DONE`` if the pipeline completed (changes applied, user
        cancelled, or nothing to do). ``OLD_TEMPLATE_FAILED`` or
        ``NEW_TEMPLATE_FAILED`` if that template could not be generated.
        Nothing was written then; the caller reports it or falls back to an
        alternative strategy.

    Raises:
        SystemExit: If writing the merged dependencies fails. The post-apply
            hook is skipped, so a rerun retries the merge.
    """
    same_config = sorted(old_args) == sorted(new_args) and old_version is None

    temp_base = pathlib.Path(tempfile.mkdtemp(prefix=f"acli_{operation_label}_"))
    old_template_dir = temp_base / "old"
    new_template_dir = temp_base / "new"

    try:
        console.print()
        console.print("[dim]Generating templates for comparison...[/dim]")

        if same_config:
            # Optimisation: identical args -> generate once, reuse for both.
            console.print("[dim]  - Template...[/dim]")
            if not run_create_command(old_args, old_template_dir, project_name):
                return MergeResult.NEW_TEMPLATE_FAILED
            old_template_project = old_template_dir / project_name
            new_template_project = old_template_project  # same reference
        else:
            # Generate old template
            console.print("[dim]  - Old template...[/dim]")
            if not run_create_command(
                old_args, old_template_dir, project_name, old_version
            ):
                return MergeResult.OLD_TEMPLATE_FAILED

            # Generate new template
            console.print("[dim]  - New template...[/dim]")
            if not run_create_command(new_args, new_template_dir, project_name):
                return MergeResult.NEW_TEMPLATE_FAILED

            old_template_project = old_template_dir / project_name
            new_template_project = new_template_dir / project_name

        if rendered_manifest_hook:
            rendered_manifest_hook(new_template_project)

        console.print()

        # ── Compare ──────────────────────────────────────────────────
        console.print("[dim]Comparing files...[/dim]")
        results = compare_all_files(
            project_dir,
            old_template_project,
            new_template_project,
            agent_directory,
        )
        groups = group_results_by_action(results)

        # ── Dependency merging ───────────────────────────────────────
        dep_resolutions: list[DependencyResolution] = []
        merge_dependencies = MERGE_DEPENDENCY_HANDLERS.get(language)
        if merge_dependencies is None:
            # Warn rather than silently leaving the manifest untouched
            # (java/typescript, etc.).
            logging.warning(
                "Dependency merging isn't supported for %s projects yet — your "
                "dependency manifest was left unchanged. Review it manually "
                "after the %s.",
                language,
                operation_label,
            )
        else:
            try:
                dep_resolutions = merge_dependencies(
                    project_dir, old_template_project, new_template_project
                )
            except DependencyReadError as e:
                # Skip rather than dropping every requirement into a destructive diff.
                logging.warning(
                    "Skipping dependency merge — %s. Review your dependency manifest "
                    "manually after the %s.",
                    e,
                    operation_label,
                )

        extension_entries = pending_template_extensions(project_dir, new_template_project)

        console.print()

        # ── Display ──────────────────────────────────────────────────
        display_results(groups, dep_resolutions, extension_entries, dry_run)

        total_changes = (
            len(groups["auto_update"])
            + len(groups["new"])
            + len(groups["removed"])
            + len(groups["conflict"])
        )
        has_dep_changes = any(r.status != "unchanged" for r in dep_resolutions)
        if total_changes == 0 and not has_dep_changes and not extension_entries:
            console.print(
                "[bold green]✅[/bold green] No changes needed to project files or "
                "dependencies."
            )
            if post_apply_no_changes_hook and not dry_run:
                post_apply_no_changes_hook(project_dir, language)
            return MergeResult.DONE

        # ── Confirm ──────────────────────────────────────────────────
        if mode.interactive and not dry_run:
            prompt_text = f"\nProceed with {operation_label}?"
            if groups["conflict"]:
                prompt_text = "\nProceed? (you'll resolve conflicts next)"
            proceed = Prompt.ask(
                prompt_text,
                choices=["y", "n"],
                case_sensitive=False,
                default="y",
            ).lower()
            if proceed != "y":
                console.print(
                    f"[yellow]{operation_label.capitalize()} cancelled.[/yellow]"
                )
                return MergeResult.DONE

        # ── Pre-apply hook (e.g. backup) ─────────────────────────────
        if pre_apply_hook and not dry_run:
            if not pre_apply_hook(project_dir):
                # The hook signalled abort (e.g. the user cancelled the backup).
                return MergeResult.DONE

        # ── Apply ────────────────────────────────────────────────────
        counts = apply_changes(
            groups=groups,
            project_dir=project_dir,
            new_template_dir=new_template_project,
            auto_approve=mode.auto_approve,
            dry_run=dry_run,
            prefer_new=prefer_new,
            interactive=mode.interactive,
        )

        write_dependencies = WRITE_DEPENDENCY_HANDLERS.get(language)
        should_write_dependencies = (
            not dry_run and has_dep_changes and write_dependencies is not None
        )
        wrote_dependencies = bool(
            should_write_dependencies and write_dependencies(project_dir, dep_resolutions)
        )

        if should_write_dependencies and not wrote_dependencies:
            raise click.ClickException(
                "Project files were updated, but dependencies were not. "
                "Fix the error above and rerun."
            )

        if wrote_dependencies:
            console.print(
                "[dim] Dependencies updated — run [bold]agents-cli install[/bold] "
                "to install the new versions.[/dim]"
            )

        if not dry_run:
            record_template_extensions(project_dir, extension_entries)

        # ── Post-apply hook (e.g. metadata update) ───────────────────
        if post_apply_hook and not dry_run:
            post_apply_hook(project_dir, language)

        # ── Summary ──────────────────────────────────────────────────
        console.print()
        if dry_run:
            console.print(
                "[bold yellow]Dry run complete.[/bold yellow] "
                "Run without --dry-run to apply changes."
            )
        else:
            console.print(f"  Updated: {counts['updated']} files")
            console.print(f"  Added: {counts['added']} files")
            console.print(f"  Removed: {counts['removed']} files")
            if counts["conflicts_kept"] or counts["conflicts_updated"]:
                console.print(
                    f"  Conflicts: {counts['conflicts_updated']} updated, "
                    f"{counts['conflicts_kept']} kept yours"
                )
            console.print()
            console.print(
                f"[bold green]✅ {operation_label.capitalize()} complete![/bold green]"
            )

        return MergeResult.DONE

    finally:
        shutil.rmtree(temp_base, ignore_errors=True)
