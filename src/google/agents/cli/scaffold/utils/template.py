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

import json
import logging
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import tomlkit
import yaml
from cookiecutter.environment import StrictEnvironment
from cookiecutter.main import cookiecutter
from jinja2 import TemplateError
from packaging.utils import canonicalize_name
from rich.markup import escape
from rich.prompt import Confirm, IntPrompt

from google.agents.cli._defaults import DEFAULT_MODEL
from google.agents.cli._output import Console
from google.agents.cli._project import (
    AGENT_IDENTITY,
    SERVICE_ACCOUNT,
    agent_package_name,
)
from google.agents.cli._project import root_agent_name as derive_root_agent_name

from . import go_overlay
from .copy_files import (
    DEFAULT_FRONTEND,
    CopyOptions,
    copy_dir_contents,
    copy_files,
    copy_flat_structure_agent_files,
    copy_frontend_files,
)
from .language import LANGUAGE_CONFIGS, get_language_config, is_language_enabled
from .lock_utils import get_lock_filename, is_empty_agent
from .remote_template import FLAT_AGENT_DIRECTORY, get_base_template_name
from .upgrade import DependencyReadError, union_python_base_dependencies
from .version import agents_cli_version_pin, get_current_version

# =============================================================================
# Agent Name Aliases (Backwards Compatibility)
# =============================================================================
# Maps legacy agent names to their current names.
# This allows users to continue using old names like `--agent adk_base`
# and for remote templates to reference `base_template: adk_base`.
# =============================================================================

AGENT_ALIASES: dict[str, str] = {
    "adk": "adk_python",
    "adk_base": "adk_python",
    "adk_a2a_base": "adk_python",
    "adk_a2a": "adk_python",
}

SESSION_TYPES = {
    "in_memory": {
        "display_name": "in_memory",
        "description": "Stateless, data in memory",
    },
    "cloud_sql": {
        "display_name": "cloud_sql",
        "description": "PostgreSQL persistence",
    },
    "agent_platform_sessions": {
        "display_name": "agent_platform_sessions",
        "description": "Managed session service",
    },
}


def resolve_agent_alias(name: str | None) -> str | None:
    """Resolve legacy agent name to current name.

    Args:
        name: Agent name (possibly a legacy alias)

    Returns:
        Current agent name, or original if not an alias
    """
    if name is None:
        return None
    resolved = AGENT_ALIASES.get(name, name)
    if resolved != name:
        logging.info("Agent '%s' has been renamed to '%s'", name, resolved)
    return resolved


# =============================================================================
# Conditional Files Configuration
# =============================================================================
# Maps file/directory paths to their inclusion conditions.
# Files are stored with simple names and deleted if condition is False.
# This replaces Jinja2 conditionals in filenames for Windows compatibility.
#
# Format: "relative/path/to/file_or_dir": lambda config: bool_condition
# The config dict contains: agent_name, deployment_target, cicd_runner, is_a2a
# =============================================================================


CONDITIONAL_FILES = {
    # The deployment targets' e2e test drives ADK's own routes (/run_sse, the
    # session API) and imports a2a, which a non-ADK project neither serves nor
    # depends on, so it would fail at import. A template shipping its own copy
    # keeps it: the overlay has already replaced the file by this point.
    "tests/integration/test_server_e2e.py": lambda c: (
        c.get("is_a2a", False) or c.get("template_owns_e2e", False)
    ),
    # The reasoning-engine adapter is only usable on Agent Runtime, which is the
    # only target that installs aiplatform[agent-engines].
    "{agent_directory}/app_utils/reasoning_engine_adapter.py": (
        lambda c: c.get("deployment_target") == "agent_runtime"
    ),
    # CI/CD runner conditional files (base_template)
    ".cloudbuild": lambda c: c.get("cicd_runner") == "google_cloud_build",
    ".github": lambda c: c.get("cicd_runner") == "github_actions",
    "deployment/terraform/cicd/build_triggers.tf": (
        lambda c: c.get("cicd_runner") == "google_cloud_build"
    ),
    "deployment/terraform/cicd/wif.tf": (
        lambda c: c.get("cicd_runner") == "github_actions"
    ),
}


def apply_conditional_files(
    project_path: pathlib.Path,
    config: dict[str, Any],
    agent_directory: str = "app",
) -> None:
    """Apply conditional file logic by deleting files that don't match conditions.

    This function checks each conditional file against its condition and either
    keeps the file (condition True) or renames it to unused_* (condition False)
    so it gets cleaned up by the existing unused file cleanup logic.

    Args:
        project_path: Path to the generated project directory
        config: Configuration dict with keys: agent_name, deployment_target,
            cicd_runner, is_a2a
        agent_directory: Name of the agent directory (replaces {agent_directory} placeholder)
    """
    for rel_path_template, condition_fn in CONDITIONAL_FILES.items():
        # Replace {agent_directory} placeholder
        rel_path = rel_path_template.replace("{agent_directory}", agent_directory)
        file_path = project_path / rel_path

        if not file_path.exists():
            continue

        should_include = condition_fn(config)

        if not should_include:
            # Rename to unused_* so existing cleanup logic handles it
            parent = file_path.parent
            name = file_path.name
            unused_path = parent / f"unused_{name}"

            logging.debug(
                "Conditional file '%s' condition False, renaming to %s",
                rel_path,
                unused_path.name,
            )

            if unused_path.exists():
                if unused_path.is_dir():
                    shutil.rmtree(unused_path)
                else:
                    unused_path.unlink()

            file_path.rename(unused_path)
        else:
            logging.debug("Conditional file '%s' condition True, keeping", rel_path)


def _add_dependencies(
    project_path: pathlib.Path,
    dependencies: list[str],
    success_message: str,
    auto_approve: bool = False,
    interactive: bool = False,
) -> bool:
    """Helper function to interactively add dependencies using uv add.

    Args:
        project_path: Path to the project directory
        dependencies: List of dependencies to install
        success_message: Message to show upon success
        auto_approve: Whether to skip confirmation and auto-install
        interactive: Whether to show interactive confirmation prompt

    Returns:
        True if dependencies were added successfully, False otherwise
    """
    if not dependencies:
        return True

    console = Console()
    deps_str = shlex.join(dependencies)

    should_add = True
    if interactive:
        should_add = Confirm.ask(
            "\n? Add these dependencies automatically?", default=True
        )

    if not should_add:
        console.print("\n⚠️  Skipped dependency installation.", style="yellow")
        console.print("   To add them manually later, run:", style="dim")
        console.print(f"       cd {project_path.name}", style="dim")
        console.print(f"       uv add {deps_str}\n", style="dim")
        return False

    # Run uv add
    try:
        if auto_approve:
            console.print(
                f"✓ Auto-installing dependencies: {', '.join(dependencies)}",
                style="bold cyan",
            )
        else:
            console.print(f"\n✓ Running: uv add {deps_str}", style="bold cyan")

        from google.agents.cli._runner import run_resolved
        from google.agents.cli._tools import ToolNotFoundError

        # Run uv add in the project directory
        cmd = ["uv", "add", *dependencies]
        result = run_resolved(
            cmd,
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )

        # Show success message
        if not auto_approve:
            # Show a summary line from uv output
            output_lines = result.stderr.strip().split("\n")
            for line in output_lines:
                if "Resolved" in line or "Installed" in line:
                    console.print(f"  {line}", style="dim")
                    break

        console.print(f"✓ {success_message}\n", style="bold green")
        return True

    except subprocess.CalledProcessError as e:
        console.print(
            f"\n✗ Failed to add dependencies: {e.stderr.strip()}", style="bold red"
        )
        console.print("  You can add them manually:", style="yellow")
        console.print(f"      cd {project_path.name}", style="dim")
        console.print(f"      uv add {deps_str}\n", style="dim")
        return False
    except ToolNotFoundError:
        console.print(
            "\n✗ uv command not found. Please install uv first.", style="bold red"
        )
        console.print("  Install from: https://docs.astral.sh/uv/", style="dim")
        console.print("\n  To add dependencies manually:", style="yellow")
        console.print(f"      cd {project_path.name}", style="dim")
        console.print(f"      uv add {deps_str}\n", style="dim")
        return False


