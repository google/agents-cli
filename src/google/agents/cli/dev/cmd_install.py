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

"""agents-cli install command — install project dependencies."""

import logging
import shutil
from pathlib import Path

import click

from google.agents.cli import _tools
from google.agents.cli._fs import delete_folder
from google.agents.cli._project import (
    chdir_project_root,
    find_project_root,
    read_project_config,
)
from google.agents.cli._runner import run
from google.agents.cli._uv_index import (
    sync_failure_message,
    uv_index_env,
    warn_if_lock_uses_non_public_index,
)
from google.agents.cli.scaffold.utils.language import dispatch_language


@click.command("install")
@click.option(
    "--clean",
    is_flag=True,
    help="Delete installed dependencies or build output first (.venv, node_modules, Gradle build/), for example after moving or renaming the project folder. Ignored for Go projects.",
)
@click.option(
    "--locked",
    is_flag=True,
    help="Fail instead of updating the lock file (uv.lock, go.sum, package-lock.json). Ignored for Gradle projects, which have no lock file.",
)
def cmd_install(clean: bool, locked: bool):
    """Install project dependencies."""
    chdir_project_root()
    # Re-materialize any missing extension working copies from their pinned
    # SHAs, before the dispatch: the loader tells a user with a missing copy to
    # run this command, and a language with no install handler raises below.
    # Never advances pins.
    from google.agents.cli.extension._sync import sync_extensions

    sync_extensions(find_project_root(Path.cwd()))

    handler = dispatch_language(
        "install", LANGUAGE_HANDLERS, read_project_config().language
    )
    handler(clean=clean, locked=locked)


def _install_python(*, clean: bool, locked: bool) -> None:
    """Install Python dependencies with ``uv sync``.

    When ``locked`` is true, runs ``uv sync --locked`` so a stale uv.lock fails the command instead
    of being updated.
    When ``clean`` is true, deletes the project's ``.venv``.
    Resolves against the same public index the scaffold locks against (see
    :func:`~google.agents.cli._uv_index.uv_index_env`), and warns if the
    resulting uv.lock still points at an index remote builds may not reach.
    """
    # Resolve uv up front: --clean deletes the venv. Fail quickly here (before
    # any destructive work) rather than when calling uv.
    _tools.require_tool("uv")
    if clean:
        _delete_venv()
    cmd = ["uv", "sync"]
    if locked:
        cmd.append("--locked")
    root = Path.cwd()
    env = uv_index_env(root)
    run(
        cmd,
        env=env,
        check_err_msg=sync_failure_message(
            "Failed to install dependencies", env, locked=locked
        ),
    )
    warn_if_lock_uses_non_public_index(root)


def _install_go(*, clean: bool, locked: bool) -> None:
    """Install Go dependencies with ``go mod tidy``.

    ``locked`` verifies go.mod/go.sum are current with ``go mod tidy -diff``
    (then downloads deps) instead of updating them.
    ``clean`` has no Go equivalent (there is no per-project environment to delete),
    so it is ignored with a warning rather than silently accepted.
    """
    if clean:
        logging.warning("--clean does not apply for Go projects, ignoring.")
    if locked:
        # -diff exits with non-zero code if go.mod/go.sum are stale, without modifying them.
        # Later `go mod download` will pull in the missing dependencies.
        run(
            ["go", "mod", "tidy", "-diff"],
            check_err_msg=(
                "go.mod/go.sum are out of date; rerun without --locked to update them"
            ),
        )
        run(["go", "mod", "download"], check_err_msg="Failed to download dependencies")
        return
    run(["go", "mod", "tidy"], check_err_msg="Failed to install dependencies")


def _install_gradle(*, clean: bool, locked: bool) -> None:
    """Resolve Java/Kotlin dependencies by compiling the main and test sources.

    Gradle projects have no lock file by default, so ``locked`` is ignored with a
    warning. ``clean`` runs ``gradle clean`` first.
    """
    if locked:
        logging.warning(
            "--locked does not apply: Gradle projects have no lock file, ignoring."
        )
    tasks = ["clean", "testClasses"] if clean else ["testClasses"]
    # No --quiet: Gradle's progress shows a long first download isn't stuck.
    run(
        ["gradle", *tasks],
        check_err_msg="Failed to resolve dependencies or compile the project",
    )


def _install_typescript(*, clean: bool, locked: bool) -> None:
    """Install npm dependencies.

    ``npm install`` updates package-lock.json after package.json changes.
    ``locked`` runs ``npm ci`` instead, which fails on a stale lock rather than
    updating it. ``clean`` deletes node_modules first.
    """
    if clean:
        # Resolve npm first, so a missing npm doesn't cost the user node_modules.
        _tools.require_tool("npm")
        delete_folder(Path("node_modules"))
    if locked:
        run(
            ["npm", "ci"],
            check_err_msg=(
                "Failed to install dependencies; if package-lock.json is out of "
                "date, rerun without --locked to update it"
            ),
        )
        return
    run(["npm", "install"], check_err_msg="Failed to install dependencies")


# `None` = not supported yet; dispatch_language raises a clear error.
LANGUAGE_HANDLERS = {
    "python": _install_python,
    "go": _install_go,
    "java": _install_gradle,
    "typescript": _install_typescript,
    "kotlin": _install_gradle,
}


def _delete_venv():
    root = find_project_root()
    if not root:
        logging.warning(
            "Could not find project root: no pyproject.toml found in the current directory or any parent."
        )
        return

    venv_path = root / ".venv"

    if not venv_path.exists():
        return

    try:
        shutil.rmtree(venv_path)
    except Exception as e:
        logging.warning(f"Failed to remove venv: {e}")
