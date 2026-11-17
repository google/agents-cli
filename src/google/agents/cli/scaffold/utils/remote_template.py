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

import logging
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
from dataclasses import dataclass, replace
from typing import Any, NamedTuple, TypedDict

import click

from google.agents.cli._git import is_option_like
from google.agents.cli._output import Console
from google.agents.cli._project import DEFAULT_BASE_TEMPLATE
from google.agents.cli.extension._deprecation import warn_if_agents_cli_repo_template

from .fs import walk_source_tree
from .language import get_language_config

ADK_RECIPES_REPO_URL = "https://github.com/google/adk-recipes"
# adk-samples was renamed to adk-recipes; GitHub serves both names.
_ADK_REPO_NAMES = frozenset({"adk-samples", "adk-recipes"})
ADK_SHORTCUT_PREFIX = "adk@"
# Where the Python recipes live in adk-recipes: scanned by discover_adk_agents
# and used to resolve the deprecated adk@ shortcut.
ADK_RECIPE_DIRS = ("core/python", "contrib/python")
ADK_RECIPE_INDEX_URLS = tuple(
    f"{ADK_RECIPES_REPO_URL}/tree/main/{recipe_dir}" for recipe_dir in ADK_RECIPE_DIRS
)
# The adk-recipes manifest; not agents-cli-manifest.yaml.
_RECIPE_MANIFEST_FILE = "manifest.yaml"
AGENTS_CLI_MANIFEST_FILE = "agents-cli-manifest.yaml"

# Marks a flat template: agent code in the template root rather than a package.
FLAT_AGENT_DIRECTORY = "."
_REMOTE_BASE_TEMPLATES = {"python": DEFAULT_BASE_TEMPLATE, "go": "adk_go"}
_SUPPORTED_REMOTE_LANGUAGES = ", ".join(sorted(_REMOTE_BASE_TEMPLATES))
# Folders whose agent files are fixtures or helpers, never the agent.
# frontend/ and tests/ are also overwritten by the base template on render.
_NON_AGENT_DIRECTORIES = frozenset(
    {
        "deployment",
        "docs",
        "eval",
        "evals",
        "examples",
        "frontend",
        "notebooks",
        "samples",
        "scripts",
        "test",
        "tests",
    }
)


@dataclass
class RemoteTemplateSpec:
    """Parsed remote template specification."""

    repo_url: str
    template_path: str
    git_ref: str
    is_adk_recipes: bool = False
    # ``<ref>/<path>`` from a /tree/ URL; re-split on fetch as refs may contain slashes.
    tree_path: str | None = None


@dataclass(frozen=True)
class AdkRecipe:
    """An adk-recipes recipe offered in the scaffold browse menu."""

    name: str
    description: str
    spec: str  # GitHub URL, usable as --agent
    has_explicit_config: bool


class FetchedTemplate(NamedTuple):
    """Result of fetching a remote template.

    Attributes:
        template_dir: Path to the fetched template directory.
        temp_dir: Top-level temporary directory that should be cleaned up.
        repo_root: Root of the cloned repository.
    """

    template_dir: pathlib.Path
    temp_dir: pathlib.Path
    repo_root: pathlib.Path


class _ProjectFallbacks(TypedDict, total=False):
    name: str
    description: str


class _TemplateFileConfig(NamedTuple):
    declared: dict[str, Any]
    fallbacks: _ProjectFallbacks


def is_template_spec(value: str) -> bool:
    """True for a value naming a template to fetch rather than one this CLI ships."""
    return (
        value.startswith("local@")
        or is_adk_shortcut(value)
        or parse_agent_spec(value) is not None
    )


