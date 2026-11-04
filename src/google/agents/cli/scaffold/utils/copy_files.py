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

"""Copying template files into a generated project."""

import logging
import pathlib
import shutil
import sys
from dataclasses import dataclass, replace

from google.agents.cli._symlinks import (
    MAX_COPY_DEPTH,
    ScaffoldSymlinkSecurityError,
    require_safe_symlink,
)
from google.agents.cli.scaffold.utils.fs import is_ignored_name, is_secret_file

DEFAULT_FRONTEND = "None"

_TOOL_CACHES = frozenset(
    {"__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache", ".venv"}
)

# Flat-structure entries never copied into the generated project.
_FLAT_SKIP_FILES = frozenset(
    {"pyproject.toml", "uv.lock", "README.md", ".gitignore", "__pycache__"}
)


@dataclass(frozen=True)
class CopyOptions:
    """Policy for copying a template tree into a generated project.

    Attributes:
        overwrite: Overwrite existing files (True) or skip them (False).
        skip_manifest: Skip a root agents-cli-manifest.yaml.
        keep_template_dir: Retain a ``.template`` directory instead of dropping
            it.
        guidance_filename: Write a root AGENTS.md under this name instead, so a
            template's guide replaces the base one whatever the project calls it.
        skip_secret_files: Drop credential files (``.env`` / ``.env.*``) while
            copying.
        skip_root_files: Names not to copy from the top level of the source.
    """

    overwrite: bool = False
    skip_manifest: bool = False
    keep_template_dir: bool = False
    guidance_filename: str | None = None
    skip_secret_files: bool = False
    skip_root_files: frozenset[str] = frozenset()


def copy_files(
    src: pathlib.Path,
    dst: pathlib.Path,
    *,
    clone_root: pathlib.Path | None = None,
    options: CopyOptions | None = None,
    _visited: frozenset[tuple[int, int]] | None = None,
    _depth: int = 0,
) -> None:
    """
    Copy files with configurable behavior for exclusions and overwrites.

    Symlink handling: when *clone_root* is given (a fetched template), a symlink
    whose resolved target stays inside the repository is **materialized** — its
    real contents are copied so the generated project is portable — while an
    unsafe link (escaping the repo, targeting an ignored path / .env, dangling,
    or cyclic) raises ``ScaffoldSymlinkSecurityError``. With no *clone_root* (a bundled,
    trusted copy) any symlink is refused.

    Args:
        src: Source path
        dst: Destination path
        clone_root: Repository root that materialized symlink targets must stay
            within. When None, symlinks are refused rather than followed.
        options: Copy policy. Defaults to a no-op ``CopyOptions()``.
        _visited: Internal — inodes of directories on the current recursion
            path, used to detect symlink cycles.
        _depth: Internal — current recursion depth, capped at MAX_COPY_DEPTH.
    """
    if options is None:
        options = CopyOptions()
    if _visited is None:
        _visited = frozenset()

    if _depth > MAX_COPY_DEPTH:
        raise ScaffoldSymlinkSecurityError(
            f"Template nesting exceeded {MAX_COPY_DEPTH} levels while copying "
            f"'{src}'; refusing to continue (possible symlink loop)."
        )

    if _should_skip(src, options):
        logging.debug("Skipping file/directory: %s", src)
        return

    if src.is_dir():
        _copy_dir(
            src,
            dst,
            clone_root=clone_root,
            options=options,
            visited=_visited,
            depth=_depth,
        )
    elif src.is_symlink():
        _materialize_symlink(
            src,
            dst,
            clone_root=clone_root,
            options=options,
            visited=_visited,
            depth=_depth,
        )
    else:
        _copy_file(src, dst, overwrite=options.overwrite)


def copy_dir_contents(
    src: pathlib.Path,
    dst: pathlib.Path,
    *,
    clone_root: pathlib.Path | None = None,
    options: CopyOptions | None = None,
) -> None:
    """Copy what *src* holds, even when its own name is an ignored one (``Dist``)."""
    _copy_dir(
        src,
        dst,
        clone_root=clone_root,
        options=options or CopyOptions(),
        visited=frozenset(),
        depth=0,
    )


def copy_frontend_files(frontend_type: str, project_template: pathlib.Path) -> None:
    """Copy files from the specified frontend folder directly to project root."""
    # Skip copying if frontend_type is "None" or empty
    if not frontend_type or frontend_type == "None":
        logging.debug("Frontend type is 'None' or empty, skipping frontend files")
        return

    # Get the frontends directory path
    frontends_path = pathlib.Path(__file__).parent.parent / "frontends" / frontend_type

    if frontends_path.exists():
        logging.debug("Copying frontend files from %s", frontends_path)
        # Copy frontend files directly to project root instead of a nested frontend directory
        copy_files(frontends_path, project_template, options=CopyOptions(overwrite=True))
    else:
        logging.warning("Frontend type directory not found: %s", frontends_path)
        # Don't fall back to default if it's "None" - just skip
        if DEFAULT_FRONTEND != "None":
            logging.info("Falling back to default frontend: %s", DEFAULT_FRONTEND)
            copy_frontend_files(DEFAULT_FRONTEND, project_template)
        else:
            logging.debug("No default frontend configured, skipping frontend files")


