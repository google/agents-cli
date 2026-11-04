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

"""Fitting a remote ADK Go template onto a project rendered from adk_go."""

import logging
import pathlib
import re
from collections.abc import Iterator
from dataclasses import dataclass

import click

from google.agents.cli._runner import run
from google.agents.cli._symlinks import require_safe_symlink
from google.agents.cli._tools import ToolNotExecutableError, ToolNotFoundError

from .fs import is_ignored_name, walk_source_tree
from .language import get_language_config
from .upgrade import (
    DependencyReadError,
    DependencyResolution,
    load_go_mod_requirements,
    write_go_dependencies,
)

_ADK_GO_MODULE = "google.golang.org/adk"
_SUPPORTED_ADK_GO_MAJOR_VERSION = 2

# A template's copies are skipped; its go.mod and go.sum are merged in instead.
SCAFFOLD_ROOT_FILES = frozenset(
    {"main.go", "main_test.go", "go.mod", "go.sum", "Dockerfile"}
)
# Packages the generated main.go imports by path; its Dockerfile copies them by name.
SCAFFOLD_PACKAGES = frozenset({"appinfo", "sessions"})

_PACKAGE_MAIN = re.compile(
    r"^[ \t]*package\s+main(_test)?(?=\s*(?://.*)?$)", re.MULTILINE
)
_GO_MODULE = re.compile(r"^module\s+(\S+)", re.MULTILINE)
_GO_DIRECTIVE = re.compile(r"^go\s+(\d+(?:\.\d+)*)(?=\s*(?://.*)?$)", re.MULTILINE)
_UNMERGED_DIRECTIVES = ("replace", "toolchain")
_GOLANG_IMAGE = re.compile(r"^FROM\s+golang:(\d+(?:\.\d+)*)", re.MULTILINE)
_GO_MOD_REQUIRE_BLOCK = re.compile(r"^require\s*\(\s*(?://.*)?$")
_GO_MOD_REQUIRE_LINE = re.compile(r"^require\s+(.*)$")
# The trailing group holds any "// indirect" marker.
_GO_MOD_REQUIREMENT = re.compile(r"^(\S+)\s+(v\S+)(.*)$")
_ADK_GO_MODULE_PATH = re.compile(rf"^{re.escape(_ADK_GO_MODULE)}(?:/v(\d+))?$")
_ADK_GO_IMPORT = re.compile(rf'"({re.escape(_ADK_GO_MODULE)}(?:/v(\d+))?)(?:/[^"]*)?"')


@dataclass(frozen=True)
class _AdkGoVersion:
    """``version`` is exact from go.mod, but only the major (``v2``) from imports."""

    major: int
    module: str
    version: str


@dataclass(frozen=True, order=True)
class _GoVersion:
    """A Go version (``1.26.5``, ``@v1.2.3``, pre-release, pseudo-version) in semver order."""

    release: tuple[int, ...]
    is_final: bool
    # Pseudo-version timestamps compare as strings.
    prerelease: tuple[tuple[int, int, str], ...]

    @classmethod
    def parse(cls, version: str) -> "_GoVersion":
        core, _, prerelease = (
            version.removeprefix("@").removeprefix("v").split("+")[0].partition("-")
        )

        release = tuple(int(n) for n in core.split("."))
        prerelease = tuple(
            (0, int(part), "") if part.isdigit() else (1, 0, part)
            for part in prerelease.split(".")
            if prerelease
        )

        return cls(
            release=release,
            is_final=not prerelease,
            prerelease=prerelease,
        )

    @property
    def minor(self) -> tuple[int, ...]:
        return self.release[:2]