def parse_agent_spec(agent_spec: str) -> RemoteTemplateSpec | None:
    """Parse agent specification to determine if it's a remote template.

    Args:
        agent_spec: Agent specification string

    Returns:
        RemoteTemplateSpec if remote template, None if local template
    """
    # Not remote: local@ paths, and the deprecated adk@ shortcut
    if agent_spec.startswith("local@") or is_adk_shortcut(agent_spec):
        return None

    # GitHub /tree/ URL pattern
    tree_pattern = r"^(https?://[^/]+/[^/]+/[^/]+)/tree/([^/]+)/(.*)$"
    match = re.match(tree_pattern, agent_spec)
    if match:
        repo_url = match.group(1)
        git_ref = match.group(2)
        template_path = match.group(3)
        return RemoteTemplateSpec(
            repo_url=repo_url,
            template_path=template_path.strip("/"),
            git_ref=git_ref,
            is_adk_recipes=_is_adk_repo_url(repo_url),
            tree_path=f"{git_ref}/{template_path}".strip("/"),
        )

    # General remote pattern: <repo_url>[/<path>][@<ref>]
    # Handles github.com, gitlab.com, etc.
    remote_pattern = r"^(https?://[^/]+/[^/]+/[^/@]+)(?:/(.*?))?(?:@([^/]+))?/?$"
    match = re.match(remote_pattern, agent_spec)
    if match:
        repo_url = match.group(1)
        template_path_with_ref = match.group(2) or ""
        git_ref_from_url = match.group(3)

        # Separate path and ref if ref is part of the path
        template_path = template_path_with_ref
        git_ref = git_ref_from_url or "main"

        if "@" in template_path:
            path_parts = template_path.split("@")
            template_path = path_parts[0]
            git_ref = path_parts[1]

        return RemoteTemplateSpec(
            repo_url=repo_url,
            template_path=template_path.strip("/"),
            git_ref=git_ref,
            is_adk_recipes=_is_adk_repo_url(repo_url),
        )

    # GitHub shorthand: <org>/<repo>[/<path>][@<ref>]
    github_shorthand_pattern = r"^([^/]+)/([^/@]+)(?:/(.*?))?(?:@([^/]+))?/?$"
    match = re.match(github_shorthand_pattern, agent_spec)
    if match and "/" in agent_spec:  # Ensure it has at least one slash
        org = match.group(1)
        repo = match.group(2)
        template_path = match.group(3) or ""
        git_ref = match.group(4) or "main"

        repo_url = f"https://github.com/{org}/{repo}"

        return RemoteTemplateSpec(
            repo_url=repo_url,
            template_path=template_path,
            git_ref=git_ref,
            is_adk_recipes=_is_adk_repo_url(repo_url),
        )

    return None


def is_adk_shortcut(agent_spec: str) -> bool:
    """True for the deprecated ``adk@<name>`` shortcut (see ``adk_shortcut``)."""
    return agent_spec.startswith(ADK_SHORTCUT_PREFIX)


def check_and_execute_with_version_lock(
    template_dir: pathlib.Path,
    original_agent_spec: str | None = None,
    locked: bool = False,
    project_name: str | None = None,
) -> bool:
    """Check if remote template has agents-cli version lock.

    Since the ACLI code is vendored into agents-cli, version locking
    is no longer needed — we always use the vendored version. This
    function now simply logs the version info and returns False to let
    the caller proceed with the vendored code.

    Returns:
        Always False (proceed with vendored code).
    """
    if locked or os.environ.get("ACLI_SKIP_VERSION_LOCK") == "1":
        return False

    uv_lock_path = template_dir / "uv.lock"
    version = parse_acli_version_from_lock(uv_lock_path)

    if version:
        logging.debug(
            "Remote template specifies agents-cli version %s; using vendored code instead.",
            version,
        )

    return False


