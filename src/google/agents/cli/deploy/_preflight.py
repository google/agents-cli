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

"""Offline pre-flight checks for source deploys of Python projects.

Runs before ``agents-cli deploy`` uploads source for a remote build (Agent
Runtime, Cloud Run ``--source``, GKE via Cloud Build), and catches project
problems that are certain to fail that build, or to break the deployed agent.
Otherwise they only show up as an opaque "Build failed" minutes later, with the
real cause buried in a build log most users never find.

Every check is offline: it only reads files in the project, so the whole step
takes about a second. An ``error`` finding blocks the deploy and a ``warning``
does not. ``--skip-preflight`` bypasses the checks.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from google.agents.cli._uv_index import (
    PIN_PUBLIC_INDEX_SNIPPET,
    RELOCK_PUBLIC_COMMAND,
    format_index_counts,
    load_toml,
    non_public_lock_indexes,
    project_pins_default_index,
)

ERROR = "error"
WARNING = "warning"

_SKIP_HINT = "Skip with --skip-preflight if you're sure the remote build will succeed."


@dataclass(frozen=True)
class Finding:
    """One pre-flight result.

    ``check`` is a stable identifier (e.g. ``lockfile-index``) so callers, such
    as coding agents, can match on it; ``message`` names the cause and the fix;
    ``skip_hint`` says when bypassing the check with --skip-preflight is safe.
    """

    check: str
    severity: str
    message: str
    skip_hint: str = _SKIP_HINT


@dataclass(frozen=True)
class _Project:
    """The project files every check reads, loaded once."""

    root: Path
    pyproject: dict[str, Any]
    lock: dict[str, Any] | None
    dockerfile: list[tuple[str, str]] | None
    packaged: frozenset[str]

    @property
    def ignore_file(self) -> str:
        if (self.root / ".gcloudignore").exists():
            return ".gcloudignore"
        return ".gitignore"


def run_preflight(root: Path) -> list[Finding]:
    """Run every pre-flight check against the project at ``root``.

    Returns no findings when ``root`` has no pyproject.toml, since every check
    is about a uv-managed Python build.
    """
    pyproject = load_toml(root / "pyproject.toml")
    if pyproject is None:
        return []
    # Same file list the Agent Runtime upload uses. Imported here so loading
    # this module stays cheap (agent_runtime pulls in the Cloud SDKs).
    from google.agents.cli.deploy.agent_runtime import _packaged_files

    dockerfile_path = root / "Dockerfile"
    project = _Project(
        root=root,
        pyproject=pyproject,
        lock=load_toml(root / "uv.lock"),
        dockerfile=(
            _parse_dockerfile(dockerfile_path.read_text(encoding="utf-8"))
            if dockerfile_path.is_file()
            else None
        ),
        packaged=frozenset(p.removeprefix("./") for p in _packaged_files(root)),
    )
    findings: list[Finding] = []
    for check in _CHECKS:
        findings.extend(check(project))
    return findings


def check_before_deploy(root: Path, *, dry_run: bool) -> None:
    """Run the pre-flight checks, report the findings, and block on errors.

    Raises:
        click.ClickException: If any check found an error.
    """
    findings = run_preflight(root)
    for finding in findings:
        if finding.severity == WARNING:
            logging.warning("Pre-flight [%s]: %s", finding.check, finding.message)
    errors = [f for f in findings if f.severity == ERROR]
    if errors:
        body = "\n\n".join(
            dict.fromkeys(f"Pre-flight [{f.check}]: {f.message}" for f in errors)
        )
        hints = "\n".join(dict.fromkeys(f.skip_hint for f in errors))  # Dedupe.
        raise click.ClickException(f"{body}\n\n{hints}")
    if dry_run:
        logging.info("✓ Pre-flight checks passed.")


# --- Lockfile ---------------------------------------------------------------


def _check_lockfile_index(project: _Project) -> list[Finding]:
    """uv.lock pins packages to an index the remote builder may not reach."""
    if project.lock is None:
        return []
    undeclared, pinned = non_public_lock_indexes(project.root, project.lock)
    findings = []
    if undeclared:
        total, urls = format_index_counts(undeclared)
        findings.append(
            Finding(
                "lockfile-index",
                ERROR,
                f"uv.lock pins {total} packages to a package index that remote "
                f"builds may not be able to reach:\n{urls}\n"
                "  This usually comes from a machine-wide package manager config "
                "(e.g. a system uv.toml).\n"
                "  Fix (any one):\n"
                "    • Re-lock against the public index:\n"
                f"        {RELOCK_PUBLIC_COMMAND}\n"
                "    • Pin the public index in pyproject.toml so future locks stay "
                "deployable:\n" + _indent(PIN_PUBLIC_INDEX_SNIPPET, 8),
                skip_hint="Skip with --skip-preflight if this index is reachable "
                "from your build environment.",
            )
        )
    if pinned:
        total, urls = format_index_counts(pinned)
        findings.append(
            Finding(
                "lockfile-index",
                WARNING,
                f"uv.lock pins {total} packages to a package index other than "
                f"PyPI, declared in the project:\n{urls}\n"
                "  The remote build fails if it can't reach this index.",
            )
        )
    return findings


def _check_lockfile_missing(project: _Project) -> list[Finding]:
    """The Dockerfile needs uv.lock (``uv sync --frozen``/``--locked``)."""
    if project.lock is not None or (project.root / "uv.lock").exists():
        return []
    if not any(
        instruction == "RUN"
        and re.search(r"\buv\s+sync\b", args)
        and re.search(r"--(frozen|locked)\b", args)
        for instruction, args in project.dockerfile or []
    ):
        return []
    return [
        Finding(
            "lockfile-missing",
            ERROR,
            "uv.lock not found, but the Dockerfile installs dependencies with "
            "`uv sync --frozen`/`--locked`, which requires it.\n"
            f"  Generate it with `{_relock_command(project)}` and redeploy.",
        )
    ]


def _check_lockfile_stale(project: _Project) -> list[Finding]:
    """uv.lock doesn't match pyproject.toml's declared dependencies."""
    if project.lock is None:
        if (project.root / "uv.lock").exists():
            return [
                Finding(
                    "lockfile-stale",
                    ERROR,
                    "uv.lock is not valid TOML, so the remote build cannot "
                    "read it.\n"
                    f"  Regenerate it with `{_relock_command(project)}` and redeploy.",
                )
            ]
        return []
    project_table = project.pyproject.get("project")
    if not isinstance(project_table, dict) or not isinstance(
        project_table.get("name"), str
    ):
        return []
    metadata = _root_lock_metadata(project.lock, project_table["name"])
    if metadata is None:
        return []

    differences: list[str] = []
    if "dependencies" not in _as_list(project_table.get("dynamic")):
        declared = [*_as_list(project_table.get("dependencies"))]
        optional = project_table.get("optional-dependencies")
        for extra in (optional if isinstance(optional, dict) else {}).values():
            declared += _as_list(extra)
        differences += _diff_requirements(
            declared, _as_list(metadata.get("requires-dist")), group=None
        )

    raw_groups = metadata.get("requires-dev")
    locked_groups: dict[str, Any] = {
        canonicalize_name(name): items
        for name, items in (raw_groups if isinstance(raw_groups, dict) else {}).items()
    }
    declared_groups = _declared_dependency_groups(project.pyproject)
    for group in sorted(set(declared_groups) | set(locked_groups)):
        declared_group = declared_groups.get(group, [])
        if any(not isinstance(item, str) for item in declared_group):
            continue  # `{include-group = ...}` entries: too uv-specific to compare.
        differences += _diff_requirements(
            declared_group, _as_list(locked_groups.get(group)), group=group
        )

    if not differences:
        return []
    shown = "\n".join(f"    • {d}" for d in differences[:5])
    more = f"\n    • ...and {len(differences) - 5} more" if len(differences) > 5 else ""
    return [
        Finding(
            "lockfile-stale",
            ERROR,
            "uv.lock is out of sync with pyproject.toml, so the build would install "
            f"an outdated set of dependencies:\n{shown}{more}\n"
            f"  Re-lock with `{_relock_command(project)}` and redeploy.",
        )
    ]


def _root_lock_metadata(lock: dict[str, Any], name: str) -> dict[str, Any] | None:
    """``[package.metadata]`` of the project itself in uv.lock."""
    wanted = canonicalize_name(name)
    for package in _as_list(lock.get("package")):
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            continue
        source = package.get("source")
        is_root = isinstance(source, dict) and "." in (
            source.get("editable"),
            source.get("virtual"),
        )
        if is_root and canonicalize_name(package["name"]) == wanted:
            metadata = package.get("metadata")
            return metadata if isinstance(metadata, dict) else None
    return None


def _declared_dependency_groups(pyproject: dict[str, Any]) -> dict[str, list[Any]]:
    """PEP 735 ``[dependency-groups]``, plus uv's legacy ``dev-dependencies``."""
    groups: dict[str, list[Any]] = {}
    raw = pyproject.get("dependency-groups")
    for name, items in (raw if isinstance(raw, dict) else {}).items():
        groups[canonicalize_name(name)] = _as_list(items)
    tool = pyproject.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    legacy_dev = uv.get("dev-dependencies") if isinstance(uv, dict) else None
    if isinstance(legacy_dev, list):
        groups.setdefault("dev", []).extend(legacy_dev)
    return groups