def check_adk_go_version(
    template_dir: pathlib.Path, clone_root: pathlib.Path | None
) -> None:
    """Reject a template built for an ADK Go major version other than v2.

    Reads go.mod's direct ADK Go requirement, or without a go.mod the first ADK
    Go import in the template's Go files. A template that names no ADK Go
    version is let through.
    """
    go_mod = _template_file(template_dir / "go.mod", clone_root)
    if go_mod is not None:
        found = _adk_go_version_from_go_mod(go_mod.read_text(encoding="utf-8"))
    else:
        found = _adk_go_version_from_imports(template_dir)
    if found is None:
        return
    if found.major != _SUPPORTED_ADK_GO_MAJOR_VERSION:
        raise click.ClickException(
            f"{template_dir.name} is built for ADK Go {found.version} ({found.module}), "
            f"but agents-cli supports ADK Go v{_SUPPORTED_ADK_GO_MAJOR_VERSION} "
            f"({_ADK_GO_MODULE}/v{_SUPPORTED_ADK_GO_MAJOR_VERSION}). Use a template "
            f"built for ADK Go v{_SUPPORTED_ADK_GO_MAJOR_VERSION}, or migrate its "
            "agent code to that version."
        )


def applies_to(language: str | None, *, overlay_is_project: bool) -> bool:
    # A project overlaid onto itself (local@.) already has its own server and module.
    return language == "go" and not overlay_is_project


def package_dirs(template_dir: pathlib.Path, agent_directory: str) -> list[str]:
    """Top-level template folders with Go packages that need their own Dockerfile COPY."""
    copied = {agent_directory, *SCAFFOLD_PACKAGES}
    return [
        entry.name
        for entry in sorted(template_dir.iterdir())
        if entry.is_dir()
        and not entry.name.startswith(".")
        and not is_ignored_name(entry.name)
        and entry.name not in copied
        and _has_go_package(entry)
    ]


def skipped_root_files(
    template_dir: pathlib.Path, *, is_flat_structure: bool
) -> frozenset[str]:
    """Root files of the template that the generated project replaces."""
    root_go = {p.name for p in template_dir.glob("*.go")}

    if is_flat_structure:
        # The other root Go files move into the agent directory.
        skipped = SCAFFOLD_ROOT_FILES
        dropped = root_go & SCAFFOLD_ROOT_FILES
    else:
        skipped = SCAFFOLD_ROOT_FILES | root_go
        dropped = root_go - SCAFFOLD_ROOT_FILES

    if dropped:
        logging.warning(
            "Not copying %s from the template root: the generated main.go replaces "
            "the template's main package. Move shared code into a package folder.",
            ", ".join(sorted(dropped)),
        )
    return skipped


def clear_overlaid_packages(
    template_dir: pathlib.Path,
    project_dir: pathlib.Path,
    *,
    flat_agent_directory: str | None,
) -> None:
    """Delete scaffolded Go files wherever the template ships its own package.

    A leftover scaffolded file (the base agent's agent_test.go) would join the
    template's package and break the build. Without ``flat_agent_directory``
    the template root maps to the project root, which holds the server and stays.
    """
    for src_dir, files in walk_source_tree(template_dir, frozenset()):
        if not any(name.endswith(".go") for name in files):
            continue
        if src_dir != template_dir:
            dst_dir = project_dir / src_dir.relative_to(template_dir)
        elif flat_agent_directory is not None:
            dst_dir = project_dir / flat_agent_directory
        else:
            continue
        for package_file in dst_dir.glob("*.go"):
            if package_file.is_file():
                logging.debug("Removing %s, replaced by the template", package_file)
                package_file.unlink()


def rename_flat_package(agent_dir: pathlib.Path) -> None:
    """Turn a flat template's ``package main`` files into the agent directory's package."""
    package = agent_dir.name
    for go_file in agent_dir.glob("*.go"):
        source = go_file.read_text(encoding="utf-8")
        renamed = _PACKAGE_MAIN.sub(
            lambda m: f"package {package}{m.group(1) or ''}", source, count=1
        )
        if renamed != source:
            go_file.write_text(renamed, encoding="utf-8")


