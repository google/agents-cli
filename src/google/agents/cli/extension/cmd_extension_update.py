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

"""agents-cli extension update command."""

from __future__ import annotations

import contextlib
import logging
import shutil
import stat
import tempfile
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

import click

from google.agents.cli.__init__ import __version__
from google.agents.cli._fs import delete_folder, first_copy_error, make_folders_writable
from google.agents.cli._trust import require_confirmation
from google.agents.cli.extension._loader import is_out_of_range, load_extension_spec
from google.agents.cli.extension._manifest import (
    EXTENSIONS_FILE,
    ExtensionEntry,
    ExtensionListError,
    read_extension_entries,
    upsert_extension_entry,
)
from google.agents.cli.extension._paths import (
    installed_scope_roots,
    vendored_extensions_dir,
)
from google.agents.cli.extension._refs import (
    LATEST,
    UNPINNED_REF,
    ExtensionRef,
    RefParseError,
    is_pypi_reference,
    parse_ref,
    strip_pin,
)
from google.agents.cli.extension._resolver import (
    LOCAL_SHA,
    CopyChangedError,
    ResolverError,
    materialize,
    pin_ref,
    resolve_sha,
    validate_extension_name,
)
from google.agents.cli.extension._skills import (
    SkillError,
    SkillOwnedError,
    SkillUndoError,
    installed_skill_names,
    link_skills_for_antigravity_if_global,
    other_scope_extensions,
    replace_skills,
    report_skill_changes,
    skills_dir_for_scope,
)
from google.agents.cli.extension._spec import ExtensionSpec, ExtensionSpecError
from google.agents.cli.extension._trust import confirm_trust

# "unchanged": updated to the pin it already had.
_Outcome: TypeAlias = Literal["updated", "unchanged", "skipped", "retry"]
_OldCopy: TypeAlias = Literal["kept", "lost", "none"]

_BOTH_SCOPES_REASON = (
    "it is installed at both scopes, which share ~/.agents/skills/ because "
    "your home folder is a project"
)
_NO_OLD_COPY_HINT = (
    "Point the source at a version this agents-cli supports, then run agents-cli install"
)


@dataclass
class _ScopeRun:
    """One scope's state while its entries are updated.

    `pending` holds the entries after the current one. Only `_update_scope`
    changes `pending`, `retried` and `updated`.
    """

    scope: str
    root: Path
    skills_dir: Path
    installed_extensions: frozenset[str]
    other_scope: frozenset[str]
    at_both_scopes: frozenset[str]
    backup_dir: Path
    auto_approve: bool
    updates_all: bool
    to: str | None
    pending: deque[ExtensionEntry]
    retried: set[str]
    updated_earlier: tuple[str, ...]
    updated: list[str]


@dataclass(frozen=True)
class _Updated:
    name: str
    scope: str
    pin_changed: bool


@click.command("update")
@click.argument("name", required=False)
@click.option(
    "--to",
    default=None,
    help=(
        "Re-pin NAME to this branch, tag, or SHA, or for a pypi@ extension to "
        f"this version. For first-party and pypi@ extensions, {LATEST} pins the "
        "latest release, and works without NAME."
    ),
)
@require_confirmation("Update extension(s) and replace their skills?")
def cmd_update(
    name: str | None,
    to: str | None,
    auto_approve: bool,
) -> None:
    """Advance extension pins and replace their skills.

    Updates all if NAME omitted.
    """
    if to is not None and not strip_pin(to):
        raise click.UsageError(f"--to needs a branch, tag, SHA, version, or {LATEST}.")
    if to is not None and to != LATEST and name is None:
        raise click.UsageError(
            f"--to {to} needs an extension NAME; only --to {LATEST} "
            "applies to every extension."
        )
    updated: list[_Updated] = []
    skipped: list[str] = []
    scope_roots = list(installed_scope_roots())
    at_both_scopes = _names_at_both_scopes(scope_roots)
    with _backups_root() as backups:
        for scope, root in scope_roots:
            scope_updated, scope_skipped = _update_scope(
                scope,
                root,
                name,
                to,
                auto_approve=auto_approve,
                backup_dir=backups / scope,
                at_both_scopes=at_both_scopes,
                updated_earlier=tuple(u.name for u in updated),
            )
            updated.extend(scope_updated)
            skipped.extend(scope_skipped)
    if not updated and not skipped:
        raise click.ClickException(
            f"No extension named {name!r} found." if name else "No extensions to update."
        )
    changed = [
        f"{u.name}{_scope_suffix(u.scope, u.name, at_both_scopes)}"
        for u in updated
        if u.pin_changed
    ]
    if changed:
        logging.info("Updated: %s.", ", ".join(changed))
    # A batch update skips by design, so only a named one fails.
    if name and skipped:
        raise click.ClickException(f"Could not update {name!r}; see the warnings above.")


