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

"""Shared language configuration and utilities for CLI commands.

This module centralizes language-specific configuration (Python, Go, Java, TypeScript)
used by enhance and upgrade commands. It provides:

- LANGUAGE_CONFIGS: Configuration dict for each supported language
- get_language_config(): Get config dict for a language
"""

import logging
import pathlib
import tomllib
from collections.abc import Callable, Mapping
from typing import Any

import click

from google.agents.cli._experiments import resolve_experiment


def _read_python_version(root: pathlib.Path) -> str:
    """Return the ``[project].version`` from ``pyproject.toml`` or raise."""
    pyproject_path = root / "pyproject.toml"
    if not pyproject_path.exists():
        raise FileNotFoundError(f"pyproject.toml not found in {root}")
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)  # PEP 621 [project] section
    version = data.get("project", {}).get("version")
    if version and isinstance(version, str):
        return version
    raise KeyError(f"no [project].version in {pyproject_path}")


# =============================================================================
# Language Configuration
# =============================================================================
# To add a new language, add an entry with the required keys.


LANGUAGE_CONFIGS: dict[str, dict[str, Any]] = {
    "python": {
        "lock_file": "uv.lock",
        "lock_command": ["uv", "lock"],
        "lock_command_name": "uv lock",
        "display_name": "Python",
        # Header icon in the `create` agent picker.
        "picker_icon": "🐍",
        "agent_file": "agent.py",
        "agent_variable": "root_agent",
        "agent_in_subdirectory": False,
        "version_reader": _read_python_version,
        "api_base_path": "",
        "a2a_base_path_factory": lambda app_name: f"/a2a/{app_name}",
        "default_agent_directory": "app",
        # Content capture off. The new OTel GenAI semconv reads this variable as
        # an enum (NO_CONTENT); the old one, still used by ADK Go, reads a boolean.
        "otel_capture_message_content_default": "NO_CONTENT",
        # Suffix of the bundled template names (adk_go, ...); None = no suffix.
        "base_template_suffix": None,
        # Experiment that lists this language's templates; None = always listed.
        "experiment": None,
        # Env var that selects the Gemini platform backend in this language's
        # google-genai SDK. google-genai >= 1.74.0 (Python) reads the new name;
        # the other SDKs still read GOOGLE_GENAI_USE_VERTEXAI.
        "genai_backend_env_var": "GOOGLE_GENAI_USE_ENTERPRISE",
        # Whether the template ships the BigQuery Agent Analytics plugin. Also
        # decides whether the CI/CD Terraform enables the BigQuery API.
        "supports_bq_analytics": True,
        # Files, besides the agent directory and deployment/, whose changes run
        # the Cloud Build CI/CD triggers.
        "ci_trigger_files": ["uv.lock", "tests/**"],
        # GKE container preStop command.
        "gke_prestop_command": ["sleep", "10"],
        # Agent template folders copied on top of the base template, besides
        # the agent directory and the shared folders (tests, deployment, ...).
        "extra_template_folders": [],
        # Whether the project uses a JVM package (java_package cookiecutter vars).
        "jvm_package_layout": False,
    },
    "go": {
        "lock_file": "go.sum",
        "lock_command": ["go", "mod", "tidy"],
        "lock_command_name": "go mod tidy",
        "display_name": "Go",
        "picker_icon": "🐹",
        "agent_file": "agent.go",
        "agent_variable": "RootAgent",
        "agent_in_subdirectory": False,
        "version_reader": None,
        "api_base_path": "",
        "a2a_base_path_factory": lambda _app_name: "",
        "default_agent_directory": "app",
        "otel_capture_message_content_default": "false",
        "base_template_suffix": "_go",
        "experiment": None,
        "genai_backend_env_var": "GOOGLE_GENAI_USE_VERTEXAI",
        "supports_bq_analytics": False,
        "ci_trigger_files": ["e2e/**", "go.mod", "go.sum"],
        # The distroless runtime image has no `sleep` or shell, and the typed
        # kubernetes provider can't express a native preStop.sleep, so the
        # app binary's own `sleep` subcommand is invoked (see main.go).
        "gke_prestop_command": ["/agent", "sleep", "10"],
        "extra_template_folders": [],
        "jvm_package_layout": False,
    },
    "java": {
        "lock_file": None,  # Maven doesn't have a separate lock file
        "lock_command": ["mvn", "dependency:resolve"],
        "lock_command_name": "mvn dependency:resolve",
        "display_name": "Java",
        "picker_icon": "☕",
        "agent_file": "Agent.java",
        "agent_variable": "ROOT_AGENT",
        "agent_in_subdirectory": True,  # Java uses package subdirectories
        "version_reader": None,
        "api_base_path": "",
        # The card is served at the root; A2A itself at ADK's fixed /a2a/remote.
        "a2a_base_path_factory": lambda _app_name: "",
        "default_agent_directory": "src/main/java",
        "otel_capture_message_content_default": "NO_CONTENT",
        "base_template_suffix": "_java",
        "experiment": "adk_java",
        "genai_backend_env_var": "GOOGLE_GENAI_USE_VERTEXAI",
        "supports_bq_analytics": False,
        "ci_trigger_files": [],
        "gke_prestop_command": ["sleep", "10"],
        "extra_template_folders": ["src/test"],
        "jvm_package_layout": True,
    },
    "typescript": {
        "lock_file": "package-lock.json",
        "lock_command": ["npm", "install", "--package-lock-only"],
        "lock_command_name": "npm install --package-lock-only",
        "display_name": "TypeScript",
        "picker_icon": "🔷",
        "agent_file": "agent.ts",
        "agent_variable": "rootAgent",
        "agent_in_subdirectory": False,
        "version_reader": None,
        "api_base_path": "",
        "a2a_base_path_factory": None,
        "default_agent_directory": "app",
        "otel_capture_message_content_default": "NO_CONTENT",
        "base_template_suffix": "_ts",
        "experiment": "adk_typescript",
        "genai_backend_env_var": "GOOGLE_GENAI_USE_VERTEXAI",
        "supports_bq_analytics": False,
        "ci_trigger_files": [],
        "gke_prestop_command": ["sleep", "10"],
        "extra_template_folders": [],
        "jvm_package_layout": False,
    },
    "kotlin": {
        "lock_file": None,  # Gradle doesn't lock dependencies by default
        "lock_command": ["gradle", "dependencies"],
        "lock_command_name": "gradle dependencies",
        "display_name": "Kotlin",
        "picker_icon": "🟣",
        "agent_file": "Agent.kt",
        "agent_variable": "rootAgent",
        "agent_in_subdirectory": True,
        "version_reader": None,
        "api_base_path": "",
        "a2a_base_path_factory": None,
        "default_agent_directory": "src/main/kotlin",
        "otel_capture_message_content_default": "NO_CONTENT",
        "base_template_suffix": "_kotlin",
        "experiment": "adk_kotlin",
        "genai_backend_env_var": "GOOGLE_GENAI_USE_VERTEXAI",
        "supports_bq_analytics": False,
        "ci_trigger_files": ["build.gradle.kts", "settings.gradle.kts"],
        "gke_prestop_command": ["sleep", "10"],
        "extra_template_folders": [],
        "jvm_package_layout": True,
    },
}