# Keys uv writes on a requires-dist entry resolved from a path/git/URL source.
_LOCKED_SOURCE_KEYS = ("path", "directory", "editable", "git", "url")


def _diff_requirements(
    declared: Iterable[Any], locked: Iterable[Any], *, group: str | None
) -> list[str]:
    """Human-readable differences between declared and locked requirements.

    Compares names, extras and version specifiers (as sets, so ordering and
    formatting don't matter). Markers are ignored: uv rewrites them.
    """
    where = f" (dependency group '{group}')" if group else ""
    declared_reqs: dict[str, set[tuple[frozenset[str], SpecifierSet | None]]] = {}
    for item in declared:
        try:
            req = Requirement(item) if isinstance(item, str) else None
        except InvalidRequirement:
            req = None
        if req is None:
            continue
        spec = None if req.url else req.specifier
        declared_reqs.setdefault(canonicalize_name(req.name), set()).add(
            (frozenset(canonicalize_name(e) for e in req.extras), spec)
        )

    # Locked spec: a SpecifierSet (empty if unconstrained), or None when uv
    # recorded a path/git/URL source (it drops the specifier for those).
    locked_reqs: dict[str, set[tuple[frozenset[str], SpecifierSet | None]]] = {}
    for item in locked:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        if any(key in item for key in _LOCKED_SOURCE_KEYS):
            spec = None
        else:
            raw_spec = item.get("specifier")
            try:
                spec = SpecifierSet(raw_spec if isinstance(raw_spec, str) else "")
            except InvalidSpecifier:
                spec = None
        extras = frozenset(
            canonicalize_name(e)
            for e in _as_list(item.get("extras"))
            if isinstance(e, str)
        )
        locked_reqs.setdefault(canonicalize_name(item["name"]), set()).add((extras, spec))

    differences = [
        f"'{name}' is in pyproject.toml but not in uv.lock{where}"
        for name in sorted(set(declared_reqs) - set(locked_reqs))
    ] + [
        f"'{name}' is in uv.lock but no longer in pyproject.toml{where}"
        for name in sorted(set(locked_reqs) - set(declared_reqs))
    ]
    for name in sorted(set(declared_reqs) & set(locked_reqs)):
        locked_entries = locked_reqs[name]
        declared_entries = declared_reqs[name]
        if not (
            all(
                any(
                    _requirement_matches(extras, spec, locked_extras, locked_spec)
                    for locked_extras, locked_spec in locked_entries
                )
                for extras, spec in declared_entries
            )
            and all(
                any(
                    _requirement_matches(extras, spec, locked_extras, locked_spec)
                    for extras, spec in declared_entries
                )
                for locked_extras, locked_spec in locked_entries
            )
        ):
            differences.append(f"'{name}' requirement changed in pyproject.toml{where}")
    return differences