def add_base_template_dependencies(
    project_path: pathlib.Path,
    base_dependencies: list[str],
    base_template_name: str,
    auto_approve: bool = False,
    interactive: bool = False,
) -> bool:
    """Interactively add base template dependencies using uv add.

    Args:
        project_path: Path to the project directory
        base_dependencies: List of dependencies from base template's extra_dependencies
        base_template_name: Name of the base template being used
        auto_approve: Whether to skip confirmation and auto-install
        interactive: Whether to show interactive confirmation prompt

    Returns:
        True if dependencies were added successfully, False otherwise
    """
    if not base_dependencies:
        return True

    console = Console()

    console.print(
        f"\n✓ Ensuring base template '{base_template_name}' dependencies",
        style="bold cyan",
    )
    console.print("  Adding the following dependencies:", style="white")

    # Compare each base dep with what the template declared, and also list the
    # template's own deps ("kept") so the user sees every installed dependency.
    try:
        resolutions = union_python_base_dependencies(project_path, base_dependencies)
    except DependencyReadError as e:
        logging.warning(
            "Could not compare with the template's dependencies — %s. "
            "Review your dependency manifest manually after the scaffold.",
            e,
        )
        for dep in base_dependencies:
            console.print(f"    • {escape(dep)}", style="yellow")
    else:
        for resolution in resolutions:
            name = escape(resolution.name)
            old_ver = escape(resolution.old_version or "")
            new_ver = escape(resolution.new_version or "")
            if resolution.status == "added":
                line = f"Added: {name}{new_ver}"
            elif resolution.status == "updated":
                line = f"Updated: {name} {old_ver} → {new_ver}"
            elif resolution.status == "unchanged" or resolution.status == "kept":
                line = f"Kept: {name}{new_ver}"
            else:
                continue
            console.print(f"    • {line}", style="yellow", highlight=False)

    return _add_dependencies(
        project_path=project_path,
        dependencies=base_dependencies,
        success_message="Dependencies added successfully",
        auto_approve=auto_approve,
        interactive=interactive,
    )


def _validate_go_agent_dir(agent_dir: str) -> None:
    package = agent_package_name(agent_dir)
    if not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", package):
        raise ValueError(
            f"Agent directory '{agent_dir}' ends in '{package}', which is not a "
            "valid Go package name. It must start with a letter and contain only "
            "letters, digits, and underscores."
        )

    if agent_dir in go_overlay.SCAFFOLD_PACKAGES:
        raise ValueError(
            f"Agent directory '{agent_dir}' is a package of the generated Go "
            "project. Rename the template's agent directory, or pass "
            "--agent-directory with a name other than "
            f"{', '.join(sorted(go_overlay.SCAFFOLD_PACKAGES))}."
        )


def _validate_python_agent_dir(agent_dir: str) -> None:
    package = agent_package_name(agent_dir)
    if not package.isidentifier():
        raise ValueError(
            f"Agent directory '{agent_dir}' ends in '{package}', which is not a "
            "valid Python identifier. The agent directory is imported as a Python "
            "package, so use only letters, numbers, and underscores, and don't "
            "start with a number."
        )


# Language-specific agent directory rules on top of the shared allowlist.
# `None` = no extra rules for that language.
AGENT_DIR_VALIDATORS: dict[str, Callable[[str], None] | None] = {
    "python": _validate_python_agent_dir,
    "go": _validate_go_agent_dir,
    "java": None,
    "typescript": None,
    "kotlin": None,
}


def validate_agent_directory_name(
    agent_dir: str, allow_dot: bool = False, language: str = "python"
) -> None:
    """Validate that an agent directory name is a valid identifier for the language.

    Enforces an allowlist regex for all languages, then applies language-specific
    rules.

    Args:
        agent_dir: The agent directory name to validate
        allow_dot: If True, allows "." as a special value indicating flat structure
        language: The project language

    Raises:
        ValueError: If the agent directory name is not valid

    Note:
        The special value "." indicates flat structure - agent code is in the
        template root. When "." is used, the target directory name will be
        derived from the template folder name.
    """
    if agent_dir == ".":
        if allow_dot:
            return  # "." is valid when explicitly allowed (will be resolved later)
        raise ValueError(
            "Agent directory '.' is not valid in this context. "
            "Use '.' only to indicate flat structure templates."
        )
    # Allowlist check — enforced for ALL languages.
    # Accepts one or more path components separated by forward slashes.
    # Each component can only contain letters, digits, hyphens, and underscores.
    # This implicitly blocks absolute paths (/), dot-dot (..), backslashes (\),
    # tilde (~), Windows drives (C:), and any other path-traversal payload.
    if not re.match(r"^[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*$", agent_dir):
        raise ValueError(
            f"Invalid agent directory name '{agent_dir}'. It can only contain "
            "letters, numbers, hyphens, underscores, and forward slashes (no "
            "absolute paths or dot-dot components)."
        )

    validator = AGENT_DIR_VALIDATORS.get(language)
    if validator is not None:
        validator(agent_dir)


def get_overwrite_folders(agent_directory: str) -> list[str]:
    """Get folders to overwrite with configurable agent directory."""
    return [agent_directory, "frontend", "tests"]


TEMPLATE_CONFIG_FILE = "templateconfig.yaml"

# Written to the generated .env when no GCP project could be resolved.
GCP_PROJECT_PLACEHOLDER = "your-gcp-project-id"

DEPLOYMENT_TARGETS = {
    "agent_runtime": {
        "display_name": "agent_runtime",
        "description": "Agent Runtime in Gemini platform (fully managed)",
    },
    "cloud_run": {
        "display_name": "cloud_run",
        "description": "Serverless container platform",
    },
    "gke": {
        "display_name": "gke",
        "description": "Managed Kubernetes (Autopilot)",
    },
    "none": {
        "display_name": "none",
        "description": "No Cloud deployment",
    },
}


_JAVA_PACKAGE_PREFIX = "com.example"


def generate_java_package_vars(project_name: str) -> dict[str, str]:
    """Generate Java package variables from project name.

    Args:
        project_name: The project name (e.g., "my-agent", "myAgent")

    Returns:
        Dict with java_package and java_package_path
    """
    # Sanitize for Java conventions: lowercase, no hyphens, no dots
    # Java package names should be all lowercase with no separators
    sanitized = "".join(c for c in project_name.lower() if c.isalnum())

    # Remove leading digits if any
    if sanitized and sanitized[0].isdigit():
        sanitized = "_" + sanitized

    # Maven convention: a reverse-domain package, as in the ADK Java examples.
    java_package = f"{_JAVA_PACKAGE_PREFIX}.{sanitized}"

    return {
        "java_package": java_package,
        "java_package_path": java_package.replace(".", "/"),
    }


def get_available_agents(
    deployment_target: str | None = None, include_hidden: bool = False
) -> dict:
    """Dynamically load available agents from the agents directory.

    Returns agents grouped by language and framework for display purposes.
    Each agent dict includes: name, description, language, framework.

    Args:
        deployment_target: Optional deployment target to filter agents
        include_hidden: If True, include agents marked as hidden in templateconfig.
            Agents of a language whose experiment is off are never included.
    """
    # Define display order for agents within each group
    PRIORITY_ORDER = {
        "adk_python": 0,
    }

    agents_list = []
    agents_dir = pathlib.Path(__file__).parent.parent / "agents"
    # Resolved once, so a malformed AGENTS_CLI_EXPERIMENTS warns once per language.
    disabled_languages = {
        lang for lang in LANGUAGE_CONFIGS if not is_language_enabled(lang)
    }

    for agent_dir in agents_dir.iterdir():
        if agent_dir.is_dir() and not agent_dir.name.startswith("__"):
            template_config_path = agent_dir / ".template" / "templateconfig.yaml"
            if template_config_path.exists():
                try:
                    with open(template_config_path, encoding="utf-8") as f:
                        config = yaml.safe_load(f)
                    agent_name = agent_dir.name
                    settings = config.get("settings", {})

                    # Determine language (default to python)
                    language = settings.get("language", "python")

                    if language in disabled_languages:
                        continue
                    # Skip hidden agents unless explicitly requested
                    if not include_hidden and config.get("hidden", False):
                        continue

                    # Skip if deployment target specified and agent doesn't support it
                    if deployment_target:
                        targets = settings.get("deployment_targets", [])
                        if isinstance(targets, str):
                            targets = [targets]
                        if deployment_target not in targets:
                            continue

                    # Determine framework from tags
                    tags = settings.get("tags", [])
                    if "custom_a2a" in tags or "langgraph" in tags:
                        framework = "custom_a2a"
                    elif "adk" in tags:
                        framework = "adk"
                    else:
                        framework = "other"

                    description = config.get("description", "No description available")
                    display_name = config.get("display_name", agent_name)
                    priority = PRIORITY_ORDER.get(agent_name, 100)

                    agent_info = {
                        "name": agent_name,
                        "display_name": display_name,
                        "description": description,
                        "language": language,
                        "framework": framework,
                        "priority": priority,
                    }
                    agents_list.append(agent_info)
                except Exception as e:
                    logging.warning(f"Could not load agent from {agent_dir}: {e}")

    # Group by language in LANGUAGE_CONFIGS order; unknown languages sort last.
    group_order_by_language = {lang: i for i, lang in enumerate(LANGUAGE_CONFIGS)}

    def sort_key(agent: dict) -> tuple:
        group_order = group_order_by_language.get(
            agent["language"], len(group_order_by_language)
        )
        return (group_order, agent["priority"], agent["name"])

    agents_list.sort(key=sort_key)

    # Convert to numbered dictionary starting from 1
    agents = {i + 1: agent for i, agent in enumerate(agents_list)}

    return agents


