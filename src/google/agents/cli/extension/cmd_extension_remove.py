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

"""agents-cli extension remove command."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import click

from google.agents.cli._fs import delete_path
from google.agents.cli._trust import require_confirmation
from google.agents.cli.extension._loader import is_inline_extension
from google.agents.cli.extension._manifest import (
    EXTENSIONS_FILE,
    ExtensionListError,
    read_extension_list,
    remove_extension_entry,
)
from google.agents.cli.extension._paths import (
    installed_scope_roots,
    scope_root,
    vendored_extensions_dir,
)
from google.agents.cli.extension._resolver import (
    ResolverError,
    validate_extension_name,
)
from google.agents.cli.extension._skills import (
    SkillError,
    installed_skill_names,
    link_skills_for_antigravity_if_global,
    other_scope_manifest,
    remove_skills,
    skills_dir_for_scope,
)


@click.command("remove")
@click.argument("name")
@click.option(
    "--global",
    "global_",
    is_flag=True,
    default=False,
    help="Remove the user-scope install only.",
)
@require_confirmation(
    "Remove this extension and its vendored copy? "
    "Skills that another scope still uses are kept."
)
def cmd_remove(name: str, global_: bool, auto_approve: bool) -> None:
    """Remove an installed extension (project scope first, then user scope)."""
    unreadable_project_list: Path | None = None
    unreadable_user_list: Path | None = None
    for scope, root in installed_scope_roots():
        if global_ and scope == "project":
            continue
        manifest = root / EXTENSIONS_FILE
        extension_list = read_extension_list(manifest, warn=True)
        if not extension_list.complete:
            if scope == "project":
                unreadable_project_list = manifest
            else:
                unreadable_user_list = manifest
        if name not in extension_list.names:
            continue
        # The name comes from a committed manifest and decides what gets
        # deleted, so check it before touching anything.
        try:
            safe_name = validate_extension_name(name)
        except ResolverError as e:
            raise click.ClickException(
                f"Refusing to remove extension {name!r}: {e}"
            ) from e
        if scope == "user" and unreadable_project_list is not None:
            raise _project_list_unreadable_error(name, unreadable_project_list)
        extension_dir = vendored_extensions_dir(root) / safe_name
        if not os.path.lexists(extension_dir) and is_inline_extension(root, safe_name):
            raise click.ClickException(
                f"Cannot remove extension {name!r}: it comes from the project template."
            )
        skills_removed = _remove_skills_or_fail(scope, root, safe_name, global_=global_)
        try:
            delete_path(extension_dir)
        except OSError as e:
            left = (
                "Its skills are deleted, but its copy" if skills_removed else "Its copy"
            )
            raise click.ClickException(
                f"Could not delete {extension_dir}: {e}.\n"
                f"{left} may be incomplete. {_retry_hint(name, global_=global_)}"
            ) from e
        # Entry last: if anything fails on the way, a second remove finds the
        # extension again and finishes the job.
        try:
            remove_extension_entry(manifest, name)
        except (OSError, ExtensionListError) as e:
            deleted = "Its skills and copy are" if skills_removed else "Its copy is"
            raise click.ClickException(
                f"Could not write {manifest}: {e}.\n{deleted} deleted, but it is "
                "still listed. Check the file permissions. Then run "
                f"{_remove_command(name, global_=global_)} again."
            ) from e
        click.secho(f"Removed extension {name!r} ({scope} scope).", fg="green")
        return
    for scope, unreadable in (
        ("project", unreadable_project_list),
        ("user", unreadable_user_list),
    ):
        if unreadable is not None:
            raise _unreadable_list_error(
                name, unreadable, f"it is installed at {scope} scope", global_=global_
            )
    raise _not_installed_error(name, global_=global_)


def _project_list_unreadable_error(name: str, project_list: Path) -> click.ClickException:
    # Plain `remove` acts at project scope first, so with that list unreadable it
    # must not fall through to the user-scope install.
    return _unreadable_list_error(
        name,
        project_list,
        "it is installed at project scope",
        global_=False,
        extra=f", or run {_remove_command(name, global_=True)} to remove the "
        "user-scope install",
    )


def _unreadable_list_error(
    name: str, path: Path, unknown: str, *, global_: bool, extra: str = ""
) -> click.ClickException:
    return click.ClickException(
        f"Cannot remove extension {name!r}: {path} cannot be fully read, so it is "
        f"not known whether {unknown}. Nothing was changed. Fix that file, then run "
        f"{_remove_command(name, global_=global_)} again{extra}."
    )


def _remove_skills_or_fail(
    scope: str, root: Path, extension_name: str, *, global_: bool
) -> bool:
    """Delete the extension's skills; returns whether it deleted any."""
    skills_dir = skills_dir_for_scope(scope, root)
    try:
        skill_names = installed_skill_names(skills_dir, extension_name)
    except SkillError as e:
        raise click.ClickException(
            f"Cannot remove the skills of extension {extension_name!r}: {e}\n"
            f"{_retry_hint(extension_name, global_=global_)}"
        ) from e
    other_manifest = other_scope_manifest(scope, root) if skill_names else None
    if other_manifest is not None:
        if scope == "project":
            other = "user"
            next_step = _remove_command(extension_name, global_=True)
        else:
            other = "project"
            next_step = (
                f"{_remove_command(extension_name, global_=False)} from your home folder"
            )
        other_list = read_extension_list(other_manifest, warn=True)
        # Listed there, so keep the skills even if the rest of the file is broken.
        if extension_name in other_list.names:
            logging.warning(
                "Kept the skills of extension %r: it is still installed at %s scope, "
                "and both scopes use ~/.agents/skills/ because your home folder is a "
                "project. To delete them, also run %s.",
                extension_name,
                other,
                next_step,
            )
            return False
        if not other_list.complete:
            raise _unreadable_list_error(
                extension_name,
                other_manifest,
                f"the {other} scope still uses its skills",
                global_=global_,
            )
    failures = remove_skills(skills_dir, skill_names) if skill_names else []
    link_skills_for_antigravity_if_global(skills_dir)
    if failures:
        raise click.ClickException(
            "\n".join(failures) + f"\n{_retry_hint(extension_name, global_=global_)}"
        )
    return bool(skill_names)


def _retry_hint(name: str, *, global_: bool) -> str:
    return (
        f"Extension {name!r} is still installed. Check the file permissions and "
        "close any program that uses these files. Then run "
        f"{_remove_command(name, global_=global_)} again."
    )


def _not_installed_error(name: str, *, global_: bool) -> click.ClickException:
    if not global_:
        return click.ClickException(f"Extension {name!r} is not installed.")
    project_root = scope_root("project")
    if project_root is not None:
        project_list = read_extension_list(project_root / EXTENSIONS_FILE, warn=False)
        if name in project_list.names:
            return click.ClickException(
                f"Extension {name!r} is installed at project scope only. To remove "
                f"it, run {_remove_command(name, global_=False)}."
            )
    return click.ClickException(f"Extension {name!r} is not installed at user scope.")


def _remove_command(name: str, *, global_: bool) -> str:
    flag = " --global" if global_ else ""
    return f"`agents-cli extension remove {name}{flag}`"