class UnsupportedLanguageError(click.ClickException):
    """Raised when agents-cli or one of its commands doesn't support a language."""


def get_language_config(language: str) -> dict[str, Any]:
    """Return the config for ``language``.

    Raises:
        UnsupportedLanguageError: If this agents-cli doesn't support ``language``.
    """
    # Values read from user files may not be strings, and a list isn't hashable.
    if not isinstance(language, str) or language not in LANGUAGE_CONFIGS:
        supported = sorted(lang for lang in LANGUAGE_CONFIGS if is_language_enabled(lang))
        raise UnsupportedLanguageError(
            f"Unsupported language {language!r}.\n"
            f"  Supported languages: {', '.join(supported)}.\n"
            "  A newer agents-cli may support it; upgrade with: "
            "uv tool upgrade google-agents-cli"
        )
    return LANGUAGE_CONFIGS[language]


def a2a_base_path(language: str, app_name: str) -> str | None:
    factory: Callable[[str], str] | None = get_language_config(language).get(
        "a2a_base_path_factory"
    )
    return factory(app_name) if factory is not None else None


def is_language_enabled(language: str) -> bool:
    """False while ``language``'s experiment is off.

    A disabled language is treated as if the CLI didn't know it: its templates
    can't be listed or selected, and messages don't name it.
    """
    experiment = LANGUAGE_CONFIGS.get(language, {}).get("experiment")
    return not experiment or bool(resolve_experiment(experiment))


