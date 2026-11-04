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

"""Install the skills an extension declares into a `.agents/skills` folder."""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import stat
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path

import click

from google.agents.cli._fs import delete_path, make_folders_writable
from google.agents.cli._project import find_project_root
from google.agents.cli._symlinks import (
    ScaffoldSymlinkSecurityError,
    follow_symlink,
    vet_symlinks,
)
from google.agents.cli.extension._manifest import (
    EXTENSIONS_FILE,
    ExtensionList,
    read_extension_list,
)
from google.agents.cli.extension._paths import user_config_root
from google.agents.cli.extension._spec import ExtensionSkill, ExtensionSpec
from google.agents.cli.scaffold.utils.fs import source_ignore_patterns

SKILL_FILE = "SKILL.md"
# Written into each installed skill folder; holds the extension name, then the
# skill name. It is the only proof of ownership: the vendored copy can be
# missing or newer than what is installed. A folder without the marker, or whose
# marker names another folder (a user's copy), is the user's.
MARKER_FILE = ".agents-cli-extension"
FOLDER_STAGED_ON_SAVE_SUFFIX = ".incoming"
FOLDER_STAGED_ON_DELETE_SUFFIX = ".outgoing"


class SkillError(Exception):
    """Raised when a declared skill cannot be installed."""


class SkillOwnedError(SkillError):
    """Raised when another extension installed a declared skill."""

    def __init__(self, skill_name: str, owner: str):
        self.skill_name = skill_name
        self.owner = owner
        super().__init__(self.with_hint("Update or remove that extension first."))

    def with_hint(self, hint: str) -> str:
        return (
            f"skill {self.skill_name!r} is already installed by extension "
            f"{self.owner!r}. {hint}"
        )


class SkillExistsError(SkillError):
    """Raised when a declared skill's folder is the user's."""

    def __init__(self, skill_name: str, skills_dir: Path):
        self.skill_name = skill_name
        self.location = _display(skills_dir)
        super().__init__(self.with_retry("try again"))

    def with_retry(self, retry: str) -> str:
        return (
            f"skill {self.skill_name!r} already exists in {self.location} and was "
            f"not installed by this extension. Rename or delete that folder, then "
            f"{retry}."
        )


class SkillUndoError(SkillError):
    """Raised when a failed swap could not put every old skill back."""

    def __init__(self, cause: str, not_restored: list[str]):
        self.cause = cause
        self.not_restored = not_restored
        super().__init__(self.with_fix("`agents-cli install`"))

    def with_fix(self, command: str) -> str:
        return (
            f"{self.cause}\nThese skills may be missing or changed: "
            f"{', '.join(self.not_restored)}. Run {command} to fix them."
        )


@dataclass(frozen=True)
class ValidatedSkill:
    """A skill `check_skills` accepted; `source_dir` is its folder in the vendored copy.

    `extension_root` is the vendored copy, which links in the skill must stay inside.
    """

    name: str
    source_dir: Path
    extension_root: Path


@dataclass(frozen=True)
class StagedSkill:
    """A skill copied to `staged_dir`, waiting to become `target`.

    `staged_dir` is `.<name>` plus FOLDER_STAGED_ON_SAVE_SUFFIX.
    """

    staged_dir: Path
    target: Path


@dataclass(frozen=True)
class MovedAside:
    """An old skill folder renamed from `target` to `aside`.

    `aside` is `.<name>` plus FOLDER_STAGED_ON_DELETE_SUFFIX.
    """

    target: Path
    aside: Path


def skills_dir_for_scope(scope: str, scope_root: Path) -> Path:
    """The folder coding agents read skills from, matching `agents-cli setup`.

    User scope lives in the home directory rather than beside the extension
    list, because that is where agents look for global skills.
    """
    base = Path.home() if scope == "user" else scope_root
    return base / ".agents" / "skills"


def other_scope_extensions(scope: str, scope_root: Path) -> set[str]:
    """Extensions recorded at the other scope when both scopes use one skills folder.

    That happens when the home folder is a project. A marker does not say which
    scope installed a skill, so these extensions own skills there too.
    """
    return other_scope_list(scope, scope_root, warn=True).names


def other_scope_list(scope: str, scope_root: Path, *, warn: bool) -> ExtensionList:
    """The other scope's extension list when both scopes use one skills folder."""
    other_manifest = other_scope_manifest(scope, scope_root)
    if other_manifest is None:
        return ExtensionList(entries=[], complete=True)
    return read_extension_list(other_manifest, warn=warn)


