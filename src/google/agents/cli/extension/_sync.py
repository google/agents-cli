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

"""Re-materialize vendored extension working copies from pinned SHAs."""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Literal, TypeAlias

import click

from google.agents.cli.__init__ import __version__
from google.agents.cli._fs import is_real_dir
from google.agents.cli.extension._loader import incompatible_spec, load_extension_spec
from google.agents.cli.extension._manifest import (
    EXTENSIONS_FILE,
    ExtensionEntry,
    read_extension_list,
)
from google.agents.cli.extension._paths import user_config_root, vendored_extensions_dir
from google.agents.cli.extension._refs import RefParseError, parse_ref
from google.agents.cli.extension._resolver import (
    STAMP_FILE,
    CopyChangedError,
    ResolverError,
    expected_stamp,
    fingerprint,
    materialize,
    read_stamp,
    validate_extension_name,
)
from google.agents.cli.extension._skills import (
    MARKER_FILE,
    SkillError,
    SkillOwnedError,
    ValidatedSkill,
    check_skills,
    install_skills,
    installed_skill_names,
    link_skills_for_antigravity_if_global,
    other_scope_list,
    remove_skills_or_warn,
    report_skill_changes,
    skills_dir_for_scope,
    unrecorded_skill_names,
)
from google.agents.cli.extension._spec import ExtensionSpecError
from google.agents.cli.scaffold.utils.fs import source_ignore_patterns

_CopyState: TypeAlias = Literal["restored", "usable", "missing"]


def sync_extensions(project_root: Path | None) -> list[str]:
    """Ensure every recorded extension is vendored on disk, with its skills.

    Reports what it re-materialized and returns those names.
    """
    remove_unrecorded = project_root is None or not _project_shares_skills_dir(
        project_root
    )
    # The user pass warns about the home project's list unless the project pass read it.
    project_is_home = (
        project_root is not None and project_root.resolve() == Path.home().resolve()
    )
    rematerialized: list[str] = []
    for scope, scope_root in _scope_roots(project_root):
        rematerialized += _sync_scope(
            scope,
            scope_root,
            remove_unrecorded=remove_unrecorded,
            warn_other=scope == "user" and not project_is_home,
        )
    if rematerialized:
        click.echo(f"Restored extensions: {', '.join(rematerialized)}")
    return rematerialized


def _project_shares_skills_dir(project_root: Path) -> bool:
    """Whether the project's skills folder can hold another scope's or project's skills.

    That is so when the project reaches it through a link, or `~/.agents/skills`
    leads to it. Then what one list does not record can still be another list's.
    """
    skills_dir = skills_dir_for_scope("project", project_root)
    try:
        resolved = skills_dir.resolve()
        resolved_root = project_root.resolve()
        # Against the resolved root, so a link above the project does not count.
        if resolved != resolved_root / skills_dir.relative_to(project_root):
            return True
        home = Path.home()
        return resolved_root != home.resolve() and resolved == (
            skills_dir_for_scope("user", home).resolve()
        )
    except (OSError, RuntimeError):
        # Before Python 3.13 a link loop raises RuntimeError. A folder that
        # cannot be resolved may be shared, so nothing is deleted.
        return True


def _scope_roots(project_root: Path | None) -> list[tuple[str, Path]]:
    """(scope, root) pairs to sync, project first."""
    roots = [] if project_root is None else [("project", project_root)]
    roots.append(("user", user_config_root()))
    return roots


def _sync_scope(
    scope: str, scope_root: Path, *, remove_unrecorded: bool, warn_other: bool
) -> list[str]:
    """Restore one scope's copies, then its skills; returns the re-materialized names."""
    extension_list = read_extension_list(scope_root / EXTENSIONS_FILE, warn=True)
    vendored_dir = vendored_extensions_dir(scope_root)
    states = _restore_copies(extension_list.entries, vendored_dir)
    vendored_names = [name for name, state in states.items() if state != "missing"]
    skills_dir = skills_dir_for_scope(scope, scope_root)
    other_list = other_scope_list(scope, scope_root, warn=warn_other)
    other_names = other_list.names
    installed_extensions = extension_list.names | other_names
    # First, so a skill name that a removed extension held is free for the loop.
    # An extension that a list records but could not be read still owns its
    # skills, so nothing is deleted unless both lists are complete.
    if (
        remove_unrecorded
        and not _user_skills_dir_is_linked(skills_dir)
        and extension_list.complete
        and other_list.complete
    ):
        _remove_unrecorded_skills(skills_dir, installed_extensions)
    deferred: list[tuple[str, Path]] = []
    for name in vendored_names:
        if name in other_names:
            # Both passes skip it, but only the user pass warns, so it warns once.
            if scope == "user":
                logging.warning(
                    "Skipping the skills of extension %r: it is installed at both "
                    "scopes, which share ~/.agents/skills/ because your home "
                    "folder is a project. Remove it at one scope.",
                    name,
                )
            continue
        extension_dir = vendored_dir / name
        try:
            _sync_skills(
                name,
                extension_dir,
                skills_dir,
                installed_extensions,
                raise_if_owned=True,
            )
        except SkillOwnedError:
            deferred.append((name, extension_dir))
    # Retried after the whole list: the owner may drop the skill later in it.
    for name, extension_dir in deferred:
        _sync_skills(
            name,
            extension_dir,
            skills_dir,
            installed_extensions,
            raise_if_owned=False,
        )
    # Also when nothing changed: a skill folder deleted by hand leaves a dead
    # link. Only the user pass, so a home project links once.
    if scope == "user" and os.path.isdir(skills_dir):
        link_skills_for_antigravity_if_global(skills_dir)
    return [name for name, state in states.items() if state == "restored"]


