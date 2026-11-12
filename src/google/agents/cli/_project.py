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

"""Project configuration reader for agent projects."""

from __future__ import annotations

import logging
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import click
import yaml

# Fallback for manifests written before the framework key existed. Those are all
# ADK; new projects get the value from their template.
DEFAULT_FRAMEWORK = "google-adk"

# Base template used when a project does not name one. `adk` is an alias for it.
DEFAULT_BASE_TEMPLATE = "adk_python"

AGENT_IDENTITY = "agent_identity"
SERVICE_ACCOUNT = "service_account"


@dataclass(frozen=True)
class NestedCreateParam:
    """Where a create_param lives in the manifest, and what it may be set to."""

    section: str
    key: str
    allowed: tuple[str, ...]


# create_params the manifest keeps in a per-target section, keyed by the
# ProjectConfig field they populate.
NESTED_CREATE_PARAMS = {
    "agent_runtime_identity_default": NestedCreateParam(
        section="agent_runtime",
        key="identity_default",
        allowed=(AGENT_IDENTITY, SERVICE_ACCOUNT),
    ),
    "cloud_run_identity_default": NestedCreateParam(
        section="cloud_run",
        key="identity_default",
        allowed=(AGENT_IDENTITY, SERVICE_ACCOUNT),
    ),
}

# The create commands shown when a command runs outside a project, indented to
# sit under a "create a project first:" line.
CREATE_PROJECT_HINT = (
    "    agents-cli create my-agent -i    # choose options in prompts\n"
    "    agents-cli create my-agent -y    # accept the defaults (adk_python template, agent_runtime target)"
)


def agent_package_name(agent_directory: str) -> str:
    """Return the package name (basename) of ``agent_directory``."""
    return PurePosixPath(agent_directory).name


def agents_root(agent_directory: str) -> str:
    """Return the parent of ``agent_directory``, ``.`` for a top-level one."""
    return PurePosixPath(agent_directory).parent.as_posix()


@dataclass
class ProjectConfig:
    """Configuration derived from agents-cli-manifest.yaml."""

    project_name: str = ""
    deployment_target: str = "none"
    agent_directory: str = "app"
    is_a2a: bool = False
    region: str = "us-east1"
    base_template: str = DEFAULT_BASE_TEMPLATE
    acli_version: str = ""
    # The manifest's language as written; None when it has no language key.
    # Read `language` unless you need to tell "unset" apart from "python".
    raw_language: str | None = None
    framework: str = DEFAULT_FRAMEWORK
    session_type: str = "none"
    cicd_runner: str = "skip"
    agent_gateway: bool = False
    agent_guidance_filename: str = "GEMINI.md"
    agent_runtime_identity_default: str = SERVICE_ACCOUNT
    cloud_run_identity_default: str = SERVICE_ACCOUNT
    # The ADK name of the root agent. Empty means "derive it from the project
    # name"; see resolve_root_agent_name.
    root_agent_name: str = ""
    # create_params.app_name as written; empty when the manifest predates it.
    # Read `app_name` instead.
    raw_app_name: str = ""

    @property
    def language(self) -> str:
        return self.raw_language or "python"

    @property
    def app_name(self) -> str:
        """The {app_name} the server serves the agent under (/apps/{app_name}/...)."""
        return self.raw_app_name or agent_package_name(self.agent_directory)

    @property
    def create_params(self) -> dict[str, Any]:
        return {
            "deployment_target": self.deployment_target,
            "is_a2a": self.is_a2a,
            "session_type": self.session_type,
            "cicd_runner": self.cicd_runner,
            "agent_gateway": self.agent_gateway,
            "agent_guidance_filename": self.agent_guidance_filename,
            "root_agent_name": self.root_agent_name,
            "agent_runtime_identity_default": self.agent_runtime_identity_default,
            "cloud_run_identity_default": self.cloud_run_identity_default,
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any] | Any,
        filename: str = "agents-cli-manifest.yaml",
    ) -> ProjectConfig:
        """Create a ProjectConfig from a raw manifest dictionary."""
        if not isinstance(data, Mapping):
            raise click.ClickException(f"malformed {filename}")

        cfg = cls()
        cfg.project_name = data.get("name", cfg.project_name)
        cfg.agent_directory = data.get("agent_directory", cfg.agent_directory)
        cfg.region = data.get("region", cfg.region)
        cfg.base_template = data.get("base_template", cfg.base_template)
        cfg.acli_version = (
            data.get("acli_version") or data.get("version") or cfg.acli_version
        )
        cfg.raw_language = data.get("language")
        cfg.framework = data.get("framework", cfg.framework)

        create_params = data.get("create_params")
        if create_params is None:
            create_params = {}
        elif not isinstance(create_params, Mapping):
            raise click.ClickException(f"malformed create_params in {filename}")

        cfg.session_type = create_params.get("session_type", cfg.session_type)
        cfg.cicd_runner = create_params.get("cicd_runner", cfg.cicd_runner)
        cfg.agent_guidance_filename = create_params.get(
            "agent_guidance_filename", cfg.agent_guidance_filename
        )
        cfg.deployment_target = create_params.get(
            "deployment_target", cfg.deployment_target
        )
        cfg.is_a2a = create_params.get("is_a2a", cfg.is_a2a)
        cfg.agent_gateway = create_params.get("agent_gateway", cfg.agent_gateway)
        cfg.root_agent_name = create_params.get("root_agent_name") or ""
        cfg.raw_app_name = create_params.get("app_name") or ""

        for flat_key, param in NESTED_CREATE_PARAMS.items():
            values = create_params.get(param.section)
            if values is None:
                continue
            if not isinstance(values, Mapping):
                raise click.ClickException(
                    f"malformed create_params.{param.section} in {filename}"
                )
            if param.key not in values:
                continue
            if values[param.key] not in param.allowed:
                raise click.ClickException(
                    f"invalid create_params.{param.section}.{param.key} in "
                    f"{filename}: '{values[param.key]}' is not one of "
                    f"{', '.join(param.allowed)}"
                )
            setattr(cfg, flat_key, values[param.key])

        return cfg


