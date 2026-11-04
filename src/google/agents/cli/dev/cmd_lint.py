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

"""agents-cli lint command — run code linting."""

import logging
from pathlib import Path

import click

from google.agents.cli import _tools
from google.agents.cli._project import chdir_project_root, read_project_config
from google.agents.cli._runner import run
from google.agents.cli._uv_index import sync_failure_message, uv_index_env
from google.agents.cli.scaffold.utils.language import dispatch_language

_GOLANGCI_LINT = "github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.12.2"
_GOLANGCI_LINT_INSTALL_URL = "https://golangci-lint.run/docs/welcome/install/local/"


@click.command("lint")
@click.option("--fix", is_flag=True, default=False, help="Auto-fix linting issues.")
@click.option(
    "--mypy",
    is_flag=True,
    default=False,
    help="Also run mypy type checking. (Python only)",
)
@click.option(
    "--skip-codespell",
    is_flag=True,
    default=False,
    help="Skip codespell spell checking. (Python only)",
)
@click.option(
    "--skip-ty",
    is_flag=True,
    default=False,
    help="Skip ty type checking. (Python only)",
)
def cmd_lint(fix, mypy, skip_codespell, skip_ty):
    """Run code quality checks."""
    chdir_project_root()
    handler = dispatch_language("lint", LANGUAGE_HANDLERS, read_project_config().language)
    handler(fix=fix, mypy=mypy, skip_codespell=skip_codespell, skip_ty=skip_ty)


def _lint_python(*, fix: bool, mypy: bool, skip_codespell: bool, skip_ty: bool) -> None:
    """Run Python code quality checks.

    Runs, in order:
      - ruff check .           (adds --fix when ``fix`` is set)
      - ruff format . --check  (reformats in place when ``fix`` is set)
      - codespell              (unless ``skip_codespell``)
      - ty check .             (unless ``skip_ty``)
      - mypy .                 (only when ``mypy`` is set)
    """
    # Keep every uv call on the scaffold's index so `uv run` doesn't re-lock
    # against a machine-wide mirror right after the sync.
    env = uv_index_env(Path.cwd())
    # Sync lint extras before running
    run(
        ["uv", "sync", "--dev", "--extra", "lint"],
        env=env,
        check_err_msg=sync_failure_message("Failed to sync lint dependencies", env),
    )

    if fix:
        run(
            ["uv", "run", "ruff", "check", ".", "--fix"],
            env=env,
            check_err_msg="Ruff check --fix failed",
        )
        run(
            ["uv", "run", "ruff", "format", "."],
            env=env,
            check_err_msg="Ruff format failed",
        )
    else:
        run(
            ["uv", "run", "ruff", "check", "."],
            env=env,
            check_err_msg="Ruff check failed",
        )
        run(
            ["uv", "run", "ruff", "format", ".", "--check"],
            env=env,
            check_err_msg="Ruff format check failed",
        )

    if not skip_codespell:
        run(
            ["uv", "run", "codespell"],
            env=env,
            check_err_msg="Codespell check failed",
        )

    if not skip_ty:
        run(
            ["uv", "run", "ty", "check", "."],
            env=env,
            check_err_msg="ty type check failed",
        )

    if mypy:
        run(
            ["uv", "run", "mypy", "."],
            env=env,
            check_err_msg="Mypy check failed",
        )


def _lint_go(*, fix: bool, mypy: bool, skip_codespell: bool, skip_ty: bool) -> None:
    """Run Go code quality checks with ``golangci-lint``.

    ``fix`` maps to ``golangci-lint run --fix``. Flags
    that don't map to the Go toolchain are ignored with a warning.
    """
    ignored = [
        flag
        for flag, supplied in (
            ("--mypy", mypy),
            ("--skip-ty", skip_ty),
            ("--skip-codespell", skip_codespell),
        )
        if supplied
    ]
    if ignored:
        logging.warning(
            "Flags %s are not supported for Go, ignoring.",
            ", ".join(ignored),
        )
    cmd = [*_golangci_lint_invocation(), "run"]
    if fix:
        cmd.append("--fix")
    run(cmd, check_err_msg="golangci-lint found issues")


def _golangci_lint_invocation() -> list[str]:
    """Return the command prefix for invoking golangci-lint.

    Prefers a ``golangci-lint`` binary already on PATH,
    falls back to ``go run`` at the pinned version when no binary is found.
    """
    try:
        return [_tools.require_tool("golangci-lint")]
    except _tools.ToolNotFoundError:
        # Binary installation of golangci-lint is recommended by the maintainers.
        # See https://golangci-lint.run/docs/welcome/install/local/.
        click.echo(
            "golangci-lint not found on PATH; falling back to `go run`.\n"
            "  For faster runs, install the binary: "
            f"{_GOLANGCI_LINT_INSTALL_URL}"
        )
        return ["go", "run", _GOLANGCI_LINT]


# `None` = not supported yet; dispatch_language raises a clear error.
LANGUAGE_HANDLERS = {
    "python": _lint_python,
    "go": _lint_go,
    "java": None,
    "typescript": None,
    "kotlin": None,
}