def merge_module(
    project_dir: pathlib.Path,
    template_dir: pathlib.Path,
    clone_root: pathlib.Path | None,
) -> None:
    """Make the copied template build as part of the generated project's module."""
    template_go_mod = _template_file(template_dir / "go.mod", clone_root)
    if template_go_mod is None:
        return
    template_go_mod_text = template_go_mod.read_text(encoding="utf-8")
    project_go_mod = project_dir / "go.mod"
    _retarget_imports(
        project_dir,
        old_module=_module_path(template_go_mod_text),
        new_module=_module_path(project_go_mod.read_text(encoding="utf-8")),
    )
    _merge_go_sum(
        project_dir / "go.sum", _template_file(template_dir / "go.sum", clone_root)
    )
    _merge_requirements(project_go_mod, template_go_mod, template_dir.name)
    # Last: `go mod edit` in a module asking for a newer Go would fetch that toolchain.
    _merge_go_directive(
        project_go_mod, template_go=_go_directive_version(template_go_mod_text)
    )
    _warn_about_unmerged_directives(template_go_mod_text, template_dir.name)
    _warn_if_builder_is_older(project_dir)


def tidy(project_dir: pathlib.Path) -> None:
    hint = "Run `agents-cli install` in the project before building or deploying it."
    try:
        result = run(
            get_language_config("go")["lock_command"], cwd=project_dir, check=False
        )
    except (ToolNotFoundError, ToolNotExecutableError) as e:
        logging.warning("Could not tidy the project's go.mod: %s\n%s", e.message, hint)
        return
    if result.returncode != 0:
        logging.warning(
            "go mod tidy failed with exit code %d.\n%s", result.returncode, hint
        )


def _adk_go_version_from_go_mod(go_mod_text: str) -> _AdkGoVersion | None:
    for module, version, rest in _go_mod_requirements(go_mod_text):
        match = _ADK_GO_MODULE_PATH.match(module)
        if not match or "// indirect" in rest:
            continue
        # No /vN suffix means v0 or v1.
        major = match.group(1)
        return _AdkGoVersion(int(major) if major else 1, module, version)
    return None


def _go_mod_requirements(go_mod_text: str) -> Iterator[tuple[str, str, str]]:
    """``(module, version, rest of line)`` for each ``require`` entry in go.mod."""
    in_require_block = False
    for raw_line in go_mod_text.splitlines():
        line = raw_line.strip()
        if in_require_block:
            if line.startswith(")"):
                in_require_block = False
                continue
            entry = line
        elif _GO_MOD_REQUIRE_BLOCK.match(line):
            in_require_block = True
            continue
        elif single := _GO_MOD_REQUIRE_LINE.match(line):
            entry = single.group(1)
        else:
            continue
        if requirement := _GO_MOD_REQUIREMENT.match(entry):
            module, version, rest = requirement.groups()
            yield module, version, rest


def _adk_go_version_from_imports(
    template_dir: pathlib.Path,
) -> _AdkGoVersion | None:
    for go_file in _go_files(template_dir):
        # A symlink's target may sit outside the repository; its copy is vetted later.
        if go_file.is_symlink():
            continue
        # Tests and fixtures may pin other ADK versions without affecting the build.
        if (
            go_file.name.endswith("_test.go")
            or "testdata" in go_file.relative_to(template_dir).parts
        ):
            continue
        match = _ADK_GO_IMPORT.search(go_file.read_text(encoding="utf-8"))
        if match:
            module, major = match.groups()
            # No /vN suffix means v0 or v1; reported as major 1, both unsupported.
            if major:
                return _AdkGoVersion(int(major), module, f"v{major}")
            return _AdkGoVersion(1, module, "v0/v1")
    return None


def _has_go_package(folder: pathlib.Path) -> bool:
    for _, files in walk_source_tree(folder, frozenset()):
        for name in files:
            if name.endswith(".go") and not name.endswith("_test.go"):
                return True
    return False


def _template_file(
    path: pathlib.Path, clone_root: pathlib.Path | None
) -> pathlib.Path | None:
    """*path*, or its vetted target if it is a symlink; None when there is no file."""
    if path.is_symlink():
        path = require_safe_symlink(path, clone_root)
    return path if path.is_file() else None


def _retarget_imports(
    project_dir: pathlib.Path, *, old_module: str | None, new_module: str | None
) -> None:
    if old_module is None or new_module is None or old_module == new_module:
        return

    logging.debug("Rewriting imports of %s to %s", old_module, new_module)
    # "/" or the closing quote must follow, so a longer path with this prefix is kept.
    old_import = re.compile(rf'"{re.escape(old_module)}(?=[/"])')
    for go_file in _go_files(project_dir):
        source = go_file.read_text(encoding="utf-8")
        retargeted = old_import.sub(f'"{new_module}', source)
        if retargeted != source:
            go_file.write_text(retargeted, encoding="utf-8")


