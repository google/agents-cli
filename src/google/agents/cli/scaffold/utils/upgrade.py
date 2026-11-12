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

"""3-way file comparison and dependency merging for upgrade command."""

import fnmatch
import hashlib
import json
import logging
import pathlib
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import yaml

from google.agents.cli._project import NESTED_CREATE_PARAMS

# Patterns use {agent_directory} placeholder replaced at runtime
FILE_CATEGORIES = {
    "agent_code": [  # Never modified
        # Python agent code
        "{agent_directory}/agent.py",
        "{agent_directory}/tools/**/*.py",
        "{agent_directory}/prompts/**/*.py",
        # Go agent code
        "{agent_directory}/agent.go",
        "{agent_directory}/**/*.go",
        # Java agent code
        "{agent_directory}/**/*.java",
        # TypeScript agent code
        "{agent_directory}/agent.ts",
        "{agent_directory}/**/*.ts",
    ],
    "config_files": [  # Never overwritten
        "deployment/**/*.tfvars",
        ".env",
        "*.env",
    ],
    "user_content": [  # Owned by the user, not the template
        "tests/eval/datasets/*.json",
        "tests/eval/eval_config.yaml",
        "tests/eval/*.py",
        "tests/unit/**",
        "tests/integration/**",
        "deployment_metadata.json",
        ".gitignore",
        "**/README.md",
        "GEMINI.md",
        "AGENTS.md",
        "CLAUDE.md",
    ],
    "dependencies": [  # Special merge handling
        # ACLI config
        "agents-cli-manifest.yaml",
        # Python dependencies
        "pyproject.toml",
        # Go dependencies
        "go.mod",
        "go.sum",
        # Java and Kotlin dependencies
        "build.gradle.kts",
        # TypeScript dependencies
        "package.json",
        "package-lock.json",
    ],
    # Everything else is "scaffolding" (3-way compare)
}


# Preserve type literals for type-safe reason matching
PreserveType = Literal["acli_unchanged", "already_current", "unchanged_both", None]

DependencyStatus = Literal["updated", "added", "removed", "kept", "unchanged"]


@dataclass
class FileCompareResult:
    """Result of comparing a file across three versions."""

    path: str
    category: str
    action: Literal["auto_update", "preserve", "skip", "conflict", "new", "removed"]
    reason: str
    # For preserve actions, indicates why preserved
    preserve_type: PreserveType = None
    # For conflicts, store the content hashes
    current_hash: str | None = None
    old_template_hash: str | None = None
    new_template_hash: str | None = None


class DependencyReadError(Exception):
    """A dependency manifest could not be read — it's missing or unparseable."""


@dataclass
class DependencyResolution:
    """What the merge decided to do with one dependency.

    Every dependency gets one of these, even ones that stay the same.
    Statuses:
      - "added": the template introduced this dependency.
      - "updated": a template-managed dependency whose spec the template changed.
      - "removed": a template-managed dependency the new template dropped.
      - "unchanged": a template-managed dependency the template left as-is.
      - "kept": a dependency *the user* added, preserved untouched. In union mode
        (``--force``), also one the new template dropped.
    "kept" and "unchanged" both leave the written value the same, but differ in
    ownership: "kept" is not managed by the new template, "unchanged" is.
    They are also displayed differently ("kept" is surfaced, "unchanged" hidden).
    """

    name: str
    status: DependencyStatus
    old_version: str | None = None
    new_version: str | None = None

    def full_name(self) -> str:
        """The full name to write to a dependency file.

        For Python: ``name[extra]version`` (e.g. ``requests[security]==2.32.5``)
        For Go: ``module@version`` (e.g. ``google.golang.org/adk@v2.2.0``)

        Valid for every status except "removed" (which has no new version).
        """
        return f"{self.name}{self.new_version or ''}"


def _expand_patterns(patterns: list[str], agent_directory: str) -> list[str]:
    """Expand {agent_directory} placeholder in patterns."""
    return [p.replace("{agent_directory}", agent_directory) for p in patterns]