def get_available_base_templates() -> list[str]:
    """Get list of available base templates for inheritance.

    Returns:
        List of base template names.
    """
    agents = get_available_agents()
    return sorted([agent_info["name"] for agent_info in agents.values()])


def validate_base_template(base_template: str) -> bool:
    """Validate that a base template exists.

    Args:
        base_template: Name of the base template to validate

    Returns:
        True if the base template exists, False otherwise
    """
    agents = get_available_agents(include_hidden=True).values()
    return resolve_agent_alias(base_template) in {agent["name"] for agent in agents}


def load_template_config(template_dir: pathlib.Path) -> dict[str, Any]:
    """Read .templateconfig.yaml file to get agent configuration."""
    config_file = template_dir / TEMPLATE_CONFIG_FILE
    if not config_file.exists():
        return {}

    try:
        with open(config_file, encoding="utf-8") as f:
            config = yaml.safe_load(f)
            return config if config else {}
    except Exception as e:
        logging.error("Could not load template config: %s", e)
        return {}


def get_agent_language(
    agent_name: str, remote_config: dict[str, Any] | None = None
) -> str:
    """Get the programming language for the selected agent.

    Args:
        agent_name: Name of the agent
        remote_config: Optional remote template configuration

    Returns:
        The template's ``settings.language``, or 'python' if it sets none.
    """
    if remote_config:
        config = remote_config
    else:
        template_path = (
            pathlib.Path(__file__).parent.parent / "agents" / agent_name / ".template"
        )
        config = load_template_config(template_path)

    if not config:
        return "python"

    return (config.get("settings") or {}).get("language") or "python"


def get_deployment_targets(
    agent_name: str, remote_config: dict[str, Any] | None = None
) -> list:
    """Get available deployment targets for the selected agent."""
    if remote_config:
        config = remote_config
    else:
        template_path = (
            pathlib.Path(__file__).parent.parent / "agents" / agent_name / ".template"
        )
        config = load_template_config(template_path)

    if not config:
        return []

    targets = config.get("settings", {}).get("deployment_targets", [])
    return targets if isinstance(targets, list) else [targets]


def prompt_deployment_target(
    agent_name: str,
    remote_config: dict[str, Any] | None = None,
    default_value: str | None = None,
) -> str:
    """Ask user to select a deployment target for the agent."""
    targets = get_deployment_targets(agent_name, remote_config=remote_config)

    if not targets:
        return ""

    default_idx = 1
    if default_value and default_value in targets:
        default_idx = targets.index(default_value) + 1

    console = Console()
    console.print("\n> Please select a deployment target:")
    console.print("\n  [bold cyan]☁️  Deployment Targets[/]")
    for idx, target in enumerate(targets, 1):
        info = DEPLOYMENT_TARGETS.get(target, {})
        display_name = info.get("display_name", target)
        description = info.get("description", "")
        if target == default_value:
            name_padded = display_name.ljust(14)
            console.print(
                f"     {idx}. [bold cyan]{name_padded}[/] [dim]{description}[/]"
                "  [dim cyan](current)[/]"
            )
        elif default_value:
            console.print(f"     [dim]{idx}. {display_name.ljust(14)} {description}[/]")
        else:
            name_padded = display_name.ljust(14)
            console.print(f"     {idx}. [bold]{name_padded}[/] [dim]{description}[/]")

    choice = IntPrompt.ask(
        "\nEnter the number of your deployment target choice",
        default=default_idx,
        show_default=True,
    )
    return targets[choice - 1]


def prompt_session_type_selection(
    default_value: str | None = None, choices: list[str] | None = None
) -> str:
    """Ask user to select a session type for Cloud Run deployment.

    ``choices`` limits the menu to the session types an agent supports.
    """
    console = Console()

    default_idx = 1
    keys = choices or list(SESSION_TYPES.keys())
    if default_value and default_value in keys:
        default_idx = keys.index(default_value) + 1

    console.print("\n> Please select a session type:")
    console.print("\n  [bold cyan]💾 Session Types[/]")
    for idx, key in enumerate(keys, 1):
        info = SESSION_TYPES[key]
        display_name = info["display_name"]
        description = info["description"]
        if key == default_value:
            name_padded = display_name.ljust(14)
            console.print(
                f"     {idx}. [bold cyan]{name_padded}[/] [dim]{description}[/]"
                "  [dim cyan](current)[/]"
            )
        elif default_value:
            console.print(f"     [dim]{idx}. {display_name.ljust(14)} {description}[/]")
        else:
            name_padded = display_name.ljust(14)
            console.print(f"     {idx}. [bold]{name_padded}[/] [dim]{description}[/]")

    choice = IntPrompt.ask(
        "\nEnter the number of your session type choice",
        default=default_idx,
        show_default=True,
    )

    return keys[choice - 1]


def prompt_cicd_runner_selection(default_value: str | None = None) -> str:
    """Ask user to select a CI/CD runner."""
    console = Console()

    cicd_runners = {
        "skip": {
            "display_name": "simple",
            "description": "No CI/CD, add later with 'enhance'",
        },
        "google_cloud_build": {
            "display_name": "google_cloud_build",
            "description": "Fully managed, GCP-integrated",
        },
        "github_actions": {
            "display_name": "github_actions",
            "description": "Workload identity federation",
        },
    }

    default_idx = 1
    keys = list(cicd_runners.keys())
    if default_value and default_value in keys:
        default_idx = keys.index(default_value) + 1

    console.print("\n> Please select a CI/CD runner:")
    console.print("\n  [bold cyan]🔧 CI/CD Options[/]")
    for idx, (key, info) in enumerate(cicd_runners.items(), 1):
        name_padded = info["display_name"].ljust(20)
        current = "  [dim](current)[/]" if key == default_value else ""
        console.print(
            f"     {idx}. [bold]{name_padded}[/] [dim]{info['description']}[/]{current}"
        )

    choice = IntPrompt.ask(
        "\nEnter the number of your CI/CD runner choice",
        default=default_idx,
        show_default=True,
    )

    return keys[choice - 1]


def get_template_path(agent_name: str) -> pathlib.Path:
    """Get the absolute path to the agent template directory."""
    current_dir = pathlib.Path(__file__).parent.parent
    template_path = current_dir / "agents" / agent_name / ".template"
    logging.debug("Looking for template in: %s", template_path)
    logging.debug("Template exists: %s", template_path.exists())
    if template_path.exists():
        logging.debug("Template contents: %s", list(template_path.iterdir()))

    if not template_path.exists():
        raise ValueError(f"Template directory not found at {template_path}")

    return template_path