def copy_flat_structure_agent_files(
    src: pathlib.Path,
    dst: pathlib.Path,
    agent_directory: str,
    *,
    agent_extensions: frozenset[str],
    clone_root: pathlib.Path | None = None,
    options: CopyOptions | None = None,
) -> None:
    """Copy agent files from a flat structure template to the agent directory.

    Root files with an ``agent_extensions`` suffix (the template's source files)
    go to the agent directory; everything else goes to the project root.

    Security: the resolved destination path is verified to be contained within
    *dst* before any write occurs. Symlinks are handled like the standard path
    (see ``copy_files``): with *clone_root* a link whose target stays inside the
    repository is materialized as real files, otherwise it is refused.

    Args:
        src: Source path (template root with flat structure)
        dst: Destination path (project root)
        agent_directory: Target agent directory name
        agent_extensions: Suffixes of the files that go to the agent directory.
        clone_root: Repository root that materialized symlink targets must stay
            within. When None, symlinks are refused rather than followed.
        options: Copy policy; ``skip_root_files`` adds to ``_FLAT_SKIP_FILES``.
    """
    if options is None:
        options = CopyOptions()
    agent_dst = dst / agent_directory
    # Path-containment guard: reject traversal that slipped past validation
    _assert_path_within(agent_dst, dst)
    agent_dst.mkdir(parents=True, exist_ok=True)

    skipped = _FLAT_SKIP_FILES | options.skip_root_files
    for item in src.iterdir():
        if item.name.startswith(".") or item.name in skipped:
            continue
        # A symlink is materialized from its vetted target; a plain entry is
        # copied from itself. Either way it is named and classified below by the
        # entry's own name, not the target's.
        source = item
        if item.is_symlink():
            source = require_safe_symlink(item, clone_root)

        _place_flat_entry(
            item,
            source,
            dst=dst,
            agent_dst=agent_dst,
            agent_extensions=agent_extensions,
            clone_root=clone_root,
            options=options,
        )


def _should_skip(path: pathlib.Path, options: CopyOptions) -> bool:
    """Whether a template entry should be skipped during copying.

    Drops compiled artifacts and build caches, VCS directories, the template's
    own ``.template`` config (unless ``optios.skip_secret_files`` is set (only
    while materializing a symlinked directory), also drops credential files so a
    ``.env`` reached through a directory link is never copied into the project.ns.keep_template_dir``), and
    (optionally) its manifest. When ``options.skip_secret_files`` is set also drops credential files so a
    ``.env`` reached through a directory link is never copied into the project.
    """
    if is_ignored_name(path.name):
        return True
    if options.skip_secret_files and is_secret_file(path.name):
        return True
    if path.suffix in [".pyc"]:
        return True
    if path.is_dir() and path.name == ".template" and not options.keep_template_dir:
        return True
    if options.skip_manifest and path.name == "agents-cli-manifest.yaml":
        return True
    return False


def _warn_if_long_windows_path(path: pathlib.Path) -> None:
    """Log a warning if *path* exceeds the Windows MAX_PATH limit."""
    if sys.platform == "win32":
        path_str = str(path.absolute())
        if len(path_str) >= 260:
            logging.error(
                "Path length (%d chars) may exceed Windows limit. "
                "Try using a shorter output directory.",
                len(path_str),
            )


def _copy_file(src: pathlib.Path, dst: pathlib.Path, *, overwrite: bool) -> None:
    """Copy a single regular file, creating parent dirs; skip if it exists."""
    if not overwrite and dst.exists():
        logging.debug("Skipping existing file: %s", dst)
        return
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        logging.debug("Copying file: %s -> %s", src, dst)
        shutil.copy2(src, dst)
    except OSError:
        logging.error("Failed to copy: %s -> %s", src, dst)
        _warn_if_long_windows_path(dst)
        raise