def other_scope_manifest(scope: str, scope_root: Path) -> Path | None:
    """Path of the list `other_scope_list` reads; None when there is none."""
    home = Path.home()
    if scope == "user":
        home_project = find_project_root(home)
        if home_project is None or home_project.resolve() != home.resolve():
            return None
        return home / EXTENSIONS_FILE
    if scope_root.resolve() == home.resolve():
        return user_config_root() / EXTENSIONS_FILE
    return None


def installed_skill_names(skills_dir: Path, extension_name: str) -> set[str]:
    """Names of the skills in `skills_dir` whose marker names `extension_name`.

    Each name is the one in the marker, which can differ from the folder name in
    case only (see `_owner`), so it compares equal to the declared name.
    """
    return {skill for owner, skill in _markers(skills_dir) if owner == extension_name}


def unrecorded_skill_names(skills_dir: Path, installed_extensions: set[str]) -> set[str]:
    """Names of the skills in `skills_dir` whose marker names none of `installed_extensions`."""
    return {
        skill
        for owner, skill in _markers(skills_dir)
        if owner not in installed_extensions
    }


def check_skills(
    spec: ExtensionSpec | None,
    skills_dir: Path,
    extension_name: str,
    installed_extensions: Collection[str],
) -> list[ValidatedSkill]:
    """Check every declared skill can be copied, touching nothing.

    A folder `extension_name` installed before is its own, so re-adding or
    updating it replaces the folder instead of conflicting with it.
    """
    if spec is None:
        return []
    skills = [_check_skill(spec.root, declared) for declared in spec.skills]

    for skill in skills:
        target = skills_dir / skill.name
        try:
            os.lstat(target)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as e:
            raise SkillError(f"cannot read {skills_dir}: {e}.") from e
        owner = _owner(target)
        if owner == extension_name:
            continue
        if owner is None:
            raise SkillExistsError(skill.name, skills_dir)
        if owner not in installed_extensions:
            raise SkillError(
                f"folder {target} has a marker from {owner!r}, which is not "
                "installed. Delete the folder or its marker."
            )
        raise SkillOwnedError(skill.name, owner)
    return skills


def install_skills(
    skills_dir: Path,
    skills: list[ValidatedSkill],
    extension_name: str,
    previously_installed: Iterable[str],
) -> tuple[str, ...]:
    """Copy `skills` into `skills_dir`, then drop any of `previously_installed` no longer declared.

    Every skill is staged before any is swapped in, and the swap is undone if
    it fails, so on SkillError all installed skills keep their old version.
    Returns the names of the dropped skills it deleted.
    """
    staged: list[StagedSkill] = []
    try:
        for skill in skills:
            staged.append(_stage(skill, skills_dir / skill.name, extension_name))
        _replace_with_staged(staged)
    finally:
        for skill in staged:
            with contextlib.suppress(OSError):
                delete_path(skill.staged_dir)
    return remove_skills_or_warn(
        skills_dir, set(previously_installed) - {skill.name for skill in skills}
    )


def replace_skills(
    spec: ExtensionSpec | None,
    skills_dir: Path,
    extension_name: str,
    installed_extensions: Collection[str],
) -> tuple[str, ...]:
    """Check, then copy the declared skills over the ones `extension_name` installed before.

    Raises SkillError when a check or a copy fails; installed skills then keep
    their old version. Returns the names of the dropped skills it deleted.
    """
    previously_installed = installed_skill_names(skills_dir, extension_name)
    validated_skills = check_skills(
        spec, skills_dir, extension_name, installed_extensions
    )
    return install_skills(
        skills_dir, validated_skills, extension_name, previously_installed
    )


def report_skill_changes(
    spec: ExtensionSpec | None,
    extension_name: str,
    skills_dir: Path,
    removed: Collection[str],
) -> None:
    if spec is not None and spec.skills:
        skill_names = ", ".join(skill.name for skill in spec.skills)
        click.echo(
            f"Installed skills for extension {extension_name!r} into {skills_dir}: "
            f"{skill_names}"
        )
    if removed:
        click.echo(
            f"Removed skills of extension {extension_name!r} from {skills_dir}: "
            f"{', '.join(removed)}"
        )


def remove_skills(skills_dir: Path, names: Iterable[str]) -> list[str]:
    """Delete the named skill folders; returns one message per folder that is still there."""
    failures = []
    for name in names:
        try:
            _delete_skill(skills_dir, name)
        except OSError as e:
            failures.append(f"Could not remove skill {skills_dir / name}: {e}.")
    return failures