def _extract_agent_garden_labels(
    agent_garden: bool,
    remote_spec: Any | None,
    remote_template_path: pathlib.Path | None,
) -> tuple[str | None, str | None]:
    """Extract agent sample ID and publisher for Agent Garden labeling.

    This function supports two mechanisms for extracting label information:
    1. From remote_spec metadata (for ADK recipes)
    2. Fallback to pyproject.toml parsing (for version-locked templates)

    Args:
        agent_garden: Whether this deployment is from Agent Garden
        remote_spec: Remote template spec with ADK recipes metadata
        remote_template_path: Path to remote template directory

    Returns:
        Tuple of (agent_sample_id, agent_sample_publisher) or (None, None) if no labels found
    """
    if not agent_garden:
        return None, None

    agent_sample_id = None
    agent_sample_publisher = None

    # Handle remote specs with ADK recipes metadata
    if (
        remote_spec
        and hasattr(remote_spec, "is_adk_recipes")
        and remote_spec.is_adk_recipes
    ):
        # For ADK recipes, template_path is like "core/python/recipe-name"
        agent_sample_id = pathlib.Path(remote_spec.template_path).name
        # For ADK recipes, publisher is always "google"
        agent_sample_publisher = "google"
        logging.debug("Detected ADK recipe from remote_spec: %s", agent_sample_id)
        return agent_sample_id, agent_sample_publisher

    # Fallback: Detect ADK recipes from pyproject.toml (for version-locked templates)
    if remote_template_path:
        pyproject_path = remote_template_path / "pyproject.toml"
        if pyproject_path.exists():
            try:
                import tomllib

                with open(pyproject_path, "rb") as toml_file:
                    pyproject_data = tomllib.load(toml_file)

                # Extract project name from pyproject.toml
                project_name_from_toml = pyproject_data.get("project", {}).get("name")

                if project_name_from_toml:
                    agent_sample_id = project_name_from_toml
                    agent_sample_publisher = "google"  # ADK recipes are from Google
                    logging.debug(
                        "Detected ADK recipe from pyproject.toml: %s", agent_sample_id
                    )
            except Exception as e:
                logging.debug("Failed to read pyproject.toml: %s", e)

    return agent_sample_id, agent_sample_publisher


def _inject_app_object_if_missing(
    agent_py_path: pathlib.Path, agent_directory: str, app_name: str, console: Console
) -> None:
    """Inject app object into agent.py if missing (backward compatibility for ADK).

    Args:
        agent_py_path: Path to the agent.py file
        agent_directory: Name of the agent directory for logging
        app_name: Name to give the injected App
        console: Rich console for user feedback
    """
    try:
        content = agent_py_path.read_text(encoding="utf-8")

        # Only inject if root_agent is defined — non-ADK agents (e.g., LangChain)
        # don't have root_agent so App(root_agent=root_agent) would be invalid.
        if not re.search(r"^\s*root_agent\s*=", content, re.MULTILINE):
            return

        # Check for app object (assignment, function definition, or import)
        app_patterns = [
            r"^\s*app\s*=",  # assignment: app = ...
            r"^\s*def\s+app\(",  # function: def app(...)
            r"from\s+.*\s+import\s+.*\bapp\b",  # import: from ... import app
        ]
        has_app = any(
            re.search(pattern, content, re.MULTILINE) for pattern in app_patterns
        )

        if not has_app:
            console.print(
                f"ℹ️  Adding 'app' object to [cyan]{agent_directory}/agent.py[/cyan] for backward compatibility",
                style="dim",
            )
            # Add import and app object at the end of the file
            content = content.rstrip()
            if "from google.adk.apps import App" not in content:
                content += "\n\nfrom google.adk.apps import App\n"
            content += f'\napp = App(root_agent=root_agent, name="{app_name}")\n'

            # Write the modified content back
            agent_py_path.write_text(content, encoding="utf-8")
    except Exception as e:
        logging.warning(
            f"Could not inject app object into {agent_directory}/agent.py: {type(e).__name__}: {e}"
        )


def _render_overlay_files(
    project_dir: pathlib.Path, rel_paths: Any, context: dict[str, Any]
) -> None:
    """Render the files a remote template opts in to with the cookiecutter context.

    The remote overlay is copied after cookiecutter runs, so placeholders in it
    are otherwise left as written. Rendering is opt-in per file because other
    remote templates may contain `{{ }}` for unrelated reasons.
    """
    # rel_paths comes straight from the template's YAML, so check its shape.
    if not isinstance(rel_paths, list) or not all(
        isinstance(rel, str) and rel for rel in rel_paths
    ):
        raise ValueError(
            "render_files in the template's .template/templateconfig.yaml must "
            'be a list of file paths, for example ["app/agent.py"].'
        )
    env = StrictEnvironment(keep_trailing_newline=True)
    root = project_dir.resolve()
    for rel in rel_paths:
        path = (project_dir / rel).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(
                f"render_files entry {rel!r} in the template's "
                ".template/templateconfig.yaml must name a file inside the "
                "template. Fix or remove the entry."
            )
        try:
            rendered = env.from_string(path.read_text(encoding="utf-8")).render(
                cookiecutter=context
            )
        except TemplateError as e:
            raise ValueError(
                f"Could not render {rel!r}, listed in render_files in the "
                f"template's .template/templateconfig.yaml: {e}. Fix the Jinja "
                "in that file or remove it from render_files."
            ) from e
        path.write_text(rendered, encoding="utf-8")
        logging.debug("Rendered remote template file %s", rel)


def _generate_yaml_agent_shim(
    agent_py_path: pathlib.Path,
    agent_directory: str,
    app_name: str,
    console: Console,
    force: bool = False,
) -> None:
    """Generate agent.py shim for YAML config agents.

    When a root_agent.yaml is detected, this function generates an agent.py
    that loads the YAML config and exposes the root_agent and app objects
    required by the deployment pipeline.

    Args:
        agent_py_path: Path where agent.py should be created/updated
        agent_directory: Name of the agent directory for logging
        app_name: Name to give the App
        console: Rich console for user feedback
        force: If True, overwrite existing agent.py even if it has root_agent defined.
               Used when the user explicitly has a root_agent.yaml.
    """
    root_agent_yaml = agent_py_path.parent / "root_agent.yaml"

    if not root_agent_yaml.exists():
        return

    # Check if agent.py already exists and has root_agent defined
    if agent_py_path.exists() and not force:
        try:
            content = agent_py_path.read_text(encoding="utf-8")
            if re.search(r"^\s*root_agent\s*=", content, re.MULTILINE):
                logging.debug(
                    "%s/agent.py already has root_agent defined", agent_directory
                )
                return
        except Exception as e:
            logging.warning(f"Could not read existing agent.py: {e}")

    console.print(
        f"ℹ️  Generating [cyan]{agent_directory}/agent.py[/cyan] shim for YAML config agent",
        style="dim",
    )

    shim_content = f'''"""Agent module that loads the YAML config agent.

This file is auto-generated to provide compatibility with the deployment pipeline.
Edit root_agent.yaml to modify your agent configuration.
"""

from pathlib import Path

from google.adk.agents import config_agent_utils
from google.adk.apps import App

_AGENT_DIR = Path(__file__).parent
root_agent = config_agent_utils.from_config(str(_AGENT_DIR / "root_agent.yaml"))
app = App(root_agent=root_agent, name="{app_name}")
'''

    try:
        agent_py_path.write_text(shim_content, encoding="utf-8")
        logging.debug("Generated YAML agent shim at %s", agent_py_path)
    except Exception as e:
        logging.warning(
            f"Could not generate YAML agent shim at {agent_py_path}: {type(e).__name__}: {e}"
        )


def should_keep_single_project_terraform(
    deployment_target: str | None,
) -> bool:
    """Decide whether to preserve `deployment/terraform/single-project/` (and `shared/`)
    when scaffolding without CI/CD.

    Kept when a real deployment target was selected (anything but `none`).
    """
    return deployment_target != "none"


def apply_prototype_deployment_cleanup(
    deployment_dir: pathlib.Path,
    deployment_target: str | None,
) -> None:
    """Prune `deployment/` after scaffolding in prototype/minimal mode (`cicd_runner == "skip"`).

    Either preserves `terraform/{single-project,shared,scripts}` plus `k8s/`
    (and removes everything else under `deployment/`), or removes the whole
    `deployment/` dir — based on `should_keep_single_project_terraform`.
    No-op if `deployment_dir` does not exist.
    """
    if not deployment_dir.exists():
        return

    keep = should_keep_single_project_terraform(
        deployment_target=deployment_target,
    )
    if not keep:
        shutil.rmtree(deployment_dir)
        logging.debug("Prototype mode: deleted %s", deployment_dir)
        return

    # Keep single-project/ + shared/ (observability: telemetry.tf, BigQuery schemas); drop cicd/
    terraform_dir = deployment_dir / "terraform"
    terraform_dirs_to_keep = {"single-project", "shared", "scripts"}
    if terraform_dir.exists():
        for item in terraform_dir.iterdir():
            if item.name not in terraform_dirs_to_keep:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
    # Remove non-terraform, non-k8s deployment files
    deployment_dirs_to_keep = {"terraform", "k8s"}
    for item in deployment_dir.iterdir():
        if item.name not in deployment_dirs_to_keep:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
    logging.debug(
        "Prototype mode: preserved deployment/terraform/single-project/ and shared/, removed cicd/ in %s",
        deployment_dir,
    )


