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

"""Parse and validate agents-cli-extension.yaml (schema: agents-cli-extension/v1alpha1)."""

from __future__ import annotations

import difflib
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from google.agents.cli.extension._compat import validate_range
from google.agents.cli.scaffold.utils.fs import (
    is_ignored_name,
    is_secret_file,
    is_windows_device_name,
)
from google.agents.cli.skills._bundle import SKILL_PREFIX

# The manifest format version, tracked separately from the agents-cli version.
# A second format would widen `ExtensionManifest.schema_version` to a Union.
# Alpha while the extension system is experimental: the format may still change
# in a breaking way, and `v1` is reserved for the point it stops doing so.
V1ALPHA1 = "agents-cli-extension/v1alpha1"
EXTENSION_FILE = "agents-cli-extension.yaml"
# The Agent Skills name rule, since the key becomes the folder that SKILL.md's
# `name` must match. It also keeps names clear of hidden and staging folders.
_SKILL_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SKILL_NAME_MAX_LENGTH = 64


class ExtensionSpecError(Exception):
    """Raised on structural errors in an extension spec."""


# --- Wire models -------------------------------------------------------------
# These mirror agents-cli-extension.yaml and generate the published JSON Schema.
# `extra="forbid"` so a typo like `reqires:` is an error rather than a section
# that silently does nothing.


def _reject_bool(value: Any) -> Any:
    """YAML 1.1 reads `yes`/`on` as True, which is not a number in JSON Schema."""
    if isinstance(value, bool):
        raise ValueError('expected a version range, e.g. ">=1.3,<2"')
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _empty_key_means_absent(cls, data: Any) -> Any:
        """`commands:` with nothing under it is how authors comment a section out.

        Only for keys this model knows: dropping unknown ones would hide the
        typo that `extra="forbid"` exists to catch.
        """
        if not isinstance(data, dict):
            return data
        known = {f.alias or n for n, f in cls.model_fields.items()}
        return {k: v for k, v in data.items() if v is not None or k not in known}


class CommandEntry(_Strict):
    run: list[str] = Field(min_length=1)
    description: str = ""


class Commands(_Strict):
    override: dict[str, CommandEntry] = Field(default_factory=dict)
    add: dict[str, CommandEntry] = Field(default_factory=dict)


class SkillEntry(_Strict):
    path: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def _inside_extension(cls, value: str) -> str:
        """Resolved against the vendored copy, which holds nothing outside the extension."""
        if "\\" in value:
            raise ValueError("use forward slashes")
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
            raise ValueError("must not contain a control character")
        path = PurePosixPath(value)
        if path.is_absolute() or re.match(r"^[A-Za-z]:", value):
            raise ValueError("must be relative to the extension directory")
        if ".." in path.parts:
            raise ValueError("must stay inside the extension directory")
        if not path.parts:
            raise ValueError("must name a directory inside the extension")
        for part in path.parts:
            if is_ignored_name(part) or is_secret_file(part):
                raise ValueError(
                    f"must not use {part!r}: this name is reserved (in any letter "
                    "case); rename that folder"
                )
        return value


class Requires(_Strict):
    # `agents_cli: 1.2` is natural YAML and arrives as a number; the range is
    # validated semantically below, and a bad one is dropped rather than fatal.
    agents_cli: Annotated[str | float | None, BeforeValidator(_reject_bool)] = None
    on_incompatible: Literal["warn", "error"] = "warn"


class ExtensionManifest(_Strict):
    """`agents-cli-extension.yaml` as written on disk."""

    # This is the v1alpha1 document, so it accepts only v1alpha1: an editor
    # validating against it has to agree with the loader, which rejects
    # anything else.
    schema_version: Literal["agents-cli-extension/v1alpha1"] = Field(
        default=V1ALPHA1, alias="schema"
    )
    name: str | None = Field(default=None, min_length=1)
    description: str = ""
    requires: Requires | None = None
    commands: Commands = Field(default_factory=Commands)
    skills: dict[str, SkillEntry] = Field(
        default_factory=dict,
        json_schema_extra={
            "propertyNames": {
                "pattern": f"^{_SKILL_NAME_RE.pattern}$",
                "maxLength": _SKILL_NAME_MAX_LENGTH,
            }
        },
    )

    @field_validator("skills")
    @classmethod
    def _skill_names_are_folder_names(
        cls, value: dict[str, SkillEntry]
    ) -> dict[str, SkillEntry]:
        """Each key becomes a folder under `.agents/skills`, so it must not leave it."""
        for skill_name in value:
            if len(skill_name) > _SKILL_NAME_MAX_LENGTH:
                raise ValueError(
                    f"skill name {skill_name!r} must be at most "
                    f"{_SKILL_NAME_MAX_LENGTH} characters"
                )
            if not _SKILL_NAME_RE.fullmatch(skill_name):
                raise ValueError(
                    f"skill name {skill_name!r} must use only lowercase letters, "
                    "digits and single hyphens, and must not start or end with "
                    "a hyphen"
                )
            if is_windows_device_name(skill_name):
                raise ValueError(
                    f"skill name {skill_name!r} is reserved by Windows and cannot "
                    "be a folder name"
                )
            if skill_name.startswith(SKILL_PREFIX):
                raise ValueError(
                    f"skill name {skill_name!r}: the {SKILL_PREFIX!r} prefix is "
                    "reserved for the skills `agents-cli setup` installs"
                )
        return value