def _requirement_matches(
    extras: frozenset[str],
    spec: SpecifierSet | None,
    locked_extras: frozenset[str],
    locked_spec: SpecifierSet | None,
) -> bool:
    if extras != locked_extras:
        return False
    # None means a URL requirement (declared) or a path/git/URL source (locked):
    # uv drops the specifier for those, so there is no version to compare. An
    # unconstrained dependency is an empty SpecifierSet, so adding a constraint
    # to it in pyproject.toml is still caught.
    if spec is None or locked_spec is None:
        return True
    return spec == locked_spec


# --- Build inputs -----------------------------------------------------------


def _check_build_inputs_uploaded(project: _Project) -> list[Finding]:
    """The Dockerfile, or files it COPYs/ADDs, exist locally but are excluded
    from the upload by .gcloudignore/.gitignore."""
    findings = []
    if project.dockerfile is not None and "Dockerfile" not in project.packaged:
        findings.append(
            Finding(
                "build-input-excluded",
                ERROR,
                f"The Dockerfile is present but {project.ignore_file} excludes it "
                "from the upload, so the remote build can't use it.\n"
                f"  Remove the matching pattern from {project.ignore_file}.",
            )
        )
    for source in _dockerfile_copy_sources(project.dockerfile or []):
        for rel in _excluded_paths(project, source):
            findings.append(
                Finding(
                    "build-input-excluded",
                    ERROR,
                    f"The Dockerfile copies '{rel}', but {project.ignore_file} "
                    "excludes it from the upload, so the remote build can't find "
                    "it.\n"
                    f"  Remove the matching pattern from {project.ignore_file} (or "
                    f"add a '!{rel}' exception).",
                )
            )
    return findings


