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

"""Discover active extensions across scopes into a resolved ExtensionSet."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from google.agents.cli.__init__ import __version__
from google.agents.cli.extension._compat import is_compatible
from google.agents.cli.extension._manifest import EXTENSIONS_FILE
from google.agents.cli.extension._paths import vendored_extensions_dir
from google.agents.cli.extension._refs import RefParseError, parse_ref
from google.agents.cli.extension._resolver import ResolverError, validate_extension_name
from google.agents.cli.extension._spec import (
    EXTENSION_FILE,
    ExtensionCommand,
    ExtensionSpec,
    ExtensionSpecError,
    parse_extension_spec,
)

# Commands no extension may override, ever. Some are how a user removes or
# repairs an extension, so letting one own them means a broken extension can own
# its own uninstall. The rest (`info`, `login`, `setup`, `update`) are what a
# user runs to inspect a repo or set up the CLI before trusting it, and a
# project-root `agents-cli-extension.yaml` loads with no install step, so a
# cloned repo could otherwise run its own code on them. Refused at `extension
# add` and ignored here, rather than only when the extension is out of range:
# an in-range extension owning `extension remove` is the same trap.
RECOVERY_COMMANDS = frozenset(
    {
        "install",
        "extension",
        "extension.add",
        "extension.list",
        "extension.remove",
        "extension.update",
        "info",
        "login",
        "setup",
        "update",
    }
)


@dataclass(frozen=True)
class ResolvedCommand:
    contribution: ExtensionCommand
    extension_name: str
    scope: str
    extension_root: Path
    # Set when the owning extension declared `on_incompatible: error` and the
    # running CLI is outside its range. `main` installs a command that fails
    # with an explanation instead of running the extension's vector — falling back
    # to the built-in would silently do something else (an ADK scaffold in a
    # LangChain project, say).
    blocked_requires: str | None = None
    # Whether the extension comes from the `google` GitHub org, so telemetry may
    # record its name.
    first_party: bool = False


@dataclass(frozen=True)
class ExtensionSet:
    # Tuples (and frozen specs) because a set is parsed once and then read by
    # main, info and extension list; no consumer may mutate it.
    extensions: tuple[ExtensionSpec, ...]
    commands: dict[str, ResolvedCommand]
    conflicts: tuple[str, ...]
    # Extensions whose declared range excludes `running_version`.
    incompatible: tuple[ExtensionSpec, ...] = ()
    running_version: str = __version__

    def command_rows(self) -> list[dict]:
        """Contributed commands, one JSON-ready row each, sorted by command name.

        A blocked row is a command the CLI will refuse to run (its extension is out
        of range in `error` mode), so it is reported but not as active.
        """
        requires_by_name = {
            spec.name: spec.requires_agents_cli
            for spec in self.extensions
            if spec.requires_agents_cli
        }
        return [
            {
                "command": name,
                "extension": rc.extension_name,
                "scope": rc.scope,
                "kind": rc.contribution.kind,
                "run": list(rc.contribution.run),
                "requires": requires_by_name.get(rc.extension_name),
                "blocked": rc.blocked_requires is not None,
            }
            for name, rc in sorted(self.commands.items())
        ]

    def conflict_rows(self) -> list[str]:
        """Command names claimed by more than one extension at one scope."""
        return list(self.conflicts)

    def incompatible_rows(self) -> list[dict]:
        """Out-of-range extensions, one JSON-ready row each."""
        return [
            {
                "extension": spec.name,
                "scope": spec.scope,
                "requires": spec.requires_agents_cli,
                "running": self.running_version,
                "mode": "error" if spec.error_on_incompatible else "warn",
            }
            for spec in self.incompatible
        ]


def is_out_of_range(spec: ExtensionSpec) -> bool:
    """Whether the running CLI is outside the spec's `requires.agents_cli` range."""
    if not spec.requires_agents_cli:
        return False
    return not is_compatible(__version__, spec.requires_agents_cli)


def incompatible_spec(extension_dir: Path) -> ExtensionSpec | None:
    """The spec if it is out of range for this CLI, else None.

    Raises ExtensionSpecError if the manifest is there but unparseable; an
    absent manifest, or one with no `requires`, returns None.
    """
    spec = load_extension_spec(extension_dir)
    return spec if spec is not None and is_out_of_range(spec) else None


def _load_yaml(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, RecursionError, UnicodeDecodeError, yaml.YAMLError) as e:
        logging.warning("%s: could not read (%s); ignoring.", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def load_extension_spec(root: Path) -> ExtensionSpec | None:
    """Parse an extension's manifest.

    None when there is no manifest; raises ExtensionSpecError when there is a bad one.

    Everything on the load path catches it and carries on. `extension add` and
    `extension update` let it surface, so an install fails instead of reporting
    success for an extension that contributes nothing.
    """
    path = root / EXTENSION_FILE
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, RecursionError, UnicodeDecodeError, yaml.YAMLError) as e:
        # Unreadable, undecodable and unparseable are all as fatal as each other
        # for `add`, and all as skippable for the load path, so they have to
        # arrive as the same error type. RecursionError covers pathological
        # nesting, which yaml raises straight through.
        raise ExtensionSpecError(f"could not be read or parsed: {e}") from e
    if data is not None and not isinstance(data, dict):
        raise ExtensionSpecError(f"expected a mapping, found {type(data).__name__}")
    return parse_extension_spec(data or {}, root=root, default_name=root.name)


