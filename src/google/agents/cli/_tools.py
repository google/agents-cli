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

"""Tool resolution utilities."""

import os
import re
import shlex
import shutil
from functools import cache
from pathlib import Path

import click

_tool_paths: dict[str, str] = {}

# Matches ANSI escape sequences (CSI/SGR), used to scrub any residual color
# codes from captured subprocess output. We disable color at the source via
# NO_COLOR/FORCE_COLOR, but strip defensively in case the tool emits them anyway
# (e.g. on Windows PowerShell, where embedded escapes have caused error lines
# to render as the previous line's color — see b/525049570).
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

_GCLOUD_RELATIVE_PATH = (
    Path("Google") / "Cloud SDK" / "google-cloud-sdk" / "bin" / "gcloud.cmd"
)

# Default installation hints for common tools.
# These are used as fallbacks in require_tool when no specific hint is provided,
# ensuring helpful error messages even when tools are resolved implicitly (e.g. via run_resolved).
DEFAULT_INSTALL_HINTS = {
    "npx": "Install Node.js (https://nodejs.org/en/download) and try again.",
    "npm": "Install Node.js (https://nodejs.org/en/download) and try again.",
    "gcloud": "Install the Google Cloud SDK (https://cloud.google.com/sdk/docs/install) and ensure it is in your PATH.",
    "terraform": "Install Terraform (https://developer.hashicorp.com/terraform/install) and ensure it is in your PATH.",
    "kubectl": "Install kubectl (https://kubernetes.io/docs/tasks/tools/) and ensure it is in your PATH.",
    "gh": "Install the GitHub CLI (https://cli.github.com/) and ensure it is in your PATH.",
    "git": "Install Git (https://git-scm.com/downloads) and ensure it is in your PATH.",
    "uv": "Install uv (https://docs.astral.sh/uv/getting-started/installation/) and ensure it is in your PATH.",
    # uvx is shipped by uv, so the install instructions are the same.
    "uvx": "uvx is part of uv. Install uv (https://docs.astral.sh/uv/getting-started/installation/) and ensure it is in your PATH.",
    "go": "Install Go (https://go.dev/doc/install) and ensure it is in your PATH.",
}


class ToolNotFoundError(click.ClickException):
    """Raised when a required external tool is not found on PATH."""

    pass


class ToolNotExecutableError(click.ClickException):
    """Raised when a required external tool is found but cannot be executed."""

    pass


@cache
def _get_cleaned_path() -> str:
    """Returns a cleaned and expanded version of the PATH environment variable.

    This function performs the following steps:
    1. Splits the PATH environment variable using the OS-specific path separator.
    2. Strips quotes from each path segment.
    3. Filters out empty path segments.
    4. Expands environment variables (like $HOME or %USERPROFILE%) within each segment.
    5. Reconstructs the PATH string with the cleaned segments.
    """
    raw_path = os.environ.get("PATH", "")
    parts = raw_path.split(os.pathsep)
    cleaned_parts = []

    for part in parts:
        # Strip quotes from each path segment.
        part = part.strip('"').strip("'")
        if not part:
            continue
        cleaned_parts.append(os.path.expandvars(part))
    return os.pathsep.join(cleaned_parts)


def is_windows() -> bool:
    """Returns True if the current operating system is Windows."""
    return os.name == "nt"


def _get_gcloud_fallback() -> str | None:
    """Check common installation paths for gcloud on Windows.

    This serves as a fallback when gcloud is not found on the PATH, which
    typically happens if the user opted not to add gcloud to the PATH during
    installation.
    """
    if not is_windows():
        return None

    local_app_data = Path(
        os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
    )
    program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
    program_files_x86 = Path(
        os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")
    )

    possible_paths = [
        local_app_data / _GCLOUD_RELATIVE_PATH,
        program_files_x86 / _GCLOUD_RELATIVE_PATH,
        program_files / _GCLOUD_RELATIVE_PATH,
    ]
    for p in possible_paths:
        if p.exists():
            return str(p)
    return None


def require_tool(name: str, install_hint: str = "") -> str:
    """Finds a required external tool on the system PATH and returns its path.

    This function performs the following steps:
    1. Checks if the tool's path is already cached in `_tool_paths`.
    2. If not cached, searches for the tool using `shutil.which` with the default PATH.
    3. If not found and running on Windows, searches again using `shutil.which` with a cleaned PATH.
    4. If not found and the tool is 'gcloud', attempts to find in common Windows fallback paths.
    5. If still not found, raises a `ToolNotFoundError` with an optional install hint.
    6. If found, caches the path and returns it.
    """
    if name in _tool_paths:
        return _tool_paths[name]

    path = shutil.which(name)

    if path is None and is_windows():
        path = shutil.which(name, path=_get_cleaned_path())

    if path is None:
        if name == "gcloud":
            path = _get_gcloud_fallback()

        if path is None:
            msg = f"'{name}' is not installed or not on PATH."
            hint = install_hint or DEFAULT_INSTALL_HINTS.get(name, "")
            if hint:
                msg += f"\n  {hint}"
            raise ToolNotFoundError(msg)

    _tool_paths[name] = path
    return path


# Where `agents-cli setup` fetches the bundled skills from.
DEFAULT_SKILLS_SOURCE = "https://github.com/google/agents-cli"


def run_npx_skills(args: list[str], spinner_msg: str):
    """Run an npx skills command, streaming output in real-time.

    Always starts with ``["npx", "-y", SKILLS_NPX_PACKAGE]`` and appends
    the additional ``args`` provided.

    Raises:
        click.ClickException: If the npx process exits non-zero.
    """
    from google.agents.cli._runner import run_resolved
    from google.agents.cli._skills_check import SKILLS_NPX_PACKAGE

    full_args = ["npx", "-y", SKILLS_NPX_PACKAGE, *args]
    click.secho(f"  ▸ {shlex.join(full_args)}", fg="cyan", dim=True)

    try:
        run_resolved(full_args, check=True, encoding="utf-8", errors="replace")
    except Exception as e:
        raise click.ClickException("Error running npx skills") from e