def _remove_unselected_files(
    root: pathlib.Path,
    conditional_config: dict[str, Any],
    agent_directory: str,
    cicd_runner: str | None,
    deployment_target: str | None,
) -> None:
    """Delete the rendered files this project's options don't select.

    Conditional files the options turn off, then (with no CI/CD runner) the
    deployment extras and load tests a prototype doesn't use.
    """
    import glob

    apply_conditional_files(root, conditional_config, agent_directory)

    # Clean up unused_* files and directories created by conditional templates
    for pattern in (root / "unused_*", root / "**" / "unused_*"):
        for unused_path_str in glob.glob(str(pattern), recursive=True):
            unused_path = pathlib.Path(unused_path_str)
            if unused_path.exists():
                if unused_path.is_dir():
                    shutil.rmtree(unused_path)
                    logging.debug("Deleted unused directory: %s", unused_path)
                else:
                    unused_path.unlink()
                    logging.debug("Deleted unused file: %s", unused_path)

    # Clean up additional files for prototype/minimal mode (cicd_runner == "skip")
    if cicd_runner == "skip":
        apply_prototype_deployment_cleanup(
            deployment_dir=root / "deployment",
            deployment_target=deployment_target,
        )

        # Remove load_test folder
        load_test_dir = root / "tests" / "load_test"
        if load_test_dir.exists():
            shutil.rmtree(load_test_dir)
            logging.debug("Prototype mode: deleted %s", load_test_dir)


# Where enhance moves the user's files that the template replaces, so the coding
# agent migrating the project can port them. The base .gitignore and
# .gcloudignore exclude it.
_ORIGINALS_DIR = ".agents-cli-original"

# The user's rules in these are appended to the template's version, so a file
# only the user's rules ignore (a secret, say) stays ignored.
_IGNORE_FILES = frozenset({".gitignore", ".dockerignore", ".gcloudignore"})


def _move_aside_replaced_files(
    generated_dir: pathlib.Path, destination: pathlib.Path, console: Console
) -> None:
    """Move the destination's files that `generated_dir` replaces into _ORIGINALS_DIR.

    Each keeps its path relative to the project root. Files the template doesn't
    have stay where they are, so the user's code still imports. Ignore files are
    also merged into the template's version (see _IGNORE_FILES).
    """
    originals = destination / _ORIGINALS_DIR
    if originals.exists():
        raise ValueError(
            f"{_ORIGINALS_DIR}/ already exists, left by an earlier enhance. Port "
            "what you need from it and delete it, then re-run enhance."
        )
    moved: list[str] = []
    merged: list[str] = []
    for src in sorted(generated_dir.rglob("*")):
        relative = src.relative_to(generated_dir)
        dst = destination / relative
        if not (dst.exists() or dst.is_symlink()):
            continue
        # Real directories on both sides merge. Anything else at a template path
        # (a file, or an entry of the other type) is moved aside whole, so the
        # copy that follows never fails halfway or writes into it.
        if src.is_dir() and dst.is_dir() and not dst.is_symlink():
            continue
        if src.name in _IGNORE_FILES and src.is_file() and dst.is_file():
            template_rules = src.read_bytes().rstrip(b"\r\n")
            src.write_bytes(
                template_rules
                + f"\n\n# Rules from your previous {src.name}\n".encode()
                + dst.read_bytes()
            )
            merged.append(relative.as_posix())
        target = originals / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink() and dst.is_file():
            # A relative link would dangle once moved; keep what it points to.
            shutil.copy2(dst, target)
            dst.unlink()
        else:
            dst.rename(target)
        moved.append(relative.as_posix())
    if not moved:
        return
    console.print(
        f"\nMoved your files the template replaces to {_ORIGINALS_DIR}/: "
        f"{', '.join(moved)}. Port your agent, dependencies and anything else "
        "you need from there, then delete the folder.",
        style="bold yellow",
    )
    if merged:
        console.print(
            f"Added your rules from {', '.join(merged)} to the template's version.",
            style="bold yellow",
        )