def _names_at_both_scopes(scope_roots: list[tuple[str, Path]]) -> frozenset[str]:
    listed = [
        {entry.name for entry in read_extension_entries(root / EXTENSIONS_FILE)}
        for _, root in scope_roots
    ]
    return frozenset(set.intersection(*listed)) if len(listed) == 2 else frozenset()


@contextlib.contextmanager
def _backups_root() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as backups:
        try:
            yield Path(backups)
        finally:
            # Make every folder writable first, so the cleanup never needs its own
            # chmod: before Python 3.11.8 / 3.12.2 that chmod follows symlinks
            # (CVE-2023-6597).
            make_folders_writable(Path(backups))


def _update_scope(
    scope: str,
    root: Path,
    name: str | None,
    to: str | None,
    *,
    auto_approve: bool,
    backup_dir: Path,
    at_both_scopes: frozenset[str],
    updated_earlier: tuple[str, ...],
) -> tuple[list[_Updated], list[str]]:
    """Update the scope's matching entries, retrying one whose skill owner runs later."""
    entries = read_extension_entries(root / EXTENSIONS_FILE)
    other_scope = frozenset(other_scope_extensions(scope, root))
    run = _ScopeRun(
        scope=scope,
        root=root,
        skills_dir=skills_dir_for_scope(scope, root),
        installed_extensions=frozenset(entry.name for entry in entries) | other_scope,
        other_scope=other_scope,
        at_both_scopes=at_both_scopes,
        backup_dir=backup_dir,
        auto_approve=auto_approve,
        updates_all=name is None,
        to=to,
        pending=deque(entry for entry in entries if name is None or entry.name == name),
        retried=set(),
        updated_earlier=updated_earlier,
        updated=[],
    )
    done: list[_Updated] = []
    skipped: list[str] = []
    while run.pending:
        entry = run.pending.popleft()
        outcome = _update_entry(run, entry)
        if outcome == "retry":
            run.retried.add(entry.name)
            run.pending.append(entry)
        elif outcome == "skipped":
            skipped.append(entry.name)
        else:
            run.updated.append(entry.name)
            done.append(_Updated(entry.name, run.scope, pin_changed=outcome == "updated"))
    return done, skipped


def _update_entry(run: _ScopeRun, entry: ExtensionEntry) -> _Outcome:
    """Check that the entry can be updated, back up its copy, then advance it."""
    ref = _parse_entry_ref(run, entry)
    if ref is None:
        return "skipped"
    if entry.name in run.other_scope and _has_installed_skills(run, entry.name):
        logging.warning(
            "Skipped %r (%s scope): %s. Remove it at one scope first.",
            entry.name,
            run.scope,
            _BOTH_SCOPES_REASON,
        )
        return "skipped"
    already_trusted = entry.name in run.retried
    if not already_trusted and not confirm_trust(ref, auto_approve=run.auto_approve):
        logging.warning(
            "Skipped %r%s (not trusted).", entry.name, _scope_label(run, entry.name)
        )
        return "skipped"
    extension_dir = vendored_extensions_dir(run.root) / entry.name
    try:
        backup = _back_up_copy(ref, extension_dir, run.backup_dir)
    except OSError as e:
        _report_skipped(
            run,
            entry,
            ref,
            f"could not back up the old copy ({first_copy_error(e)})",
            "kept",
        )
        return "skipped"
    return _advance(run, entry, ref, extension_dir, backup)