def fetch_remote_template(
    spec: RemoteTemplateSpec,
    original_agent_spec: str | None = None,
    locked: bool = False,
    project_name: str | None = None,
) -> FetchedTemplate:
    """Fetch remote template and return path to template directory.

    Uses Git to clone the remote repository. If the template contains a uv.lock
    with agents-cli version constraint, will execute nested uvx command.

    Args:
        spec: Remote template specification
        original_agent_spec: Original agent spec string (used to prevent recursion)
        locked: Whether this is already a locked execution (prevents recursion)
        project_name: Project name (may have been entered interactively)

    Returns:
        A ``FetchedTemplate`` with the fetched template directory, the top-level
        temporary directory to clean up, and the cloned repository root.
    """
    if spec.tree_path:
        git_ref, template_path = _split_tree_path(spec.repo_url, spec.tree_path)
        spec = replace(spec, git_ref=git_ref, template_path=template_path)
    warn_if_agents_cli_repo_template(
        spec.repo_url, spec.template_path, original_agent_spec
    )
    if is_option_like(spec.git_ref):
        raise RuntimeError(
            f"Invalid git ref {spec.git_ref!r}: pass a branch, tag, or commit, "
            "which cannot start with '-'."
        )
    temp_dir = tempfile.mkdtemp(prefix="acli_remote_template_")
    temp_path = pathlib.Path(temp_dir)
    repo_path = temp_path / "repo"

    # Attempt Git Clone
    try:
        clone_url = spec.repo_url

        # Build clone command with --single-branch (optimized for branches)
        clone_cmd = [
            "git",
            "clone",
            "--depth",
            "1",
            "--single-branch",
            "--branch",
            spec.git_ref,
            "--",
            clone_url,
            str(repo_path),
        ]

        from google.agents.cli._runner import run_resolved

        logging.debug(
            "Attempting to clone remote template with Git: %s", shlex.join(clone_cmd)
        )
        # GIT_TERMINAL_PROMPT=0 prevents git from prompting for credentials
        result = run_resolved(
            clone_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )

        # If clone with --single-branch fails, retry without it (for tags)
        if result.returncode != 0:
            # Check if the error is related to branch not found (indicates it's likely a tag)
            if "Remote branch" in result.stderr or "not found" in result.stderr:
                logging.debug(
                    "Clone with --single-branch failed, retrying without it (git_ref '%s' is likely a tag)",
                    spec.git_ref,
                )
                clone_cmd_without_single_branch = [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    spec.git_ref,
                    "--",
                    clone_url,
                    str(repo_path),
                ]
                run_resolved(
                    clone_cmd_without_single_branch,
                    capture_output=True,
                    text=True,
                    check=True,
                    encoding="utf-8",
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                )
                logging.debug("Git clone successful (without --single-branch).")
            else:
                # Different error, raise it
                raise subprocess.CalledProcessError(
                    result.returncode, clone_cmd, result.stdout, result.stderr
                )
        else:
            logging.debug("Git clone successful.")
    except subprocess.CalledProcessError as e:
        shutil.rmtree(temp_path, ignore_errors=True)
        raise RuntimeError(f"Git clone failed: {e.stderr.strip()}") from e

    # Process the successfully fetched template
    try:
        if spec.template_path:
            template_dir = repo_path / spec.template_path
        else:
            template_dir = repo_path

        if not template_dir.exists():
            raise FileNotFoundError(
                f"Template path not found in the repository: {spec.template_path}"
            )

        # Check for version lock and execute nested command if found
        if check_and_execute_with_version_lock(
            template_dir, original_agent_spec, locked, project_name
        ):
            # If we executed with locked version, the nested process will handle everything
            # Clean up and exit successfully
            shutil.rmtree(temp_path, ignore_errors=True)
            # Exit with success since the nested command will handle the rest
            sys.exit(0)

        return FetchedTemplate(
            template_dir=template_dir, temp_dir=temp_path, repo_root=repo_path
        )
    except Exception as e:
        # Clean up on error
        shutil.rmtree(temp_path, ignore_errors=True)
        raise RuntimeError(
            f"An unexpected error occurred after fetching remote template: {e}"
        ) from e