def _describe(error: Mapping[str, Any]) -> str:
    """One validation error as a line an extension author can act on."""
    loc = ".".join(str(part) for part in error["loc"] if part != "[key]")
    if error["type"] == "extra_forbidden":
        key = str(error["loc"][-1])
        near = difflib.get_close_matches(key, _known_keys(error["loc"][:-1]), n=1)
        hint = f" (did you mean {near[0]!r}?)" if near else ""
        return f"unknown key {loc!r}{hint}"
    # Model class names mean nothing to someone editing YAML.
    msg = re.sub(r" or instance of \w+", "", error["msg"])
    return f"{loc or '(root)'}: {msg}"


def _known_keys(loc: tuple[Any, ...]) -> list[str]:
    """Field names of the manifest model at `loc`, for did-you-mean hints.

    A container's key or index consumes a `loc` part without changing the model,
    so `commands.add.<name>.<typo>` still resolves to CommandEntry's fields.
    """
    current: Any = ExtensionManifest
    for part in loc:
        fields = getattr(current, "model_fields", {})
        field = fields.get(str(part)) or next(
            (f for f in fields.values() if f.alias == str(part)), None
        )
        if field is None:
            # This part is a dict key or a list index, not a field name.
            nested = _model_in(current)
            if nested is None:
                return []
            current = nested
            continue
        current = _unwrap_optional(field.annotation)
    return [f.alias or n for n, f in getattr(current, "model_fields", {}).items()]


def _unwrap_optional(annotation: Any) -> Any:
    """`X | None` -> X, leaving `dict[str, X]` and `list[X]` for the key/index step."""
    if get_origin(annotation) not in (Union, UnionType):
        return annotation
    args = [a for a in get_args(annotation) if a is not type(None)]
    return args[0] if len(args) == 1 else annotation


def _model_in(annotation: Any) -> Any:
    """The model a container holds, e.g. dict[str, CommandEntry] -> CommandEntry."""
    for arg in getattr(annotation, "__args__", ()):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None


@dataclass(frozen=True)
class ExtensionCommand:
    name: str  # dotted command path, e.g. "deploy" or "eval.generate"
    run: tuple[str, ...]
    description: str
    kind: str  # "override" | "add"


@dataclass(frozen=True)
class ExtensionSkill:
    name: str
    path: str  # relative to the extension root


@dataclass(frozen=True)
class ExtensionSpec:
    name: str
    description: str
    root: Path
    commands: tuple[ExtensionCommand, ...]
    # Install scope ("user" | "project"), stamped by the loader at discovery —
    # it comes from where the extension was found, not from the manifest.
    scope: str = ""
    # Optional author-declared agents-cli compatibility range (e.g. ">=1.1,<2").
    requires_agents_cli: str | None = None
    # The manifest's "warn" | "error", resolved at parse time. Only two states
    # exist and the manifest model already validates them, so the rest of the
    # CLI reads a bool rather than re-comparing strings.
    error_on_incompatible: bool = False
    # Copied only from an installed extension's vendored copy; a project-root
    # manifest's skills are ignored.
    skills: tuple[ExtensionSkill, ...] = ()
    # The `source:` recorded for this extension's install in
    # agents-cli-extensions.yaml, as an `extension add` reference.
    source: str | None = None


def _to_command(name: str, entry: CommandEntry, kind: str) -> ExtensionCommand:
    _warn_if_script_entrypoint(name, kind, entry.run[0])
    return ExtensionCommand(
        name=name, run=tuple(entry.run), description=entry.description, kind=kind
    )


# Suffixes that are run by an interpreter, not executed directly on Windows.
_SCRIPT_SUFFIXES = frozenset({".py", ".sh", ".js", ".ts", ".rb", ".pl"})


def _warn_if_script_entrypoint(name: str, kind: str, first_token: str) -> None:
    """Executed without a shell: a bare script needs +x and a shebang on POSIX, and never runs on Windows."""
    if PurePosixPath(first_token).suffix in _SCRIPT_SUFFIXES:
        logging.warning(
            "commands.%s.%s: `run` starts with the script %r; prefix an "
            'interpreter (e.g. ["uv", "run", "python", %r]) so it also '
            "runs on Windows.",
            kind,
            name,
            first_token,
            first_token,
        )


def parse_extension_spec(
    data: dict[str, Any], *, root: Path, default_name: str
) -> ExtensionSpec:
    """Raises ExtensionSpecError on any structural problem; the caller decides whether that is fatal."""
    try:
        manifest = ExtensionManifest.model_validate(data)
    except ValidationError as e:
        problems = "\n  - ".join(_describe(err) for err in e.errors())
        if len(e.errors()) > 1:
            problems = f"\n  - {problems}"
        raise ExtensionSpecError(problems) from e

    commands = [
        _to_command(name, entry, kind)
        for kind in ("override", "add")
        for name, entry in getattr(manifest.commands, kind).items()
    ]
    requires_agents_cli, error_on_incompatible = _parse_requires(manifest.requires)
    return ExtensionSpec(
        name=manifest.name or default_name,
        description=manifest.description,
        root=root,
        commands=tuple(commands),
        requires_agents_cli=requires_agents_cli,
        error_on_incompatible=error_on_incompatible,
        skills=tuple(
            ExtensionSkill(name=skill_name, path=entry.path)
            for skill_name, entry in manifest.skills.items()
        ),
    )


def _parse_requires(requires: Requires | None) -> tuple[str | None, bool]:
    """An unparseable range is warned about and dropped: a bad compat hint must not make an otherwise-valid extension unloadable."""
    if requires is None:
        return None, False
    raw_range = None if requires.agents_cli is None else str(requires.agents_cli)
    if raw_range is not None and not validate_range(raw_range):
        logging.warning(
            "requires.agents_cli %r is not a valid version range; ignoring the "
            "range (the extension still loads).",
            raw_range,
        )
        raw_range = None
    return raw_range, requires.on_incompatible == "error"
