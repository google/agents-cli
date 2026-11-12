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

"""Package-index policy for the uv commands agents-cli runs.

uv records the index URL of every package in ``uv.lock``, and the scaffolded
Dockerfile's ``uv sync --frozen`` downloads from exactly those URLs. Remote
builders (Agent Runtime, Cloud Build) can only reach public indexes, so the
scaffold locks against public PyPI (``scaffold/utils/generate_locks.py``).

A machine-wide uv config (e.g. a system ``uv.toml`` pointing at an internal
mirror) would otherwise make every ``uv sync``/``uv run`` agents-cli runs
re-resolve that lock against the mirror, turning a deployable project into one
whose remote build cannot download its dependencies. The helpers here keep those
commands on the same public index the scaffold uses, unless the project pins its
own default index or the user chose an index or config explicitly for this
shell, and detect non-PyPI indexes in ``uv.lock`` for ``install``'s warning
and the deploy pre-flight check.

One case is only detected, not prevented: when the project declares extra
indexes of its own, a machine-wide extra index can still win for packages it
also hosts (see :func:`uv_index_env`).
"""

from __future__ import annotations

import functools
import logging
import os
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

PUBLIC_INDEX_URL = "https://pypi.org/simple"
_PUBLIC_INDEX_HOSTS = frozenset({"pypi.org", "files.pythonhosted.org"})

# An index or config file chosen in the environment (UV_CONFIG_FILE,
# UV_NO_CONFIG) is an explicit choice for this shell or CI job, unlike a
# machine-wide config file, so agents-cli never overrides it.
_EXPLICIT_INDEX_ENV_VARS = ("UV_DEFAULT_INDEX", "UV_INDEX_URL")
_EXPLICIT_EXTRA_INDEX_ENV_VARS = ("UV_INDEX", "UV_EXTRA_INDEX_URL")
# How uv reads a boolean environment variable as "off".
_FALSY_ENV_VALUES = frozenset({"", "0", "false", "no", "off"})

RELOCK_PUBLIC_COMMAND = f"uv lock --default-index {PUBLIC_INDEX_URL}"
PIN_PUBLIC_INDEX_SNIPPET = (
    f'[[tool.uv.index]]\nname = "pypi"\nurl = "{PUBLIC_INDEX_URL}"\ndefault = true'
)


def load_toml(path: Path) -> dict[str, Any] | None:
    """Parse a TOML file, or return None if it is missing or unparseable."""
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def normalize_index_url(url: str) -> str:
    """Canonical form for comparing index URLs (case-insensitive host, no
    trailing slash)."""
    parts = urlsplit(url.strip())
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", "")
    )


def is_public_index(url: str) -> bool:
    """Whether ``url`` is public PyPI, which every remote builder can reach."""
    return (urlsplit(url).hostname or "").lower() in _PUBLIC_INDEX_HOSTS


def is_remote_index(url: str) -> bool:
    """Whether ``url`` is fetched over the network (not a local directory)."""
    return urlsplit(url).scheme in ("http", "https")


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _tool_uv(directory: Path) -> dict[str, Any] | None:
    """The ``[tool.uv]`` table of ``directory/pyproject.toml``, if it has one."""
    tool = (load_toml(directory / "pyproject.toml") or {}).get("tool")
    tool_uv = tool.get("uv") if isinstance(tool, dict) else None
    return tool_uv if isinstance(tool_uv, dict) else None


def _glob_matches(base: Path, patterns: Any, target: Path) -> bool:
    for pattern in _as_list(patterns):
        if not isinstance(pattern, str):
            continue
        try:
            if any(match.resolve() == target for match in base.glob(pattern)):
                return True
        except (ValueError, NotImplementedError):  # e.g. absolute patterns
            continue
    return False


def _is_workspace_member(workspace_root: Path, workspace: dict, root: Path) -> bool:
    return _glob_matches(
        workspace_root, workspace.get("members"), root
    ) and not _glob_matches(workspace_root, workspace.get("exclude"), root)