def _check_path_dependencies(project: _Project) -> list[Finding]:
    """Local path dependencies ([tool.uv.sources] ``path = ...``) that the
    upload won't contain."""
    tool = project.pyproject.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    sources = uv.get("sources") if isinstance(uv, dict) else None
    if not isinstance(sources, dict):
        return []
    root = project.root.resolve()
    findings = []
    for name, entries in sorted(sources.items()):
        candidates: list[Any] = entries if isinstance(entries, list) else [entries]
        for entry in candidates:
            path = entry.get("path") if isinstance(entry, dict) else None
            if not isinstance(path, str):
                continue
            resolved = (project.root / path).resolve()
            if not resolved.is_relative_to(root):
                findings.append(
                    Finding(
                        "path-dependency-outside-upload",
                        ERROR,
                        f"Dependency '{name}' comes from the local path '{path}', "
                        "which is outside the project directory, so the deploy "
                        "doesn't upload it and the remote build can't install it.\n"
                        "  Move it inside the project directory (and update the "
                        "path), or publish it to a package index the build can "
                        "reach.",
                    )
                )
                continue
            rel = resolved.relative_to(root).as_posix()
            if rel != "." and _is_excluded(project, rel):
                findings.append(
                    Finding(
                        "path-dependency-outside-upload",
                        ERROR,
                        f"Dependency '{name}' comes from the local path '{path}', "
                        f"but {project.ignore_file} excludes it from the upload, "
                        "so the remote build can't install it.\n"
                        f"  Remove the matching pattern from {project.ignore_file}.",
                    )
                )
    return findings


def _excluded_paths(project: _Project, source: str) -> list[str]:
    """Project-relative paths matching a COPY source that exist locally but are
    excluded from the upload. Sources that don't exist are left to Docker."""
    root = project.root.resolve()
    if any(c in source for c in "*?["):
        try:
            matches = sorted(project.root.glob(source))
        except (ValueError, IndexError, NotImplementedError):
            return []
    else:
        path = project.root / source
        matches = [path] if path.exists() else []
    excluded = []
    for match in matches:
        resolved = match.resolve()
        if not resolved.is_relative_to(root):
            continue
        rel = resolved.relative_to(root).as_posix()
        if rel != "." and _is_excluded(project, rel):
            excluded.append(rel)
    return excluded


def _is_excluded(project: _Project, rel: str) -> bool:
    """Whether an existing file, or every file in an existing directory, is
    left out of the upload."""
    path = project.root / rel
    if path.is_file():
        return rel not in project.packaged
    if not path.is_dir() or not any(files for _, _, files in os.walk(path)):
        return False  # An empty directory has nothing to lose.
    prefix = f"{rel}/"
    return not any(p.startswith(prefix) for p in project.packaged)


def _dockerfile_copy_sources(
    instructions: list[tuple[str, str]],
) -> list[str]:
    """Build-context sources of COPY/ADD instructions (not ``--from`` stages,
    URLs, or variable references)."""
    sources: list[str] = []
    for instruction, args in instructions:
        if instruction not in ("COPY", "ADD"):
            continue
        tokens = _instruction_tokens(args)
        flags = [t for t in tokens if t.startswith("--")]
        if any(f.startswith("--from") for f in flags):
            continue
        paths = [t for t in tokens if not t.startswith("--")]
        for source in paths[:-1]:  # The last path is the destination.
            if "://" in source or "$" in source:
                continue
            source = source.lstrip("/")
            while source.startswith("./"):
                source = source[2:]
            source = source.rstrip("/")
            if source and source != ".":
                sources.append(source)
    return sources


