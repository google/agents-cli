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

"""agents-cli setup command — install skills via npx skills CLI."""

import logging
import random
import shlex
import shutil
from pathlib import Path

import click

from google.agents.cli._runner import run
from google.agents.cli._skills_check import SKILLS_NPX_PACKAGE, list_skills_all_scopes
from google.agents.cli._tools import DEFAULT_SKILLS_SOURCE, run_npx_skills
from google.agents.cli.skills._bundle import (
    SKILL_BUNDLE_DIR,
    get_bundled_skills_dir,
    is_skill_dir,
)

_MOTTOS = [
    "Give your coding agent the power to build ADK projects.",
    "From prototype to production — one CLI away.",
    "Skills up. Ship faster.",
    "Agents skills, installed in seconds.",
    "Your coding agent just got an upgrade.",
]


def _print_logo():
    """Print the AGENTS CLI ASCII art logo with a random motto."""
    click.secho(
        " █▀█ █▀▀ █▀▀ █▄ █ ▀█▀ █▀   █▀▀ █  █",
        fg="blue",
        bold=True,
    )
    click.secho(
        " █▀█ █▄█ ██▄ █ ▀█  █  ▄█   █▄▄ █▄ █",
        fg="cyan",
        bold=True,
    )
    click.echo()
    click.echo(f" {random.choice(_MOTTOS)}")


def _print_section(number, title):
    """Print a numbered section header."""
    click.echo()
    click.secho(f" {number}. {title}", bold=True)
    click.echo(f" {'─' * (len(title) + 3)}")


def _get_source_root():
    """Return the cwd if it is the agents-cli repo root, else None.

    Checks that a ``pyproject.toml`` with the ``agents-cli`` package
    name exists in the current working directory.
    """
    candidate = Path.cwd() / "pyproject.toml"
    if candidate.is_file():
        try:
            text = candidate.read_text()
            if 'name = "google-agents-cli"' in text:
                return Path.cwd()
        except OSError:
            pass
    return None


def _build_skills_args(source, *, workspace, agent):
    """Build the ``npx skills add`` argument list for ``source``."""
    args = ["add", source, "-y"]
    if "all" in agent:
        args.append("--all")
    elif agent:
        for a in agent:
            args.extend(["--agent", a])
    if not workspace:
        args.append("-g")
    return args


def _skills_store_dir(*, workspace):
    """Return the canonical skills store (global home or workspace-relative)."""
    base = Path.cwd() if workspace else Path.home()
    return base / ".agents" / "skills"