def load_remote_template_config(
    template_dir: pathlib.Path,
    cli_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a remote template's config.

    Starts from the config file the template ships, resolves its language, base
    template and (when it declares none) agent directory, then applies CLI
    overrides (highest precedence).

    Raises:
        click.ClickException: If templateconfig.yaml is invalid, a config
            file or its settings is not a mapping, pyproject.toml's
            [tool.agents-cli] is not a table, or the declared language
            contradicts the base template or is unsupported and the template
            declares no base template.
    """
    cli_overrides = cli_overrides or {}
    file_config = _read_template_file_config(template_dir)
    config: dict[str, Any] = {
        "base_template": DEFAULT_BASE_TEMPLATE,
        "name": template_dir.name,
        "description": "",
        "agent_directory": "app",
        **file_config.fallbacks,
        **file_config.declared,
    }
    settings = config.get("settings")
    if settings is None:  # missing, or an empty `settings:` key
        settings = config["settings"] = {}
    elif not isinstance(settings, dict):
        raise click.ClickException(
            f"settings in {template_dir.name}'s agents-cli config must be a mapping "
            f"of keys to values, not a {type(settings).__name__}."
        )

    language, config["base_template"] = _resolve_language_and_base(
        template_dir, file_config.declared, settings, cli_overrides
    )
    # Recorded only when it changes, so a template declaring nothing keeps empty settings.
    if language != settings.get("language", "python"):
        settings["language"] = language

    cli_settings = cli_overrides.get("settings")
    cli_sets_agent_directory = isinstance(cli_settings, dict) and bool(
        cli_settings.get("agent_directory")
    )
    template_agent_directory = settings.get("agent_directory")
    if not template_agent_directory and cli_sets_agent_directory:
        # The CLI picks the target; only flatness is still needed, so skip the search.
        template_agent_directory = _conventional_agent_directory(
            template_dir, get_language_config(language)["agent_file"]
        )
    elif not template_agent_directory:
        template_agent_directory = resolve_agent_directory(template_dir, language)
        # Left unset when nothing is found, so the base template's
        # agent_directory (e.g. adk_java's src/main/java) is inherited.
        if template_agent_directory is not None:
            settings["agent_directory"] = template_agent_directory

    # Kept apart from settings.agent_directory, which a CLI override replaces:
    # a flat template is copied into whatever target directory the CLI picks.
    config["is_flat_structure"] = template_agent_directory == FLAT_AGENT_DIRECTORY
    config["has_explicit_config"] = bool(file_config.declared)
    return merge_template_configs(config, cli_overrides)


def _resolve_language_and_base(
    template_dir: pathlib.Path,
    declared: dict[str, Any],
    settings: dict[str, Any],
    cli_overrides: dict[str, Any],
) -> tuple[str, str]:
    """Return the template's ``(language, base_template)``.

    The declared language picks the base when the template names none. The base
    that renders the project (``--base-template`` first) then decides the
    language, since it lays down that language's server and toolchain files.
    """
    language = _declared_language(template_dir, declared, settings)
    cli_base = cli_overrides.get("base_template")
    base = declared.get("base_template")
    if not base:
        language, base = _infer_base_from_language(template_dir.name, language)

    effective_base = get_base_template_name({"base_template": cli_base or base})
    base_language = next(
        (lang for lang, name in _REMOTE_BASE_TEMPLATES.items() if name == effective_base),
        None,
    )
    if base_language is None:
        # adk_java, or the spec a project was created from: keep the declared or settings' language.
        return language or settings.get("language", "python"), base
    if language and language != base_language:
        raise click.ClickException(
            _language_conflict_message(
                template_dir.name,
                language,
                effective_base,
                base_language,
                base_from_cli=bool(cli_base),
            )
        )
    return base_language, base


def _declared_language(
    template_dir: pathlib.Path, declared: dict[str, Any], settings: dict[str, Any]
) -> str | None:
    language = (
        _read_recipe_manifest(template_dir).get("language")
        or declared.get("language")
        or settings.get("language")
    )
    return str(language).lower() if language else None


def _infer_base_from_language(
    template_name: str, language: str | None
) -> tuple[str | None, str]:
    if language and language not in _REMOTE_BASE_TEMPLATES:
        raise click.ClickException(
            f"{template_name} declares language '{language}', which remote templates "
            f"don't support yet (supported: {_SUPPORTED_REMOTE_LANGUAGES}). Set "
            f"base_template in the template's config to a base that renders "
            f"'{language}' projects."
        )
    return language, _REMOTE_BASE_TEMPLATES.get(
        language or "python", DEFAULT_BASE_TEMPLATE
    )


def _language_conflict_message(
    template_name: str,
    language: str,
    base: str,
    base_language: str,
    *,
    base_from_cli: bool,
) -> str:
    display_name = get_language_config(base_language)["display_name"]
    conflict = (
        f"{template_name} declares language '{language}', but base "
        f"template '{base}' renders {display_name} projects."
    )
    if base_from_cli:
        language_base = _REMOTE_BASE_TEMPLATES.get(language)
        fix = (
            f"Use --base-template {language_base}"
            if language_base
            else f"Use a --base-template that renders '{language}' projects"
        )
        return f"{conflict} {fix}, or drop --base-template."
    if language not in _REMOTE_BASE_TEMPLATES:
        return (
            f"{conflict} Remote templates don't support '{language}' yet "
            f"(supported: {_SUPPORTED_REMOTE_LANGUAGES})."
        )
    return (
        f"{conflict} Drop base_template from the template's config, or use a "
        f"{display_name} template."
    )


def resolve_agent_directory(template_dir: pathlib.Path, language: str) -> str | None:
    """Return the directory holding the template's agent file, or None.

    Resolution order, first match wins:
      1. ``app/``
      2. the template folder name in snake_case
      3. the template root, returned as ``FLAT_AGENT_DIRECTORY``
      4. the shallowest valid outermost directory outside ``_NON_AGENT_DIRECTORIES``,
         then alphabetical; it may be nested, e.g. ``src/my_agent``
      5. None, so the caller keeps the base template's agent_directory
    """
    agent_file = get_language_config(language)["agent_file"]
    conventional = _conventional_agent_directory(template_dir, agent_file)
    if conventional is not None:
        return conventional

    found = _find_agent_directories(template_dir, agent_file)
    valid = [d for d in found if _is_valid_agent_directory(d, language)]
    if not valid:
        if found:
            logging.warning(
                "Ignored %s in %s: not a valid %s agent directory, so the project "
                "falls back to the base template's agent directory. Rename or move "
                "the folder, or pass --agent-directory.",
                ", ".join(f"{d}/{agent_file}" for d in found),
                template_dir.name,
                get_language_config(language)["display_name"],
            )
        else:
            logging.debug("No %s found in %s", agent_file, template_dir)
        return None

    if len(valid) > 1:
        logging.warning(
            "Found %d %s files in %s; using %s/%s. Pass --agent-directory to "
            "pick another.",
            len(valid),
            agent_file,
            template_dir.name,
            valid[0],
            agent_file,
        )
    return valid[0]


def _conventional_agent_directory(
    template_dir: pathlib.Path, agent_file: str
) -> str | None:
    """Steps 1-3 of ``resolve_agent_directory``: the checks that need no search."""
    folder_pkg = template_dir.name.replace("-", "_")
    for candidate in ("app", folder_pkg):
        if (template_dir / candidate / agent_file).is_file():
            logging.debug("Found %s in %s/", agent_file, candidate)
            return candidate

    if (template_dir / agent_file).is_file():
        logging.debug("Flat structure: %s in the template root", agent_file)
        return FLAT_AGENT_DIRECTORY
    return None


def _find_agent_directories(template_dir: pathlib.Path, agent_file: str) -> list[str]:
    found: list[str] = []
    for directory, files in walk_source_tree(template_dir, _NON_AGENT_DIRECTORIES):
        relative = directory.relative_to(template_dir).as_posix()
        # Agent files inside an agent package (e.g. sub_agents/x/agent.py) belong to it.
        if agent_file in files and not any(relative.startswith(f"{p}/") for p in found):
            found.append(relative)
    return sorted(found, key=lambda path: (path.count("/"), path))


def _is_valid_agent_directory(path: str, language: str) -> bool:
    # Lazy import to avoid circular dependency
    from .template import validate_agent_directory_name

    try:
        validate_agent_directory_name(path, language=language)
    except ValueError:
        return False
    return True


def _read_template_file_config(template_dir: pathlib.Path) -> _TemplateFileConfig:
    templateconfig_path = template_dir / ".template" / "templateconfig.yaml"
    manifest_path = template_dir / AGENTS_CLI_MANIFEST_FILE
    pyproject_path = template_dir / "pyproject.toml"

    if templateconfig_path.exists():
        return _TemplateFileConfig(_read_templateconfig(templateconfig_path), {})
    if manifest_path.exists():
        return _TemplateFileConfig(_read_agents_cli_manifest(manifest_path), {})
    if pyproject_path.exists():
        return _read_pyproject_config(pyproject_path)
    # adk-recipes recipes ship manifest.yaml and rely on agent directory resolution.
    if not (template_dir / _RECIPE_MANIFEST_FILE).is_file():
        logging.warning(
            "%s declares no agents-cli configuration, so it is treated as an ADK "
            "template. Add .template/templateconfig.yaml to say otherwise.",
            template_dir.name,
        )
    return _TemplateFileConfig({}, {})


def _read_templateconfig(path: pathlib.Path) -> dict[str, Any]:
    import yaml

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        # Falling back would render a different project than the template declares.
        raise click.ClickException(f"{path.name} is not valid YAML: {e}") from e
    return _validate_yaml_mapping(path, data)


def _read_agents_cli_manifest(path: pathlib.Path) -> dict[str, Any]:
    import yaml

    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        logging.error("Could not load %s config: %s", path.name, e)
        return {}
    data = _validate_yaml_mapping(path, raw)
    # A project's manifest records agent_directory at the top level; rendering
    # reads it from settings. Any other settings value is left for the caller
    # to reject.
    settings = data.get("settings")
    if data.get("agent_directory") and (settings is None or isinstance(settings, dict)):
        data["settings"] = {
            "agent_directory": data["agent_directory"],
            **(settings or {}),
        }
    return data


def _validate_yaml_mapping(path: pathlib.Path, data: Any) -> dict[str, Any]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise click.ClickException(
            f"{path.name} must be a mapping of keys to values, not a "
            f"{type(data).__name__}."
        )
    return data


def _read_pyproject_config(path: pathlib.Path) -> _TemplateFileConfig:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        logging.error("Could not load pyproject.toml config: %s", e)
        return _TemplateFileConfig({}, {})
    tool = data.get("tool")
    declared = tool.get("agents-cli", {}) if isinstance(tool, dict) else {}
    if not isinstance(declared, dict):
        raise click.ClickException(
            f"[tool.agents-cli] in {path.name} must be a table of config keys."
        )
    project = data.get("project")
    if not isinstance(project, dict):
        project = {}
    fallbacks: _ProjectFallbacks = {}
    if isinstance(project.get("name"), str):
        fallbacks["name"] = project["name"]
    if isinstance(project.get("description"), str):
        fallbacks["description"] = project["description"]
    return _TemplateFileConfig(declared=declared, fallbacks=fallbacks)


def get_base_template_name(config: dict[str, Any]) -> str:
    """Get base template name from remote template config.

    Args:
        config: Template configuration dictionary

    Returns:
        Base template name (defaults to "adk_python"), with legacy aliases resolved
    """
    # Lazy import to avoid circular dependency
    from .template import resolve_agent_alias

    base_template = config.get("base_template", DEFAULT_BASE_TEMPLATE)
    return resolve_agent_alias(base_template) or DEFAULT_BASE_TEMPLATE


def merge_template_configs(
    base_config: dict[str, Any], remote_config: dict[str, Any]
) -> dict[str, Any]:
    """Merge base template config with remote template config using a deep merge.

    Args:
        base_config: Base template configuration
        remote_config: Remote template configuration

    Returns:
        Merged configuration with remote overriding base
    """
    import copy

    def deep_merge(d1: dict[str, Any], d2: dict[str, Any]) -> dict[str, Any]:
        """Recursively merges d2 into d1."""
        for k, v in d2.items():
            if k in d1 and isinstance(d1[k], dict) and isinstance(v, dict):
                d1[k] = deep_merge(d1[k], v)
            else:
                d1[k] = v
        return d1

    # Start with a deep copy of the base to avoid modifying it
    merged_config = copy.deepcopy(base_config)

    # Perform the deep merge
    return deep_merge(merged_config, remote_config)


def discover_adk_agents(repo_path: pathlib.Path) -> dict[int, AdkRecipe]:
    """Discover and load the Python recipes in a clone of adk-recipes.

    Scans every ``ADK_RECIPE_DIRS`` directory, skipping recipes whose
    ``manifest.yaml`` marks them inactive.

    Args:
        repo_path: Path to the cloned adk-recipes repository

    Returns:
        Dictionary mapping menu numbers (from 1) to recipes.
    """
    all_agents = []
    for recipe_dir in _recipe_dirs(repo_path):
        agent = _load_recipe(repo_path, recipe_dir)
        if agent is not None:
            all_agents.append(agent)
    # Sort agents: explicit config first, then inferred (both alphabetically within their groups)
    all_agents.sort(key=lambda x: (not x.has_explicit_config, x.name.lower()))
    return dict(enumerate(all_agents, 1))


def _recipe_dirs(repo_path: pathlib.Path) -> list[pathlib.Path]:
    """Every recipe directory under ADK_RECIPE_DIRS, skipping hidden ones."""
    recipe_dirs = []
    for index_dir in ADK_RECIPE_DIRS:
        index_path = repo_path / index_dir
        logging.debug("Looking for recipes in: %s", index_path)
        if not index_path.is_dir():
            continue
        recipe_dirs.extend(
            path
            for path in sorted(index_path.iterdir())
            if path.is_dir() and not path.name.startswith(".")
        )
    return recipe_dirs


def _load_recipe(repo_path: pathlib.Path, recipe_dir: pathlib.Path) -> AdkRecipe | None:
    """The browse-menu entry for one recipe; None if inactive or unloadable."""
    manifest = _read_recipe_manifest(recipe_dir)
    if manifest.get("status") == "inactive":
        logging.debug("Skipping inactive recipe: %s", recipe_dir.name)
        return None

    # The menu needs no agent directory, so skip load_remote_template_config's search.
    try:
        file_config = _read_template_file_config(recipe_dir)
    except click.ClickException as e:
        logging.warning("Could not load agent from %s: %s", recipe_dir, e.message)
        return None
    declared = file_config.declared

    relative_path = recipe_dir.relative_to(repo_path).as_posix()
    return AdkRecipe(
        name=declared.get("name") or file_config.fallbacks.get("name") or recipe_dir.name,
        description=declared.get("description")
        or file_config.fallbacks.get("description")
        or manifest.get("description", ""),
        spec=f"{ADK_RECIPES_REPO_URL}/tree/main/{relative_path}",
        has_explicit_config=bool(declared),
    )


def _read_recipe_manifest(recipe_dir: pathlib.Path) -> dict[str, Any]:
    """The recipe's manifest.yaml; empty when missing or unreadable."""
    path = recipe_dir / _RECIPE_MANIFEST_FILE
    if not path.is_file():
        return {}
    import yaml

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        logging.warning("Could not read %s: %s", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def display_adk_caveat_if_needed(agents: dict[int, AdkRecipe]) -> None:
    """Display helpful note for agents that use inference.

    Args:
        agents: Recipes from discover_adk_agents
    """
    console = Console()
    inferred_agents = [a for a in agents.values() if not a.has_explicit_config]
    if inferred_agents:
        console.print(
            "\n[blue]ℹ️  Note: Agents marked with * are templated using Agents CLI heuristics for ADK recipes.[/]"
        )
        console.print(
            "[dim]   Agents CLI attempts to create a working codebase, but you'll need to follow the generated README for complete setup.[/]"
        )


def parse_acli_version_from_lock(
    uv_lock_path: pathlib.Path,
) -> str | None:
    """Parse agents-cli version from uv.lock file.

    Args:
        uv_lock_path: Path to uv.lock file

    Returns:
        Version string if found, None otherwise
    """
    if not uv_lock_path.exists():
        return None

    try:
        with open(uv_lock_path, "rb") as f:
            lock_data = tomllib.load(f)

        # Look for agents-cli in the packages section
        packages = lock_data.get("package", [])
        for package in packages:
            if package.get("name") == "google-agents-cli":
                version = package.get("version")
                if version:
                    logging.debug("Found agents-cli version %s in uv.lock", version)
                    return version

    except Exception as e:
        logging.warning(f"Error parsing uv.lock file {uv_lock_path}: {e}")

    return None


def _is_adk_repo_url(repo_url: str) -> bool:
    """True for github.com/google/adk-recipes (or adk-samples), in any URL form."""
    parsed = urllib.parse.urlparse(repo_url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.strip("/").lower().removesuffix(".git")
    org, _, repo = path.partition("/")
    return host == "github.com" and org == "google" and repo in _ADK_REPO_NAMES


def _split_tree_path(repo_url: str, tree_path: str) -> tuple[str, str]:
    """Split on the remote's longest branch or tag prefix, else on the first segment."""
    first, _, rest = tree_path.partition("/")
    if not rest:
        return first, ""
    from google.agents.cli._runner import run_resolved

    result = run_resolved(
        ["git", "ls-remote", "--heads", "--tags", repo_url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode != 0:
        logging.debug("git ls-remote %s failed: %s", repo_url, result.stderr.strip())
        return first, rest
    refs: set[str] = set()
    for line in result.stdout.splitlines():
        name = line.partition("\t")[2].removesuffix("^{}")
        for prefix in ("refs/heads/", "refs/tags/"):
            if name.startswith(prefix):
                refs.add(name.removeprefix(prefix))
    matches = [r for r in refs if tree_path == r or tree_path.startswith(f"{r}/")]
    if not matches:
        return first, rest
    ref = max(matches, key=lambda name: len(name))
    return ref, tree_path.removeprefix(ref).strip("/")
