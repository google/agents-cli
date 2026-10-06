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

"""Synthesize Click commands that dispatch to an extension's run vector."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import click

from google.agents.cli._output import print_error
from google.agents.cli._project import find_project_root
from google.agents.cli._runner import DISABLE_OVERRIDES_ENV, run_extension_command
from google.agents.cli.extension._spec import ExtensionCommand

if TYPE_CHECKING:
    from google.agents.cli.extension._loader import ResolvedCommand


def installed_override(dotted: str) -> ResolvedCommand | None:
    """The extension override installed for `dotted` (e.g. `eval.generate`), if any.

    Click dispatch is where overrides normally take effect, so a built-in that
    calls another command's callback in-process (`eval run` → `eval generate`)
    would silently run the built-in. Such a command looks the override up here
    and dispatches to it instead.
    """
    if os.environ.get(DISABLE_OVERRIDES_ENV) == "1":
        return None
    from google.agents.cli.extension._loader import load_extension_set
    from google.agents.cli.extension._paths import user_config_root

    extension_set = load_extension_set(find_project_root(Path.cwd()), user_config_root())
    resolved = extension_set.commands.get(dotted)
    if resolved is None or resolved.contribution.kind != "override":
        return None
    if resolved.blocked_requires:
        raise click.ClickException(
            f"{dotted!r} comes from extension {resolved.extension_name!r}, which "
            f"requires agents-cli {resolved.blocked_requires}.\n"
            f"  Update it:      agents-cli extension update {resolved.extension_name}\n"
            f"  Or remove it:   agents-cli extension remove {resolved.extension_name}"
        )
    return resolved


def run_override(
    resolved: ResolvedCommand, argv: list[str], *, display_path: str
) -> None:
    """Run a resolved override's vector, exiting with its code on failure."""
    if resolved.contribution.kind == "override":
        click.echo(
            f"running {display_path!r} via extension {resolved.extension_name!r} ({resolved.scope} scope)",
            err=True,
        )
    cwd = find_project_root() or Path.cwd()
    try:
        code = run_extension_command(
            resolved.contribution.run,
            argv,
            cwd=cwd,
            extension_root=resolved.extension_root,
        )
    except FileNotFoundError as e:
        raise click.ClickException(
            f"Extension '{resolved.extension_name}' command {display_path!r} could not "
            f"run: executable {resolved.contribution.run[0]!r} not found. "
            "Check the extension's `run:` entry."
        ) from e
    if code != 0:
        # Exit with the child's code, matching what a direct `agents-cli <cmd>`
        # would return; a ClickException would flatten every failure to 1.
        print_error(
            f"{display_path} (extension '{resolved.extension_name}') failed with "
            f"exit code {code}."
        )
        raise click.exceptions.Exit(code)


def bypass_hint(display_path: str) -> str:
    """How to run the built-in instead, in the shell the user is actually in.

    `VAR=1 cmd` is not valid on cmd.exe or PowerShell, and this text is what
    someone reads when an override has already broken something for them.
    """
    if os.name == "nt":
        return f"set {DISABLE_OVERRIDES_ENV}=1 && agents-cli {display_path}"
    return f"{DISABLE_OVERRIDES_ENV}=1 agents-cli {display_path}"


def make_blocked_command(
    *,
    extension_name: str,
    leaf_name: str,
    display_path: str,
    requires: str,
    running: str,
) -> click.Command:
    """Build a Click command that refuses to run and says why.

    Used when an extension declared `on_incompatible: error` and the running CLI is
    outside its range. Falling back to the built-in is not safe for an override
    — it would quietly do something else — so the command fails and points at
    the ways out. Only this command is affected; the rest of the CLI works.
    """
    short = f"unavailable: extension '{extension_name}' needs agents-cli {requires}"

    @click.command(
        name=leaf_name,
        short_help=short,
        help=(
            f"`{display_path}` is provided by extension '{extension_name}', which "
            f"requires agents-cli {requires}; you are running {running}. The "
            "extension asked to fail rather than fall back (`on_incompatible: "
            "error`), because the built-in would do something different.\n\n"
            "Fix it by updating the extension, removing it, or running the "
            "built-in explicitly:\n"
            f"  agents-cli extension update {extension_name}\n"
            f"  agents-cli extension remove {extension_name}\n"
            f"  {bypass_hint(display_path)}"
        ),
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    )
    @click.argument("extra_args", nargs=-1, type=click.UNPROCESSED)
    def _blocked(extra_args: tuple[str, ...]) -> None:
        raise click.ClickException(
            f"{display_path!r} comes from extension {extension_name!r}, which requires "
            f"agents-cli {requires} (running {running}).\n"
            f"  Update it:      agents-cli extension update {extension_name}\n"
            f"  Or remove it:   agents-cli extension remove {extension_name}\n"
            f"  Or bypass it:   {bypass_hint(display_path)}"
        )

    return _blocked


def make_override_command(
    contribution: ExtensionCommand,
    *,
    extension_name: str,
    scope: str,
    leaf_name: str,
    display_path: str,
    extension_root: Path,
) -> click.Command:
    """Build a Click command that runs an extension contribution.

    `--help` is intercepted to describe the contribution; all other argv after
    the command name passes verbatim to the run vector.
    """
    run_display = " ".join(contribution.run)
    verb = "Overrides" if contribution.kind == "override" else "Adds"
    help_text = (
        f"{verb} `{display_path}` via extension '{extension_name}' ({scope} scope).\n\n"
        f"{contribution.description}\n\n"
        f"Runs: {run_display}\n\n"
        "Argv after the command name passes through to the script verbatim.\n\n"
        "To bypass extension overrides and run the built-in:\n"
        f"  {bypass_hint(display_path)}"
    )
    short = f"{contribution.description or run_display}  [\u2191 {extension_name}]"

    @click.command(
        name=leaf_name,
        short_help=short,
        help=help_text,
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    )
    @click.argument("extra_args", nargs=-1, type=click.UNPROCESSED)
    def _cmd(extra_args: tuple[str, ...]) -> None:
        if contribution.kind == "override":
            click.echo(
                f"running {display_path!r} via extension {extension_name!r} ({scope} scope)",
                err=True,
            )
        cwd = find_project_root() or Path.cwd()
        try:
            code = run_extension_command(
                contribution.run,
                list(extra_args),
                cwd=cwd,
                extension_root=extension_root,
            )
        except FileNotFoundError as e:
            raise click.ClickException(
                f"Extension '{extension_name}' command {display_path!r} could not run: "
                f"executable {contribution.run[0]!r} not found. "
                "Check the extension's `run:` entry."
            ) from e
        raise click.exceptions.Exit(code)

    return _cmd