def _matches_any_pattern(path: str, patterns: list[str]) -> bool:
    """Check if path matches any glob pattern, including ** recursive patterns."""
    path = path.replace("\\", "/")

    for pattern in patterns:
        pattern = pattern.replace("\\", "/")

        if fnmatch.fnmatch(path, pattern):
            return True

        if "**" in pattern:
            regex = re.escape(pattern)
            regex = regex.replace(r"\*\*/", "(?:.*/)?")  # **/ = zero or more dirs
            regex = regex.replace(r"\*\*", ".*")
            regex = regex.replace(r"\*", "[^/]*")
            if re.match(f"^{regex}$", path):
                return True

    return False


def categorize_file(path: str, agent_directory: str = "app") -> str:
    """Return the FILE_CATEGORIES key that matches path, or "scaffolding"."""
    for category, patterns in FILE_CATEGORIES.items():
        expanded = _expand_patterns(patterns, agent_directory)
        if _matches_any_pattern(path, expanded):
            return category
    return "scaffolding"


def _file_hash(file_path: pathlib.Path) -> str | None:
    """Calculate SHA256 hash of a file's contents."""
    if not file_path.exists():
        return None
    try:
        content = file_path.read_bytes()
        return hashlib.sha256(content).hexdigest()
    except Exception as e:
        logging.warning(f"Could not hash file {file_path}: {e}")
        return None


def three_way_compare(
    relative_path: str,
    project_dir: pathlib.Path,
    old_template_dir: pathlib.Path,
    new_template_dir: pathlib.Path,
    agent_directory: str = "app",
) -> FileCompareResult:
    """Compare file across current, old template, and new template.

    Returns action based on:
    - current == old -> auto-update (user didn't modify)
    - old == new -> preserve (ACLI didn't change)
    - all differ -> conflict
    """
    category = categorize_file(relative_path, agent_directory)

    if category == "agent_code":
        return FileCompareResult(
            path=relative_path,
            category=category,
            action="skip",
            reason="Agent code (never modified by upgrade)",
        )

    current_file = project_dir / relative_path

    # A missing config or dependency file falls through to the comparison below
    # and is created from the new template rather than skipped.
    if category == "config_files" and current_file.exists():
        return FileCompareResult(
            path=relative_path,
            category=category,
            action="skip",
            reason="Config file (user's environment settings)",
        )

    if category == "dependencies" and current_file.exists():
        return FileCompareResult(
            path=relative_path,
            category=category,
            action="preserve",
            reason="Dependencies (requires merge handling)",
        )

    old_template_file = old_template_dir / relative_path
    new_template_file = new_template_dir / relative_path

    current_hash = _file_hash(current_file)
    old_hash = _file_hash(old_template_file)
    new_hash = _file_hash(new_template_file)

    # New file in ACLI (not in project, regardless of old template)
    if current_hash is None and new_hash is not None:
        return FileCompareResult(
            path=relative_path,
            category=category,
            action="new",
            reason="New file in ACLI",
            new_template_hash=new_hash,
        )

    # File removed in new template
    if current_hash is not None and old_hash is not None and new_hash is None:
        if current_hash == old_hash:
            return FileCompareResult(
                path=relative_path,
                category=category,
                action="removed",
                reason="File removed in ACLI (you didn't modify it)",
                current_hash=current_hash,
                old_template_hash=old_hash,
            )
        return FileCompareResult(
            path=relative_path,
            category=category,
            action="conflict",
            reason="File removed in ACLI but you modified it",
            current_hash=current_hash,
            old_template_hash=old_hash,
        )

    # File only in current project (user-added, not part of ACLI template)
    if current_hash is not None and old_hash is None and new_hash is None:
        return FileCompareResult(
            path=relative_path,
            category=category,
            action="skip",
            reason="User-added file (not part of ACLI template)",
        )

    # File doesn't exist anywhere relevant
    if current_hash is None and new_hash is None:
        return FileCompareResult(
            path=relative_path,
            category=category,
            action="skip",
            reason="File not present",
        )

    # User didn't modify (current == old)
    if current_hash == old_hash and new_hash is not None:
        if old_hash == new_hash:
            return FileCompareResult(
                path=relative_path,
                category=category,
                action="preserve",
                reason="Unchanged in both project and ACLI",
                preserve_type="unchanged_both",
                current_hash=current_hash,
                old_template_hash=old_hash,
                new_template_hash=new_hash,
            )
        return FileCompareResult(
            path=relative_path,
            category=category,
            action="auto_update",
            reason="You didn't modify this file",
            current_hash=current_hash,
            old_template_hash=old_hash,
            new_template_hash=new_hash,
        )

    # ACLI didn't change (old == new)
    if old_hash == new_hash and current_hash is not None:
        return FileCompareResult(
            path=relative_path,
            category=category,
            action="preserve",
            reason="ACLI didn't change this file",
            preserve_type="acli_unchanged",
            current_hash=current_hash,
            old_template_hash=old_hash,
            new_template_hash=new_hash,
        )

    # Already up to date (current == new)
    if current_hash == new_hash:
        return FileCompareResult(
            path=relative_path,
            category=category,
            action="preserve",
            reason="Already up to date",
            preserve_type="already_current",
            current_hash=current_hash,
            old_template_hash=old_hash,
            new_template_hash=new_hash,
        )

    # Migrated eval dataset: present in project + new template but not the old
    # one (the old template shipped evalsets/, not datasets/). This is the
    # converted user content from migrate_legacy_evalsets; preserve it rather
    # than treating it as a conflict against the stock default.
    if (
        old_hash is None
        and relative_path.startswith(_NEW_DATASETS_DIR + "/")
        and relative_path.endswith("-dataset.json")
    ):
        return FileCompareResult(
            path=relative_path,
            category=category,
            action="preserve",
            reason="Migrated eval dataset (your content)",
            preserve_type="acli_unchanged",
            current_hash=current_hash,
            old_template_hash=old_hash,
            new_template_hash=new_hash,
        )

    # All three differ -> conflict
    return FileCompareResult(
        path=relative_path,
        category=category,
        action="conflict",
        reason="Both you and ACLI modified this file",
        current_hash=current_hash,
        old_template_hash=old_hash,
        new_template_hash=new_hash,
    )