def _instruction_tokens(args: str) -> list[str]:
    """Split instruction arguments, in exec (JSON array) or shell form."""
    flags = []
    rest = args
    # Flags come first in both forms: COPY --chown=a:b ["src", "dst"]
    while rest.startswith("--"):
        flag, *tail = rest.split(maxsplit=1)
        flags.append(flag)
        rest = tail[0] if tail else ""
    if rest.startswith("["):
        try:
            parsed = json.loads(rest)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list) and all(isinstance(p, str) for p in parsed):
            return flags + parsed
    return flags + rest.split()


# --- Python version ---------------------------------------------------------

_PYTHON_IMAGE_TAG_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")
_UV_IMAGE_TAG_RE = re.compile(r"python(\d+)\.(\d+)(?:\.(\d+))?")


def _check_python_version(project: _Project) -> list[Finding]:
    """The Dockerfile's base image Python doesn't satisfy requires-python."""
    project_table = project.pyproject.get("project")
    requires_python = (
        project_table.get("requires-python") if isinstance(project_table, dict) else None
    )
    if not isinstance(requires_python, str):
        return []
    try:
        spec = SpecifierSet(requires_python)
    except InvalidSpecifier:
        return []
    findings = []
    from_instructions = [
        args for instruction, args in (project.dockerfile or []) if instruction == "FROM"
    ]
    for args in from_instructions[-1:] if from_instructions else []:
        image = next((t for t in args.split() if not t.startswith("--")), "")
        version = _image_python_version(image)
        if version is None:
            continue
        major, minor, patch = version
        if patch is not None:
            satisfied = spec.contains(Version(f"{major}.{minor}.{patch}"))
        else:
            # A bare X.Y tag tracks the latest patch release.
            satisfied = any(spec.contains(f"{major}.{minor}.{p}") for p in range(100))
        if not satisfied:
            shown = f"{major}.{minor}" + (f".{patch}" if patch is not None else "")
            findings.append(
                Finding(
                    "python-version",
                    ERROR,
                    f"The Dockerfile builds on Python {shown} (FROM {image}), "
                    f"which doesn't satisfy requires-python '{requires_python}' "
                    "in pyproject.toml.\n"
                    "  Change the base image to a Python version in that range, or "
                    "update requires-python.",
                )
            )
    return findings


def _image_python_version(image: str) -> tuple[int, int, int | None] | None:
    """Python version of an official ``python`` or astral ``uv`` image tag."""
    if not image or "$" in image:
        return None
    name, _, tag = image.split("@", 1)[0].rsplit("/", 1)[-1].partition(":")
    if name == "python":
        match = _PYTHON_IMAGE_TAG_RE.match(tag)
    elif name == "uv":
        match = _UV_IMAGE_TAG_RE.search(tag)
    else:
        return None
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch) if patch is not None else None


# --- Helpers ----------------------------------------------------------------


def _parse_dockerfile(text: str) -> list[tuple[str, str]]:
    """``(INSTRUCTION, arguments)`` pairs, with line continuations joined and
    comments dropped."""
    logical_lines: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#") or (not line and not buffer):
            continue
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        logical_lines.append(buffer + line)
        buffer = ""
    logical_lines.append(buffer)  # A trailing continuation with no last line.
    instructions = []
    for logical in logical_lines:
        parts = logical.split(maxsplit=1)  # Any whitespace, tabs included.
        if parts:
            instructions.append((parts[0].upper(), parts[1] if len(parts) > 1 else ""))
    return instructions


def _relock_command(project: _Project) -> str:
    """How to (re-)lock without picking up a machine-wide mirror."""
    if project_pins_default_index(project.root):
        return "uv lock"
    return RELOCK_PUBLIC_COMMAND


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _indent(text: str, width: int) -> str:
    return "\n".join(" " * width + line for line in text.splitlines())


_CHECKS: tuple[Callable[[_Project], list[Finding]], ...] = (
    _check_lockfile_index,
    _check_lockfile_missing,
    _check_lockfile_stale,
    _check_build_inputs_uploaded,
    _check_path_dependencies,
    _check_python_version,
)
