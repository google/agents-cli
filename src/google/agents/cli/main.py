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

"""Root Click group for the 'agents-cli' CLI.

Every command is registered lazily via `add_lazy_command`. The command
modules are imported only when the user invokes the command (or asks for
its specific --help). See `LazyGroup` in `_click.py`.
"""

import io
import os
import sys
import traceback

import click

from google.agents.cli import _tools
from google.agents.cli.__init__ import __version__
from google.agents.cli._click import LazyGroup, patch_source_in_help
from google.agents.cli._project import is_project_moved

# Force utf-8 encoding and non-exception fallback for printing
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _print_is_project_moved_tip() -> None:
    message = (
        "\n💡 Tip: It looks like the project folder may have been moved or renamed."
        " Try running `agents-cli install --clean` to reset the environment, then"
        " re-run your original command"
    )
    if is_project_moved():
        from rich.console import Console

        Console().print(message, style="cyan")


class _MainGroup(LazyGroup):
    """Click group with lazy command loading and full-traceback exception handling."""

    def invoke(self, ctx: click.Context) -> None:
        try:
            super().invoke(ctx)
        except click.exceptions.Exit:
            raise
        except click.ClickException:
            click.echo(f"agents-cli v{__version__}", err=True)
            _print_is_project_moved_tip()
            raise
        except KeyboardInterrupt:
            from rich.console import Console

            Console().print(f"\nagents-cli v{__version__}", style="dim")
            Console().print("Operation cancelled by user", style="yellow")
            ctx.exit(130)
        except Exception:
            click.echo(f"agents-cli v{__version__}", err=True)
            _print_is_project_moved_tip()
            traceback.print_exc()
            ctx.exit(1)


@click.group(cls=_MainGroup)
@click.version_option(version=__version__, prog_name="agents-cli")
def main():
    """Agents CLI — Agent Development Lifecycle toolchain.

    Build, evaluate, and deploy ADK agents with a single unified CLI.

    \b
    Quick start:
      agents-cli setup                 Install skills to your coding agent
      agents-cli create my-agent       Create a new agent project
      agents-cli playground            Start the local playground
      agents-cli eval generate         Run agent inference over eval cases
      agents-cli eval grade            Grade generated traces
      agents-cli scaffold enhance .    Add deployment/CI-CD to a project
      agents-cli deploy                Deploy the agent
    """
    # Disable gcloud interactive prompts for all CLI subprocesses
    # unless the user explicitly passes --interactive / -i.
    if "--interactive" not in sys.argv and "-i" not in sys.argv:
        os.environ["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"

    from google.agents.cli._skills_check import check_skills_version
    from google.agents.cli.scaffold.utils.version import display_update_message

    display_update_message()
    check_skills_version()
    _tools.require_tool("uv")


# Setup commands
main.add_lazy_command(
    "setup",
    "google.agents.cli.setup.cmd_setup:cmd_setup",
    "Install agents-cli and skills to detected coding agents.",
)
main.add_lazy_command(
    "update",
    "google.agents.cli.setup.cmd_update:cmd_update",
    "Force reinstall agents skills to all detected coding agents.",
)

# Auth commands
main.add_lazy_command(
    "login",
    "google.agents.cli.setup.cmd_auth:cmd_login",
    "Authenticate with Google Cloud or AI Studio.",
)

# Scaffold command group + top-level `create` alias
main.add_lazy_command(
    "scaffold",
    "google.agents.cli.scaffold.cmd_scaffold_group:scaffold_group",
    "Scaffold, enhance, and upgrade agent projects.",
)
main.add_lazy_command(
    "create",
    "google.agents.cli.scaffold.commands.create:create",
    "Create GCP-based AI agent projects from templates.",
)

# Dev commands
main.add_lazy_command(
    "playground",
    "google.agents.cli.dev.cmd_playground:cmd_playground",
    "Start the local agent playground.",
)
main.add_lazy_command(
    "run",
    "google.agents.cli.run.cmd_run:cmd_run",
    "Run the agent with a single prompt (non-interactive).",
)
main.add_lazy_command(
    "lint",
    "google.agents.cli.dev.cmd_lint:cmd_lint",
    "Run code quality checks.",
)
main.add_lazy_command(
    "install",
    "google.agents.cli.dev.cmd_install:cmd_install",
    "Install project dependencies.",
)

# Data commands
main.add_lazy_command(
    "data-ingestion",
    "google.agents.cli.data.cmd_data_ingestion:cmd_data_ingestion",
    "Removed: RAG is now a clone-and-study recipe.",
)

# Eval commands
main.add_lazy_command(
    "eval",
    "google.agents.cli.eval.cmd_eval_group:eval_group",
    "Evaluate agents and compare results.",
)

# Deploy + publish commands
main.add_lazy_command(
    "deploy",
    "google.agents.cli.deploy.cmd_deploy:cmd_deploy",
    "Deploy the agent.",
)
main.add_lazy_command(
    "publish",
    "google.agents.cli.publish.cmd_publish_group:publish_group",
    "Publish agents to various targets.",
)

# Infra commands
main.add_lazy_command(
    "infra",
    "google.agents.cli.infra.cmd_infra:infra_group",
    "Provision infrastructure for your agent project.",
)

# Info command
main.add_lazy_command(
    "info",
    "google.agents.cli.info.cmd_info:cmd_info",
    "Show project configuration, paths, and CLI version.",
)

# Patch the root group itself to show source file in --help.
# Lazy commands get patched on first access by LazyGroup.get_command.
patch_source_in_help(main)


if __name__ == "__main__":
    main()
