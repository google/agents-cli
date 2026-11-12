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
from dataclasses import dataclass
from typing import Any

import click

from google.agents.cli._experiments import resolve_experiment
from google.agents.cli._fs import stays_inside


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
        "lock_file": None,  # Gradle doesn't lock dependencies by default
        "lock_command": ["gradle", "dependencies"],
        "lock_command_name": "gradle dependencies",
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
        # src/main/resources and src/test sit outside the agent directory.
        "ci_trigger_files": [
            "src/**",
            "build.gradle.kts",
            "settings.gradle.kts",
            "Dockerfile",
        ],
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
        "ci_trigger_files": [
            "tests/**",
            "package.json",
            "package-lock.json",
            "tsconfig.json",
            "Dockerfile",
        ],
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
        "ci_trigger_files": [
            "src/test/**",
            "build.gradle.kts",
            "settings.gradle.kts",
            "Dockerfile",
        ],
        "gke_prestop_command": ["sleep", "10"],
        "extra_template_folders": ["src/test"],
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


# =============================================================================
# Project detection (projects agents-cli did not create)
# =============================================================================

# Directories that never hold agent code, skipped when scanning or listing
# candidate agent directories.
NON_AGENT_DIRS = {
    ".git",
    ".github",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "build",
    "dist",
    ".terraform",
}

# Files at the project root that identify a language's project, consulted only
# when no language-specific agent file settles it (a root_agent.yaml agent).
_LANGUAGE_MARKER_FILES: dict[str, tuple[str, ...]] = {
    "python": ("pyproject.toml", "requirements.txt"),
    "go": ("go.mod",),
    "java": ("pom.xml",),
    "typescript": ("package.json",),
    "kotlin": ("build.gradle.kts", "build.gradle"),
}


class ProjectDetectionError(click.ClickException):
    """Raised when a project's language or agent directory can't be told apart."""


@dataclass(frozen=True)
class DetectedProject:
    """What ``detect_project`` found in a project without a manifest."""

    language: str
    # Relative to the project root, as the manifest records it.
    agent_directory: str
    # The agent file the directory was recognised by.
    agent_file: pathlib.Path


def detect_project(
    project_dir: pathlib.Path,
    *,
    agent_directory: str | None = None,
    choose_agent_directory: Callable[[list[str]], str] | None = None,
) -> DetectedProject:
    """Find the language and agent directory of a project without a manifest.

    Scans the top-level directories (and each language's default agent
    directory) for a language's ``agent_file`` or a ``root_agent.yaml``. Stops
    with an error whenever the answer isn't unambiguous: two languages' agent
    files, several agent directories without a way to choose, or none at all.

    Args:
        project_dir: The project root.
        agent_directory: Skips directory detection. Must be a relative path
            inside ``project_dir`` holding an agent file.
        choose_agent_directory: Called with the sorted candidates when more than
            one directory holds an agent file; None makes that an error.

    Raises:
        ProjectDetectionError: When the project can't be read unambiguously.
    """
    if agent_directory is not None:
        agent_directory = agent_directory.rstrip("/")
        if not stays_inside(agent_directory):
            raise ProjectDetectionError(
                "--agent-directory must be a relative path inside the project, "
                f"not '{agent_directory}'."
            )
        if not (project_dir / agent_directory).is_dir():
            raise ProjectDetectionError(
                f"--agent-directory '{agent_directory}' is not a directory in "
                f"'{project_dir}'."
            )
        candidates = [agent_directory]
    else:
        candidates = _candidate_agent_directories(project_dir)

    # language -> directories holding that language's agent file; the yaml set
    # holds directories whose agent is a root_agent.yaml, which fits any language.
    by_language: dict[str, set[str]] = {}
    yaml_dirs: set[str] = set()
    for candidate in candidates:
        for language in _enabled_languages():
            config = LANGUAGE_CONFIGS[language]
            # Java/Kotlin agents sit below a package tree; only the conventional
            # source root is searched for them, so that `src` and `src/main/java`
            # don't both match.
            if (
                config["agent_in_subdirectory"]
                and agent_directory is None
                and candidate != config["default_agent_directory"]
            ):
                continue
            agent_file = find_agent_file(project_dir, language, candidate)
            if agent_file is None:
                continue
            if agent_file.name == "root_agent.yaml":
                yaml_dirs.add(candidate)
            else:
                by_language.setdefault(language, set()).add(candidate)

    language = _resolve_project_language(project_dir, by_language, yaml_dirs, candidates)
    directories = sorted(by_language.get(language, set()) | yaml_dirs)

    if agent_directory is not None:
        chosen = agent_directory
    elif len(directories) == 1:
        chosen = directories[0]
    elif choose_agent_directory is not None:
        chosen = choose_agent_directory(directories)
    else:
        raise ProjectDetectionError(
            f"Several directories contain an agent: {', '.join(directories)}.\n"
            "  Pass --agent-directory <dir> to pick one."
        )
    agent_file = find_agent_file(project_dir, language, chosen)
    if agent_file is None:
        # Only reachable if the chooser returned a directory it wasn't offered.
        raise ProjectDetectionError(f"No agent file found in {chosen}/.")
    return DetectedProject(
        language=language, agent_directory=chosen, agent_file=agent_file
    )