def process_template(
    *,
    agent_name: str,
    template_dir: pathlib.Path,
    project_name: str,
    deployment_target: str | None = None,
    cicd_runner: str | None = None,
    session_type: str | None = None,
    output_dir: pathlib.Path | None = None,
    remote_template_path: pathlib.Path | None = None,
    template_repo_root: pathlib.Path | None = None,
    remote_config: dict[str, Any] | None = None,
    in_folder: bool = False,
    overlay_is_project: bool = False,
    keep_existing_files: bool = False,
    cli_overrides: dict[str, Any] | None = None,
    agent_garden: bool = False,
    remote_spec: Any | None = None,
    recorded_base_template: str | None = None,
    google_cloud_project: str | None = None,
    bq_analytics: bool = False,
    agent_gateway: bool = False,
    agent_runtime_identity_default: str = SERVICE_ACCOUNT,
    cloud_run_identity_default: str = SERVICE_ACCOUNT,
    agent_guidance_filename: str = "GEMINI.md",
    root_agent_name: str = "",
    app_name: str = "",
) -> list[pathlib.Path] | None:
    """Process the template directory and create a new project.

    Args:
        agent_name: Name of the agent template to use
        template_dir: Directory containing the template files
        project_name: Name of the project to create
        deployment_target: Optional deployment target (agent_runtime or cloud_run)
        cicd_runner: Optional CI/CD runner to use
        session_type: Optional session type for cloud_run deployment
        output_dir: Optional output directory path, defaults to current directory
        remote_template_path: Optional path to remote template for overlay
        template_repo_root: Root of the fetched template's repository (the git clone).
        in_folder: Whether to template directly into the output directory instead of creating a subdirectory
        overlay_is_project: True when the overlay source is the output project
            itself (--agent local@.), so its agents-cli-manifest.yaml is the
            project's own and is copied. False for any other source, whose
            manifest describes the template and is skipped.
        keep_existing_files: With in_folder, move the destination's files the
            template replaces into .agents-cli-original/ instead of overwriting
            them, and never replace whole directories. For a project agents-cli
            did not create.
        cli_overrides: Optional CLI override values that should take precedence over template config
        agent_garden: Whether this deployment is from Agent Garden
        google_cloud_project: Optional GCP project ID to populate .env file
        bq_analytics: Whether to include BigQuery Agent Analytics Plugin
        agent_gateway: Whether the Dockerfile should trust the Agent Gateway root CA

    Returns:
        With keep_existing_files, the rendered files copied into the output
        directory, relative to it, so later steps can leave the user's own files
        alone. None otherwise.
    """
    logging.debug("Processing template from %s", template_dir)
    logging.debug("Project name: %s", project_name)
    logging.debug("Output directory: %s", output_dir)

    # Create console for user feedback
    console = Console()

    def get_configured_agent_directory(
        template_config: dict[str, Any],
        cli_overrides: dict[str, Any] | None,
        language: str,
    ) -> str:
        """Like get_agent_directory, but returns ``FLAT_AGENT_DIRECTORY`` unmapped."""
        cli_settings = cli_overrides.get("settings") if cli_overrides else None
        if isinstance(cli_settings, dict) and cli_settings.get("agent_directory"):
            return cli_settings["agent_directory"]
        return (template_config.get("settings") or {}).get(
            "agent_directory"
        ) or get_language_config(language)["default_agent_directory"]

    def get_agent_directory(
        template_config: dict[str, Any],
        cli_overrides: dict[str, Any] | None = None,
        language: str = "python",
    ) -> str:
        """Get agent directory with CLI override support.

        Handles the special case where agent_directory is "." (flat structure),
        deriving the target directory name from the remote template folder name.
        """
        agent_dir = get_configured_agent_directory(
            template_config, cli_overrides, language
        )

        # Handle "." (flat structure) - derive target from folder name
        if agent_dir == FLAT_AGENT_DIRECTORY:
            if language == "go":
                # Go agent code is a package next to main.go, so a flat Go
                # template lands in app/ like the built-in adk_go template.
                agent_dir = "app"
            elif remote_template_path:
                # Derive from remote template folder name
                folder_name = remote_template_path.name.replace("-", "_")
                logging.debug(
                    "Flat structure (-dir .): deriving target '%s' from folder name",
                    folder_name,
                )
                agent_dir = folder_name
            else:
                # Fallback to "app" for non-remote templates
                logging.debug("Flat structure (-dir .): using 'app' as fallback")
                agent_dir = "app"

        # Validate agent directory is valid for the language
        validate_agent_directory_name(agent_dir, language=language)

        return agent_dir

    # Handle remote vs local templates
    is_remote = remote_template_path is not None

    if is_remote:
        # For remote templates, determine the base template
        base_template_name = get_base_template_name(remote_config or {})
        agent_path = pathlib.Path(__file__).parent.parent / "agents" / base_template_name
        logging.debug("Remote template using base: %s", base_template_name)
    elif cli_overrides and cli_overrides.get("base_template"):
        # For in-folder mode with base_template override, use the agent template
        base_template_name = cli_overrides["base_template"]
        agent_path = pathlib.Path(__file__).parent.parent / "agents" / base_template_name
        logging.debug("Using base template override: %s", base_template_name)
    else:
        # For local templates, use the existing logic
        base_template_name = (
            agent_name  # agent_name is the base template for local templates
        )
        agent_path = template_dir.parent  # Get parent of template dir

    logging.debug("agent path: %s", agent_path)
    if not agent_path.exists():
        # Fail here rather than carry on: every later use of agent_path is guarded
        # by exists(), so an unresolvable base silently copies no agent layer and
        # produces a half-built project.
        raise ValueError(
            f"Base template '{base_template_name}' not found at {agent_path}. "
            "A template declares its base in .template/templateconfig.yaml; "
            f"'{base_template_name}' is not one this CLI version provides."
        )
    logging.debug("agent path contents: %s", list(agent_path.iterdir()))

    # Resolve template config once.
    if remote_config:
        template_config = remote_config
    else:
        template_config = load_template_config(pathlib.Path(template_dir))

    if not template_config:
        raise ValueError("Could not load template config")

    # Use provided output_dir or current directory
    destination_dir = output_dir if output_dir else pathlib.Path.cwd()

    # Create output directory if it doesn't exist
    if not destination_dir.exists():
        destination_dir.mkdir(parents=True)

    rendered_files: list[pathlib.Path] | None = None
    # Create a new temporary directory and use it as our working directory
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = pathlib.Path(temp_dir)

        # Important: Store the original working directory
        original_dir = pathlib.Path.cwd()

        try:
            os.chdir(temp_path)  # Change to temp directory

            # Extract agent sample info for labeling when using agent garden with remote templates
            agent_sample_id, agent_sample_publisher = _extract_agent_garden_labels(
                agent_garden, remote_spec, remote_template_path
            )

            # Create the cookiecutter template structure
            cookiecutter_template = temp_path / "template"
            cookiecutter_template.mkdir(parents=True)
            project_template = cookiecutter_template / "{{cookiecutter.project_name}}"
            project_template.mkdir(parents=True)

            # Get agent directory and language from the already-loaded template_config
            language = get_agent_language(agent_name, template_config)
            lang_config = get_language_config(language)
            agent_directory = get_agent_directory(
                template_config, cli_overrides, language
            )

            # Base paths for template structure
            base_templates_path = pathlib.Path(__file__).parent.parent / "base_templates"

            # 1. First copy shared base template files (language-agnostic)
            shared_base_path = base_templates_path / "_shared"
            if shared_base_path.exists():
                copy_files(
                    shared_base_path,
                    project_template,
                    options=CopyOptions(overwrite=True),
                )
                logging.debug("1a. Copied shared base template from %s", shared_base_path)

            # 1b. Copy language-specific base template files
            language_base_path = base_templates_path / language
            if language_base_path.exists():
                copy_files(
                    language_base_path,
                    project_template,
                    options=CopyOptions(overwrite=True),
                )
                logging.debug(
                    "1b. Copied %s base template from %s", language, language_base_path
                )
            else:
                raise FileNotFoundError(
                    f"Language base template not found: {language_base_path}"
                )

            # 2. Process deployment target if specified
            if deployment_target and deployment_target in DEPLOYMENT_TARGETS:
                deployment_targets_path = (
                    pathlib.Path(__file__).parent.parent / "deployment_targets"
                )

                # 2a. Copy shared deployment target files (language-agnostic)
                shared_deployment_path = (
                    deployment_targets_path / deployment_target / "_shared"
                )
                if shared_deployment_path.exists():
                    copy_files(
                        shared_deployment_path,
                        project_template,
                        options=CopyOptions(overwrite=True),
                    )
                    logging.debug(
                        "2a. Copied shared deployment files from %s",
                        shared_deployment_path,
                    )

                # 2b. Copy language-specific deployment target files
                language_deployment_path = (
                    deployment_targets_path / deployment_target / language
                )
                if language_deployment_path.exists():
                    copy_files(
                        language_deployment_path,
                        project_template,
                        options=CopyOptions(overwrite=True),
                    )
                    logging.debug(
                        "2b. Copied %s deployment files from %s",
                        language,
                        language_deployment_path,
                    )

            # 4. Skip remote template files during cookiecutter processing
            # Remote files will be copied after cookiecutter to avoid Jinja conflicts
            if is_remote and remote_template_path:
                logging.debug(
                    "4. Skipping remote template files during cookiecutter processing - will copy after templating"
                )

            # Validate deployment target
            available_targets = template_config.get("settings", {}).get(
                "deployment_targets", []
            )
            if isinstance(available_targets, str):
                available_targets = [available_targets]

            if deployment_target and deployment_target not in available_targets:
                raise ValueError(
                    f"Invalid deployment target '{deployment_target}'. Available targets: {available_targets}"
                )

            # Process frontend files (after config is properly loaded with CLI overrides)
            frontend_type = template_config.get("settings", {}).get(
                "frontend_type", DEFAULT_FRONTEND
            )
            copy_frontend_files(frontend_type, project_template)
            logging.debug("5. Processed frontend files for type: %s", frontend_type)

            # 6. Copy agent-specific files to override base template (using final config)
            if agent_path.exists():
                agent_directory = get_agent_directory(
                    template_config, cli_overrides, language
                )

                # For remote/local templates with base_template override, always use "app"
                # as the source directory since base templates store agent code in "app/"
                if is_remote or (cli_overrides and cli_overrides.get("base_template")):
                    template_agent_directory = "app"
                else:
                    # Get the template's default agent directory (usually "app")
                    template_agent_directory = template_config.get("settings", {}).get(
                        "agent_directory", "app"
                    )

                # Copy agent directory (always from "app" to target directory)
                source_agent_folder = agent_path / template_agent_directory
                logging.debug(
                    "6. Source agent folder: %s, exists: %s",
                    source_agent_folder,
                    source_agent_folder.exists(),
                )
                target_agent_folder = project_template / agent_directory
                if source_agent_folder.exists():
                    logging.debug(
                        "6. Copying agent folder %s -> %s with override",
                        template_agent_directory,
                        agent_directory,
                    )
                    copy_files(
                        source_agent_folder,
                        target_agent_folder,
                        options=CopyOptions(overwrite=True),
                    )

                # Language-specific folders outside the agent directory, such
                # as Java's src/test.
                for folder in lang_config["extra_template_folders"]:
                    source_extra_folder = agent_path / folder
                    if source_extra_folder.exists():
                        logging.debug("6b. Copying %s folder with override", folder)
                        copy_files(
                            source_extra_folder,
                            project_template / folder,
                            options=CopyOptions(overwrite=True),
                        )

                # Copy other folders (frontend, tests, notebooks, deployment)
                other_folders = [
                    "frontend",
                    "tests",
                    "deployment",
                ]
                for folder in other_folders:
                    agent_folder = agent_path / folder
                    project_folder = project_template / folder
                    if agent_folder.exists():
                        logging.debug("6. Copying %s folder with override", folder)
                        copy_files(
                            agent_folder,
                            project_folder,
                            options=CopyOptions(overwrite=True),
                        )

                # 6c. Overlay any remaining top-level files/dirs the agent ships
                # (e.g. a Go agent's main.go/go.mod/Dockerfile, extra CI, e2e/) so
                # a framework agent can own the whole project, not just its agent
                # directory. No-op for agents that only ship an agent directory.
                already_copied = {
                    ".template",
                    "__pycache__",
                    # First component of the agent dir — "app" (python/ts),
                    # "agent" (go), or "src" (java's src/main/java, whose
                    # src/main + src/test are handled by the copies above).
                    template_agent_directory.split("/")[0],
                    *other_folders,
                }
                for item in agent_path.iterdir():
                    if item.name in already_copied:
                        continue
                    logging.debug("6c. Overlaying agent-owned %s", item.name)
                    copy_files(
                        item,
                        project_template / item.name,
                        options=CopyOptions(overwrite=True),
                    )

            # Create cookiecutter.json in the template root
            # Get settings from template config
            settings = template_config.get("settings", {})
            extra_deps = settings.get("extra_dependencies", [])
            frontend_type = settings.get("frontend_type", DEFAULT_FRONTEND)
            tags = settings.get("tags", ["None"])

            java_vars = (
                generate_java_package_vars(project_name)
                if lang_config["jvm_package_layout"]
                else {}
            )

            current_version = get_current_version()

            go_template_path = (
                remote_template_path
                if go_overlay.applies_to(language, overlay_is_project=overlay_is_project)
                else None
            )

            cookiecutter_config = {
                "project_name": project_name,
                "agent_name": agent_name,
                # The name of the root agent could be different than the
                # project name, because of character set restrictions.
                "root_agent_name": root_agent_name
                or derive_root_agent_name(project_name),
                "package_version": current_version,
                "generated_at": datetime.now(tz=UTC).isoformat(),
                "default_model": DEFAULT_MODEL,
                "agent_description": template_config.get("description", ""),
                "example_question": template_config.get("example_question", "").ljust(61),
                "settings": settings,
                "tags": tags,
                "is_a2a": "a2a" in tags,
                "framework": "google-adk" if "adk" in tags else "custom",
                "language": language,
                "otel_capture_message_content_default": lang_config[
                    "otel_capture_message_content_default"
                ],
                "genai_backend_env_var": lang_config["genai_backend_env_var"],
                "supports_bq_analytics": lang_config["supports_bq_analytics"],
                # Lists are wrapped because cookiecutter treats a list as a
                # choice and would keep only its first element.
                "ci_trigger_files": [lang_config["ci_trigger_files"]],
                "gke_prestop_command": [lang_config["gke_prestop_command"]],
                "deployment_target": deployment_target or "",
                "cicd_runner": cicd_runner or "google_cloud_build",
                "session_type": session_type or "",
                "frontend_type": frontend_type,
                "extra_dependencies": [extra_deps],
                # What upgrade and enhance must re-fetch: the spec this project
                # was created from, or the bundled template name.
                "recorded_base_template": recorded_base_template or agent_name,
                # Group-scoped deps ([project.optional-dependencies]); mirrored
                # in generate_locks so a rendered lock matches a rendered project.
                "extra_optional_dependencies": settings.get(
                    "extra_optional_dependencies", {}
                ),
                "agent_directory": agent_directory,
                "agent_package": agent_package_name(agent_directory),
                # A JVM agent directory is a source root, not a package, so its
                # basename is no app name.
                "app_name": app_name
                or (
                    "app"
                    if lang_config["jvm_package_layout"]
                    else agent_package_name(agent_directory)
                ),
                "agent_garden": agent_garden,
                "agent_sample_id": agent_sample_id or "",
                "agent_sample_publisher": agent_sample_publisher or "",
                "google_cloud_project": google_cloud_project or GCP_PROJECT_PLACEHOLDER,
                # Java package variables (only populated for Java projects)
                "java_package": java_vars.get("java_package", ""),
                "java_package_path": java_vars.get("java_package_path", ""),
                "bq_analytics": bq_analytics,
                "agent_gateway": agent_gateway,
                "agent_runtime_identity_default": agent_runtime_identity_default,
                "cloud_run_identity_default": cloud_run_identity_default,
                "agent_identity_enabled": _agent_identity_enabled(
                    deployment_target,
                    agent_runtime_identity_default=agent_runtime_identity_default,
                    cloud_run_identity_default=cloud_run_identity_default,
                ),
                "wheel_gcs_uri": os.environ.get("WHEEL_GCS_URI", ""),
                "agents_cli_version_pin": agents_cli_version_pin(),
                "agent_guidance_filename": agent_guidance_filename,
                # Wrapped like extra_dependencies so cookiecutter keeps the list.
                "extra_go_package_dirs": [
                    go_overlay.package_dirs(go_template_path, agent_directory)
                    if go_template_path
                    else []
                ],
                "_copy_without_render": [
                    "*.ipynb",  # Don't render notebooks
                    "*.sum",  # Don't render Go sum files
                    "e2e/**/*",  # Don't render Go e2e test files (contain Go {{ }} syntax)
                    "appinfo/*",  # Don't render Go app-info files (contain Go {{ }} syntax)
                    "frontend/**/*",  # Don't render frontend directory (covers all JS/TS/CSS/JSON files)
                    "sample_data/*",  # Don't render sample data files
                    ".git/*",  # Don't render git directory
                    "__pycache__/*",  # Don't render cache
                    "**/__pycache__/*",
                    ".pytest_cache/*",
                    ".venv/*",
                    "**/.venv/*",  # Don't render .venv at any depth
                    "node_modules/**/*",  # Don't render node_modules (TS/JS deps contain {{ }} syntax)
                    "*templates.py",  # Don't render templates files
                ],
            }

            with open(
                cookiecutter_template / "cookiecutter.json", "w", encoding="utf-8"
            ) as json_file:
                json.dump(cookiecutter_config, json_file, indent=4)

            logging.debug("Template structure created at %s", cookiecutter_template)
            logging.debug("Directory contents: %s", list(cookiecutter_template.iterdir()))

            # Process the template
            cookiecutter(
                str(cookiecutter_template),
                no_input=True,
                overwrite_if_exists=True,
                extra_context={
                    "project_name": project_name,
                    "agent_name": agent_name,
                },
            )
            logging.debug("Template processing completed successfully")

            # Now overlay remote template files if present (after cookiecutter processing)
            if is_remote and remote_template_path:
                generated_project_dir = temp_path / project_name
                logging.debug(
                    "Copying remote template files from %s to %s",
                    remote_template_path,
                    generated_project_dir,
                )

                is_flat_structure = bool(template_config.get("is_flat_structure")) or (
                    get_configured_agent_directory(
                        template_config, cli_overrides, language
                    )
                    == FLAT_AGENT_DIRECTORY
                )
                scaffold_root_files: frozenset[str] = frozenset()
                if go_template_path:
                    scaffold_root_files = go_overlay.skipped_root_files(
                        go_template_path, is_flat_structure=is_flat_structure
                    )
                    go_overlay.clear_overlaid_packages(
                        go_template_path,
                        generated_project_dir,
                        flat_agent_directory=agent_directory
                        if is_flat_structure
                        else None,
                    )

                if is_flat_structure:
                    logging.debug(
                        "Flat structure detected: copying files to %s/", agent_directory
                    )
                    copy_flat_structure_agent_files(
                        remote_template_path,
                        generated_project_dir,
                        agent_directory,
                        agent_extensions=frozenset(
                            {".go"} if go_template_path else {".py"}
                        ),
                        clone_root=template_repo_root,
                        options=CopyOptions(
                            guidance_filename=agent_guidance_filename,
                            skip_root_files=scaffold_root_files,
                        ),
                    )
                    if go_template_path:
                        go_overlay.rename_flat_package(
                            generated_project_dir / agent_directory
                        )
                else:
                    # Standard structure: copy as-is
                    copy_dir_contents(
                        remote_template_path,
                        generated_project_dir,
                        clone_root=template_repo_root,
                        options=CopyOptions(
                            overwrite=True,
                            skip_manifest=not overlay_is_project,
                            guidance_filename=agent_guidance_filename,
                            skip_root_files=scaffold_root_files,
                        ),
                    )
                    render_files = (remote_config or {}).get("render_files") or []
                    if render_files and not overlay_is_project:
                        _render_overlay_files(
                            generated_project_dir, render_files, cookiecutter_config
                        )
                if go_template_path:
                    go_overlay.merge_module(
                        generated_project_dir, go_template_path, template_repo_root
                    )
                logging.debug("Remote template files copied successfully")

                # ADK agent compatibility: prefer YAML shim, otherwise inject
                # the app object into agent.py if missing.
                agent_py_path = generated_project_dir / agent_directory / "agent.py"
                root_agent_yaml = (
                    generated_project_dir / agent_directory / "root_agent.yaml"
                )

                # Only for ADK: the shim imports google.adk, which a project on
                # the empty base does not depend on.
                if "adk" in tags:
                    if root_agent_yaml.exists():
                        _generate_yaml_agent_shim(
                            agent_py_path,
                            agent_directory,
                            cookiecutter_config["app_name"],
                            console,
                        )
                    elif agent_py_path.exists():
                        _inject_app_object_if_missing(
                            agent_py_path,
                            agent_directory,
                            cookiecutter_config["app_name"],
                            console,
                        )

            # Move the generated project to the final destination
            generated_project_dir = temp_path / project_name
            agent_directory = get_agent_directory(
                template_config, cli_overrides, language
            )
            # Apply conditional file logic (Windows-compatible replacement for Jinja2 filenames)
            conditional_config = {
                "agent_name": agent_name,
                "deployment_target": deployment_target,
                "cicd_runner": cicd_runner or "google_cloud_build",
                "is_a2a": "a2a" in tags,
                "template_owns_e2e": bool(
                    remote_template_path
                    and (
                        remote_template_path
                        / "tests"
                        / "integration"
                        / "test_server_e2e.py"
                    ).is_file()
                ),
            }

            def cleanup_rendered(root: pathlib.Path) -> None:
                _remove_unselected_files(
                    root,
                    conditional_config,
                    agent_directory,
                    cicd_runner,
                    deployment_target,
                )

            if in_folder:
                # For in-folder mode, copy files directly to the destination directory
                final_destination = destination_dir
                logging.debug(
                    "In-folder mode: copying files from %s to %s",
                    generated_project_dir,
                    final_destination,
                )

                if generated_project_dir.exists() and keep_existing_files:
                    # The destination is the user's own project: drop what this
                    # render doesn't select in the staged copy, so the cleanup
                    # never reaches the user's .github/, deployment/ or tests/,
                    # and copy file by file rather than replacing directories.
                    cleanup_rendered(generated_project_dir)
                    _move_aside_replaced_files(
                        generated_project_dir, final_destination, console
                    )
                    rendered_files = [
                        p.relative_to(generated_project_dir)
                        for p in generated_project_dir.rglob("*")
                        if p.is_file()
                    ]
                    shutil.copytree(
                        generated_project_dir, final_destination, dirs_exist_ok=True
                    )
                elif generated_project_dir.exists():
                    # Copy all files from generated project to destination directory
                    for item in generated_project_dir.iterdir():
                        dest_item = final_destination / item.name

                        if item.is_dir():
                            if dest_item.exists():
                                shutil.rmtree(dest_item)
                            shutil.copytree(item, dest_item, dirs_exist_ok=True)
                        else:
                            shutil.copy2(item, dest_item)
                    logging.debug(
                        "Project files successfully copied to %s", final_destination
                    )
            else:
                # Standard mode: create project subdirectory
                final_destination = destination_dir / project_name
                logging.debug(
                    "Standard mode: moving project from %s to %s",
                    generated_project_dir,
                    final_destination,
                )

                if generated_project_dir.exists():
                    if final_destination.exists():
                        shutil.rmtree(final_destination)

                    shutil.copytree(
                        generated_project_dir, final_destination, dirs_exist_ok=True
                    )

                    logging.debug("Project successfully created at %s", final_destination)

            # Always check if the project was successfully created before proceeding
            if not final_destination.exists():
                logging.error(
                    f"Final destination directory not found at {final_destination}"
                )
                raise FileNotFoundError(
                    f"Final destination directory not found at {final_destination}"
                )

            # Handle YAML config agents for in-folder mode
            # This runs after all files have been copied to the final destination
            # Use force=True because the user's root_agent.yaml takes precedence
            # over the base template's agent.py. Not for a project agents-cli
            # didn't create: a root_agent.yaml there is the user's, not the
            # template's agent.
            if in_folder and not keep_existing_files:
                final_agent_py_path = final_destination / agent_directory / "agent.py"
                final_root_agent_yaml = (
                    final_destination / agent_directory / "root_agent.yaml"
                )
                if final_root_agent_yaml.exists():
                    _generate_yaml_agent_shim(
                        final_agent_py_path,
                        agent_directory,
                        cookiecutter_config["app_name"],
                        console,
                        force=True,
                    )

            if not keep_existing_files:
                cleanup_rendered(final_destination)

            # Handle pyproject.toml and uv.lock files (Python only)
            if language == "python":
                if is_remote and remote_template_path:
                    # For remote templates, use their pyproject.toml and uv.lock if they exist
                    remote_pyproject = remote_template_path / "pyproject.toml"
                    remote_uv_lock = remote_template_path / "uv.lock"

                    if remote_pyproject.exists():
                        shutil.copy2(
                            remote_pyproject, final_destination / "pyproject.toml"
                        )
                        logging.debug("Used pyproject.toml from remote template")

                    if remote_uv_lock.exists():
                        shutil.copy2(remote_uv_lock, final_destination / "uv.lock")
                        logging.debug("Used uv.lock from remote template")

                    # A framework template's pyproject names the template, so
                    # rename the package to the project, as the ADK base does.
                    if (
                        remote_pyproject.exists()
                        and not overlay_is_project
                        and is_empty_agent(base_template_name)
                    ):
                        _rename_package(final_destination, project_name)
                elif (
                    deployment_target
                    and deployment_target != "none"
                    and not is_empty_agent(agent_name)
                ):
                    # For local templates, use the existing logic. The empty
                    # templates ship no lock; a framework template brings its own.
                    lock_path = (
                        pathlib.Path(__file__).parent.parent
                        / "resources"
                        / "locks"
                        / get_lock_filename(agent_name, deployment_target)
                    )
                    logging.debug("Looking for lock file at: %s", lock_path)
                    logging.debug("Lock file exists: %s", lock_path.exists())
                    if not lock_path.exists():
                        raise FileNotFoundError(f"Lock file not found: {lock_path}")
                    # Copy and rename to uv.lock in the project directory
                    shutil.copy2(lock_path, final_destination / "uv.lock")
                    logging.debug(
                        "Copied lock file from %s to %s/uv.lock",
                        lock_path,
                        final_destination,
                    )

                    # Replace cookiecutter project name with actual project name in lock file
                    lock_file_path = final_destination / "uv.lock"
                    with open(lock_file_path, "r+", encoding="utf-8") as lock_file:
                        content = lock_file.read()
                        lock_file.seek(0)
                        lock_file.write(
                            content.replace("{{cookiecutter.project_name}}", project_name)
                        )
                        lock_file.truncate()
                    logging.debug(
                        "Updated project name in lock file at %s", lock_file_path
                    )

        finally:
            # Always restore the original working directory
            os.chdir(original_dir)

    return rendered_files


