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

"""agents-cli playground command — start local agent playground."""

import logging
import shlex
from pathlib import Path

import click
from click.core import ParameterSource
from rich.panel import Panel

from google.agents.cli._output import Console
from google.agents.cli._project import (
    agents_root,
    chdir_project_root,
    read_project_config,
    require_agent_directory,
)
from google.agents.cli._runner import run
from google.agents.cli._tools import is_windows
from google.agents.cli._uv_index import uv_index_env
from google.agents.cli.scaffold.utils.language import dispatch_language

_console = Console()


@click.command("playground")
@click.option("--port", default=8080, help="Port for the playground server.")
@click.option("--host", default="127.0.0.1", help="Host the server binds to.")
@click.option(
    "--reload_agents/--no-reload_agents",
    default=True,
    help="Enable / disable live reload when agent code changes.",
)
@click.option(
    "--otel-to-cloud",
    is_flag=True,
    default=False,
    help="Export OpenTelemetry traces/logs to Google Cloud.",
)
def cmd_playground(port, host, reload_agents, otel_to_cloud):
    """Start the local agent playground."""
    chdir_project_root()
    cfg = read_project_config()
    require_agent_directory(cfg)
    # Use 127.0.0.1 instead of localhost to avoid IPv6 resolution issues on Windows.
    browser_host = "127.0.0.1" if host == "0.0.0.0" else host

    build = dispatch_language("playground", LANGUAGE_HANDLERS, cfg.language)
    url, args = build(
        cfg=cfg,
        browser_host=browser_host,
        host=host,
        port=port,
        reload_agents=reload_agents,
        otel_to_cloud=otel_to_cloud,
    )

    _print_banner(url, args)
    env = uv_index_env(Path.cwd()) if cfg.language == "python" else None
    run(args, print_cmd=False, env=env, check_err_msg="Failed to start playground")


def _build_python_playground(
    *,
    cfg,
    browser_host: str,
    host: str,
    port: int,
    reload_agents: bool,
    otel_to_cloud: bool,
) -> tuple[str, list[str]]:
    """Build the (banner URL, command) for a Python (adk web) playground."""
    # adk web doesn't auto-select the agent — pre-fill it via ?app= so the
    # URL we print drops the user straight into their agent.
    url = f"http://{browser_host}:{port}/dev-ui/?app={cfg.app_name}"

    args = [
        "uv",
        "run",
        "adk",
        "web",
        agents_root(cfg.agent_directory),
        "--host",
        host,
        "--port",
        str(port),
    ]

    # A '*' value is improperly shell-expanded on Windows due to an ADK bug,
    # see b/525208532. Work around it by only passing the flag for non-Windows.
    if not is_windows():
        args.extend(["--allow_origins", "*"])

    if reload_agents:
        args.append("--reload_agents")
    if otel_to_cloud:
        args.append("--otel_to_cloud")
    return url, args


def _user_set(option: str) -> bool:
    """Whether ``option`` came from the command line rather than its default."""
    ctx = click.get_current_context(silent=True)
    return (
        ctx is not None
        and ctx.get_parameter_source(option) == ParameterSource.COMMANDLINE
    )


def _build_go_playground(
    *, port: int, reload_agents: bool, otel_to_cloud: bool, **_
) -> tuple[str, list[str]]:
    """Build the (banner URL, command) for a Go (adk-go web) playground.

    ADK Go serves the web UI under /ui/ and mounts the ADK REST API at root (/).
    """
    # `adk-go web` always binds ":port" (all interfaces) and has no reload, so
    # warn only when the user actually asked for what we're about to drop.
    if _user_set("host"):
        logging.warning(
            "--host is not supported by `adk-go web`, which always binds all "
            "interfaces; ignoring it."
        )
    if reload_agents and _user_set("reload_agents"):
        logging.warning("--reload_agents is not supported by `adk-go web`; ignoring it.")

    url = f"http://127.0.0.1:{port}/ui/"
    args = ["go", "run", ".", "web", "--port", str(port)]
    if otel_to_cloud:
        args.append("--otel_to_cloud")
    args += [
        "api",
        "-path_prefix",
        "/",
        "webui",
        # Serve the UI and the API from the same origin: an empty
        # api_server_address makes the web UI issue relative (same-origin)
        # requests, so it works whether the browser hits 127.0.0.1, localhost,
        # or a LAN IP, and never touches localhost/::1 on Windows. A non-empty
        # value (e.g. http://localhost:PORT) is a different origin from the
        # 127.0.0.1 page above and gets blocked by the browser (b/557275644).
        "--api_server_address=",
    ]
    return url, args


# `None` = not supported yet; dispatch_language raises a clear error.
LANGUAGE_HANDLERS = {
    "python": _build_python_playground,
    "go": _build_go_playground,
    "java": None,
    "typescript": None,
    "kotlin": None,
}


def _print_banner(url: str, cmd_args: list[str]) -> None:
    """Print a styled banner with a clickable URL pointing at the agent."""
    cmd_str = shlex.join(cmd_args)
    body = (
        "[bold cyan]Starting your agent playground...[/]\n"
        "\n"
        f"[bold]Running command:[/]       {cmd_str}\n"
        f"[bold]Will be available at:[/]  [green underline]{url}[/]"
    )
    _console.print(Panel(body, border_style="cyan"))
