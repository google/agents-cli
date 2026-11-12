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

"""Resolve extension references to pinned revisions and vendor extension directories."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from filelock import FileLock, Timeout
from packaging.version import InvalidVersion, Version

from google.agents.cli.__init__ import __version__
from google.agents.cli._fs import delete_folder, first_copy_error, make_folders_writable
from google.agents.cli._git import is_commit_sha, is_option_like
from google.agents.cli._runner import run_resolved
from google.agents.cli._symlinks import (
    ScaffoldSymlinkSecurityError,
    find_repo_boundary,
    follow_symlink,
    vet_symlinks,
)
from google.agents.cli.extension._paths import git_cache_dir_name, git_cache_root
from google.agents.cli.extension._pypi import (
    PypiError,
    extension_from_wheel,
    latest_wheel_version,
    resolve_wheel_sha256,
)
from google.agents.cli.extension._refs import (
    LATEST,
    UNPINNED_REF,
    ExtensionRef,
    pypi_source,
)
from google.agents.cli.extension._spec import EXTENSION_FILE
from google.agents.cli.scaffold.utils.fs import source_ignore_patterns

# An extension name becomes a directory component under the vendoring root, so it
# must not contain path separators or traversal — reject anything else.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_RELEASE_TAG_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")


def validate_extension_name(name: str) -> str:
    """Return ``name`` if safe as a single path component, else raise.

    Guards the vendoring path (``dest_root / name``, which ``_replace_tree``
    ``rmtree``s and recreates) against a malicious ``name:`` (e.g. ``../../x``)
    read from an upstream extension file or a committed manifest.
    """
    if name in (".", "..") or not _SAFE_NAME_RE.match(name):
        raise ResolverError(
            f"unsafe extension name {name!r}: use only letters, digits, '.', '_', '-'."
        )
    return name


# Seconds to wait for the shared git-cache lock before giving up.
_CACHE_LOCK_TIMEOUT_SECONDS = 120

# Records what a vendored copy was materialized from, so sync can detect a
# stale or partial copy (dir exists but doesn't match its source) instead of
# trusting mere directory existence.
STAMP_FILE = ".agents-cli-extension-sha"

# Recorded as the pin of a `local@` extension, which has no commit to point at.
LOCAL_SHA = "local"


def _git_ignored(root: Path) -> frozenset[str]:
    """POSIX paths, relative to ``root``, that git considers ignored there.

    ``root`` is the repository root a ``local@`` source's links must stay
    inside, so the list also covers the folders those links lead to.

    Asked of git rather than parsed out of ``.gitignore``, which buys four
    things no pattern match of our own would get right: nested ignore files and
    their negations, ``.git/info/exclude`` and the user's global
    ``core.excludesFile``, correct results when ``root`` is a subdirectory of
    the repository whose rules apply (a ``local@…#selector`` ref), and — the one
    that is a correctness bug rather than a gap — force-added files. Those are
    tracked, so they are part of the extension however the patterns read, and
    ``--others`` never lists them.

    Empty when ``root`` is not in a work tree, which is a perfectly ordinary way
    to hold an extension source — and empty, rather than an error, when there is
    no git at all: vendoring a local source has never needed one, and gaining a
    hard dependency on it here would be a regression, not a feature.
    """
    try:
        result = _git(
            "-C",
            str(root),
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            # Collapse a wholly-ignored directory to its own name: the point is
            # to skip `.venv/` without first enumerating what is inside it.
            "--directory",
            "-z",
        )
    except OSError as e:
        logging.debug("Could not ask git what %s ignores: %s", root, e)
        return frozenset()
    if result.returncode != 0:
        logging.debug("No git ignore rules apply to %s: %s", root, result.stderr.strip())
        return frozenset()
    return frozenset(path.rstrip("/") for path in result.stdout.split("\0") if path)


class _SourceExclusions:
    """What to leave behind when reading an extension source.

    One instance answers for both the fingerprint and the copy, deliberately. If
    the hash counted something the copy leaves out, every change to it would
    stamp a mismatch and provoke a re-copy that produces a byte-identical tree —
    reporting a repair that did not happen.

    Git's ignore rules are matched against real paths, relative to
    ``gitignore_root``, so they also apply to what a symlink leads to: a path
    reached through a link is one git has never heard of. A link whose target
    git ignores is refused, as templates refuse a link into an ignored folder.

    For a path inside ``source``, a folder at or above ``source`` does not
    count: an extension kept in an ignored scratch folder (``tmp/ext``) is still
    the user's to vendor. A link target outside ``source`` gets no such pass,
    or ``tmp/ext/key.json -> ../creds.json`` would copy a file git ignores.
    """

    def __init__(self, source: Path, gitignore_root: Path | None):
        # None for a git checkout. It holds tracked files and nothing else, so
        # there is nothing ignored in it to find — and asking anyway would drop
        # a force-added file that the commit genuinely contains.
        self._root = os.path.realpath(gitignore_root) if gitignore_root else ""
        self._ignored = _git_ignored(gitignore_root) if gitignore_root else frozenset()
        self._source_parts: tuple[str, ...] = ()
        if gitignore_root:
            self._source_parts = self._relative_parts(os.path.realpath(source)) or ()

    def __call__(self, directory: str, names: list[str]) -> set[str]:
        excluded = source_ignore_patterns(directory, names)
        if not self._ignored:
            return excluded
        real_dir = os.path.realpath(directory)
        for name in names:
            if name in excluded:
                continue
            path = os.path.join(directory, name)
            # A link git ignores is left behind like any other ignored file,
            # whatever it points to.
            if self._is_git_ignored(os.path.join(real_dir, name)):
                excluded.add(name)
                continue
            if not os.path.islink(path):
                continue
            target = os.path.realpath(path)
            if self._is_git_ignored(target):
                raise ScaffoldSymlinkSecurityError(
                    f"Symlink '{path}' points to '{target}', which git ignores, so "
                    "it is never copied. Link to a file the repository tracks instead."
                )
        return excluded

    def _is_git_ignored(self, real_path: str) -> bool:
        """Whether git ignores ``real_path`` or a folder above it (see the class docstring)."""
        parts = self._relative_parts(real_path)
        if parts is None:
            return False
        inside_source = parts[: len(self._source_parts)] == self._source_parts
        first = len(self._source_parts) if inside_source else 0
        # A wholly ignored folder is listed once, by its own name (`--directory`).
        return any(
            PurePosixPath(*parts[: i + 1]).as_posix() in self._ignored
            for i in range(first, len(parts))
        )

    def _relative_parts(self, real_path: str) -> tuple[str, ...] | None:
        """Parts of ``real_path`` relative to the gitignore root.

        A path outside the root starts with ``..``, which git never lists, so it
        matches nothing; ``follow_symlink`` refuses a link that leads there.
        """
        try:
            return Path(os.path.relpath(real_path, self._root)).parts
        except ValueError:  # On Windows, a path on another drive.
            return None


class ResolverError(Exception):
    """Raised when an extension cannot be resolved or vendored."""


class CopyChangedError(ResolverError):
    """Raised when vendoring fails after it started to change the vendored copy."""

    def __init__(self, message: str, extension_dir: Path) -> None:
        super().__init__(message)
        self.extension_dir = extension_dir


def read_stamp(extension_dir: Path) -> str | None:
    """Return what an extension dir was materialized from, or None if unstamped."""
    stamp = extension_dir / STAMP_FILE
    if not stamp.exists():
        return None
    return stamp.read_text(encoding="utf-8").strip() or None


def unstamp(extension_dir: Path) -> None:
    """Delete the stamp, so the next `agents-cli install` copies the extension again."""
    try:
        (extension_dir / STAMP_FILE).unlink(missing_ok=True)
    except OSError as e:
        # A stale stamp would pass a half-replaced copy as current.
        logging.warning(
            "Could not mark %s as incomplete (%s). Delete that folder, then run "
            "`agents-cli install`.",
            extension_dir,
            e,
        )


def expected_stamp(ref: ExtensionRef, sha: str) -> str:
    """Return the stamp a freshly materialized copy of ``ref`` would carry.

    For a git ref that is the pinned SHA, and for a pypi ref the wheel's sha256.
    A ``local@`` ref has no commit to pin — its recorded pin is the constant
    ``LOCAL_SHA`` — so its copy is stamped with a hash of the source tree instead.
    Comparing that hash is what lets sync notice edits to a local source;
    comparing the pin would only match the sentinel against itself and never
    refresh.
    """
    if ref.kind != "local":
        return sha
    src, repo_root = _local_source(ref)
    return fingerprint(src, _SourceExclusions(src, repo_root), repo_root)


def fingerprint(
    root: Path, exclude: Callable[[str, list[str]], set[str]], repo_root: Path
) -> str:
    """Return a hash of the name and contents of every file under ``root``.

    Walked in sorted order and keyed on POSIX-style relative paths, so a given
    tree hashes identically across runs and platforms.

    Hashes exactly what the copy would contain: a symlink is hashed as the files
    it points to, and everything ``exclude`` drops is skipped.

    Scanned with ``os.scandir``, whose entries answer "directory or symlink?"
    from the directory read itself: one syscall per directory rather than a
    ``stat`` per name. An unreadable directory raises rather than being skipped,
    since a tree walked only in part hashes the same after a change inside the
    part that was missed.

    Raises ResolverError when a folder or file under ``root`` cannot be read,
    or a symlink under it cannot be followed.
    """
    digest = hashlib.sha256()
    # (directory, its POSIX path relative to root). A stack, so a deep source
    # tree cannot exhaust the interpreter's recursion limit.
    stack: list[tuple[str, str]] = [(str(root), "")]
    try:
        while stack:
            directory, rel_dir = stack.pop()
            with os.scandir(directory) as scan:
                entries = list(scan)
            excluded = exclude(directory, [entry.name for entry in entries])
            subdirs: list[os.DirEntry[str]] = []
            files: list[os.DirEntry[str]] = []
            for entry in entries:
                if entry.name in excluded:
                    continue
                if entry.is_symlink():
                    is_dir = follow_symlink(Path(entry.path), repo_root).is_dir()
                else:
                    is_dir = entry.is_dir(follow_symlinks=False)
                if is_dir:
                    subdirs.append(entry)
                else:
                    files.append(entry)
            prefix = f"{rel_dir}/" if rel_dir else ""
            for entry in sorted(files, key=lambda e: e.name):
                with open(entry.path, "rb") as f:
                    contents = hashlib.file_digest(f, "sha256").hexdigest()
                digest.update(f"{prefix}{entry.name}\0{contents}\0".encode())
            # Reversed, so popping visits the subdirectories in sorted order.
            for entry in sorted(subdirs, key=lambda e: e.name, reverse=True):
                stack.append((entry.path, f"{prefix}{entry.name}"))
    except (OSError, ScaffoldSymlinkSecurityError) as e:
        raise ResolverError(f"could not read local extension source {root}: {e}") from e
    return digest.hexdigest()


def _local_source(ref: ExtensionRef) -> tuple[Path, Path]:
    """Return the ``local@`` source dir and the root its symlinks must stay inside."""
    src = Path(ref.upstream)
    if not src.exists():
        raise ResolverError(f"local extension path not found: {src}.")
    sub = _inside(src, ref.selector) if ref.selector else src
    if not (sub / EXTENSION_FILE).exists():
        raise ResolverError(f"{sub} has no {EXTENSION_FILE}.")
    return sub, find_repo_boundary(sub) or src


def _inside(root: Path, selector: str) -> Path:
    """Return ``root / selector``, or raise if it resolves outside ``root``.

    parse_ref keeps `..` and absolute paths out of a selector, but a symlink
    (or Windows junction) at any of its components would still carry the copy
    out of the source: copytree follows ``src`` itself, and ``vet_symlinks``
    only checks the entries beneath it.
    """
    sub = root / selector
    try:
        inside = sub.resolve().is_relative_to(root.resolve())
    # Python 3.11 reports a symlink loop as RuntimeError rather than OSError.
    except (OSError, RuntimeError) as e:
        raise ResolverError(f"could not resolve {sub}: {e}") from e
    if not inside:
        raise ResolverError(
            f"{sub} leads outside {root} through a symlink; an extension selector "
            "must name a real directory inside the source."
        )
    return sub


def _https_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


def _clone_url(ref: ExtensionRef) -> str:
    """Return the URL git should clone or query for a non-local ref."""
    if ref.url is not None:
        return ref.url
    return _https_url(ref.upstream)


def _git(*args: str) -> subprocess.CompletedProcess:
    """Run a git command with output captured (no shell, no PATH resolution)."""
    return run_resolved(
        ["git", *args],
        resolve_executable=False,
        capture_output=True,
        text=True,
    )


def resolve_sha(ref: ExtensionRef) -> str:
    """Resolve an ExtensionRef to the exact revision to pin.

    That is the sentinel ``LOCAL_SHA`` for a local ref (unpinned), the wheel's
    sha256 for a pypi ref, and the commit SHA for a git ref.
    """
    if ref.kind == "local":
        return LOCAL_SHA
    if ref.kind == "pypi":
        return _resolve_pypi_sha(ref)
    return _resolve_commit_sha(ref)


def _resolve_pypi_sha(ref: ExtensionRef) -> str:
    try:
        return resolve_wheel_sha256(ref.upstream, ref.ref)
    except PypiError as e:
        raise ResolverError(str(e)) from e


def _resolve_commit_sha(ref: ExtensionRef) -> str:
    # Anything short of a full SHA goes through ls-remote: a short hex string
    # could be a branch or tag named like hex.
    if ref.ref and is_commit_sha(ref.ref):
        return ref.ref
    target = ref.ref or "HEAD"
    if is_option_like(target):
        raise ResolverError(
            f"invalid ref {target!r}: pass a branch, tag, or commit SHA, "
            "which cannot start with '-'."
        )
    # git lists an annotated tag's `^{}` commit only when asked for it by name.
    result = _git("ls-remote", "--", _clone_url(ref), target, f"{target}^{{}}")
    if result.returncode != 0 or not result.stdout.strip():
        raise ResolverError(
            f"could not resolve {target!r} in {ref.upstream!r}{_git_reason(result)}; "
            "check the repo and ref."
        )
    lines = [line.partition("\t") for line in result.stdout.splitlines() if line]
    sha = next((s for s, _, name in lines if name.endswith("^{}")), lines[0][0])
    if not is_commit_sha(sha):
        raise ResolverError(
            f"git ls-remote returned {sha!r} for {target!r} in {ref.upstream!r}, "
            "which is not a commit SHA."
        )
    return sha


def pin_ref(ref: ExtensionRef) -> ExtensionRef:
    """Pin an unpinned first-party or pypi ref, or ``LATEST``, to a release.

    A first-party ref gets the newest release tag no newer than the running
    CLI; an unversioned pypi ref gets the newest release, recorded as ``==<version>``.
    """
    if ref.kind == "pypi":
        return _pin_pypi_release(ref)
    if ref.ref == LATEST and ref.kind != "first_party":
        raise ResolverError(
            f"{LATEST} is only for first-party and pypi@ extensions; pin a branch, "
            "tag, or SHA instead."
        )
    # UNPINNED_REF reads back as "no ref", which pins the latest release.
    if ref.kind == "first_party" and ref.ref == UNPINNED_REF:
        raise ResolverError(
            f"{UNPINNED_REF} is not supported for a first-party extension; pin main to follow "
            f"the default branch, or {LATEST} for the latest release."
        )
    if ref.kind == "first_party" and ref.ref in (None, LATEST):
        tag = _pick_release(_list_release_tags(ref), ref)
        logging.info(
            "Using %s, the latest release of %s for agents-cli %s.",
            tag,
            ref.upstream,
            __version__,
        )
        return replace(ref, ref=tag)
    return ref


def _pin_pypi_release(ref: ExtensionRef) -> ExtensionRef:
    if ref.ref not in (None, LATEST):
        return ref
    try:
        version = latest_wheel_version(ref.upstream)
    except PypiError as e:
        raise ResolverError(str(e)) from e
    return replace(ref, ref=version, raw=pypi_source(ref.upstream, version, ref.selector))


def _list_release_tags(ref: ExtensionRef) -> list[str]:
    result = _git("ls-remote", "--tags", "--", _clone_url(ref), "v*")
    if result.returncode != 0:
        raise ResolverError(
            f"could not list the release tags of {ref.upstream!r}{_git_reason(result)}; "
            "check your network and git access, or pin a branch, tag, or SHA instead."
        )
    names = (
        line.partition("\t")[2].removeprefix("refs/tags/")
        for line in result.stdout.splitlines()
    )
    tags = [name for name in names if _RELEASE_TAG_RE.fullmatch(name)]
    if not tags:
        raise ResolverError(
            f"{ref.upstream!r} has no release tags; pin a branch, tag, or SHA instead."
        )
    return tags


def _git_reason(result: subprocess.CompletedProcess) -> str:
    stderr = " ".join(result.stderr.split())
    return f" ({stderr})" if stderr else ""


def _pick_release(tags: Sequence[str], ref: ExtensionRef) -> str:
    # A release newer than the running CLI may need commands it doesn't have yet.
    running = _running_release()
    eligible = [tag for tag in tags if running is None or Version(tag) <= running]
    if not eligible:
        raise ResolverError(
            f"{ref.upstream!r} has no release at or below agents-cli {__version__}; "
            "upgrade agents-cli, or pin a branch, tag, or SHA instead."
        )
    return max(eligible, key=Version)


def _running_release() -> Version | None:
    # A source build has no release to match, so it gets the newest one.
    try:
        running = Version(__version__)
    except InvalidVersion:
        return None
    return None if running.is_devrelease else running


def _extension_name_from_dir(extension_dir: Path, fallback: str) -> str:
    spec_path = extension_dir / EXTENSION_FILE
    if spec_path.exists():
        try:
            with open(spec_path, encoding="utf-8") as f:
                data: Any = yaml.safe_load(f) or {}
            if isinstance(data, dict) and isinstance(data.get("name"), str):
                return str(data["name"])
        except (OSError, RecursionError, UnicodeDecodeError, yaml.YAMLError):
            # Best-effort name detection only: fall back to the caller's name if
            # the file is unreadable/malformed. The loader reports it properly —
            # warned about and skipped on load, fatal for `add`/`update`.
            pass
    return fallback


def _replace_tree(
    src: Path, dest: Path, exclude: _SourceExclusions, repo_root: Path
) -> None:
    """Replace ``dest`` with a copy of ``src``, following links inside ``repo_root``.

    Leaves ``dest`` alone unless the swap itself fails; then raises
    CopyChangedError.
    """
    if dest.is_symlink():
        raise ResolverError(f"{dest} is a symlink, so it cannot be replaced.")
    # Copy alongside and swap, so an unreadable source cannot delete the working
    # copy on its way to failing. shutil.Error is an OSError.
    staged = dest.parent / f".{dest.name}.incoming"
    try:
        delete_folder(staged)
        # copytree follows every link it is not told to ignore, so each one is
        # checked first: dereferencing `notes.md -> ~/.ssh/id_rsa` would put the
        # key in the project, where it gets committed (CWE-59).
        shutil.copytree(src, staged, ignore=vet_symlinks(repo_root, exclude))
    except (OSError, ScaffoldSymlinkSecurityError) as e:
        delete_folder(staged)
        reason = first_copy_error(e) if isinstance(e, OSError) else str(e)
        raise ResolverError(f"could not copy {src} into {dest}: {reason}") from e
    try:
        if dest.exists():
            make_folders_writable(dest)
            shutil.rmtree(dest)
        staged.rename(dest)
    except OSError as e:
        delete_folder(staged)
        message = f"could not copy {src} into {dest}: {first_copy_error(e)}"
        raise CopyChangedError(message, dest) from e


def _write_stamp(extension_dir: Path, sha: str) -> None:
    """Record the SHA the copy was materialized from (written last, post-copy)."""
    try:
        (extension_dir / STAMP_FILE).write_text(sha, encoding="utf-8")
    except OSError as e:
        raise CopyChangedError(
            f"could not stamp {extension_dir}: {e}", extension_dir
        ) from e


def materialize(
    ref: ExtensionRef,
    sha: str,
    dest_root: Path,
    *,
    name: str | None = None,
    ref_flag: str | None = None,
) -> tuple[str, Path]:
    """Vendor an extension directory into ``dest_root/<extension_name>``.

    Returns ``(extension_name, extension_dir)``.
    For ``local@`` refs, copies from the local path.
    For ``pypi@`` refs, unpacks the wheel whose sha256 is ``sha``.
    For git refs, clones/reuses the cache and checks out ``sha``.

    When ``name`` is provided the vendored directory is placed at
    ``dest_root/<name>`` (the caller's stable id) and that name is returned,
    regardless of what the upstream ``agents-cli-extension.yaml`` declares.  This
    prevents an extension rename upstream from silently relocating (and losing) the
    vendored copy.

    ``ref_flag`` is the caller's flag for pinning a ref, named in error hints.
    """
    dest_root.mkdir(parents=True, exist_ok=True)
    if ref.kind == "local":
        return _materialize_local(ref, dest_root, name=name)
    if ref.kind == "pypi":
        return _materialize_pypi(ref, sha, dest_root, name=name)
    return _materialize_commit(ref, sha, dest_root, name=name, ref_flag=ref_flag)


def _materialize_local(
    ref: ExtensionRef, dest_root: Path, *, name: str | None
) -> tuple[str, Path]:
    sub, repo_root = _local_source(ref)
    spec_name = _extension_name_from_dir(sub, sub.name)
    resolved_name = validate_extension_name(name if name is not None else spec_name)
    extension_dir = dest_root / resolved_name
    exclude = _SourceExclusions(sub, repo_root)
    # Fingerprinted before the copy, so a source edited while it is being
    # copied leaves the older stamp and the next sync redoes the copy,
    # rather than blessing a tree that is half of each.
    stamp = fingerprint(sub, exclude, repo_root)
    _replace_tree(sub, extension_dir, exclude, repo_root)
    _write_stamp(extension_dir, stamp)
    return resolved_name, extension_dir


def _materialize_pypi(
    ref: ExtensionRef, sha: str, dest_root: Path, *, name: str | None
) -> tuple[str, Path]:
    try:
        with extension_from_wheel(ref.upstream, sha, ref.selector) as sub:
            # Unpacking refused the wheel's symlinks, so none lead outside sub.
            return _vendor(ref, sha, sub, sub, dest_root, name=name)
    except PypiError as e:
        raise ResolverError(str(e)) from e


def _materialize_commit(
    ref: ExtensionRef,
    sha: str,
    dest_root: Path,
    *,
    name: str | None,
    ref_flag: str | None,
) -> tuple[str, Path]:
    checkout = _checkout(ref, sha)
    if ref.kind == "first_party":
        sub = (
            _first_party_source(checkout, ref, sha, ref_flag)
            if ref.selector
            else checkout
        )
    else:
        sub = _inside(checkout, ref.selector) if ref.selector else checkout
    if not (sub / EXTENSION_FILE).exists():
        raise ResolverError(
            f"{ref.upstream}#{ref.selector or ''} has no {EXTENSION_FILE} at the pinned commit."
        )
    return _vendor(ref, sha, sub, checkout, dest_root, name=name)


def _checkout(ref: ExtensionRef, sha: str) -> Path:
    """Clone (or reuse) the repo in the shared cache and check out ``sha``.

    Returns the worktree root path.
    """
    # The SHA can come from a committed extension list, and git would read
    # anything but a commit (e.g. ``--upload-pack=<cmd>``) as an option.
    if not is_commit_sha(sha):
        raise ResolverError(
            f"invalid commit SHA {sha!r}: expected 40 lowercase hex characters. "
            "If it is the `sha:` recorded in agents-cli-extensions.yaml, fix it "
            "or remove and re-add the extension."
        )
    cache = git_cache_root()
    cache.mkdir(parents=True, exist_ok=True)
    repo_dir = cache / git_cache_dir_name(ref.upstream, ref.url)
    url = _clone_url(ref)

    # The cache is shared across processes and checkout mutates a single working
    # tree, so serialize all git operations on this repo with a per-repo lock.
    try:
        lock = FileLock(
            str(cache / f"{repo_dir.name}.lock"),
            timeout=_CACHE_LOCK_TIMEOUT_SECONDS,
        )
        with lock:
            return _checkout_locked(repo_dir, url, ref.upstream, sha)
    except Timeout as e:
        raise ResolverError(
            f"timed out waiting for the extension cache lock for {ref.upstream}; "
            "another agents-cli process may be resolving extensions."
        ) from e


def _clone(repo_dir: Path, url: str, repo: str) -> None:
    """Clone ``url`` into ``repo_dir``, fetching as little as possible.

    Only the pinned commit is ever checked out, so the default branch's file
    contents are pure waste: ``--no-checkout`` skips materializing them and
    ``--filter=blob:none`` skips downloading them, leaving git to fetch blobs
    lazily for the commit that is actually wanted.

    ``--depth=1`` is deliberately not used. The caller checks out an arbitrary
    SHA, which a shallow clone can only reach if the server allows
    ``uploadpack.allowReachableSHA1InWant``, and the existing ``fetch --all``
    fallback does not unshallow.
    """
    base = ["clone", "--quiet", "--no-checkout"]
    res = _git(*base, "--filter=blob:none", "--", url, str(repo_dir))
    if res.returncode == 0:
        return

    # A server that does not set uploadpack.allowFilter needs no handling here:
    # measured against git 2.51, the client warns ("filtering not recognized by
    # server, ignoring") and completes a full clone, exit 0. What does fail is a
    # client older than the flag (git < 2.19), which rejects it as an unknown
    # option — so retry without it rather than breaking `extension add` outright.
    logging.debug("Blobless clone of %s failed, retrying in full: %s", repo, res.stderr)
    shutil.rmtree(repo_dir, ignore_errors=True)
    res = _git(*base, "--", url, str(repo_dir))
    if res.returncode != 0:
        raise ResolverError(f"git clone failed for {repo}: {res.stderr.strip()}")


def _checkout_locked(repo_dir: Path, url: str, repo: str, sha: str) -> Path:
    if not (repo_dir / ".git").exists():
        _clone(repo_dir, url, repo)

    fetch = _git("-C", str(repo_dir), "fetch", "--quiet", "--", "origin", sha)
    # Some hosts reject fetching an arbitrary SHA; fall back to a full fetch.
    if fetch.returncode != 0:
        _git("-C", str(repo_dir), "fetch", "--quiet", "--all")

    checkout = _git("-C", str(repo_dir), "checkout", "--quiet", sha)
    if checkout.returncode != 0:
        raise ResolverError(f"git checkout {sha} failed: {checkout.stderr.strip()}")
    return repo_dir


def _first_party_source(
    checkout: Path, ref: ExtensionRef, sha: str, ref_flag: str | None
) -> Path:
    """Find the extension directory for ``ref.selector`` under extensions/."""
    sub = _inside(checkout, f"extensions/{ref.selector}")
    if (sub / EXTENSION_FILE).exists():
        return sub
    template = _inside(checkout, f"extensions/{ref.selector}/template")
    if (template / EXTENSION_FILE).exists():
        return template
    pin = f" --ref {ref.ref}" if ref.ref else ""
    nested = sorted(
        manifest.parent.relative_to(checkout / "extensions").as_posix()
        for manifest in sub.rglob(EXTENSION_FILE)
    )
    if nested:
        commands = "\n".join(
            f"  agents-cli extension add {path.split('/', 1)[0]}#{path}{pin}"
            for path in nested
        )
        raise ResolverError(
            f"{ref.selector!r} has no {EXTENSION_FILE} in extensions/{ref.selector}/ "
            f"or its template/ at {sha}. Install one of the extensions found under "
            f"it instead:\n{commands}"
        )
    upgrade = (
        "upgrade agents-cli for a newer release, "
        if ref.ref and _RELEASE_TAG_RE.fullmatch(ref.ref)
        else ""
    )
    example = f", for example {ref_flag} main" if ref_flag and ref.ref != "main" else ""
    raise ResolverError(
        f"no first-party extension {ref.selector!r} in {ref.upstream} at {ref.ref or sha}. "
        f"Check the name, {upgrade}or pin a branch, tag, or commit where it "
        f"exists{example}."
    )


def _vendor(
    ref: ExtensionRef,
    sha: str,
    sub: Path,
    source_root: Path,
    dest_root: Path,
    *,
    name: str | None,
) -> tuple[str, Path]:
    """Copy a fetched extension folder into ``dest_root`` and stamp it with ``sha``."""
    # The selected folder's own name, like a local@ copy; a nested selector is a path.
    fallback = PurePosixPath(ref.selector).name if ref.selector else None
    spec_name = _extension_name_from_dir(sub, fallback or ref.upstream.split("/")[-1])
    resolved_name = validate_extension_name(name if name is not None else spec_name)
    extension_dir = dest_root / resolved_name
    _replace_tree(sub, extension_dir, _SourceExclusions(sub, None), source_root)
    _write_stamp(extension_dir, sha)
    return resolved_name, extension_dir
