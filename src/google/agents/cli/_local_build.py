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

"""Compile Gradle (Java, Kotlin) and npm (TypeScript) projects before running them."""

from pathlib import Path

import click

from google.agents.cli._runner import run
from google.agents.cli._tools import is_windows


def gradle_install_dist(project_root: Path) -> Path:
    """Compile with ``gradle installDist`` and return the start script it writes.

    Running that script instead of ``gradle run`` keeps Gradle out of the
    server's startup time and process tree.
    """
    run(
        ["gradle", "--quiet", "installDist"],
        cwd=project_root,
        check_err_msg="Failed to build agent",
    )
    return gradle_start_script(project_root)


def gradle_start_script(project_root: Path) -> Path:
    """Return the start script ``gradle installDist`` wrote under build/install/."""
    windows = is_windows()
    scripts = sorted(
        path
        for path in (project_root / "build" / "install").glob("*/bin/*")
        if path.is_file() and (path.suffix == ".bat") == windows
    )
    if not scripts:
        raise click.ClickException(
            "No start script found under build/install/.\n"
            "  Run `agents-cli build`, and check that build.gradle.kts applies the "
            "`application` plugin."
        )
    # installDist never removes the folder of a renamed project, so several
    # scripts means one may be stale.
    if len(scripts) > 1:
        names = ", ".join(path.relative_to(project_root).as_posix() for path in scripts)
        raise click.ClickException(
            f"Found more than one start script: {names}.\n"
            "  Run `gradle clean`, then `agents-cli build`."
        )
    return scripts[0]


def npm_build(project_root: Path) -> None:
    """Compile with ``npm run build`` after syncing dependencies with ``npm install``."""
    run(
        ["npm", "install"],
        cwd=project_root,
        check_err_msg="Failed to install dependencies",
    )
    run(
        ["npm", "run", "build"],
        cwd=project_root,
        check_err_msg="Failed to build agent",
    )
