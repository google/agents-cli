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

"""agents-cli extension add command."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import click

from google.agents.cli.__init__ import __version__
from google.agents.cli._fs import delete_folder, make_folders_writable
from google.agents.cli._project import find_project_root
from google.agents.cli._trust import require_confirmation
from google.agents.cli.extension._loader import (
    RECOVERY_COMMANDS,
    is_out_of_range,
    load_extension_set,
    load_extension_spec,
)
from google.agents.cli.extension._manifest import (
    EXTENSIONS_FILE,
    ExtensionEntry,
    ExtensionListError,
    check_extension_list,
    read_extension_entries,
    upsert_extension_entry,
)
from google.agents.cli.extension._paths import (
    scope_root,
    user_config_root,
    vendored_extensions_dir,
)
from google.agents.cli.extension._refs import (
    LATEST,
    UNPINNED_REF,
    RefParseError,
    parse_ref,
)
from google.agents.cli.extension._resolver import (
    ResolverError,
    materialize,
    pin_ref,
    resolve_sha,
)
from google.agents.cli.extension._skills import (
    SkillError,
    SkillExistsError,
    SkillUndoError,
    installed_skill_names,
    link_skills_for_antigravity_if_global,
    other_scope_extensions,
    remove_skills,
    replace_skills,
    report_skill_changes,
    skills_dir_for_scope,
)
from google.agents.cli.extension._spec import (
    EXTENSION_FILE,
    ExtensionSpec,
    ExtensionSpecError,
)
from google.agents.cli.extension._trust import confirm_trust


def _parse_or_fail(extension_dir: Path) -> ExtensionSpec | None:
    """Parse the vendored manifest, turning a bad one into a failed install.

    Parsed once here and handed to both gates, so a manifest is not read twice
    and the two cannot disagree about what it says.
    """
    try:
        return load_extension_spec(extension_dir)
    except ExtensionSpecError as e:
        raise click.ClickException(
            f"{EXTENSION_FILE} is not valid: {e}\nNothing was installed."
        ) from e


def _check_compat(spec: ExtensionSpec | None) -> None:
    """Gate a freshly vendored extension against the running CLI version.

    `error` mode raises so the install fails; `warn` mode logs and proceeds.
    This is the deliberate install gate, the only place an incompatibility
    hard-fails; at runtime it only ever warns.
    """
    if spec is None or not is_out_of_range(spec):
        return
    msg = (
        f"Extension requires agents-cli {spec.requires_agents_cli} but you're "
        f"running {__version__}."
    )
    if spec.error_on_incompatible:
        raise click.ClickException(
            f"{msg} Not installing (on_incompatible: error). "
            "Pin a compatible ref with --ref, or upgrade the CLI."
        )
    logging.warning("%s Installing anyway (on_incompatible: warn).", msg)


def _check_reserved_commands(spec: ExtensionSpec | None) -> None:
    """Refuse an extension that claims a reserved command (see RECOVERY_COMMANDS).

    Caught at install because that is where a human can read why. The loader
    ignores such a claim too, for manifests edited by hand.
    """
    if spec is None:
        return
    reserved = sorted({c.name for c in spec.commands} & RECOVERY_COMMANDS)
    if reserved:
        raise click.ClickException(
            f"Extension claims {', '.join(repr(r) for r in reserved)}, which "
            "cannot be overridden: these commands inspect, set up or repair "
            "agents-cli and its extensions, so they always run the built-in."
        )


def _check_command_conflicts(
    project_root: Path | None, spec: ExtensionSpec | None, scope: str
) -> None:
    """Raise if the candidate claims a command *another* extension already owns.

    Re-adding the same name at the same scope is a replace, which is how a pin
    moves, so the candidate must not be read as conflicting with the copy it is
    about to overwrite.
    """
    if spec is None:
        return
    installed = load_extension_set(project_root, user_config_root())
    clashes = sorted(
        (contribution.name, owner.extension_name)
        for contribution in spec.commands
        if (owner := installed.commands.get(contribution.name)) is not None
        and owner.scope == scope
        and owner.extension_name != spec.name
    )
    if clashes:
        raise click.ClickException(
            "Command conflict(s) with an already-installed extension: "
            + ", ".join(f"{cmd} (owned by {owner!r})" for cmd, owner in clashes)
            + ". Remove or rename the conflicting extension."
        )


@click.command("add")
@click.argument("reference")
@click.option(
    "--global",
    "global_",
    is_flag=True,
    default=False,
    help=(
        "Install for all projects (user scope, global/org-wide). Default is "
        "project scope (committed to this repo only), so the extension cannot "
        "affect your other projects."
    ),
)
@click.option(
    "--ref",
    "ref_override",
    default=None,
    help=(
        "Pin a branch, tag, or SHA. For a first-party extension, "
        f"{LATEST} pins the latest release."
    ),
)
@require_confirmation("Install this extension?")
def cmd_add(
    reference: str,
    global_: bool,
    ref_override: str | None,
    auto_approve: bool,
) -> None:
    """Add an extension from a git reference or local path."""
    scope = "user" if global_ else "project"
    try:
        ref = parse_ref(reference, ref_override=ref_override)
    except RefParseError as e:
        raise click.ClickException(str(e)) from e
    if ref.kind == "local" and ref_override is not None:
        raise click.UsageError(
            "--ref does not apply to a local@ extension: a local folder has no "
            "branches or tags. Drop --ref."
        )

    if not confirm_trust(ref, auto_approve=auto_approve):
        raise click.ClickException("Aborted: extension not trusted.")

    root = scope_root(scope)
    if root is None:
        raise click.ClickException(
            "No agents-cli-manifest.yaml found. Run inside a project (extensions "
            "install at project scope by default), or pass --global to install "
            "for all projects."
        )
    project_root = root if scope == "project" else find_project_root(Path.cwd())

    manifest = root / EXTENSIONS_FILE
    try:
        check_extension_list(manifest)
    except (OSError, ExtensionListError) as e:
        raise click.ClickException(
            f"Cannot record extensions in {manifest}: {e}.\nNothing was installed."
        ) from e
    skills_dir = skills_dir_for_scope(scope, root)
    entries = read_extension_entries(manifest)
    installed_extensions = {entry.name for entry in entries}
    other_scope = other_scope_extensions(scope, root)
    try:
        ref = pin_ref(ref)
        sha = resolve_sha(ref)
        name, extension_dir = materialize(
            ref, sha, vendored_extensions_dir(root), ref_flag="--ref"
        )
    except ResolverError as e:
        raise click.ClickException(str(e)) from e

    # Every gate runs before anything is recorded, so a refused install leaves
    # no entry to roll back. The vendored copy has to exist first (the gates read
    # its manifest), so that is the only thing to clean up. Skills are staged
    # before any is swapped in, so a failed copy leaves them unchanged too.
    try:
        spec = _parse_or_fail(extension_dir)
        _check_other_scope(name, scope, other_scope, spec, skills_dir)
        _check_compat(spec)
        _check_reserved_commands(spec)
        _check_command_conflicts(project_root, spec, scope)
        removed_skills = _replace_skills_or_fail(
            spec, skills_dir, name, installed_extensions | other_scope
        )
    except click.ClickException as e:
        if extension_dir.exists():
            make_folders_writable(extension_dir)
            shutil.rmtree(extension_dir)
        # Removing the copy of an extension that is still recorded leaves the
        # entry pointing at nothing, and its commands silently fall back to the
        # built-ins until the copy is back.
        if name in installed_extensions:
            raise click.ClickException(
                f"{e.format_message()}\n"
                f"  {name!r} is still recorded; restore its copy with: agents-cli install"
            ) from e
        raise

    entry = ExtensionEntry(
        name=name, source=ref.raw, ref=ref.ref or UNPINNED_REF, sha=sha, scope=scope
    )
    try:
        upsert_extension_entry(manifest, entry)
    except (OSError, ExtensionListError) as e:
        if name in installed_extensions:
            raise click.ClickException(
                f"Could not record the new pin of {name!r} in {manifest}: {e}. Its copy "
                "and skills are already the new version. Fix the cause (for example, "
                "file permissions), then run the same `agents-cli extension add` again."
            ) from e
        _delete_unrecorded_install(extension_dir, skills_dir, spec)
        raise click.ClickException(
            f"Could not record {name!r} in {manifest}: {e}\nNothing was installed."
        ) from e
    # Even when no skill changed, a skill folder deleted by hand leaves a dead link.
    if skills_dir.is_dir():
        link_skills_for_antigravity_if_global(skills_dir)

    click.secho(f"Added extension {name!r} ({scope} scope) from {ref.raw}.", fg="green")
    report_skill_changes(spec, name, skills_dir, removed_skills)
    click.secho(
        "Extensions are experimental; the manifest format may still change.",
        dim=True,
    )


def _replace_skills_or_fail(
    spec: ExtensionSpec | None,
    skills_dir: Path,
    extension_name: str,
    installed_extensions: set[str],
) -> tuple[str, ...]:
    prefix = "Cannot install the extension's skills: "
    try:
        return replace_skills(spec, skills_dir, extension_name, installed_extensions)
    except SkillExistsError as e:
        raise click.ClickException(e.with_retry("run add again")) from e
    except SkillUndoError as e:
        # `install` only restores skills of recorded extensions.
        message = (
            str(e)
            if extension_name in installed_extensions
            else e.with_fix("`agents-cli extension add` again")
        )
        raise click.ClickException(f"{prefix}{message}") from e
    except SkillError as e:
        raise click.ClickException(f"{prefix}{e}\nNothing was installed.") from e


def _check_other_scope(
    name: str,
    scope: str,
    other_scope: set[str],
    spec: ExtensionSpec | None,
    skills_dir: Path,
) -> None:
    """Refuse a name with skills the other scope has installed when both use one folder.

    Markers do not say which scope installed a skill, so the two copies would
    replace and delete each other's skills. Skills installed by the other copy
    count too: this copy would delete them.
    """
    if name not in other_scope:
        return
    try:
        has_skills = bool(spec is not None and spec.skills) or bool(
            installed_skill_names(skills_dir, name)
        )
    except SkillError as e:
        raise click.ClickException(str(e)) from e
    if has_skills:
        other = "user" if scope == "project" else "project"
        raise click.ClickException(
            f"Extension {name!r} is already installed at {other} scope. Your home "
            "folder is a project, so both scopes use ~/.agents/skills/ and cannot "
            f"both install it. Remove it at {other} scope first."
        )


def _delete_unrecorded_install(
    extension_dir: Path, skills_dir: Path, spec: ExtensionSpec | None
) -> None:
    delete_folder(extension_dir)
    if spec is not None:
        for failure in remove_skills(skills_dir, [skill.name for skill in spec.skills]):
            logging.warning("%s", failure)
