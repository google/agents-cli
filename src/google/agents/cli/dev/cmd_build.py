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

"""agents-cli build command — compile the agent."""

import click

from google.agents.cli._project import chdir_project_root, read_project_config
from google.agents.cli._runner import run
from google.agents.cli.scaffold.utils.language import dispatch_language

DEFAULT_OUTPUT = "bin/agent"


@click.command("build")
@click.option(
    "--output",
    "-o",
    default=DEFAULT_OUTPUT,
    show_default=True,
    help="Path for the compiled binary.",
)
def cmd_build(output: str):
    """Build the agent binary."""
    chdir_project_root()
    handler = dispatch_language(
        "build", LANGUAGE_HANDLERS, read_project_config().language
    )
    handler(output=output)


def _build_python(*, output: str) -> None:
    """Python has no compile step; point the user at install."""
    click.echo(
        "Nothing to build: Python projects have no compile step.\n"
        "  Run `agents-cli install` to sync dependencies."
    )


def _build_go(*, output: str) -> None:
    """Compile the Go agent to the given output path."""
    run(["go", "build", "-o", output, "."], check_err_msg="Failed to build agent")
    click.echo(f"Built binary: {output}")


# `None` = not supported yet; dispatch_language raises a clear error.
LANGUAGE_HANDLERS = {
    "python": _build_python,
    "go": _build_go,
    "java": None,
    "typescript": None,
    "kotlin": None,
}