def _parse_entry_ref(run: _ScopeRun, entry: ExtensionEntry) -> ExtensionRef | None:
    """The ref to update the entry from.

    Returns None when its name or ref is invalid (with a warning), or when a
    batch ``--to :latest`` doesn't apply to it.
    """
    try:
        validate_extension_name(entry.name)
        ref = parse_ref(
            entry.source,
            ref_override=run.to or _recorded_ref(entry),
        )
        if (
            run.updates_all
            and run.to == LATEST
            and ref.kind not in ("first_party", "pypi")
        ):
            logging.info(
                "Kept %r: --to %s moves first-party and pypi@ extensions only.",
                entry.name,
                LATEST,
            )
            return None
        if run.to is not None and ref.kind == "local":
            raise ResolverError(
                "--to does not apply to a local@ extension: a local folder has "
                "no branches or tags. Drop --to."
            )
        return pin_ref(ref)
    except (ResolverError, RefParseError) as e:
        logging.warning("Skipping extension %r: %s", entry.name, e)
        return None


def _recorded_ref(entry: ExtensionEntry) -> str | None:
    if entry.ref == UNPINNED_REF:
        return None
    # A pypi@ source with an == pin wins over a stale `ref:`.
    if is_pypi_reference(entry.source) and parse_ref(entry.source).ref is not None:
        return None
    return entry.ref


def _has_installed_skills(run: _ScopeRun, name: str) -> bool:
    try:
        return bool(installed_skill_names(run.skills_dir, name))
    except SkillError as e:
        logging.debug("Could not list the skills of %r: %s", name, e)
        # The unreadable folder may hold them.
        return True


def _back_up_copy(
    ref: ExtensionRef, extension_dir: Path, backup_dir: Path
) -> Path | None:
    """Copy a `local@` or `pypi@` extension's vendored copy aside before it is replaced.

    A git pin can be materialized again from its SHA. A local source has no
    history, so copying it again would bring back the refused version. An index
    can stop serving a pinned wheel.
    A symlinked folder is skipped: update cannot replace it, so it never needs
    restoring.
    """
    if (
        ref.kind not in ("local", "pypi")
        or extension_dir.is_symlink()
        or not extension_dir.is_dir()
    ):
        return None
    backup = backup_dir / extension_dir.name
    if backup.exists():
        # A name listed twice is backed up twice; the first backup can hold
        # read-only folders copied from the old copy.
        make_folders_writable(backup)
        shutil.rmtree(backup)
    shutil.copytree(extension_dir, backup, symlinks=True)
    return backup


def _report_skipped(
    run: _ScopeRun,
    entry: ExtensionEntry,
    ref: ExtensionRef,
    reason: str,
    old_copy: _OldCopy,
    *,
    no_old_copy_hint: str = _NO_OLD_COPY_HINT,
) -> None:
    logging.warning(
        "Skipped %r%s: %s; %s.",
        entry.name,
        _scope_label(run, entry.name),
        reason,
        _old_copy_status(entry, ref, old_copy, no_old_copy_hint),
    )


def _scope_label(run: _ScopeRun, name: str) -> str:
    return _scope_suffix(run.scope, name, run.at_both_scopes)


def _scope_suffix(scope: str, name: str, at_both_scopes: frozenset[str]) -> str:
    # A name listed at both scopes gets one line per scope.
    return f" ({scope} scope)" if name in at_both_scopes else ""


