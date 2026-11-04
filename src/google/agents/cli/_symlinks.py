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

"""Security vetting for symlinks found in fetched template and extension sources.

Isolates the CWE-59 policy: a template or an extension may share code via a
symlink that stays within its own repository, but a link that escapes the repo,
points at VCS metadata / an environment file, dangles, or forms a cycle is
refused.
"""

import os
import pathlib
from collections.abc import Callable

from google.agents.cli.scaffold.utils.fs import is_ignored_name, is_secret_file

# Backstop to the inode-based cycle guard: refuse to materialize a template
# tree nested more deeply than this (a symlink loop, or a pathological repo).
MAX_COPY_DEPTH = 64
_REPO_MARKERS = (".git", ".hg", ".jj")


class ScaffoldSymlinkSecurityError(ValueError):
    """A template or extension source contains a symlink that cannot be copied safely.

    Subclasses ``ValueError`` so ``create()``'s handler renders it as a concise
    one-line ``ClickException`` rather than a traceback.
    """


def find_repo_boundary(path: pathlib.Path) -> pathlib.Path | None:
    """Nearest ancestor of *path* (inclusive) holding a VCS working-copy marker.

    Returns:
        The boundary directory, or None when *path* is not inside such a working
        copy.
    """
    path = path.resolve()
    for candidate in (path, *path.parents):
        if any((candidate / marker).exists() for marker in _REPO_MARKERS):
            return candidate
    return None


def resolve_safe_symlink(link: pathlib.Path, clone_root: pathlib.Path) -> pathlib.Path:
    """Resolve a template symlink and verify it is safe to materialize.

    A symlink is safe only when its fully-resolved target exists and stays
    within *clone_root* (the fetched repository), and does not point at an
    ignored path (VCS metadata, caches, build output, dependencies) or an
    environment/secret file (``.env``). This preserves the CWE-59 protection — a
    link escaping the repository (e.g. ``id_rsa -> ~/.ssh/id_rsa``) is still
    refused — while letting a template share code within its own repo.

    The containment check is against the **cloned repo root**, not the template
    subdirectory: a shared library naturally sits at the repo root while the
    template is a subdirectory, so a link legitimately points "up" out of the
    template dir but never out of the repo.

    Args:
        link: The symlink found in the template source.
        clone_root: Root the resolved target must stay within (the clone).

    Returns:
        The canonical (fully resolved) target path.

    Raises:
        ScaffoldSymlinkSecurityError: If the link is dangling, cyclic, escapes the
            repo, or targets an ignored path / an environment file.
    """
    target = _resolve_symlink(link=link)

    root = clone_root.resolve()
    if not target.is_relative_to(root):
        raise ScaffoldSymlinkSecurityError(
            f"Symlink '{link}' resolves to '{target}', which "
            "is outside the repository. For security reasons a symlink may not "
            "escape the repository; copy the shared files into it instead."
        )

    rel_parts = target.relative_to(root).parts
    ignored = next((part for part in rel_parts if is_ignored_name(part)), None)
    if ignored is not None:
        raise ScaffoldSymlinkSecurityError(
            f"Symlink '{_symlink_display(link)}' resolves into an ignored path "
            f"('{ignored}': version-control metadata, a cache, build output, or "
            "dependencies), which is never copied. Point the link at the source "
            f"file instead, or move the shared file out of '{ignored}'."
        )
    if is_secret_file(target.name):
        raise ScaffoldSymlinkSecurityError(
            f"Symlink '{link}' targets a credential file "
            f"('{target.name}'); refusing to copy it into the project."
        )
    return target


def require_safe_symlink(
    link: pathlib.Path,
    clone_root: pathlib.Path | None,
) -> pathlib.Path:
    """Resolve *link* to its target, or reject it.

    With no *clone_root* (a bundled, trusted copy) any symlink is refused;
    otherwise the target is vetted against *clone_root* by
    :func:`resolve_safe_symlink`.
    """
    if clone_root is None:
        raise ScaffoldSymlinkSecurityError(
            f"Symlink detected at '{_symlink_display(link)}'. Symlinks are not "
            "allowed here for security reasons; copy the files directly instead."
        )
    return resolve_safe_symlink(link, clone_root)


def follow_symlink(link: pathlib.Path, repo_root: pathlib.Path) -> pathlib.Path:
    """Return the target of ``link``, or raise ScaffoldSymlinkSecurityError.

    Vets the target with :func:`resolve_safe_symlink`, and also refuses a link to
    a folder that holds the link itself, since copying through it would never end.
    """
    target = resolve_safe_symlink(link, repo_root)
    if target.is_dir() and any(
        parent.resolve().is_relative_to(target) for parent in link.absolute().parents
    ):
        raise ScaffoldSymlinkSecurityError(
            f"Symlink '{link}' points to '{target}', a folder that contains the "
            "link itself, so copying it would never end. Link to a folder beside "
            "it instead."
        )
    return target


def vet_symlinks(
    repo_root: pathlib.Path,
    exclude: Callable[[str, list[str]], set[str]] | None = None,
) -> Callable[[str, list[str]], set[str]]:
    """Return a ``copytree(ignore=...)`` callback that refuses unsafe links.

    It returns what ``exclude`` drops, and checks every other link with
    :func:`follow_symlink`. ``copytree`` then follows the links that passed, so
    the copy holds their files rather than the links.
    """

    def ignore(directory: str, names: list[str]) -> set[str]:
        excluded = exclude(directory, names) if exclude else set()
        for name in names:
            link = pathlib.Path(directory, name)
            if name not in excluded and link.is_symlink():
                follow_symlink(link, repo_root)
        return excluded

    return ignore


def _resolve_symlink(link: pathlib.Path) -> pathlib.Path:
    """Resolve *link* to its target with no containment check."""
    try:
        # strict=True still rejects a dead link or a loop; there is just no
        # containment / ignored-path / secret-file check.
        return link.resolve(strict=True)
    except (OSError, RuntimeError) as e:
        raise ScaffoldSymlinkSecurityError(
            f"Symlink '{_symlink_display(link)}' points to a missing target or "
            "forms a loop. Fix the link, or copy the shared files instead of "
            "linking to them."
        ) from e


def _symlink_display(link: pathlib.Path) -> str:
    """``<link> -> <raw target>`` for error messages, best-effort."""
    try:
        return f"{link} -> {os.readlink(link)}"
    except OSError:
        return f"{link} -> (unresolvable)"