def _display_path(path):
    """Render ``path`` with the home directory collapsed to ``~`` for output."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _copy_bundled_skills(bundled_dir, dest_root):
    """Copy every bundled skill into ``dest_root``.

    Returns the number of skills copied. Existing copies are replaced so the
    store always reflects the version shipped with this CLI.
    """
    dest_root.mkdir(parents=True, exist_ok=True)
    count = 0
    for skill in sorted(bundled_dir.iterdir()):
        if not is_skill_dir(skill):
            continue
        target = dest_root / skill.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(skill, target)
        count += 1
    return count


def _install_skills(remote_source, *, workspace, agent, allow_fallback):
    """Install skills, falling back progressively when the remote path fails.

    1. ``npx skills add`` from ``remote_source`` (the GitHub repo by default,
       which requires ``git`` and network access).
    2. ``npx skills add`` from the skills bundled with this CLI (no ``git`` or
       network needed).
    3. Copy the bundled skills straight into the ``.agents/skills`` store and
       warn the user.

    Fallbacks (2 and 3) are only attempted when ``allow_fallback`` is true — an
    explicit ``--skills-source`` disables them so the user's chosen source is
    never silently swapped for the bundled copy.
    """

    args = _build_skills_args(remote_source, workspace=workspace, agent=agent)
    bundled_dir = get_bundled_skills_dir() if allow_fallback else None

    # Remote (or explicitly requested) source
    try:
        run_npx_skills(args)
        return None
    except click.ClickException:
        if bundled_dir is None:
            raise
        logging.warning("Could not install skills from %s.", remote_source)
        click.secho("  Falling back to the skills bundled with this CLI.", dim=True)

    # Bundled skills via npx (skips a redundant identical run)
    if bundled_dir.resolve() != Path(remote_source).resolve():
        local_args = _build_skills_args(
            str(bundled_dir), workspace=workspace, agent=agent
        )
        try:
            run_npx_skills(local_args)
            return None
        except click.ClickException:
            logging.warning("Could not install bundled skills via npx skills.")

    # Copy the bundled skills directly (no npx / git needed)
    dest_root = _skills_store_dir(workspace=workspace)
    copied = _copy_bundled_skills(bundled_dir, dest_root)
    logging.warning(
        "Installed %s skills by copying them into %s (npx skills was unavailable).",
        copied,
        _display_path(dest_root),
    )
    click.secho(
        "     'agents-cli update' may not manage these copied skills; re-run "
        "'agents-cli setup' once git and npx are available.",
        dim=True,
    )


def _check_legacy_skills():
    """Check for legacy ADK skills and warn the user if found."""
    # ── Legacy Skills Detection ──
    try:
        import logging

        installed_skills = list_skills_all_scopes()
        legacy_skills = {
            "adk-cheatsheet",
            "adk-deploy-guide",
            "adk-dev-guide",
            "adk-eval-guide",
            "adk-observability-guide",
            "adk-scaffold",
        }

        found_legacy = []
        for skill in installed_skills:
            name = skill.get("name")
            if name in legacy_skills:
                found_legacy.append(name)

        if found_legacy:
            logging.warning(
                "Found legacy ADK skills installed: %s. "
                "These may conflict with the new `agents-cli` skills.",
                ", ".join(found_legacy),
            )
            click.secho(
                "     We suggest you uninstall them to avoid confusion, e.g.:",
                dim=True,
            )
            for name in found_legacy:
                click.secho(f"     npx skills remove {name}", dim=True)
            click.echo()
    except Exception as e:
        # Don't fail the setup if the check fails
        logging.warning("Could not check for legacy skills: %s", e)


@click.command("setup")
@click.option(
    "--workspace",
    is_flag=True,
    default=False,
    help=(
        "Install to project/workspace scope instead of global. "
        "Skills are installed relative to the current directory."
    ),
)
@click.option(
    "--skip-auth",
    is_flag=True,
    default=False,
    help="Skip the authentication step.",
)
@click.option(
    "--dry-run",
    "--dryrun",
    is_flag=True,
    default=False,
    help="Show what would be done without making changes.",
)
@click.option(
    "--dev",
    is_flag=True,
    default=False,
    help="Install as editable from the local repo (for contributors).",
)
@click.option(
    "--interactive",
    "-i",
    is_flag=True,
    default=False,
    help="Enable interactive authentication prompt if not already authenticated.",
)
@click.option(
    "--skills-source",
    default=None,
    help="Skills source: local path, GitHub owner/repo, or URL. Overrides the bundled skills.",
)
@click.option(
    "--agent",
    multiple=True,
    help=(
        "Specify the agent to install skills to (e.g. --agent claude-code --agent cursor). "
        "Use 'all' to install for all supported agents."
    ),
)
def cmd_setup(*, workspace, skip_auth, dry_run, dev, interactive, skills_source, agent):
    """Install agents-cli and skills to detected coding agents.

    Installs the agents-cli tool (via uv tool install) and detects
    installed coding agents (Claude Code, Antigravity CLI, Cursor,
    Windsurf, etc.) to install ADK development skills via npx skills.

    By default, skills are installed globally for all detected agents.
    Use --workspace to install at the project level instead.
    Use --agent to specify specific coding agents (e.g. --agent claude-code --agent cursor) or 'all'.
    Use --dry-run to preview what would happen without executing.
    Use --dev to install agents-cli as editable from the local repo (for contributors).
    Use --interactive / -i to enable interactive authentication if not already logged in.
    """
    click.echo("Setting up...")
    click.echo()
    _print_logo()

    scope = "workspace" if workspace else "global"

    source = DEFAULT_SKILLS_SOURCE
    if dev and not skills_source:
        # In dev mode, use skills from the local repo checkout
        source = str(SKILL_BUNDLE_DIR)
    elif skills_source:
        source_path = Path(skills_source)
        # Only resolve to an absolute path if the source is a local file/directory
        # to prevent resolving remote URIs (e.g., GitHub URLs or package identifiers).
        if skills_source.startswith((".", "/")) or source_path.exists():
            source = str(source_path.resolve())
        else:
            source = skills_source
    args = _build_skills_args(source, workspace=workspace, agent=agent)

    # ── Dry Run ──
    if dry_run:
        _print_section(1, "Dry Run")
        click.echo()
        if dev:
            project_root = _get_source_root()
            if not project_root:
                raise click.ClickException(
                    "--dev requires running from the root of the agents-cli repository"
                )
            click.echo("  Would install agents-cli (editable):")
            click.secho(
                f"  ▸ uv tool install --force --editable {project_root}",
                fg="cyan",
                dim=True,
            )
        else:
            click.echo("  Would install agents-cli:")
            click.secho(
                "  ▸ uv tool install google-agents-cli",
                fg="cyan",
                dim=True,
            )
        click.echo()
        click.echo("  Would install skills:")
        full_args = ["npx", "-y", SKILLS_NPX_PACKAGE, *args]
        click.secho(f"  ▸ {shlex.join(full_args)}", fg="cyan", dim=True)
        click.echo(f"  Scope: {scope}")
        # Temporary compatibility step (see TODO at the real linking call below).
        if not workspace and (Path.home() / ".gemini").is_dir():
            click.echo(
                "  Would link global skills into Antigravity's skill directories "
                "(~/.gemini/config/skills, ~/.gemini/antigravity-cli/skills)."
            )
        click.echo()

        if not skip_auth:
            from google.agents.cli.auth import is_authenticated

            authed, display = is_authenticated()
            if authed:
                click.echo(f"  Auth:  {display}")
            else:
                click.echo("  Auth:  Not authenticated")
        else:
            click.echo("  Auth:  Skipped")

        click.echo()
        click.secho("  No changes made (dry run).", fg="yellow")
        click.echo()
        return

    step = 1

    # ── Authentication ──
    _print_section(step, "Authentication")
    step += 1
    if not skip_auth:
        from google.agents.cli.auth import is_authenticated, run_auth_step

        authed, display = is_authenticated()
        if authed:
            click.echo()
            click.secho(f"  Authenticated as {display}", fg="green")
        elif interactive:
            run_auth_step(show_header=False)
        else:
            click.echo()
            click.secho(
                "  Not authenticated. Run with --interactive (-i) to authenticate interactively.",
                fg="yellow",
                dim=True,
            )
    else:
        click.echo()
        click.secho("  Skipped (--skip-auth)", dim=True)

    # ── CLI Installation ──
    _print_section(step, "CLI Installation")
    step += 1
    click.echo()
    cli_installed = False
    if dev:
        project_root = _get_source_root()
        if not project_root:
            raise click.ClickException(
                "--dev requires running from the root of the agents-cli repository"
            )
        tool_args = [
            "uv",
            "tool",
            "install",
            "--force",
            "--editable",
            str(project_root),
        ]
    else:
        tool_args = ["uv", "tool", "install", "google-agents-cli"]
    result = run(tool_args, capture=True, check=False)
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if "already installed" in stdout.lower() or "already installed" in stderr.lower():
        from google.agents.cli.scaffold.utils.version import check_for_updates

        needs_update, current, latest = check_for_updates()
        if needs_update:
            click.secho(
                f"  Installed ({current}), but {latest} is available.",
                fg="yellow",
            )
            click.secho(
                "  Run 'uv tool upgrade google-agents-cli' to update.",
                dim=True,
            )
        else:
            click.secho("  Already installed and up to date.", dim=True)
        cli_installed = True
    elif result.returncode != 0:
        logging.warning("Could not install agents-cli automatically.")
        if stderr.strip():
            for line in stderr.strip().splitlines():
                click.echo(f"  {line}")
        click.secho("  Install manually: uv tool install google-agents-cli", dim=True)
    else:
        for line in stdout.strip().splitlines():
            click.echo(f"  {line}")
        cli_installed = True

    # ── Skills Installation ──
    _print_section(step, "Skills Installation")
    step += 1
    click.echo()

    # ── Legacy Skills Detection ──
    _check_legacy_skills()

    _install_skills(
        source,
        workspace=workspace,
        agent=agent,
        allow_fallback=not skills_source,
    )

    # ── Antigravity skill links ──
    # Temporary until npx skills supports Antigravity's IDE / CLI / 2.0 paths:
    # npx installs global skills to ~/.agents/skills, which Antigravity does not
    # read, so mirror them into the locations the IDE/2.0 and CLI look in.
    # TODO(b/520131431): remove once Antigravity/npx align on skill paths.
    if not workspace:
        from google.agents.cli.setup._antigravity import link_skills_for_antigravity

        for line in link_skills_for_antigravity():
            click.echo(f"  {line}")

    # ── Summary ──
    _print_section(step, "Summary")
    click.echo()

    # Auth status
    if skip_auth:
        click.echo("  Auth:   Skipped")
    else:
        from google.agents.cli.auth import is_authenticated

        authed, display = is_authenticated()
        if authed:
            click.echo(f"  Auth:   {display}")
        else:
            click.echo("  Auth:   Not authenticated")

    # CLI tool status
    if cli_installed:
        if dev:
            click.echo("  CLI:    agents-cli installed (editable)")
        else:
            click.echo("  CLI:    agents-cli installed")
    else:
        click.echo("  CLI:    Not installed (run: uv tool install google-agents-cli)")

    # Skills status
    click.echo("  Skills: Installed")
    click.echo(f"  Scope:  {scope}")

    click.echo()
    click.secho("  Done.", fg="green", bold=True)
    click.echo()
    # Coding agents load skills at session start, so one running setup won't see them yet.
    workflow_skill = (
        _skills_store_dir(workspace=workspace) / "google-agents-cli-workflow" / "SKILL.md"
    )
    click.echo(f"  Coding agent? Read {workflow_skill} now.")
    click.echo()