def _module_path(go_mod_text: str) -> str | None:
    match = _GO_MODULE.search(go_mod_text)
    return match.group(1).strip('"') if match else None


def _go_files(root: pathlib.Path) -> Iterator[pathlib.Path]:
    for directory, files in walk_source_tree(root, frozenset()):
        for name in sorted(files):
            if name.endswith(".go"):
                yield directory / name


def _merge_go_sum(
    project_go_sum: pathlib.Path, template_go_sum: pathlib.Path | None
) -> None:
    if template_go_sum is None:
        return
    lines = set(template_go_sum.read_text(encoding="utf-8").splitlines())
    if project_go_sum.is_file():
        lines |= set(project_go_sum.read_text(encoding="utf-8").splitlines())
    lines.discard("")
    project_go_sum.write_text("\n".join(sorted(lines)) + "\n", encoding="utf-8")


def _merge_requirements(
    project_go_mod: pathlib.Path, template_go_mod: pathlib.Path, template_name: str
) -> None:
    try:
        project_requires = load_go_mod_requirements(project_go_mod)
        template_requires = load_go_mod_requirements(template_go_mod)
    except DependencyReadError as e:
        logging.warning(
            "Could not add %s's go.mod requirements to the project: %s. Install Go "
            "and run `agents-cli install` in the project to resolve them.",
            template_name,
            e,
        )
        return

    newer = [
        DependencyResolution(
            name=name,
            status="updated" if name in project_requires else "added",
            new_version=version,
        )
        for name, version in template_requires.items()
        if name not in project_requires
        or _GoVersion.parse(version) > _GoVersion.parse(project_requires[name])
    ]
    # Logs its own warning on failure; the tidy that follows resolves the rest.
    write_go_dependencies(project_go_mod.parent, newer)


def _merge_go_directive(project_go_mod: pathlib.Path, *, template_go: str | None) -> None:
    """``go mod tidy`` raises the directive for dependencies, not the template's own code."""
    project_text = project_go_mod.read_text(encoding="utf-8")
    project = _GO_DIRECTIVE.search(project_text)
    if not template_go or not project:
        return
    if _GoVersion.parse(template_go) > _GoVersion.parse(project.group(1)):
        project_go_mod.write_text(
            project_text[: project.start(1)]
            + template_go
            + project_text[project.end(1) :],
            encoding="utf-8",
        )


def _warn_about_unmerged_directives(
    template_go_mod_text: str, template_name: str
) -> None:
    found = [
        name
        for name in _UNMERGED_DIRECTIVES
        if re.search(rf"^{name}\b", template_go_mod_text, re.MULTILINE)
    ]
    if found:
        logging.warning(
            "%s's go.mod has %s directives, which are not copied into the project. "
            "Add them to the project's go.mod if the template needs them.",
            template_name,
            " and ".join(found),
        )


def _warn_if_builder_is_older(project_dir: pathlib.Path) -> None:
    required = _go_directive_version((project_dir / "go.mod").read_text(encoding="utf-8"))
    dockerfile = project_dir / "Dockerfile"
    if not required or not dockerfile.is_file():
        return
    builder = _GOLANG_IMAGE.search(dockerfile.read_text(encoding="utf-8"))
    # golang:1.26 is the latest 1.26.x, so only the minor version matters.
    if (
        builder
        and _GoVersion.parse(required).minor > _GoVersion.parse(builder.group(1)).minor
    ):
        logging.warning(
            "The project's go.mod needs Go %s, but its Dockerfile builds with "
            "golang:%s. Update the Go version in the Dockerfile and any CI "
            "pipeline files.",
            required,
            builder.group(1),
        )


def _go_directive_version(go_mod_text: str) -> str | None:
    match = _GO_DIRECTIVE.search(go_mod_text)
    return match.group(1) if match else None