def remove_skills_or_warn(skills_dir: Path, names: Iterable[str]) -> tuple[str, ...]:
    """Delete the named skill folders one by one, warning for each that is still there.

    Returns the names it deleted.
    """
    removed = []
    for name in sorted(names):
        failures = remove_skills(skills_dir, [name])
        for failure in failures:
            logging.warning("%s", failure)
        if not failures:
            removed.append(name)
    return tuple(removed)


def link_skills_for_antigravity_if_global(skills_dir: Path) -> None:
    """Link user-scope skills into the folders Antigravity reads global skills from.

    Antigravity reads project skills from `.agents/skills`, but global skills
    from `~/.gemini/...`, not `~/.agents/skills`. `agents-cli setup` does the same.
    Decided by the folder, not the scope: a home-folder project uses it too.
    """
    if skills_dir != skills_dir_for_scope("user", Path.home()):
        return
    from google.agents.cli.setup._antigravity import link_skills_for_antigravity

    try:
        # After the last skill is removed the folder is empty on purpose; the
        # call still prunes the links.
        link_skills_for_antigravity(warn_if_empty=False)
    except OSError as e:
        logging.warning("Could not link skills for Antigravity: %s", e)


def _check_skill(extension_root: Path, declared: ExtensionSkill) -> ValidatedSkill:
    source_dir = extension_root / declared.path
    skill_file = source_dir / SKILL_FILE
    try:
        # A committed vendored copy can hold links, and copytree follows a link
        # at the top of the tree. Like templates, a link is fine while it stays
        # inside the extension. The resolved folder is checked whether or not a
        # link leads there, so a link above the skill folder cannot skip a check.
        real_dir = source_dir.resolve()
        root = extension_root.resolve()
        if not real_dir.is_relative_to(root):
            raise SkillError(f"skill path {declared.path!r} leads outside the extension.")
        if source_ignore_patterns(str(root), list(real_dir.relative_to(root).parts)):
            raise SkillError(
                f"skill path {declared.path!r} leads into a folder that is never "
                "copied (version-control metadata, caches, build output, "
                "dependencies, or credentials)."
            )
        skill_mode = os.lstat(skill_file).st_mode
        if stat.S_ISLNK(skill_mode):
            skill_mode = os.stat(follow_symlink(skill_file, extension_root)).st_mode
    except (FileNotFoundError, NotADirectoryError):
        skill_mode = 0
    # Python reports a symlink loop as RuntimeError rather than OSError.
    except (ScaffoldSymlinkSecurityError, RuntimeError) as e:
        raise SkillError(f"skill path {declared.path!r} is refused: {e}") from e
    except (OSError, ValueError) as e:
        raise SkillError(f"cannot read skill path {declared.path!r}: {e}.") from e
    if not stat.S_ISREG(skill_mode):
        raise SkillError(f"skill path {declared.path!r} has no {SKILL_FILE}.")
    return ValidatedSkill(
        name=declared.name, source_dir=source_dir, extension_root=extension_root
    )


def _display(skills_dir: Path) -> str:
    home_skills = Path.home() / ".agents" / "skills"
    return "~/.agents/skills/" if skills_dir == home_skills else ".agents/skills/"


def _delete_skill(skills_dir: Path, name: str) -> None:
    """Move the folder aside in one rename, then delete it with its marker last.

    If the delete fails, the folder goes back with its marker, so it still
    belongs to the extension and a later run can delete it.
    """
    target = skills_dir / name
    aside = _move_aside(target).aside
    try:
        make_folders_writable(aside)
        # Delete the marker last: if a delete fails halfway, the folder goes back
        # still marked as this extension's, so a later run can finish. Without the
        # marker it would count as the user's and never be touched again.
        for child in aside.iterdir():
            if child.name != MARKER_FILE:
                delete_path(child)
        shutil.rmtree(os.fspath(aside))
    except OSError:
        with contextlib.suppress(OSError):
            aside.rename(target)
        raise


def _owner(skill_dir: Path) -> str | None:
    """Return the extension that owns `skill_dir`, or None when it is the user's.

    The marker holds `<extension_name>\n<skill_name>`. The extension name tells
    which extension may replace or delete the folder, finds its folders on
    `update`, `remove` and `install` even without its vendored copy, and names the
    owner in conflict errors. The skill name must match the folder name, so a
    folder the user copied under another name, marker included, stays the user's.
    """
    marker = _read_marker(skill_dir)
    return marker[0] if marker is not None else None


def _markers(skills_dir: Path) -> list[tuple[str, str]]:
    """The (extension name, skill name) of every extension-owned folder in `skills_dir`."""
    try:
        if not skills_dir.is_dir():
            return []
        # Listing a folder needs only read access, but its markers need search
        # access too; without it every folder would look like the user's.
        os.lstat(os.path.join(skills_dir, "."))
        markers = (_read_marker(path) for path in skills_dir.iterdir())
        return [marker for marker in markers if marker is not None]
    except OSError as e:
        raise SkillError(f"cannot read {skills_dir}: {e}.") from e