_WARNED_LEGACY_CONFIG = False


def _warn_legacy_config() -> None:
    """Warn about legacy configuration once."""
    global _WARNED_LEGACY_CONFIG
    if not _WARNED_LEGACY_CONFIG:
        logging.warning(
            "Legacy configuration detected in pyproject.toml. Run "
            "`agents-cli scaffold upgrade` to migrate to agents-cli-manifest.yaml."
        )
        _WARNED_LEGACY_CONFIG = True


def _read_project_config_from_manifest(manifest_path: Path) -> ProjectConfig:
    """Read project configuration from an agents-cli-manifest.yaml file."""
    with open(manifest_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        data = {}
    return ProjectConfig.from_dict(data, filename=manifest_path.name)


def _read_project_config_from_pyproject(pyproject_path: Path) -> ProjectConfig:
    """Read project configuration from a legacy pyproject.toml file."""
    with open(pyproject_path, "rb") as f:
        pyproj_data = tomllib.load(f)
    tool_section = pyproj_data.get("tool", {})
    if not isinstance(tool_section, Mapping) or "agents-cli" not in tool_section:
        return ProjectConfig()
    _warn_legacy_config()
    data = tool_section["agents-cli"]
    if data is None:
        data = {}
    elif isinstance(data, Mapping):
        data = dict(data)
        project_section = pyproj_data.get("project", {})
        if isinstance(project_section, Mapping):
            data["name"] = project_section.get("name", "")
    return ProjectConfig.from_dict(data, filename=pyproject_path.name)


def read_project_config(project_dir: str | None = None) -> ProjectConfig:
    """Read project metadata from agents-cli-manifest.yaml.

    Falls back to sensible defaults if the file doesn't exist.

    Args:
        project_dir: Directory containing agents-cli-manifest.yaml.
            Defaults to current working directory.

    Returns:
        ProjectConfig with values from the manifest or defaults.
    """
    root = Path(project_dir) if project_dir else Path.cwd()
    manifest_path = root / "agents-cli-manifest.yaml"
    pyproject_path = root / "pyproject.toml"

    if manifest_path.exists():
        return _read_project_config_from_manifest(manifest_path)
    elif pyproject_path.exists():
        return _read_project_config_from_pyproject(pyproject_path)
    else:
        # If neither exists, return a default project config
        return ProjectConfig()


def parse_version(v: str) -> tuple[int, ...]:
    """Parse a dotted numeric version into a comparable tuple.

    Raises ``ValueError`` on non-numeric components (e.g. a ``0.0.0-dev``
    source build), which callers treat as "version unknown — don't compare".
    """
    return tuple(int(x) for x in v.split("."))


def scaffold_older_than(cfg: ProjectConfig, version: str) -> bool:
    """True when the version that scaffolded ``cfg`` is older than ``version``.

    Best-effort: returns False when the scaffold version is missing or
    non-numeric (e.g. a ``0.0.0-dev`` source build), so callers fall back to
    generic guidance instead of guessing.
    """
    if not cfg.acli_version:
        return False
    try:
        return parse_version(cfg.acli_version) < parse_version(version)
    except ValueError:
        return False


def check_cli_version(cfg: ProjectConfig) -> None:
    """Warn if the running CLI version doesn't match the version that scaffolded the project.

    Compares ``acli_version`` from project config with the running
    ``__version__``.  Emits a warning with upgrade guidance when there is a
    mismatch; never blocks execution.
    """
    acli_version = cfg.acli_version
    if not acli_version:
        return

    from google.agents.cli import __version__

    try:
        project_ver = parse_version(acli_version)
        cli_ver = parse_version(__version__)
    except ValueError:
        return

    if cli_ver < project_ver:
        logging.warning(
            "Version mismatch: project was scaffolded with agents-cli %s, running "
            "%s. Upgrade the CLI: uv tool install google-agents-cli@%s",
            acli_version,
            __version__,
            acli_version,
        )
    elif cli_ver > project_ver:
        logging.warning(
            "Version mismatch: project was scaffolded with agents-cli %s, running "
            "%s. Upgrade the project: agents-cli scaffold upgrade",
            acli_version,
            __version__,
        )


def _find_legacy_project_root(start_dir: Path) -> Path | None:
    """Find a legacy project root containing a pyproject.toml with agents-cli config."""
    for parent in [start_dir, *start_dir.parents]:
        pyproject_path = parent / "pyproject.toml"
        if pyproject_path.exists():
            try:
                with open(pyproject_path, "rb") as f:
                    data = tomllib.load(f)
                tool_data = data.get("tool")
                if isinstance(tool_data, Mapping) and "agents-cli" in tool_data:
                    return parent
            except Exception:
                pass
    return None


def find_project_root(dir: Path | None = None) -> Path | None:
    """Find the project root by walking up looking for agents-cli-manifest.yaml."""
    if dir is None:
        dir = Path.cwd()
    for parent in [dir, *dir.parents]:
        manifest_path = parent / "agents-cli-manifest.yaml"
        if manifest_path.exists():
            return parent

    # Fallback to legacy project root discovery
    legacy_root = _find_legacy_project_root(dir)
    if legacy_root is not None:
        _warn_legacy_config()
        return legacy_root

    return None


def is_project_moved() -> bool:
    """Check if the project has been moved by comparing the current path with .venv/bin/activate."""
    root = find_project_root(Path.cwd())
    if not root:
        return False

    venv_dir = root / ".venv"
    # Support both Unix-style (bin) and Windows-style (Scripts) virtualenvs
    activate_script = venv_dir / "bin" / "activate"
    if not activate_script.exists():
        activate_script = venv_dir / "Scripts" / "activate"

    if not activate_script.exists():
        return False

    try:
        with open(activate_script, encoding="utf-8") as f:
            for line in f:
                if line.startswith("VIRTUAL_ENV="):
                    stored_path_str = line.split("=", 1)[1].strip().strip("'\"")
                    stored_path = Path(stored_path_str).resolve()
                    current_path = venv_dir.resolve()
                    return stored_path != current_path
    except Exception as e:
        logging.warning(f"Error checking if project moved: {e}")
    return False


# ---------------------------------------------------------------------------
# Prerequisite guards — reusable checks for CLI commands
# ---------------------------------------------------------------------------


def chdir_project_root(dir: Path | None = None) -> None:
    """
    Locate the project root relative to the supplied directory and chdir to it.
    Raise if no root is found.
    """
    if dir is None:
        dir = Path.cwd()
    root = find_project_root(dir)
    if not root:
        raise click.ClickException(
            "No agents-cli-manifest.yaml found in the current directory or its parents.\n"
            "  Run this command from your project root, or create a project first:\n"
            f"{CREATE_PROJECT_HINT}"
        )
    # Only announce the root when we actually move (i.e. run from a subdir).
    if root.resolve() != Path.cwd().resolve():
        click.echo(f"Using project root directory: {root}", err=True)
    os.chdir(root)


def require_agent_directory(cfg: ProjectConfig) -> None:
    """
    Raise if the configured agent_directory doesn't exist.
    Assumes cwd is the project root.
    """
    agent_path = Path(cfg.agent_directory)
    if not agent_path.is_dir():
        raise click.ClickException(
            f"Agent directory '{cfg.agent_directory}' not found.\n"
            "  Ensure you're in the project root and that the directory exists.\n"
            "  The agent_directory is configured in agents-cli-manifest.yaml."
        )


def require_deployment_target(cfg: ProjectConfig) -> None:
    """Raise if no deployment target is configured."""
    if cfg.deployment_target in ("none", ""):
        raise click.ClickException(
            "No deployment target configured.\n"
            "  Set deployment_target in agents-cli-manifest.yaml,\n"
            "  or add deployment support to your project:\n"
            "    agents-cli scaffold enhance"
        )


def require_a2a_project(cfg: ProjectConfig) -> None:
    """Raise if the project is not an A2A agent."""
    if not cfg.is_a2a:
        raise click.ClickException(
            "This command requires an A2A agent project (is_a2a = true).\n"
            "  To add A2A support to your project, run:\n"
            "    agents-cli scaffold enhance"
        )


def find_project_config(project_dir: Path | None = None) -> ProjectConfig | None:
    """Read agents-cli config from project config files, resolving the project root.

    Args:
        project_dir: Optional path to start searching from. Defaults to cwd.

    Returns:
        ProjectConfig object if found, None otherwise.
    """
    project_root_dir = find_project_root(project_dir)
    if project_root_dir is None:
        return None

    return read_project_config(str(project_root_dir))


def root_agent_name(project_name: str) -> str:
    """The ADK agent name the scaffolded template gives a project's root agent.

    Single definition on purpose. The scaffold bakes this into
    ``Agent(name=...)``, and it then travels out in telemetry as the OpenTelemetry
    ``gen_ai.agent.name`` attribute -- which is what an observability backend
    filters an agent's traces on. Deriving it from the project name in one
    place is what lets agents-cli know the value without parsing the
    generated ``agent.py``.

    Coerced to a valid Python identifier, since ADK agent names must be:
    ``my-agent`` becomes ``my_agent``. Falls back to ``root_agent`` (the
    historical literal) when the project name has nothing usable in it.
    """
    candidate = re.sub(r"\W+", "_", project_name).strip("_").lower()
    if not candidate:
        return "root_agent"
    # A leading digit is legal in a project name but not in an identifier.
    return f"agent_{candidate}" if candidate[0].isdigit() else candidate


def resolve_root_agent_name(cfg: ProjectConfig) -> str:
    """The project's root agent name: recorded in the manifest, else derived.

    The recorded value wins so that renaming the agent in code stays
    expressible. Derivation alone can only produce names that follow from the
    project name, so a project called ``it-support-agent`` whose agent is
    ``billing_bot`` has no way to say so -- and nothing reports the mismatch,
    because a telemetry query filtered on the wrong name simply returns no rows.
    """
    return cfg.root_agent_name or root_agent_name(cfg.project_name)


def find_nearest_dotenv(start_dir: Path) -> Path | None:
    """The nearest ``.env`` at or above ``start_dir``, or None.

    Finds the same file an agent server loads: ADK's ``load_dotenv_for_agent``
    and python-dotenv's ``load_dotenv()`` both walk up from the agent folder
    and stop at the first ``.env``.
    """
    start = start_dir.resolve()
    for folder in (start, *start.parents):
        candidate = folder / ".env"
        if candidate.is_file():
            return candidate
    return None