def _dir_cycle_key(path: pathlib.Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` identity of a directory, for cycle detection."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _destination_for(
    item: pathlib.Path, dst: pathlib.Path, options: CopyOptions
) -> pathlib.Path:
    """Destination for *item*, renaming a root AGENTS.md to the guidance file.

    Renamed whether AGENTS.md is a real file or a symlink to a file, so a
    template that shares its guide via an intra-repo symlink overwrites the base
    guidance file instead of leaving it in place and dropping a stray AGENTS.md.
    """
    if options.guidance_filename and item.name == "AGENTS.md" and item.is_file():
        return dst / options.guidance_filename
    return dst / item.name


def _materialize_symlink(
    link: pathlib.Path,
    dest: pathlib.Path,
    *,
    clone_root: pathlib.Path | None,
    options: CopyOptions,
    visited: frozenset[tuple[int, int]],
    depth: int,
) -> None:
    """Copy a symlink's resolved contents into the project, or reject it.

    The link is vetted by ``require_safe_symlink`` (which refuses it outright
    when there is no *clone_root*); a directory target is then walked via
    ``copy_files`` so nested symlinks are vetted too and the cycle guard sees the
    target's inode, while a file target is copied as real bytes.
    """
    target = require_safe_symlink(link, clone_root)
    if target.is_dir():
        # A direct link to a .env file is already refused by require_safe_symlink;
        # skip_secret_files closes the gap for a secret reached by recursively
        # materializing a linked directory (link -> some_dir/ with some_dir/.env).
        copy_files(
            target,
            dest,
            clone_root=clone_root,
            options=replace(options, skip_secret_files=True),
            _visited=visited,
            _depth=depth + 1,
        )
    else:
        logging.debug("Materializing symlink: %s -> %s", link, dest)
        _copy_file(target, dest, overwrite=options.overwrite)


def _copy_dir(
    src: pathlib.Path,
    dst: pathlib.Path,
    *,
    clone_root: pathlib.Path | None,
    options: CopyOptions,
    visited: frozenset[tuple[int, int]],
    depth: int,
) -> None:
    """Copy the contents of directory *src* into *dst* (see ``copy_files``)."""
    # Cycle guard: re-entering a directory already on the recursion path means a
    # symlink pointed back up the tree.
    key = _dir_cycle_key(src)
    if key is not None and key in visited:
        raise ScaffoldSymlinkSecurityError(
            f"Symlink cycle detected while copying template at '{src}'."
        )
    child_visited = visited | {key} if key is not None else visited

    if not dst.exists():
        try:
            dst.mkdir(parents=True)
            logging.debug("Created directory: '%s'", dst)
        except OSError as e:
            logging.error("Failed to create directory %s: %s", dst, e)
            raise

    for item in src.iterdir():
        is_skipped_root_file = depth == 0 and item.name in options.skip_root_files
        if is_skipped_root_file or _should_skip(item, options):
            logging.debug("Skipping file/directory: %s", item)
            continue
        dest = _destination_for(item, dst, options)
        if item.is_symlink():
            _materialize_symlink(
                item,
                dest,
                clone_root=clone_root,
                options=options,
                visited=child_visited,
                depth=depth,
            )
        elif item.is_dir():
            copy_files(
                item,
                dest,
                clone_root=clone_root,
                options=options,
                _visited=child_visited,
                _depth=depth + 1,
            )
        else:
            _copy_file(item, dest, overwrite=options.overwrite)


def _assert_path_within(
    candidate: pathlib.Path,
    root: pathlib.Path,
) -> None:
    """Raise ValueError if *candidate* is not contained within *root*.

    Both paths are resolved to their real absolute forms before the check so
    that symbolic links and ``..`` components cannot be used to bypass the
    boundary.

    Args:
        candidate: The path that must be inside *root*.
        root: The allowed root directory.

    Raises:
        ValueError: If *candidate* resolves to a location outside *root*.
    """
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as e:
        raise ValueError(
            f"Security check failed: '{candidate}' would be written "
            f"outside the project directory '{root}'. "
            "Aborting to prevent path-traversal exploitation."
        ) from e


def _place_flat_entry(
    item: pathlib.Path,
    source: pathlib.Path,
    *,
    dst: pathlib.Path,
    agent_dst: pathlib.Path,
    agent_extensions: frozenset[str],
    clone_root: pathlib.Path | None,
    options: CopyOptions,
) -> None:
    """Copy one flat-structure entry, reading bytes from *source*."""
    if source.is_dir():
        dest_dir = dst / item.name
        _assert_path_within(dest_dir, dst)
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        # When the entry is a symlink to a directory, materializing it must not
        # pull a .env inside it into the project (see _materialize_symlink).
        copy_files(
            source,
            dest_dir,
            clone_root=clone_root,
            options=CopyOptions(overwrite=True, skip_secret_files=item.is_symlink()),
        )
        return

    if item.suffix in agent_extensions:
        dest_file = agent_dst / item.name
    else:
        dest_file = _destination_for(item, dst, options)
    _assert_path_within(dest_file, dst)
    _copy_file(source, dest_file, overwrite=True)