def _read_marker(skill_dir: Path) -> tuple[str, str] | None:
    """The (extension name, skill name) in the marker, if the marker is this folder's."""
    marker = skill_dir / MARKER_FILE
    try:
        if skill_dir.is_symlink() or marker.is_symlink() or not marker.is_file():
            return None
        lines = marker.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        # A folder or marker that cannot be read proves nothing, so it stays the user's.
        return None
    if len(lines) != 2 or not lines[0] or not _names_folder(lines[1], skill_dir):
        return None
    return lines[0], lines[1]


def _names_folder(skill_name: str, skill_dir: Path) -> bool:
    """Whether `skill_name` names `skill_dir`.

    A case-only difference counts when both names open the same folder, as on a
    case-insensitive file system. Elsewhere `Guide/` beside `guide/` is a copy.
    """
    if skill_name == skill_dir.name:
        return True
    if skill_name.lower() != skill_dir.name.lower():
        return False
    named = skill_dir.parent / skill_name
    try:
        return not named.is_symlink() and named.samefile(skill_dir)
    except OSError:
        return False


def _stage(skill: ValidatedSkill, target: Path, extension_name: str) -> StagedSkill:
    """Copy `skill` beside `target`, so a failed copy leaves the old skill in place."""
    staged = target.parent / f".{target.name}{FOLDER_STAGED_ON_SAVE_SUFFIX}"
    try:
        delete_path(staged)
        # Links are checked as vendoring does: following one out of the
        # extension could turn a private file into a real copy in the project.
        shutil.copytree(
            skill.source_dir, staged, ignore=vet_symlinks(skill.extension_root)
        )
        (staged / MARKER_FILE).write_text(
            f"{extension_name}\n{target.name}\n", encoding="utf-8"
        )
    except (OSError, ValueError) as e:
        with contextlib.suppress(OSError):
            delete_path(staged)
        # A symlink refusal is already a full sentence.
        reason = str(e).rstrip(".")
        raise SkillError(f"could not copy skill into {target}: {reason}.") from e
    return StagedSkill(staged_dir=staged, target=target)


def _replace_with_staged(staged: list[StagedSkill]) -> None:
    """Move every old folder aside, move every staged folder in, then delete the old ones.

    Renames are cheap and easy to undo, so a failure before the last staged
    folder is in puts every old folder back. Only the final deletes can fail
    after the install is complete, and those only leave a hidden folder behind.
    """
    outgoing: list[MovedAside] = []
    moved_in: list[Path] = []
    leftovers: list[Path] = []
    current: Path | None = None
    try:
        for skill in staged:
            current = skill.target
            if current.exists() or current.is_symlink():
                outgoing.append(_move_aside(current))
            else:
                # An earlier failed undo can leave the old copy here.
                leftovers.append(
                    current.parent / f".{current.name}{FOLDER_STAGED_ON_DELETE_SUFFIX}"
                )
        for skill in staged:
            current = skill.target
            skill.staged_dir.rename(current)
            moved_in.append(current)
    except OSError as e:
        message = f"could not replace skill {current}: {e}."
        not_restored = _undo_swap(moved_in, outgoing)
        if not_restored:
            raise SkillUndoError(message, not_restored) from e
        raise SkillError(message) from e
    for aside in [moved.aside for moved in outgoing] + leftovers:
        try:
            delete_path(aside)
        except OSError as e:
            logging.warning("failed to clean up after install: %s", e)


def _move_aside(target: Path) -> MovedAside:
    aside = target.parent / f".{target.name}{FOLDER_STAGED_ON_DELETE_SUFFIX}"
    delete_path(aside)
    target.rename(aside)
    return MovedAside(target=target, aside=aside)


def _undo_swap(moved_in: list[Path], outgoing: list[MovedAside]) -> list[str]:
    """Undo every step that can be undone; returns the skills left wrong."""
    not_restored: list[str] = []
    for target in moved_in:
        try:
            delete_path(target)
        except OSError as e:
            logging.warning("Could not delete the new %s: %s", target, e)
            not_restored.append(target.name)
    for moved in reversed(outgoing):
        try:
            moved.aside.rename(moved.target)
        except OSError as e:
            logging.warning("Could not put back the old %s: %s", moved.target, e)
            if moved.target.name not in not_restored:
                not_restored.append(moved.target.name)
    return not_restored