def _old_copy_status(
    entry: ExtensionEntry, ref: ExtensionRef, old_copy: _OldCopy, no_old_copy_hint: str
) -> str:
    if old_copy == "kept":
        # A local@ entry's sha is the word "local", not a pin.
        return "kept the old copy" if ref.kind == "local" else f"kept {entry.sha[:7]}"
    if old_copy == "none":
        return f"deleted the refused copy (there was no old copy). {no_old_copy_hint}"
    return "could not restore the old copy; run agents-cli install"


def _advance(
    run: _ScopeRun,
    entry: ExtensionEntry,
    ref: ExtensionRef,
    extension_dir: Path,
    backup: Path | None,
) -> _Outcome:
    """Fetch the new version, check it, install its skills and record the new pin."""
    try:
        new_sha = _fetch_and_materialize_new_version(ref, extension_dir)
    except (ResolverError, OSError) as e:
        # One unreachable source must not abandon the others, or the
        # command reports failure after already advancing a pin.
        logging.warning(
            "Could not update %r%s: %s", entry.name, _scope_label(run, entry.name), e
        )
        if isinstance(e, CopyChangedError):
            _restore_changed_copy(entry, ref, extension_dir, backup)
        return "skipped"
    try:
        new_spec = load_extension_spec(extension_dir)
    except ExtensionSpecError as e:
        _restore_and_report(
            run,
            entry,
            ref,
            extension_dir,
            backup,
            reason=f"the new version's manifest is not valid ({e})",
        )
        return "skipped"
    refusal = _new_version_refusal(run, entry, new_spec)
    if refusal is not None:
        _restore_and_report(run, entry, ref, extension_dir, backup, reason=refusal)
        return "skipped"
    try:
        removed_skills = replace_skills(
            new_spec, run.skills_dir, entry.name, run.installed_extensions
        )
    except SkillError as e:
        return _skills_refused(run, entry, ref, extension_dir, backup, error=e)
    return _record_update(
        run, entry, ref, new_sha, new_spec=new_spec, removed_skills=removed_skills
    )


def _fetch_and_materialize_new_version(ref: ExtensionRef, extension_dir: Path) -> str:
    """Resolve the tracked ref and vendor that commit; returns its SHA."""
    new_sha = resolve_sha(ref)
    # name=extension_dir.name pins the vendored dir to the stable id (see
    # materialize) so an update survives an upstream rename.
    materialize(
        ref, new_sha, extension_dir.parent, name=extension_dir.name, ref_flag="--to"
    )
    return new_sha


def _restore_changed_copy(
    entry: ExtensionEntry, ref: ExtensionRef, extension_dir: Path, backup: Path | None
) -> None:
    """Undo a failed update that already changed the copy."""
    if backup is not None:
        restored = _restore_backup(entry, extension_dir, backup)
    elif ref.kind == "local":
        # There was no old copy, and a half-made one must not look installed.
        delete_folder(extension_dir)
        restored = not (extension_dir.exists() or extension_dir.is_symlink())
    else:
        restored = _materialize_old_pin(entry, ref, extension_dir)
    if not restored:
        logging.warning(
            "The copy of %r may be incomplete; run agents-cli install.", entry.name
        )


def _restore_backup(entry: ExtensionEntry, extension_dir: Path, backup: Path) -> bool:
    try:
        _swap_in_backup(extension_dir, backup)
    except OSError as e:
        logging.warning(
            "Could not restore the previous copy of %r: %s",
            entry.name,
            first_copy_error(e),
        )
        return False
    return True


def _swap_in_backup(extension_dir: Path, backup: Path) -> None:
    # Swapped by renames, so a failed step never half-deletes the old copy.
    work = Path(
        tempfile.mkdtemp(
            prefix=f".{extension_dir.name}.restoring-", dir=extension_dir.parent
        )
    )
    new_copy = work / "new"
    old_copy = work / "old"
    try:
        shutil.copytree(backup, new_copy, symlinks=True)
        # Linux will not move a read-only folder to another parent.
        mode = stat.S_IMODE(new_copy.stat().st_mode)
        new_copy.chmod(mode | stat.S_IRWXU)
        if extension_dir.exists():
            old_mode = stat.S_IMODE(extension_dir.stat().st_mode)
            extension_dir.chmod(old_mode | stat.S_IRWXU)
            extension_dir.rename(old_copy)
        try:
            new_copy.rename(extension_dir)
        except OSError:
            if old_copy.exists():
                old_copy.rename(extension_dir)
            raise
    finally:
        delete_folder(work)


