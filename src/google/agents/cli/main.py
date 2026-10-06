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

from __future__ import annotations

import io
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

import click

from google.agents.cli import _telemetry
from google.agents.cli.__init__ import __version__
from google.agents.cli._click import LazyGroup, patch_source_in_help
from google.agents.cli._project import find_project_root, is_project_moved
from google.agents.cli._runner import DISABLE_OVERRIDES_ENV

# Type-only: nothing under `extension.` may be imported at module scope. A run
# that bypasses overrides (AGENTS_CLI_DISABLE_OVERRIDES=1 — set for any command
# an extension re-enters) must not load extension discovery at all, and every other
# invocation pays for the import on cold start. `_apply_extensions` imports them
# only once it knows they are needed.
if TYPE_CHECKING:
    from google.agents.cli.extension._loader import ExtensionSet, ResolvedCommand

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
        from google.agents.cli._output import Console

        Console(stderr=True).print(message, style="cyan")


class _MainGroup(LazyGroup):
    """Click group with lazy command loading and full-traceback exception handling."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._extensions_applied = False

    def list_commands(self, ctx):
        self._apply_extensions()
        return super().list_commands(ctx)

    def get_command(self, ctx, cmd_name):
        self._apply_extensions()
        return super().get_command(ctx, cmd_name)

    def _apply_extensions(self) -> None:
        if self._extensions_applied:
            return
        self._extensions_applied = True
        # Extension modules are imported here (not at module top) so a bypassed run
        # (AGENTS_CLI_DISABLE_OVERRIDES=1, e.g. an override re-entering the CLI)
        # never loads the extension discovery code.
        if os.environ.get(DISABLE_OVERRIDES_ENV) == "1":
            return

        from google.agents.cli.extension._loader import load_extension_set
        from google.agents.cli.extension._paths import user_config_root

        project_root = find_project_root(Path.cwd())
        user_root = user_config_root()
        extension_set = load_extension_set(project_root, user_root)

        # Surface any extension whose declared `requires.agents_cli` range excludes
        # the running CLI. In "error" mode the loader keeps the claim and marks
        # it blocked, so only that extension's commands fail; recovery commands
        # stay on the built-in. We only warn here.
        for inc in extension_set.incompatible:
            if inc.error_on_incompatible:
                logging.warning(
                    "agents-cli: extension %r requires agents-cli %s but running %s; "
                    "its commands will fail until you run `agents-cli extension "
                    "update %s`, remove it, or pin a compatible CLI.",
                    inc.name,
                    inc.requires_agents_cli,
                    extension_set.running_version,
                    inc.name,
                )
            else:
                logging.warning(
                    "agents-cli: extension %r requires agents-cli %s but running %s; "
                    "applying anyway (may misbehave).",
                    inc.name,
                    inc.requires_agents_cli,
                    extension_set.running_version,
                )

        if not extension_set.commands:
            return

        for name, resolved in extension_set.commands.items():
            self._install(name, resolved)
            self._mirror_onto_aliases(name, resolved, extension_set)

    def _mirror_onto_aliases(
        self, dotted: str, resolved: ResolvedCommand, extension_set: ExtensionSet
    ) -> list[str]:
        """Apply an override of `group.sub` to top-level aliases of the same command.

        `create` and `scaffold create` are two registrations of one command
        (identical `module:obj` target), so overriding the canonical
        `scaffold.create` must also take over `create`. The pairing is derived
        from the registry rather than hardcoded, so it can't rot when a command
        moves — and no extra modules are imported to work it out: the parent
        group is already loaded by the subcommand install above.
        """
        if "." not in dotted:
            return []
        group_name, sub_name = dotted.split(".", 1)
        # Already imported by the subcommand install, so this is a cache hit.
        parent = self._load_builtin(group_name)
        if not isinstance(parent, LazyGroup):
            return []
        entry = parent._lazy_commands.get(sub_name)
        if entry is None:
            return []
        mirrored: list[str] = []
        for alias, (target, _short_help) in self._lazy_commands.items():
            # Skip aliases an extension claimed itself — that override wins.
            if target != entry[0] or alias in extension_set.commands:
                continue
            if self._install(alias, resolved):
                mirrored.append(alias)
        return mirrored

    def _builtin_names(self) -> set[str]:
        return set(self._lazy_commands) | set(self.commands)

    def _load_builtin(self, name: str):
        import importlib

        loaded = self.commands.get(name)
        if loaded is not None:
            return loaded
        entry = self._lazy_commands.get(name)
        if entry is None:
            return None
        module_path, attr = entry[0].split(":")
        try:
            return getattr(importlib.import_module(module_path), attr)
        except Exception as e:  # import failure shouldn't crash extension application
            logging.warning("Could not load built-in %r for override check: %s", name, e)
            return None

    def _install(self, name: str, resolved: ResolvedCommand) -> bool:
        """Install one command; dotted names (group.sub) target a subcommand."""
        if "." in name:
            return self._install_subcommand(name, resolved)
        return self._install_top_level(name, resolved)

    def _synthesize(self, resolved: ResolvedCommand, *, leaf: str, display: str):
        """Build the Click command for a contribution: the real one, or a stub.

        A stub is used when the owning extension is out of its declared range in
        `error` mode; it fails with the range and the remedies instead of
        running the extension's vector.
        """
        from google.agents.cli.extension._overrides import (
            make_blocked_command,
            make_override_command,
        )

        if resolved.blocked_requires is not None:
            return make_blocked_command(
                extension_name=resolved.extension_name,
                leaf_name=leaf,
                display_path=display,
                requires=resolved.blocked_requires,
                running=__version__,
            )
        return make_override_command(
            resolved.contribution,
            extension_name=resolved.extension_name,
            scope=resolved.scope,
            leaf_name=leaf,
            display_path=display,
            extension_root=resolved.extension_root,
        )

    def _install_top_level(self, name: str, resolved: ResolvedCommand) -> bool:

        is_builtin = name in self._builtin_names()
        if resolved.contribution.kind == "add" and is_builtin:
            logging.warning(
                "Extension %r tried to add command %r, which is a built-in; "
                "use commands.override instead. Skipping.",
                resolved.extension_name,
                name,
            )
            return False
        if resolved.contribution.kind == "override" and not is_builtin:
            logging.warning(
                "Extension %r overrides unknown command %r; skipping.",
                resolved.extension_name,
                name,
            )
            return False
        if is_builtin and isinstance(self._load_builtin(name), click.Group):
            logging.warning(
                "Extension %r cannot override command group %r; override a "
                "subcommand instead (e.g. %s.<sub>). Skipping.",
                resolved.extension_name,
                name,
                name,
            )
            return False
        self._overrides[name] = self._synthesize(resolved, leaf=name, display=name)
        return True

    def _install_subcommand(self, dotted: str, resolved: ResolvedCommand) -> bool:
        parts = dotted.split(".")
        if len(parts) != 2:
            logging.warning(
                "Extension %r: only single-level subcommand overrides "
                "(group.sub) are supported in v1; skipping %r.",
                resolved.extension_name,
                dotted,
            )
            return False
        group_name, sub_name = parts
        if group_name not in self._builtin_names():
            logging.warning(
                "Extension %r: parent command %r is not a built-in; skipping %r.",
                resolved.extension_name,
                group_name,
                dotted,
            )
            return False
        parent = self._load_builtin(group_name)
        if not isinstance(parent, LazyGroup):
            logging.warning(
                "Extension %r: %r is not a command group; skipping %r.",
                resolved.extension_name,
                group_name,
                dotted,
            )
            return False
        sub_known = sub_name in parent._lazy_commands or sub_name in parent.commands
        if resolved.contribution.kind == "override" and not sub_known:
            logging.warning(
                "Extension %r: %r has no subcommand %r; skipping.",
                resolved.extension_name,
                group_name,
                sub_name,
            )
            return False
        if resolved.contribution.kind == "add" and sub_known:
            logging.warning(
                "Extension %r: subcommand %r already exists; use override. Skipping.",
                resolved.extension_name,
                dotted,
            )
            return False
        parent._overrides[sub_name] = self._synthesize(
            resolved, leaf=sub_name, display=f"{group_name} {sub_name}"
        )
        return True

    def invoke(self, ctx: click.Context) -> None:
        # duration_ms covers dispatch through exit. Interpreter startup, imports
        # and extension loading happen before this and aren't included.
        started = time.monotonic()
        exit_code = 0
        error_class: str | None = None
        try:
            super().invoke(ctx)
        except click.exceptions.Exit as e:
            # Also how extension commands exit with their script's code.
            exit_code = e.exit_code
            raise
        # Abort (user cancelled) goes to Click, which prints "Aborted!" and exits 1,
        # instead of the traceback handler below.
        except click.Abort:
            exit_code = 1
            error_class = "Abort"
            raise
        except SystemExit as e:
            code = e.code
            exit_code = code if isinstance(code, int) else (0 if code is None else 1)
            raise
        except click.ClickException as e:
            from google.agents.cli._output import print_click_exception

            exit_code = e.exit_code
            error_class = _telemetry.error_class_of(e)
            click.echo(f"agents-cli v{__version__}", err=True)
            _print_is_project_moved_tip()
            print_click_exception(e)
            ctx.exit(e.exit_code)
        except KeyboardInterrupt:
            exit_code = 130
            error_class = "KeyboardInterrupt"
            from google.agents.cli._output import Console

            console = Console(stderr=True)
            console.print(f"\nagents-cli v{__version__}", style="dim")
            console.print("Operation cancelled by user", style="yellow")
            ctx.exit(130)
        except Exception as exc:
            exit_code = 1
            error_class = _telemetry.error_class_of(exc)
            click.echo(f"agents-cli v{__version__}", err=True)
            _print_is_project_moved_tip()
            traceback.print_exc()
            ctx.exit(1)
        finally:
            _telemetry.record_invocation(
                ctx,
                exit_code,
                int((time.monotonic() - started) * 1000),
                error_class,
            )


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
      agents-cli eval run              Run agent inference and grade the traces
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
main.add_lazy_command(
    "build",
    "google.agents.cli.dev.cmd_build:cmd_build",
    "Build the agent binary.",
    experiment="build_command",
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

# Extension commands
main.add_lazy_command(
    "extension",
    "google.agents.cli.extension.cmd_extension_group:extension_group",
    "Manage agents-cli extensions (experimental).",
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