def _restore_copies(
    entries: list[ExtensionEntry], vendored_dir: Path
) -> dict[str, _CopyState]:
    """Restore each entry's vendored copy; returns the state of each copy by name."""
    states: dict[str, _CopyState] = {}
    for entry in entries:
        # The name decides which folder is read, so check it before
        # anything else; never rely on materialize refusing it.
        try:
            validate_extension_name(entry.name)
        except ResolverError as e:
            logging.warning("Skipping extension %r: %s", entry.name, e)
            continue
        states[entry.name] = _restore_copy(entry, vendored_dir / entry.name)
    return states


def _restore_copy(entry: ExtensionEntry, extension_dir: Path) -> _CopyState:
    """Re-materialize the vendored copy when it is missing or stale.

    Returns "restored" when it restored the copy, "usable" when the copy on
    disk matches its source or could not be restored but is used as it is, and
    "missing" when there is no copy to use or the copy may be incomplete.
    """
    try:
        ref = parse_ref(entry.source, ref_override=entry.sha)
        # Re-materialize when the copy is missing OR no longer matches
        # its source: a bumped pin, a partial copy, or — for a local
        # path, whose source is a working tree rather than a commit —
        # any edit made to it since the copy was taken.
        if is_real_dir(extension_dir) and read_stamp(extension_dir) == expected_stamp(
            ref, entry.sha
        ):
            return "usable"
        # name=entry.name pins the vendored dir to the stable id (see
        # materialize) so a restore survives an upstream rename.
        materialize(ref, entry.sha, extension_dir.parent, name=entry.name)
    except CopyChangedError as e:
        # A failed swap can leave the copy half deleted. Without its stamp it
        # never passes as current, so the next run copies it again.
        with contextlib.suppress(OSError):
            (extension_dir / STAMP_FILE).unlink(missing_ok=True)
        what_is_left = (
            "may be incomplete" if os.path.lexists(extension_dir) else "is missing"
        )
        logging.warning(
            "Could not restore extension %r from %s: %s. The copy %s, so its skills "
            "were left as they are. Check the folder's permissions and free disk "
            "space, then run `agents-cli install` again.",
            entry.name,
            entry.source,
            str(e).rstrip("."),
            what_is_left,
        )
        return "missing"
    except (ResolverError, RefParseError, OSError) as e:
        # A clone can hold the committed copy without the author's local
        # source, so its skills install from that copy. A git copy that could
        # not be restored is from another pin, so the installed skills stay.
        # Checked by source, as `ref` is unset when parse_ref failed. A copy
        # without its stamp may be half deleted, so it is not used.
        local_copy = entry.source.startswith("local@") and is_real_dir(extension_dir)
        if local_copy and not os.path.isfile(extension_dir / STAMP_FILE):
            logging.warning(
                "Could not restore extension %r from %s: %s. The copy in %s may be "
                "incomplete, so its skills were not installed from it. Check that "
                "its source exists and can be read and that there is free disk "
                "space, then run `agents-cli install` again.",
                entry.name,
                entry.source,
                str(e).rstrip("."),
                extension_dir,
            )
            return "missing"
        logging.warning(
            "Could not restore extension %r from %s: %s",
            entry.name,
            entry.source,
            e,
        )
        return "usable" if local_copy else "missing"
    _warn_if_incompatible(entry.name, extension_dir)
    return "restored"