def _rename_package(project_dir: pathlib.Path, project_name: str) -> None:
    """Rename the package in pyproject.toml and uv.lock to the project's name.

    The lock records the package under the old name, so it is renamed too or
    `uv sync --locked` reports the lock out of date.
    """
    pyproject_path = project_dir / "pyproject.toml"
    doc = tomlkit.parse(pyproject_path.read_text(encoding="utf-8"))
    project = doc.get("project")
    if not isinstance(project, dict) or not isinstance(project.get("name"), str):
        return
    old_name = project["name"]
    project["name"] = project_name
    pyproject_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    lock_path = project_dir / "uv.lock"
    if lock_path.exists():
        # uv records package names normalized, per PEP 503.
        lock = lock_path.read_text(encoding="utf-8")
        lock_path.write_text(
            lock.replace(
                f'name = "{canonicalize_name(old_name)}"',
                f'name = "{canonicalize_name(project_name)}"',
            ),
            encoding="utf-8",
        )


def _agent_identity_enabled(
    deployment_target: str | None,
    *,
    agent_runtime_identity_default: str,
    cloud_run_identity_default: str,
) -> bool:
    """Whether the deployed agent runs as its own Agent Identity principal."""
    defaults = {
        "agent_runtime": agent_runtime_identity_default,
        "cloud_run": cloud_run_identity_default,
    }
    return defaults.get(deployment_target or "") == AGENT_IDENTITY
