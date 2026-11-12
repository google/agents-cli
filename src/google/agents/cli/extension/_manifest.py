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

"""Read/write the installed-extension list.

Its own file rather than a block in agents-cli-manifest.yaml: this is
read-modify-written on every add/remove/update, and a bug here should not be
able to reach a project's scaffold state. Same filename at both scopes, under
the project root or the user config dir, so there is nothing to branch on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from google.agents.cli.extension._refs import UNPINNED_REF, ExtensionRef

EXTENSIONS_FILE = "agents-cli-extensions.yaml"


class ExtensionListError(Exception):
    """Raised when the extension list as a whole cannot be read."""


@dataclass(frozen=True)
class ExtensionEntry:
    name: str
    source: str
    # A git entry sets `ref`; a pypi@ entry sets `version`, also the == pin in `source`.
    ref: str | None
    version: str | None
    sha: str
    scope: str

    @classmethod
    def for_ref(
        cls, ref: ExtensionRef, *, name: str, sha: str, scope: str
    ) -> ExtensionEntry:
        """Return the entry recording ``ref``, resolved by ``pin_ref``, pinned at ``sha``."""
        if ref.kind == "pypi":
            return cls(
                name=name, source=ref.raw, ref=None, version=ref.ref, sha=sha, scope=scope
            )
        return cls(
            name=name,
            source=ref.raw,
            ref=ref.ref or UNPINNED_REF,
            version=None,
            sha=sha,
            scope=scope,
        )


@dataclass(frozen=True)
class ExtensionList:
    entries: list[ExtensionEntry]
    complete: bool  # every extension the file records could be read

    @property
    def names(self) -> set[str]:
        return {entry.name for entry in self.entries}


def _read_manifest(path: Path) -> dict[str, Any]:
    """The file's top-level mapping; empty when the file is missing or empty.

    Raises OSError or ExtensionListError when the file as a whole cannot be
    read. Writing it back would then drop every entry it holds, and `install`
    would delete their skills next.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except (FileNotFoundError, NotADirectoryError) as e:
        if path.is_symlink():
            raise ExtensionListError(
                f"it is a link to a file that does not exist ({e}). Fix the link"
            ) from e
        return {}
    except UnicodeDecodeError as e:
        raise ExtensionListError(
            f"cannot read it ({e}). Check that it is readable UTF-8"
        ) from e
    except (RecursionError, yaml.YAMLError) as e:
        raise ExtensionListError(f"it is not valid YAML ({e}). Fix the file") from e
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ExtensionListError(
            f"it must be a mapping, not a {type(raw).__name__}. Fix the file"
        )
    items = raw.get("extensions")
    if items is not None and not isinstance(items, list):
        raise ExtensionListError(
            f"`extensions:` must be a list, not a {type(items).__name__}. Fix the file"
        )
    return raw  # type: ignore[return-value]


def _parse_extension_entries(
    manifest_path: Path,
) -> tuple[list[ExtensionEntry], list[str]]:
    """The entries the file records that can be read, and what could not be read."""
    try:
        items = _read_manifest(manifest_path).get("extensions") or []
    except ExtensionListError as e:
        return [], [f"Ignoring {manifest_path}: {e}."]
    except OSError as e:
        return [], [
            f"Ignoring {manifest_path}: cannot read it ({e}). Check that it is "
            "readable UTF-8."
        ]
    entries: list[ExtensionEntry] = []
    problems: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            problems.append(
                f"Ignoring an entry in {manifest_path}: each `extensions:` entry "
                f"must be a mapping, not {item!r}. Fix the file."
            )
            continue
        name = _entry_name(item)
        if not name:
            problems.append(
                f"Ignoring an entry in {manifest_path}: each `extensions:` entry "
                "needs a `name`. Fix the file."
            )
            continue
        entries.append(
            ExtensionEntry(
                name=name,
                source=str(item.get("source") or ""),
                ref=str(item["ref"]) if item.get("ref") else None,
                version=str(item["version"]) if item.get("version") else None,
                sha=str(item.get("sha") or ""),
                scope=str(item.get("scope") or ""),
            )
        )
    return entries, problems


def _entry_name(item: dict[str, Any]) -> str:
    """The entry's name as text; empty when it has none or it is not a name."""
    name = item.get("name")
    if isinstance(name, bool) or not isinstance(name, (str, int)):
        return ""
    return str(name).strip()


def _write_manifest(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)


def read_extension_entries(manifest_path: Path) -> list[ExtensionEntry]:
    """Read extension entries from a manifest file.

    Warns about and skips what it cannot read. Returns an empty list when the
    file is missing or cannot be read, or has no valid ``extensions:`` list.
    """
    return read_extension_list(manifest_path, warn=True).entries


def read_extension_list(manifest_path: Path, *, warn: bool) -> ExtensionList:
    """Read extension entries, and whether that is every extension the file records.

    A missing file records none. A caller that deletes what is not recorded
    checks `complete` first, because the entries skip what cannot be read.
    """
    entries, problems = _parse_extension_entries(manifest_path)
    if warn:
        for problem in problems:
            logging.warning("%s", problem)
    return ExtensionList(
        entries=entries,
        complete=not problems,
    )


def check_extension_list(manifest_path: Path) -> None:
    """Raises OSError or ExtensionListError when the file as a whole cannot be read.

    A caller checks this before it changes anything, because writing the file
    back would drop the entries it holds.
    """
    _read_manifest(manifest_path)


def upsert_extension_entry(manifest_path: Path, entry: ExtensionEntry) -> None:
    """Insert or replace an extension entry in the manifest, preserving all other keys.

    Creates the manifest file (and any missing parent directories) if absent.
    Raises OSError or ExtensionListError, and writes nothing, when the file as a
    whole cannot be read.
    """
    data = _read_manifest(manifest_path)
    existing: list[Any] = data.get("extensions") or []
    # Drop only the entry being replaced. Anything we don't recognise is kept
    # verbatim — rewriting the manifest must not delete a user's content.
    extensions: list[Any] = [
        p for p in existing if not (isinstance(p, dict) and _entry_name(p) == entry.name)
    ]
    item = {"name": entry.name, "source": entry.source}
    if entry.ref is not None:
        item["ref"] = entry.ref
    if entry.version is not None:
        item["version"] = entry.version
    extensions.append({**item, "sha": entry.sha, "scope": entry.scope})
    data["extensions"] = extensions
    _write_manifest(manifest_path, data)


def remove_extension_entry(manifest_path: Path, name: str) -> bool:
    """Remove an extension entry by name.

    Returns True if an entry was removed, False if no entry matched. Raises
    OSError or ExtensionListError, and writes nothing, when the file as a whole
    cannot be read.
    """
    data = _read_manifest(manifest_path)
    existing: list[Any] = data.get("extensions") or []
    # Same rule as upsert: unrecognised entries survive, only `name` matches go.
    kept: list[Any] = [
        p for p in existing if not (isinstance(p, dict) and _entry_name(p) == name)
    ]
    if len(kept) == len(existing):
        return False
    data["extensions"] = kept
    _write_manifest(manifest_path, data)
    return True
