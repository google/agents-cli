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

"""File system helpers for agents CLI."""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath


def delete_folder(folder: Path) -> None:
    """Delete `folder`, even when it holds read-only folders; warn when it cannot."""
    make_folders_writable(folder)
    shutil.rmtree(folder, ignore_errors=True)
    if folder.exists() or folder.is_symlink():
        logging.warning("Could not delete %s; delete it by hand.", folder)


def delete_path(path: Path) -> None:
    """Delete `path`; unlink a link instead of following it into a folder we do not own.

    Raises OSError when it cannot delete, unlike `delete_folder`, which only warns.
    """
    try:
        mode = path.lstat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return
    if stat.S_ISDIR(mode):
        make_folders_writable(path)
        shutil.rmtree(os.fspath(path))
    else:
        path.unlink()


def make_folders_writable(root: Path) -> None:
    """Let ``rmtree`` delete what read-only folders under ``root`` hold.

    Best effort: a folder it cannot change is left as it is.
    """
    if root.is_symlink():
        return
    _make_writable(root)
    for folder, subfolders, _ in os.walk(root):
        for name in subfolders:
            subfolder = os.path.join(folder, name)
            # chmod follows links, so a linked folder is skipped to keep the change
            # inside `root` (CWE-59). Each one is changed before os.walk lists it.
            if not os.path.islink(subfolder):
                _make_writable(subfolder)


def _make_writable(folder: str | Path) -> None:
    with contextlib.suppress(OSError):
        os.chmod(folder, stat.S_IRWXU)


def is_real_dir(path: Path) -> bool:
    """Whether `path` is a folder and not a link: a committed link can lead anywhere.

    Uses `os.path`: before Python 3.14, `Path.is_dir` and `Path.is_symlink` raise
    PermissionError under a folder that cannot be searched.
    """
    return not os.path.islink(path) and os.path.isdir(path)


def first_copy_error(error: OSError) -> str:
    """The reason of the first failed file, instead of copytree's list of all of them."""
    # copytree collects every failed file into one shutil.Error holding a list of
    # (source, target, reason) tuples.
    if isinstance(error, shutil.Error) and error.args:
        failures = error.args[0]
        if (
            isinstance(failures, list)
            and failures
            and isinstance(failures[0], tuple)
            and len(failures[0]) == 3
        ):
            return str(failures[0][2])
    return str(error)


def stays_inside(path: str) -> bool:
    """Whether ``path``, joined onto a folder, names something below that folder."""
    posix = PurePosixPath(path)
    return (
        bool(posix.parts)
        and "\\" not in path
        and not posix.is_absolute()
        and not PureWindowsPath(path).drive
        and ".." not in posix.parts
    )
