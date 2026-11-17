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

"""Warn about extensions taken from their old home in the agents-cli repo."""

from __future__ import annotations

import logging
from pathlib import Path

from google.agents.cli._project import find_project_config
from google.agents.cli.extension._refs import (
    AGENTS_CLI_REPO,
    EXTENSIONS_REPO,
    ExtensionRef,
    RefParseError,
    is_agents_cli_repo_url,
    is_from_agents_cli_repo,
    parse_ref,
)

_EXTENSIONS_REPO_URL = f"https://github.com/{EXTENSIONS_REPO}"
# Folder of the new repo that holds the first-party extensions.
_NEW_DIR = "core"
# Folder of the agents-cli repo the extensions moved out of.
_OLD_DIR = "extensions"
# enhance renders the template more than once in one command; warn once per spec.
_warned_templates: set[tuple[str, str]] = set()


def warn_if_agents_cli_repo_ref(ref: ExtensionRef, *, global_: bool) -> None:
    """Warn when `extension add` installs from the agents-cli repo."""
    if not is_from_agents_cli_repo(ref):
        return
    logging.warning(
        "Installing extensions from %s is deprecated; first-party extensions "
        "moved to %s. Install from there instead:\n  %s",
        ref.raw,
        _EXTENSIONS_REPO_URL,
        _add_command(ref, global_=global_),
    )


def warn_if_installed_from_agents_cli_repo(
    extension_name: str, source: str | None, scope: str
) -> None:
    """Warn when an extension recorded with an agents-cli repo source is used."""
    if source is None:
        return
    try:
        ref = parse_ref(source)
    except RefParseError:
        # A source that does not parse names no repo, so it can't be the old one.
        return
    if not is_from_agents_cli_repo(ref):
        return
    logging.warning(
        "Extension %r is installed from %s, which is deprecated; first-party "
        "extensions moved to %s. Reinstall it from there:\n  %s",
        extension_name,
        source,
        _EXTENSIONS_REPO_URL,
        _add_command(ref, global_=scope == "user"),
    )


def warn_if_agents_cli_repo_template(
    repo_url: str, template_path: str, agent_spec: str | None
) -> None:
    """Warn when a template is fetched from the agents-cli repo's extensions/ folder.

    ``agent_spec`` is the spec as given or recorded; it tells whether the spec
    came from the project's manifest.
    """
    old_path = template_path.strip("/")
    if not is_agents_cli_repo_url(repo_url):
        return
    if old_path != _OLD_DIR and not old_path.startswith(f"{_OLD_DIR}/"):
        return
    key = (repo_url, old_path)
    if key in _warned_templates:
        return
    _warned_templates.add(key)
    new_path = f"{_NEW_DIR}/{old_path.removeprefix(_OLD_DIR).lstrip('/')}".rstrip("/")
    recorded = (
        "\n  The project records it as base_template in agents-cli-manifest.yaml; "
        "change it there."
        if agent_spec is not None and _is_recorded_template(agent_spec)
        else ""
    )
    logging.warning(
        "Templates in %s/%s are deprecated; first-party templates moved to %s. "
        "Use %s/%s instead.%s",
        AGENTS_CLI_REPO,
        old_path,
        _EXTENSIONS_REPO_URL,
        EXTENSIONS_REPO,
        new_path,
        recorded,
    )


def _is_recorded_template(agent_spec: str) -> bool:
    config = find_project_config(Path.cwd())
    return config is not None and config.base_template == agent_spec


def _add_command(ref: ExtensionRef, *, global_: bool) -> str:
    """The `extension add` command for the same extension in the new repo."""
    # Old selectors read `langchain`, `langchain/template` or
    # `extensions/langchain/template`; the new repo keeps the same names.
    name = (ref.selector or "").removeprefix(f"{_OLD_DIR}/").partition("/")[0]
    selector = f"{_NEW_DIR}/{name or '<name>'}/template"
    scope = " --global" if global_ else ""
    return (
        f"agents-cli extension add {EXTENSIONS_REPO}#{selector} "
        f"--ref <tag|branch|sha>{scope}"
    )