def _project_uv_settings(root: Path) -> dict[str, Any]:
    """The project-level uv settings uv itself would use for ``root``.

    Mirrors uv's discovery: for a member of the nearest enclosing workspace the
    search starts at the workspace root (members' settings are ignored), then
    walks up to the nearest directory with a ``uv.toml`` or a ``pyproject.toml``
    that has ``[tool.uv]``. Only that one file is used, and ``uv.toml`` wins
    over ``[tool.uv]`` in the same directory.
    """
    root = root.resolve()
    dirs = [root, *root.parents]
    # Parse each pyproject.toml lazily, and at most once across both searches,
    # so nothing above the directory where a search stops is read.
    tool_uv_of = functools.cache(_tool_uv)
    start = 0
    for i, directory in enumerate(dirs):
        workspace = (tool_uv_of(directory) or {}).get("workspace")
        if isinstance(workspace, dict):
            if i and _is_workspace_member(directory, workspace, root):
                start = i
            break
    for directory in dirs[start:]:
        if (directory / "uv.toml").is_file():
            return load_toml(directory / "uv.toml") or {}
        if (tool_uv := tool_uv_of(directory)) is not None:
            return tool_uv
    return {}


def _settings_index_urls(settings: dict[str, Any]) -> set[str]:
    """Normalized URLs of every index declared in a uv settings table."""
    urls = [
        entry.get("url")
        for entry in _as_list(settings.get("index"))
        if isinstance(entry, dict)
    ]
    urls.append(settings.get("index-url"))
    extra = settings.get("extra-index-url")
    urls.extend([extra] if isinstance(extra, str) else _as_list(extra))
    return {normalize_index_url(url) for url in urls if isinstance(url, str)}


def _pins_default_index(settings: dict[str, Any]) -> bool:
    """Whether ``settings`` sets its own default index (``[[tool.uv.index]]``
    with ``default = true``, or the legacy ``index-url``).

    ``default-index`` is not checked: it is only a CLI flag and environment
    variable, and uv ignores it in config files.
    """
    return isinstance(settings.get("index-url"), str) or any(
        isinstance(entry, dict) and entry.get("default") is True
        for entry in _as_list(settings.get("index"))
    )


def project_index_urls(root: Path) -> set[str]:
    """Normalized URLs of every index the project declares itself."""
    return _settings_index_urls(_project_uv_settings(root))


def project_pins_default_index(root: Path) -> bool:
    """Whether the project sets its own default index (``[[tool.uv.index]]``
    with ``default = true``, or the legacy ``index-url``)."""
    return _pins_default_index(_project_uv_settings(root))


def user_index_urls() -> set[str]:
    """Normalized URLs of every index the user chose for this shell: index
    environment variables and the ``UV_CONFIG_FILE`` they point uv at."""
    urls: set[str] = set()
    for name in (*_EXPLICIT_INDEX_ENV_VARS, *_EXPLICIT_EXTRA_INDEX_ENV_VARS):
        for token in os.environ.get(name, "").split():
            # `UV_INDEX` also accepts `name=url`.
            index_name, sep, url = token.partition("=")
            urls.add(
                normalize_index_url(url if sep and "://" not in index_name else token)
            )
    if config_file := os.environ.get("UV_CONFIG_FILE"):
        urls |= _settings_index_urls(load_toml(Path(config_file)) or {})
    return urls


def _env_sets_any(names: tuple[str, ...]) -> bool:
    return any(os.environ.get(name) for name in names)


def _user_chose_config() -> bool:
    """Whether the user replaced uv's config discovery for this shell, so no
    machine-wide config file applies."""
    no_config = os.environ.get("UV_NO_CONFIG", "").strip().lower()
    return bool(os.environ.get("UV_CONFIG_FILE")) or no_config not in _FALSY_ENV_VALUES


def uv_index_env(root: Path) -> dict[str, str] | None:
    """Environment overrides that keep a uv command on the public index.

    In uv, environment variables outrank both machine-wide configuration files
    and project-level settings. ``UV_DEFAULT_INDEX`` replaces the default index;
    ``UV_INDEX`` also puts PyPI ahead of any extra indexes from config files,
    which uv would otherwise check first.

    Side effect: with PyPI first, a package that exists both on PyPI and on an
    internal registry added by machine-wide config now comes from PyPI. That
    gives up uv's protection against dependency confusion (its default
    ``first-index`` strategy) for indexes the project doesn't declare. This is
    an accepted tradeoff: those indexes are exactly the ones remote builds can't
    reach, and indexes the project or user declares keep their priority, since
    ``UV_INDEX`` is not set for them (see below).

    Returns None (leave the environment alone) when the project pins its own
    default index, or the user set one in the environment or chose their own
    config (``UV_CONFIG_FILE``, ``UV_NO_CONFIG``). ``UV_INDEX`` is left out when
    the project declares its own extra indexes or the user set one, since it
    would outrank those too.
    """
    if _env_sets_any(_EXPLICIT_INDEX_ENV_VARS) or _user_chose_config():
        return None
    settings = _project_uv_settings(root)
    if _pins_default_index(settings):
        return None
    env = {"UV_DEFAULT_INDEX": PUBLIC_INDEX_URL}
    if not (
        _env_sets_any(_EXPLICIT_EXTRA_INDEX_ENV_VARS) or _settings_index_urls(settings)
    ):
        env["UV_INDEX"] = PUBLIC_INDEX_URL
    return env