def _materialize_old_pin(
    entry: ExtensionEntry, ref: ExtensionRef, extension_dir: Path
) -> bool:
    try:
        materialize(ref, entry.sha, extension_dir.parent, name=entry.name)
    except (ResolverError, OSError) as e:
        logging.warning("Could not restore the previous pin for %r: %s", entry.name, e)
        return False
    return True


def _restore_and_report(
    run: _ScopeRun,
    entry: ExtensionEntry,
    ref: ExtensionRef,
    extension_dir: Path,
    backup: Path | None,
    *,
    reason: str,
) -> None:
    old_copy = _restore_refused_copy(entry, ref, extension_dir, backup)
    _report_skipped(run, entry, ref, reason, old_copy)


def _restore_refused_copy(
    entry: ExtensionEntry, ref: ExtensionRef, extension_dir: Path, backup: Path | None
) -> _OldCopy:
    """Put the previously pinned copy back after refusing to advance to a new one.

    Returns what happened to the old copy. Unless it is back, it deletes the
    refused copy (or warns that it could not), so the loader asks for
    `agents-cli install`, as the skip message does.
    """
    if backup is not None:
        restored = _restore_backup(entry, extension_dir, backup)
    elif ref.kind == "local":
        delete_folder(extension_dir)
        return "none"
    else:
        restored = _materialize_old_pin(entry, ref, extension_dir)
    if restored:
        return "kept"
    delete_folder(extension_dir)
    return "lost"


def _new_version_refusal(
    run: _ScopeRun, entry: ExtensionEntry, new_spec: ExtensionSpec | None
) -> str | None:
    """Why the fetched version must not replace the old pin, or None."""
    if new_spec is None:
        return None
    # A version whose `error`-mode range excludes this CLI would not run here.
    if is_out_of_range(new_spec) and new_spec.error_on_incompatible:
        return (
            f"new version requires agents-cli "
            f"{new_spec.requires_agents_cli}, running {__version__}"
        )
    if entry.name in run.other_scope and new_spec.skills:
        return (
            f"the new version has skills, and {_BOTH_SCOPES_REASON} "
            "(remove it at one scope first)"
        )
    return None


def _skills_refused(
    run: _ScopeRun,
    entry: ExtensionEntry,
    ref: ExtensionRef,
    extension_dir: Path,
    backup: Path | None,
    *,
    error: SkillError,
) -> _Outcome:
    """Restore the old pin, then retry once after a later owner, or report the skip."""
    owner_runs_later = isinstance(error, SkillOwnedError) and any(
        later.name == error.owner for later in run.pending
    )
    old_copy = _restore_refused_copy(entry, ref, extension_dir, backup)
    if old_copy == "kept" and owner_runs_later and entry.name not in run.retried:
        return "retry"
    owner_updates_later = isinstance(error, SkillOwnedError) and (
        owner_runs_later or _owner_updates_at_user_scope(run, error.owner)
    )
    reason = _skill_error_reason(run, error, owner_updates_later=owner_updates_later)
    _report_skipped(
        run,
        entry,
        ref,
        f"the new version's skills cannot be installed ({reason})",
        old_copy,
        no_old_copy_hint=(
            f"Fix the cause, then run `agents-cli extension update {entry.name}` again"
        ),
    )
    if owner_updates_later:
        logging.warning(
            "If that update dropped the skill, run `agents-cli extension update %s` "
            "again.",
            entry.name,
        )
    if isinstance(error, SkillUndoError):
        logging.warning(
            "These skills may be missing or changed: %s. Run "
            "`agents-cli extension update %s` again to fix them.",
            ", ".join(error.not_restored),
            entry.name,
        )
    return "skipped"