def _candidate_agent_directories(project_dir: pathlib.Path) -> list[str]:
    """Top-level directories worth checking, plus each language's default."""
    candidates = {
        item.name
        for item in project_dir.iterdir()
        if item.is_dir()
        and not item.name.startswith(".")
        and item.name not in NON_AGENT_DIRS
    }
    for language in _enabled_languages():
        default = LANGUAGE_CONFIGS[language]["default_agent_directory"]
        if (project_dir / default).is_dir():
            candidates.add(default)
    return sorted(candidates)


def _resolve_project_language(
    project_dir: pathlib.Path,
    by_language: dict[str, set[str]],
    yaml_dirs: set[str],
    candidates: list[str],
) -> str:
    if len(by_language) > 1:
        found = "; ".join(
            f"{LANGUAGE_CONFIGS[lang]['agent_file']} in {', '.join(sorted(dirs))}"
            for lang, dirs in sorted(by_language.items())
        )
        raise ProjectDetectionError(
            f"Found agent files for more than one language: {found}.\n"
            "  Pass --agent-directory <dir> to pick the agent to record."
        )
    if len(by_language) == 1:
        return next(iter(by_language))
    if not yaml_dirs:
        looked_for = ", ".join(
            sorted(
                {LANGUAGE_CONFIGS[lang]["agent_file"] for lang in _enabled_languages()}
                | {"root_agent.yaml"}
            )
        )
        searched = ", ".join(candidates) if candidates else "(no directories)"
        raise ProjectDetectionError(
            f"No ADK agent found in '{project_dir}'.\n"
            f"  Looked for {looked_for} in: {searched}.\n"
            "  Run this from the project root, or pass --agent-directory <dir>."
        )

    # Only root_agent.yaml agents: the language has to come from the project files.
    marked = [
        lang
        for lang in _enabled_languages()
        if any(
            (project_dir / marker).is_file()
            for marker in _LANGUAGE_MARKER_FILES.get(lang, ())
        )
    ]
    if len(marked) == 1:
        return marked[0]
    if not marked:
        raise ProjectDetectionError(
            "Could not tell the project language: root_agent.yaml fits any "
            "language and no project file (pyproject.toml, go.mod, ...) was "
            f"found in '{project_dir}'."
        )
    raise ProjectDetectionError(
        "Could not tell the project language: root_agent.yaml fits any "
        f"language and project files for {', '.join(marked)} were found in "
        f"'{project_dir}'."
    )


def _enabled_languages() -> list[str]:
    return [lang for lang in LANGUAGE_CONFIGS if is_language_enabled(lang)]


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