def lock_registry_counts(lock: dict[str, Any]) -> dict[str, int]:
    """Number of packages ``uv.lock`` resolves from each remote registry URL."""
    counts: dict[str, int] = {}
    for package in _as_list(lock.get("package")):
        source = package.get("source") if isinstance(package, dict) else None
        registry = source.get("registry") if isinstance(source, dict) else None
        if isinstance(registry, str) and is_remote_index(registry):
            counts[registry] = counts.get(registry, 0) + 1
    return counts


def non_public_lock_indexes(
    root: Path, lock: dict[str, Any]
) -> tuple[dict[str, int], dict[str, int]]:
    """Package counts per non-PyPI registry in ``lock``, split into
    (undeclared, declared by the project or chosen by the user).

    Undeclared ones typically leak in from a machine-wide package manager config.
    """
    declared_urls = project_index_urls(root) | user_index_urls()
    undeclared: dict[str, int] = {}
    declared: dict[str, int] = {}
    for url, count in lock_registry_counts(lock).items():
        if not is_public_index(url):
            target = declared if normalize_index_url(url) in declared_urls else undeclared
            target[url] = count
    return undeclared, declared


def format_index_counts(indexes: dict[str, int]) -> tuple[int, str]:
    """(total packages, newline-joined index URLs) for an error message."""
    total = sum(indexes.values())
    return total, "\n".join(f"    {url}" for url in sorted(indexes))


def warn_if_lock_uses_non_public_index(root: Path) -> None:
    """Warn when ``uv.lock`` resolves packages from an index that neither the
    project nor the user chose, and remote builds likely can't reach."""
    lock = load_toml(root / "uv.lock")
    if not lock:
        return
    indexes, _ = non_public_lock_indexes(root, lock)
    if not indexes:
        return
    total, urls = format_index_counts(indexes)
    logging.warning(
        "uv.lock pins %d packages to a package index that remote builds may not "
        "be able to reach:\n%s\n"
        "  This usually comes from a machine-wide package manager config (e.g. a "
        "system uv.toml), and `agents-cli deploy` will fail to build.\n"
        "  To keep the project deployable, re-lock against the public index:\n"
        "    %s\n"
        "  If your network only reaches this index, declare it in pyproject.toml "
        "([[tool.uv.index]] ... default = true) to make the choice explicit.",
        total,
        urls,
        RELOCK_PUBLIC_COMMAND,
    )


def sync_failure_message(
    base: str, env: dict[str, str] | None, *, locked: bool = False
) -> str:
    """``base``, plus a hint when the failure may be the forced public index.

    With ``locked``, also explains that a uv.lock resolved against another index
    fails ``uv sync --locked`` here, and how to re-lock it. uv's own hint (run
    ``uv lock``) would re-resolve against the machine-wide mirror again.
    """
    if not env:
        return base
    message = (
        f"{base}\n"
        f"  agents-cli resolves dependencies against {PUBLIC_INDEX_URL}, the index "
        "remote builds use.\n"
        "  If your network can only reach an internal package mirror, declare it "
        "in pyproject.toml\n"
        "  ([[tool.uv.index]] ... default = true) or set UV_DEFAULT_INDEX to use it "
        "instead."
    )
    if locked:
        message += (
            "\n  With --locked, a uv.lock resolved against a different index (e.g. "
            "a machine-wide mirror)\n"
            "  fails this check. To re-lock against the public index, run:\n"
            f"    {RELOCK_PUBLIC_COMMAND}\n"
            "  then retry."
        )
    return message