def _warn_if_incompatible(extension_name: str, extension_dir: Path) -> None:
    """Warn, never fail, if the restored pin is out of range: `install` is how a bad pin gets fixed."""
    try:
        incompatible = incompatible_spec(extension_dir)
    except ExtensionSpecError as e:
        logging.warning("%s: %s.", extension_dir, e)
        incompatible = None
    if incompatible is not None:
        logging.warning(
            "Restored extension %r requires agents-cli %s but running %s.",
            extension_name,
            incompatible.requires_agents_cli,
            __version__,
        )


def _user_skills_dir_is_linked(skills_dir: Path) -> bool:
    """Whether `skills_dir` is `~/.agents/skills` and that folder leads elsewhere.

    A link into a project brings that project's skills, which no list read here records.
    """
    home = Path.home()
    if skills_dir != skills_dir_for_scope("user", home):
        return False
    try:
        return skills_dir.resolve() != home.resolve() / skills_dir.relative_to(home)
    except (OSError, RuntimeError):
        return True


def _remove_unrecorded_skills(skills_dir: Path, installed_extensions: set[str]) -> None:
    """Delete skills whose extension is no longer recorded, as `remove` did for its author.

    Never fatal, like the rest of the sync.
    """
    try:
        unrecorded = unrecorded_skill_names(skills_dir, installed_extensions)
    except SkillError as e:
        logging.warning(
            "Could not delete the skills of extensions that are no longer recorded: "
            "%s Check the folder's permissions, then run `agents-cli install` again.",
            e,
        )
        return
    removed = remove_skills_or_warn(skills_dir, unrecorded)
    if removed:
        click.echo(
            f"Removed from {skills_dir} the skills of extensions that are no longer "
            f"recorded: {', '.join(removed)}"
        )


def _sync_skills(
    extension_name: str,
    extension_dir: Path,
    skills_dir: Path,
    installed_extensions: set[str],
    *,
    raise_if_owned: bool,
) -> None:
    """Make the installed skills match the copy's; like the restore itself, never fatal.

    With `raise_if_owned`, a skill another extension owns raises
    SkillOwnedError instead of warning.
    """
    try:
        spec = load_extension_spec(extension_dir)
        previously_installed = installed_skill_names(skills_dir, extension_name)
        # Checked before the match, so a link in the copy is refused even when
        # the installed skills look current.
        skills = check_skills(spec, skills_dir, extension_name, installed_extensions)
        differing = _differing_skills(skills, skills_dir, previously_installed)
        if not differing and {skill.name for skill in skills} == previously_installed:
            return
        removed_skills = install_skills(
            skills_dir, skills, extension_name, previously_installed
        )
    except SkillOwnedError as e:
        if raise_if_owned:
            raise
        # Updating the owner would move its pin, which install never does.
        reason = e.with_hint(
            f"If {e.owner!r} no longer declares it, delete {skills_dir / e.skill_name}, "
            "then run `agents-cli install` again. If it does, remove one of the two "
            "extensions."
        )
    except (ExtensionSpecError, SkillError, OSError) as e:
        reason = str(e)
    else:
        report_skill_changes(spec, extension_name, skills_dir, removed_skills)
        for name in differing:
            click.echo(
                f"Overwrote skill {name!r} with the version from extension "
                f"{extension_name!r}. Any local edits to it were replaced."
            )
        return
    logging.warning(
        "Could not install the skills of extension %r: %s", extension_name, reason
    )


def _differing_skills(
    skills: list[ValidatedSkill], skills_dir: Path, previously_installed: set[str]
) -> list[str]:
    """Names of the `previously_installed` skills whose content differs from the copy's.

    A skill that cannot be read on either side counts as differing. Content
    matters: after a pull the copy can be current while `.agents/` still holds
    the skills of the old pin.
    """
    differing = []
    for skill in skills:
        if skill.name not in previously_installed:
            continue
        source_fingerprint = _skill_fingerprint(skill.source_dir, skill.extension_root)
        installed = skills_dir / skill.name
        if source_fingerprint is None or source_fingerprint != _skill_fingerprint(
            installed, installed
        ):
            differing.append(skill.name)
    return differing


def _skill_fingerprint(skill_dir: Path, repo_root: Path) -> str | None:
    """Fingerprint of a skill folder without its marker; None when it cannot be read.

    Links are followed while they stay inside `repo_root`, as the copy does.

    Skips the names vendoring never copies (caches such as `__pycache__`, and
    `.env` files), so they do not count as a change.
    """

    def skip(directory: str, names: list[str]) -> set[str]:
        skipped = source_ignore_patterns(directory, names)
        if directory == str(skill_dir):
            skipped.add(MARKER_FILE)
        return skipped

    try:
        return fingerprint(skill_dir, skip, repo_root)
    except ResolverError:
        return None