def _owner_updates_at_user_scope(run: _ScopeRun, owner: str) -> bool:
    # User scope runs after project scope, so with no NAME this run also tries
    # to update an owner recorded there.
    return run.scope == "project" and run.updates_all and owner in run.other_scope


def _skill_error_reason(
    run: _ScopeRun, error: SkillError, *, owner_updates_later: bool
) -> str:
    """The error, worded so it is still true when the run ends."""
    if isinstance(error, SkillOwnedError):
        if owner_updates_later:
            return (
                f"skill {error.skill_name!r} was still installed by extension "
                f"{error.owner!r}, which this run tried to update after it"
            )
        if error.owner in run.updated_earlier or error.owner in run.updated:
            return error.with_hint("Remove that extension first.")
    if isinstance(error, SkillUndoError):
        return error.cause
    return str(error)


def _record_update(
    run: _ScopeRun,
    entry: ExtensionEntry,
    ref: ExtensionRef,
    new_sha: str,
    *,
    new_spec: ExtensionSpec | None,
    removed_skills: tuple[str, ...],
) -> _Outcome:
    """Relink the skills, record the new pin, and report the result."""
    # Even when no skill changed, a skill folder deleted by hand leaves a dead link.
    if run.skills_dir.is_dir():
        link_skills_for_antigravity_if_global(run.skills_dir)
    new_entry = ExtensionEntry.for_ref(ref, name=entry.name, sha=new_sha, scope=run.scope)
    try:
        upsert_extension_entry(run.root / EXTENSIONS_FILE, new_entry)
    except (OSError, ExtensionListError) as e:
        report_skill_changes(new_spec, entry.name, run.skills_dir, removed_skills)
        logging.warning(
            "Could not record the new pin of %r: %s. Its copy and skills are already "
            "the new version. Fix the cause (for example, file permissions), then run "
            "`agents-cli extension update %s` again.",
            entry.name,
            e,
            entry.name,
        )
        return "skipped"
    changed = _report_new_pin(run, entry, new_entry)
    if new_spec is not None and is_out_of_range(new_spec):
        logging.warning(
            "Updated %r to a version requiring agents-cli %s (running %s).",
            entry.name,
            new_spec.requires_agents_cli,
            __version__,
        )
    report_skill_changes(new_spec, entry.name, run.skills_dir, removed_skills)
    return "updated" if changed else "unchanged"


def _report_new_pin(
    run: _ScopeRun, entry: ExtensionEntry, new_entry: ExtensionEntry
) -> bool:
    """Log how the pin moved, and return whether it changed."""
    label = _scope_label(run, entry.name)
    if new_entry.sha == LOCAL_SHA:
        logging.info("Updated %r%s: copied %s again.", entry.name, label, entry.source)
        return True
    old_pin, new_pin = _pin_label(entry), _pin_label(new_entry)
    if entry.sha == new_entry.sha and old_pin == new_pin:
        # A pypi@ entry pins a release version; a git entry, a commit.
        unchanged = (
            "is already at" if new_entry.ref is None else "has no new commit; still at"
        )
        logging.info("%r%s %s %s.", entry.name, label, unchanged, new_pin)
        return False
    if new_entry.version and new_entry.version == (entry.version or entry.ref):
        logging.warning(
            "Updated %r%s: the index now serves a different wheel for %s "
            "(sha256 %s → %s); check that you trust it.",
            entry.name,
            label,
            new_entry.version,
            entry.sha[:7],
            new_entry.sha[:7],
        )
    else:
        logging.info("Updated %r%s: %s → %s.", entry.name, label, old_pin, new_pin)
    return True


def _pin_label(entry: ExtensionEntry) -> str:
    pin = entry.ref or entry.version or entry.source
    return f"{pin} ({entry.sha[:7]})"