def load_extension_set(project_root: Path | None, user_root: Path | None) -> ExtensionSet:
    discovered = _discover_specs(project_root, user_root)

    commands: dict[str, ResolvedCommand] = {}
    # (scope, command_name) -> extension_name that first claimed it at that scope.
    claimed_same_scope: dict[tuple[str, str], str] = {}
    conflicts: list[str] = []
    incompatible: list[ExtensionSpec] = []

    for spec in discovered:
        scope = spec.scope
        # An extension whose declared `requires.agents_cli` range excludes the
        # running CLI is recorded here. "warn" applies its contributions anyway;
        # "error" keeps the command claimed but marks it blocked, so invoking it
        # fails with an explanation rather than silently running the built-in,
        # which for an override does something else entirely.
        blocked_requires: str | None = None
        if spec.requires_agents_cli and not is_compatible(
            __version__, spec.requires_agents_cli
        ):
            incompatible.append(spec)
            if spec.error_on_incompatible:
                blocked_requires = spec.requires_agents_cli
        for contribution in spec.commands:
            key = contribution.name
            same_scope_key = (scope, key)
            if same_scope_key in claimed_same_scope:
                conflicts.append(f"{scope}:{key}")
                logging.warning(
                    "Command %r claimed by multiple %s-scope extensions "
                    "(%s and %s); ignoring the later one.",
                    key,
                    scope,
                    claimed_same_scope[same_scope_key],
                    spec.name,
                )
                continue
            claimed_same_scope[same_scope_key] = spec.name
            if key in RECOVERY_COMMANDS:
                # `extension add` refuses these, so reaching here means a
                # hand-edited manifest or one installed by an older CLI.
                logging.warning(
                    "Extension %r claims %r, which cannot be overridden (it is "
                    "reserved for inspecting, setting up or repairing agents-cli); "
                    "using the built-in.",
                    spec.name,
                    key,
                )
                continue
            commands[key] = ResolvedCommand(
                contribution=contribution,
                extension_name=spec.name,
                scope=scope,
                extension_root=spec.root,
                blocked_requires=blocked_requires,
                first_party=_is_first_party(spec.source),
            )

    return ExtensionSet(
        extensions=tuple(discovered),
        commands=commands,
        conflicts=tuple(conflicts),
        incompatible=tuple(incompatible),
    )


def _discover_specs(
    project_root: Path | None, user_root: Path | None
) -> list[ExtensionSpec]:
    """Every extension found, user scope first, each with its recorded `source`.

    Project scope comes last so its entries win later by overwriting user-scope
    ones. A scope holds its project-root `agents-cli-extension.yaml` and the
    vendored copies its `agents-cli-extensions.yaml` lists. The project-root
    extension has no vendored copy, but its install is still recorded by name, at
    project scope or globally.
    """
    scopes = [
        (scope, root)
        for scope, root in (("user", user_root), ("project", project_root))
        if root is not None
    ]
    recorded = {scope: _recorded_sources(root) for scope, root in scopes}
    any_scope_sources = recorded.get("user", {}) | recorded.get("project", {})
    return [
        spec
        for scope, root in scopes
        for spec in _scope_specs(scope, root, recorded[scope], any_scope_sources)
    ]


def _recorded_sources(root: Path) -> dict[str, str | None]:
    """Extension name -> recorded `source` from the scope's `agents-cli-extensions.yaml`."""
    manifest = root / EXTENSIONS_FILE
    raw = _load_yaml(manifest).get("extensions") if manifest.exists() else None
    if not isinstance(raw, list):
        return {}
    recorded: dict[str, str | None] = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        try:
            name = validate_extension_name(item["name"])
        except ResolverError as e:
            logging.warning("Skipping extension with %s", e)
            continue
        source = item.get("source")
        recorded[name] = source if isinstance(source, str) else None
    return recorded


def _scope_specs(
    scope: str,
    root: Path,
    recorded: Mapping[str, str | None],
    any_scope_sources: Mapping[str, str | None],
) -> list[ExtensionSpec]:
    """The scope's project-root extension, then the vendored ones it records."""
    specs: list[ExtensionSpec] = []
    try:
        inline = load_extension_spec(root)
    except ExtensionSpecError as e:
        logging.warning("%s: %s; ignoring this extension.", root / EXTENSION_FILE, e)
        inline = None
    if inline is not None:
        specs.append(
            replace(inline, scope=scope, source=any_scope_sources.get(inline.name))
        )
    for name, source in recorded.items():
        extension_dir = vendored_extensions_dir(root) / name
        try:
            spec = load_extension_spec(extension_dir)
        except ExtensionSpecError as e:
            logging.warning("%s: %s; ignoring this extension.", extension_dir, e)
            continue
        if spec is None:
            logging.warning(
                "Extension %r is recorded in %s but not vendored at %s; "
                "run `agents-cli install`.",
                name,
                root / EXTENSIONS_FILE,
                extension_dir,
            )
            continue
        # The manifest id is the extension's stable identity (used by
        # `extension remove`/`update`); keep it even if the upstream yaml
        # `name` was renamed after the extension was pinned.
        specs.append(replace(spec, name=name, scope=scope, source=source))
    return specs


def _is_first_party(source: str | None) -> bool:
    """Whether `source` is a repository in the `google` GitHub org."""
    if source is None:
        return False
    try:
        return parse_ref(source).tier in ("verified", "first_party")
    except RefParseError as e:
        logging.debug("Could not parse extension source %r: %s", source, e)
        return False