def collect_all_files(
    base_dirs: list[pathlib.Path],
    exclude_patterns: list[str] | None = None,
) -> set[str]:
    """Collect all unique relative file paths across the given directories."""
    if exclude_patterns is None:
        exclude_patterns = [
            ".git/**",
            ".venv/**",
            "venv/**",
            "__pycache__/**",
            "*.pyc",
            ".DS_Store",
            "*.egg-info/**",
            "uv.lock",
            ".uv/**",
            "starter_pack_*",
        ]

    all_files: set[str] = set()

    for base_dir in base_dirs:
        if not base_dir.exists():
            continue
        for file_path in base_dir.rglob("*"):
            if file_path.is_file():
                relative = str(file_path.relative_to(base_dir))
                # Check exclusions using _matches_any_pattern for ** support
                if not _matches_any_pattern(relative, exclude_patterns):
                    all_files.add(relative)

    return all_files


def _parse_dependency(dep_str: str) -> tuple[str, str, str]:
    """Parse a dependency string into (base_name, extras, version_spec).

    Extras are separated from the package name so that packages with
    different extras brackets (e.g., ``pkg[a]`` vs ``pkg[a,b]``) are
    keyed by the same base name.

    Examples:
        "google-adk>=0.2.0" -> ("google-adk", "", ">=0.2.0")
        "google-cloud-aiplatform[evaluation]>=1.0" -> ("google-cloud-aiplatform", "[evaluation]", ">=1.0")
        "requests==2.31.0" -> ("requests", "", "==2.31.0")
        "pytest" -> ("pytest", "", "")
    """
    # Match base package name, optional extras in brackets, optional version spec
    match = re.match(r"^([a-zA-Z0-9_-]+)(\[[^\]]+\])?(.*)", dep_str.strip())
    if match:
        base_name = match.group(1).lower()
        extras = match.group(2) or ""
        version = match.group(3).strip()
        return base_name, extras, version
    return dep_str.lower(), "", ""


def _load_dependencies_from_pyproject(
    pyproject_path: pathlib.Path,
) -> dict[str, str]:
    """Load dependencies as {base_name: version_spec} dict.

    version_spec is the [extras]version suffix (e.g. "[eval]>=1.0").

    Raises DependencyReadError if pyproject.toml is missing or unparseable — a
    present file with no dependencies yields {}.
    """
    if not pyproject_path.exists():
        raise DependencyReadError(f"Dependency manifest not found: {pyproject_path}")

    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)

        deps = data.get("project", {}).get("dependencies", [])
        result: dict[str, str] = {}
        for dep in deps:
            name, extras, version = _parse_dependency(dep)
            result[name] = f"{extras}{version}"
        return result
    except Exception as e:
        raise DependencyReadError(
            f"Could not read dependencies from {pyproject_path}: {e}"
        ) from e