def dispatch_language(
    command_name: str,
    handlers: Mapping[str, Callable | None],
    language: str,
) -> Callable:
    """Return the handler for ``language``, or raise a clear error.

    A ``None`` entry (or a missing key) means the command is intentionally not
    wired up for that language yet, so the user gets an actionable message
    instead of the command silently doing the wrong thing.

    Args:
        command_name: The user-facing command (e.g. ``"install"``) for messages.
        handlers: Map of language -> handler callable (or ``None`` if the
            command doesn't support that language yet).
        language: The project's language.

    Returns:
        The handler callable for ``language``.

    Raises:
        UnsupportedLanguageError: If no handler is registered for ``language``.
    """
    handler = handlers.get(language)
    if handler is None:
        supported = ", ".join(sorted(k for k, v in handlers.items() if v is not None))
        raise UnsupportedLanguageError(
            f"`agents-cli {command_name}` isn't supported for '{language}' "
            f"projects.\n  Supported languages: {supported}."
        )
    return handler


def find_agent_file(
    project_dir: pathlib.Path,
    language: str,
    agent_directory: str,
) -> pathlib.Path | None:
    """Find the primary agent file for a language.

    Returns ``root_agent.yaml`` if present, else the language's ``agent_file``
    from LANGUAGE_CONFIGS: directly in ``agent_directory``, or anywhere below it
    for languages with ``agent_in_subdirectory`` (Java packages).

    Args:
        project_dir: Project root directory
        language: Language key (a LANGUAGE_CONFIGS key)
        agent_directory: Agent directory relative to project root

    Returns:
        Path to agent file if found, None otherwise
    """
    agent_folder = project_dir / agent_directory

    if not agent_folder.exists():
        return None

    # Check for YAML config agent first (all languages)
    yaml_agent = agent_folder / "root_agent.yaml"
    if yaml_agent.exists():
        return yaml_agent

    return _locate_language_agent_file(agent_folder, get_language_config(language))


def validate_agent_file(
    agent_file: pathlib.Path,
    language: str,
) -> tuple[bool, str | None]:
    """Validate that the agent file contains the required variable.

    Args:
        agent_file: Path to the agent file
        language: Language key

    Returns:
        Tuple of (is_valid, error_message). error_message is None if valid.
    """
    lang_config = get_language_config(language)
    required_var = lang_config.get("agent_variable", "root_agent")

    # YAML config agents are always valid
    if agent_file.name == "root_agent.yaml":
        return True, None

    try:
        content = agent_file.read_text(encoding="utf-8")

        if required_var in content:
            return True, None
        else:
            return False, f"Missing '{required_var}' variable in {agent_file.name}"
    except Exception as e:
        return False, f"Could not read {agent_file.name}: {e}"


def get_agent_file_hint(dir_path: pathlib.Path) -> str:
    """Get hint string for directory selection.

    Args:
        dir_path: Directory to check

    Returns:
        Hint string like ' (has Agent.java)' or ''
    """
    if not dir_path.is_dir():
        return ""

    # Check YAML config agent first
    if (dir_path / "root_agent.yaml").exists():
        return " (has root_agent.yaml)"

    # A directory holding several languages' agent files gets the hint for the
    # first one in LANGUAGE_CONFIGS order.
    for language, lang_config in LANGUAGE_CONFIGS.items():
        if is_language_enabled(language) and _locate_language_agent_file(
            dir_path, lang_config
        ):
            return f" (has {lang_config['agent_file']})"

    return ""


def _locate_language_agent_file(
    agent_folder: pathlib.Path, lang_config: Mapping[str, Any]
) -> pathlib.Path | None:
    """Return the language's ``agent_file`` inside ``agent_folder``, if present."""
    agent_file_name = lang_config["agent_file"]

    # For languages with agent in subdirectory (Java package structure)
    if lang_config["agent_in_subdirectory"]:
        return next(agent_folder.rglob(agent_file_name), None)

    # Standard case: agent file directly in agent directory
    agent_file = agent_folder / agent_file_name
    return agent_file if agent_file.exists() else None


def get_project_version(
    project_dir: str | pathlib.Path,
    default_version: str = "0.0.0",
) -> str:
    """Extract the project version, falling back to ``default_version``.
    Dispatches on the project language to its config ``version_reader``. Languages with no reader take the default silently.

    Args:
        project_dir: The project root directory.
        default_version: The fallback version to return if not found.
    Returns:
        The extracted version string, or default_version.
    """
    from google.agents.cli._project import read_project_config

    root = pathlib.Path(project_dir)
    language = read_project_config(str(root)).language
    reader = get_language_config(language).get("version_reader")
    if reader is None:
        return default_version

    try:
        return reader(root)
    except Exception as e:
        logging.warning(
            "Could not read the project version (%s). Falling back to %s — set "
            "the version in your project, or pass AGENT_VERSION via "
            "--env-vars to override.",
            e,
            default_version,
        )
    return default_version