def _resolve_dependencies(
    current: dict[str, str],
    old: dict[str, str],
    new: dict[str, str],
) -> list[DependencyResolution]:
    """Merge deps from three sources.

    Returns a list of one DependencyResolution per dependency.

    New template deps take precedence over old template deps. User-added deps
    (``current - old``) are kept. Template deps dropped in ``new`` are removed.

    ``version_spec`` is the language-native suffix appended to the name to form
    the full identifier — Python ``"[extra]>=1.0"``, Go ``"@v1.2.3"``. Plain string comparison
    on it can be used to determine whether a dependency has changed.

    Returns one DependencyResolution per dependency, including "unchanged" ones.
    """
    resolutions: list[DependencyResolution] = []
    user_added = set(current) - set(old)
    acli_managed = set(old)

    for name, new_spec in new.items():
        if name in old:
            old_spec = old[name]
            resolutions.append(
                DependencyResolution(
                    name=name,
                    status="updated" if old_spec != new_spec else "unchanged",
                    old_version=old_spec,
                    new_version=new_spec,
                )
            )
        elif name in user_added:
            # The dependency is new to the template, but the user already added
            # it independently. Keep the user's version and emit a warning.
            # DependencyResolution will be added later as 'kept'.
            user_spec = current[name]
            logging.warning(
                f"Dependency '{name}' is now added by the template ({new_spec}) "
                f"but your project already pins it ({user_spec}). Keeping your "
                f"version; if the upgrade misbehaves, reconcile it with the "
                f"template's expected {new_spec}."
            )
        else:
            resolutions.append(
                DependencyResolution(name=name, status="added", new_version=new_spec)
            )

    for name in user_added:
        user_spec = current[name]
        resolutions.append(
            DependencyResolution(
                name=name,
                status="kept",
                old_version=user_spec,
                new_version=user_spec,
            )
        )

    for name in acli_managed:
        if name not in new and name not in user_added:
            resolutions.append(
                DependencyResolution(name=name, status="removed", old_version=old[name])
            )

    return resolutions


def merge_python_dependencies(
    current_project: pathlib.Path,
    old_template_project: pathlib.Path,
    new_template_project: pathlib.Path,
) -> list[DependencyResolution]:
    """Merge Python deps: new_template + user_added, where user_added = current - old.

    Returns a list of one DependencyResolution per dependency.
    """
    return _resolve_dependencies(
        _load_dependencies_from_pyproject(current_project / "pyproject.toml"),
        _load_dependencies_from_pyproject(old_template_project / "pyproject.toml"),
        _load_dependencies_from_pyproject(new_template_project / "pyproject.toml"),
    )


def write_python_dependencies(
    project_dir: pathlib.Path,
    resolutions: list[DependencyResolution],
) -> bool:
    """Write merged dependencies to project_dir/pyproject.toml using uv CLI.

    Uses ``uv add --frozen`` and ``uv remove --frozen`` so the lockfile
    and virtualenv are left untouched — only pyproject.toml is modified.

    Args:
        project_dir: The project directory containing pyproject.toml
        resolutions: Per-dependency merge resolutions to apply

    Returns:
        True if successful, False otherwise
    """
    pyproject_path = project_dir / "pyproject.toml"
    if not pyproject_path.exists():
        return False

    try:
        from google.agents.cli._runner import run_resolved
        from google.agents.cli._tools import ToolNotFoundError

        ordered: list[DependencyResolution] = sorted(resolutions, key=lambda r: r.name)
        to_remove = [r.name for r in ordered if r.status == "removed"]
        to_add = [r.full_name() for r in ordered if r.status != "removed"]

        if to_remove:
            result = run_resolved(
                ["uv", "remove", "--frozen", *to_remove],
                cwd=project_dir,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logging.warning(f"uv remove failed: {result.stderr}")

        if to_add:
            result = run_resolved(
                ["uv", "add", "--frozen", *to_add],
                cwd=project_dir,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logging.warning(f"uv add failed: {result.stderr}")
                return False

        return True
    # run_resolved raises ToolNotFoundError if the executable is not found
    except ToolNotFoundError:
        logging.warning("uv not found — cannot write merged dependencies")
        return False
    except Exception as e:
        logging.warning(f"Could not write dependencies to {pyproject_path}: {e}")
        return False


def load_go_mod_requirements(go_mod_path: pathlib.Path) -> dict[str, str]:
    """Load go.mod requirements as ``{module_path: "@version"}``.

    Parsing is delegated to ``go mod edit -json``.

    Raises DependencyReadError if go.mod is missing or can't be parsed
    (go toolchain unavailable, bad go.mod). A present go.mod with no requirements yields {}.
    See https://go.dev/ref/mod#go-mod-edit for the JSON schema.
    """
    if not go_mod_path.exists():
        raise DependencyReadError(f"Dependency manifest not found: {go_mod_path}")

    try:
        from google.agents.cli._runner import run_resolved

        result = run_resolved(
            ["go", "mod", "edit", "-json", str(go_mod_path)],
            capture_output=True,
            text=True,
        )
    except Exception as e:
        raise DependencyReadError(f"Could not read {go_mod_path}: {e}") from e

    if result.returncode != 0:
        raise DependencyReadError(
            f"go mod edit -json failed for {go_mod_path}: {result.stderr}"
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise DependencyReadError(
            f"Could not parse go.mod requirements from {go_mod_path}: {e}"
        ) from e

    requires: dict[str, str] = {}
    for req in data.get("Require") or []:
        if req.get("Path") and req.get("Version"):
            requires[req["Path"]] = f"@{req['Version']}"

    return requires


def merge_go_dependencies(
    current_project: pathlib.Path,
    old_template_project: pathlib.Path,
    new_template_project: pathlib.Path,
) -> list[DependencyResolution]:
    """Merge Go deps: new_template + user_added, where user_added = current - old.

    Same 3-way logic as :func:`merge_python_dependencies`, over go.mod requirements.
    """
    return _resolve_dependencies(
        load_go_mod_requirements(current_project / "go.mod"),
        load_go_mod_requirements(old_template_project / "go.mod"),
        load_go_mod_requirements(new_template_project / "go.mod"),
    )


def write_go_dependencies(
    project_dir: pathlib.Path,
    resolutions: list[DependencyResolution],
) -> bool:
    """Write merged Go requirements to project_dir/go.mod using ``go mod edit``.

    Applies ``-require`` for every non-removed dep and ``-droprequire`` for
    removed ones in a single invocation. ``go mod edit`` is offline and leaves
    go.sum (and ``module``/``go``/``replace`` directives) untouched.

    Returns True on success, False otherwise.
    """
    go_mod_path = project_dir / "go.mod"
    if not go_mod_path.exists():
        return False

    try:
        from google.agents.cli._runner import run_resolved
        from google.agents.cli._tools import ToolNotFoundError

        ordered = sorted(resolutions, key=lambda r: r.name)
        # full_name() is the module@version token that ``go mod edit -require``
        # expects.
        edit_args = [f"-droprequire={r.name}" for r in ordered if r.status == "removed"]
        edit_args += [
            f"-require={r.full_name()}" for r in ordered if r.status != "removed"
        ]

        if not edit_args:
            return True

        result = run_resolved(
            ["go", "mod", "edit", *edit_args, str(go_mod_path)],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logging.warning(f"go mod edit failed: {result.stderr}")
            return False
        return True
    # run_resolved raises ToolNotFoundError if the executable is not found
    except ToolNotFoundError:
        logging.warning("go not found — cannot write merged go.mod dependencies")
        return False
    except Exception as e:
        logging.warning(f"Could not write dependencies to {go_mod_path}: {e}")
        return False


def union_python_dependencies(
    current_project: pathlib.Path,
    new_template_project: pathlib.Path,
) -> list[DependencyResolution]:
    """Union-merge pyproject.toml deps with the new template.

    With no old template, the deps the new template still ships act as the old
    template, so nothing is removed and every other dep is kept.
    """
    current = _load_dependencies_from_pyproject(current_project / "pyproject.toml")
    new = _load_dependencies_from_pyproject(new_template_project / "pyproject.toml")
    old = {name: spec for name, spec in current.items() if name in new}
    return _resolve_dependencies(current, old, new)


def union_python_base_dependencies(
    current_project: pathlib.Path,
    base_dependencies: list[str],
) -> list[DependencyResolution]:
    """Union-merge pyproject.toml deps with a list of requirement strings.

    Same as union_python_dependencies, but the new side is a list such as a
    base template's extra_dependencies, not a pyproject.toml.
    """
    current = _load_dependencies_from_pyproject(current_project / "pyproject.toml")
    new: dict[str, str] = {}
    for dep in base_dependencies:
        name, extras, version = _parse_dependency(dep)
        new[name] = f"{extras}{version}"
    old = {name: spec for name, spec in current.items() if name in new}
    return _resolve_dependencies(current, old, new)


def union_go_dependencies(
    current_project: pathlib.Path,
    new_template_project: pathlib.Path,
) -> list[DependencyResolution]:
    """Union-merge go.mod requirements with the new template.

    See union_python_dependencies for how the old template is derived.
    """
    current = load_go_mod_requirements(current_project / "go.mod")
    new = load_go_mod_requirements(new_template_project / "go.mod")
    old = {name: spec for name, spec in current.items() if name in new}
    return _resolve_dependencies(current, old, new)


# Per-language dependency handlers; both take a project directory. None = not
# supported for that language yet.
MERGE_DEPENDENCY_HANDLERS: dict[str, Callable | None] = {
    "python": merge_python_dependencies,
    "go": merge_go_dependencies,
    "java": None,
    "typescript": None,
    "kotlin": None,
}

# Used by --force.
UNION_DEPENDENCY_HANDLERS: dict[str, Callable | None] = {
    "python": union_python_dependencies,
    "go": union_go_dependencies,
    "java": None,
    "typescript": None,
    "kotlin": None,
}

WRITE_DEPENDENCY_HANDLERS: dict[str, Callable | None] = {
    "python": write_python_dependencies,
    "go": write_go_dependencies,
    "java": None,
    "typescript": None,
    "kotlin": None,
}


def update_acli_metadata(
    project_dir: pathlib.Path,
    create_params: dict[str, Any],
    *,
    acli_version: str | None = None,
    remove_keys: list[str] | None = None,
    base_template: str | None = None,
    region: str | None = None,
) -> bool:
    """Update specific keys in the unified agents-cli-manifest.yaml metadata file.

    A manifest that cannot be read or written is logged and skipped: every
    caller writes metadata as a side effect of work that already succeeded.
    Returns whether the manifest was written.

    Args:
        project_dir: Path to the project directory
        create_params: Dict of keys to update inside create_params
        acli_version: If provided, update acli_version
        remove_keys: List of keys to remove from create_params section
        base_template: If provided, record it at the manifest top level
        region: If provided, record it at the manifest top level

    Returns:
        True if the manifest was written.
    """
    manifest_path = project_dir / "agents-cli-manifest.yaml"
    if not manifest_path.exists():
        logging.warning(f"Manifest not found: {manifest_path}")
        return False

    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        if acli_version:
            data["acli_version"] = acli_version

        if base_template is not None:
            data["base_template"] = base_template

        if region is not None:
            data["region"] = region

        if "create_params" not in data or not isinstance(data["create_params"], dict):
            data["create_params"] = {}

        params = data["create_params"]

        for key, val in create_params.items():
            if key in NESTED_CREATE_PARAMS:
                param = NESTED_CREATE_PARAMS[key]
                if not isinstance(params.get(param.section), dict):
                    params[param.section] = {}
                params[param.section][param.key] = val
            else:
                params[key] = val

        if remove_keys:
            for key in remove_keys:
                if key in NESTED_CREATE_PARAMS:
                    param = NESTED_CREATE_PARAMS[key]
                    values = params.get(param.section)
                    if isinstance(values, dict):
                        values.pop(param.key, None)
                        if not values:
                            del params[param.section]
                else:
                    params.pop(key, None)

        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    except Exception as e:
        logging.warning(f"Could not update ACLI metadata in {manifest_path}: {e}")
        return False
    return True


def migrate_legacy_python_config(
    project_dir: pathlib.Path, dry_run: bool = False
) -> None:
    """Extract legacy [tool.agents-cli] config from pyproject.toml and write to agents-cli-manifest.yaml."""
    pyproject_path = project_dir / "pyproject.toml"
    manifest_path = project_dir / "agents-cli-manifest.yaml"

    if not pyproject_path.exists():
        return

    import tomlkit

    content = pyproject_path.read_text(encoding="utf-8")
    doc = tomlkit.parse(content)

    tool_section = doc.get("tool", {})
    acli = tool_section.get("agents-cli")
    if acli is None:
        return

    if manifest_path.exists():
        import click

        click.secho(
            "  ▸ Legacy [tool.agents-cli] section found in pyproject.toml but agents-cli-manifest.yaml already exists. The legacy config will be ignored and that section can be safely deleted.",
            fg="yellow",
            dim=True,
        )
        return

    if dry_run:
        import click

        click.secho(
            "  ▸ [Dry run] Legacy pyproject.toml configuration would be migrated to agents-cli-manifest.yaml",
            fg="yellow",
            dim=True,
        )
        return

    # Reconstruct new manifest content
    manifest_data = {}
    project_section = doc.get("project", {})
    name = project_section.get("name") or acli.get("name")
    if name:
        manifest_data["name"] = str(name)

    # Copy all config parameters directly, except name
    acli_unwrapped = acli.unwrap()
    for k, v in acli_unwrapped.items():
        if k != "name" and v is not None:
            manifest_data[k] = v

    import yaml

    with open(manifest_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(manifest_data, f, default_flow_style=False, sort_keys=False)

    # Remove legacy tool config lines from pyproject.toml last, after manifest write succeeds
    del tool_section["agents-cli"]
    if not tool_section:
        del doc["tool"]
    pyproject_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    import click

    click.secho(
        "  ▸ Legacy pyproject.toml configuration successfully migrated to agents-cli-manifest.yaml",
        fg="cyan",
        dim=True,
    )


_LEGACY_EVALSETS_DIR = "tests/eval/evalsets"
_NEW_DATASETS_DIR = "tests/eval/datasets"
_LEGACY_EVALSET_GLOB = "*.evalset.json"
_LEGACY_EVAL_CONFIG = "tests/eval/eval_config.json"
_NEW_EVAL_CONFIG = "tests/eval/eval_config.yaml"
_EVAL_MIGRATION_URL = (
    "https://google.github.io/agents-cli/reference/eval-dataset-migration/"
)


def _convert_eval_case(old_case: dict[str, Any]) -> dict[str, Any]:
    new_case: dict[str, Any] = {}
    case_id = old_case.get("eval_id") or old_case.get("eval_case_id")
    if case_id:
        new_case["eval_case_id"] = case_id

    conversation = old_case.get("conversation") or []
    if not conversation:
        return new_case

    if len(conversation) == 1:
        turn = conversation[0]
        user_parts = (turn.get("user_content") or {}).get("parts") or []
        new_case["prompt"] = {"role": "user", "parts": user_parts}
        final_response = turn.get("final_response")
        if final_response:
            new_case["reference"] = {
                "response": {
                    "role": "model",
                    "parts": final_response.get("parts") or [],
                }
            }
        return new_case

    events: list[dict[str, Any]] = []
    last_idx = len(conversation) - 1
    for i, turn in enumerate(conversation):
        user_parts = (turn.get("user_content") or {}).get("parts") or []
        events.append(
            {
                "author": "user",
                "content": {"role": "user", "parts": user_parts},
            }
        )
        final_response = turn.get("final_response")
        if not final_response:
            continue
        if i < last_idx:
            events.append(
                {
                    "author": "agent",
                    "content": {
                        "role": "model",
                        "parts": final_response.get("parts") or [],
                    },
                }
            )
        else:
            new_case["reference"] = {
                "response": {
                    "role": "model",
                    "parts": final_response.get("parts") or [],
                }
            }
    new_case["agent_data"] = {
        "turns": [{"turn_index": 0, "events": events}],
    }
    return new_case


def _convert_eval_set(old_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "eval_cases": [
            _convert_eval_case(c) for c in (old_payload.get("eval_cases") or [])
        ]
    }


def _legacy_to_new_filename(legacy_name: str) -> str:
    stem = legacy_name[: -len(".evalset.json")]
    return f"{stem}-dataset.json"


def migrate_legacy_evalsets(project_dir: pathlib.Path, dry_run: bool = False) -> None:
    """Convert tests/eval/evalsets/*.evalset.json to tests/eval/datasets/*-dataset.json.

    No-op when the legacy directory is absent. Skips destination files
    that already exist, and warns only when one holds different JSON than
    the converted file. Reports legacy files it cannot read or convert.
    Does not delete the legacy directory; the user removes it after
    verifying the conversion.
    """
    import click

    legacy_dir = project_dir / _LEGACY_EVALSETS_DIR
    if not legacy_dir.is_dir():
        return

    legacy_files = sorted(legacy_dir.glob(_LEGACY_EVALSET_GLOB))
    if not legacy_files:
        return

    output_dir = project_dir / _NEW_DATASETS_DIR

    if dry_run:
        count = len(legacy_files)
        noun = "file" if count == 1 else "files"
        click.secho(
            f"  ▸ [Dry run] {count} legacy eval {noun} would be migrated "
            f"to {_NEW_DATASETS_DIR}/",
            fg="yellow",
            dim=True,
        )
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    converted = 0
    conflicting: list[str] = []
    failed: list[tuple[str, str]] = []

    for src in legacy_files:
        dest = output_dir / _legacy_to_new_filename(src.name)
        try:
            old_payload = json.loads(src.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            failed.append((src.name, str(e)))
            continue
        try:
            new_payload = _convert_eval_set(old_payload)
        except (AttributeError, TypeError):
            failed.append((src.name, "not in the legacy .evalset.json format"))
            continue
        if dest.exists():
            if _read_json_or_none(dest) != new_payload:
                conflicting.append(dest.name)
            continue
        dest.write_text(json.dumps(new_payload, indent=2) + "\n", encoding="utf-8")
        converted += 1

    if converted:
        noun = "file" if converted == 1 else "files"
        click.secho(
            f"  ▸ Migrated {converted} legacy eval {noun} to {_NEW_DATASETS_DIR}/. "
            "Run `agents-cli eval generate` to populate the trace.",
            fg="cyan",
            dim=True,
        )
    if conflicting:
        logging.warning(
            "Did NOT migrate %d legacy eval file(s) — these files already exist "
            "in %s/ and differ from the converted ones: %s. The legacy file(s) "
            "remain in %s/ unconverted. Reconcile manually before deleting the "
            "legacy directory.",
            len(conflicting),
            _NEW_DATASETS_DIR,
            ", ".join(conflicting),
            _LEGACY_EVALSETS_DIR,
        )
    if failed:
        for name, err in failed:
            click.secho(f"  ▸ Failed to migrate {name}: {err}", fg="red", dim=True)
        click.secho(
            f"  ▸ See migration guide: {_EVAL_MIGRATION_URL}",
            fg="yellow",
            dim=True,
        )
    if converted and not conflicting and not failed:
        click.secho(
            f"  ▸ All legacy evalsets migrated. You can delete "
            f"{_LEGACY_EVALSETS_DIR}/ once you've verified the converted files.",
            fg="cyan",
            dim=True,
        )


def _read_json_or_none(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None


def warn_legacy_eval_config(project_dir: pathlib.Path) -> None:
    """Warn if a legacy tests/eval/eval_config.json is left over after upgrade."""
    if not (project_dir / _LEGACY_EVAL_CONFIG).is_file():
        return

    logging.warning(
        "Found legacy %s. Grading now reads %s; your custom criteria will NOT "
        "apply until migrated. The two schemas differ, so this is not "
        "auto-converted. See migration guide: %s",
        _LEGACY_EVAL_CONFIG,
        _NEW_EVAL_CONFIG,
        _EVAL_MIGRATION_URL,
    )


def compare_all_files(
    project_dir: pathlib.Path,
    old_template_dir: pathlib.Path,
    new_template_dir: pathlib.Path,
    agent_directory: str = "app",
) -> list[FileCompareResult]:
    """Compare all files using 3-way comparison."""
    all_files = collect_all_files([project_dir, old_template_dir, new_template_dir])

    results = []
    for relative_path in sorted(all_files):
        result = three_way_compare(
            relative_path,
            project_dir,
            old_template_dir,
            new_template_dir,
            agent_directory,
        )
        results.append(result)

    return results


def group_results_by_action(
    results: list[FileCompareResult],
) -> dict[str, list[FileCompareResult]]:
    """Group results by action type."""
    groups: dict[str, list[FileCompareResult]] = {
        "auto_update": [],
        "preserve": [],
        "skip": [],
        "conflict": [],
        "new": [],
        "removed": [],
    }

    for result in results:
        groups[result.action].append(result)

    return groups
