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

"""Move extensions from their old home in the agents-cli repo to the new repo.

First-party extensions and their templates moved from ``extensions/`` in
google/agents-cli to ``core/`` in google/agents-cli-extensions. A reference to
an old location is rewritten to the new one, with a warning.
"""

from __future__ import annotations

import logging

from google.agents.cli._git import is_commit_sha
from google.agents.cli.extension._refs import (
    AGENTS_CLI_REPO,
    EXTENSIONS_REPO,
    LATEST,
    UNPINNED_REF,
    ExtensionRef,
    RefParseError,
    github_path,
    is_release_tag,
    parse_ref,
)
from google.agents.cli.extension._spec import TEMPLATE_DIR

_EXTENSIONS_REPO_URL = f"https://github.com/{EXTENSIONS_REPO}"
# Folder of the agents-cli repo the extensions moved out of.
_OLD_DIR = "extensions"
# Folder of the new repo that holds them.
_NEW_DIR = "core"
_warned_templates: set[str] = set()


def move_if_deprecated(
    ref: ExtensionRef, *, pin: str | None, recorded: str | None = None
) -> ExtensionRef:
    """Return ``ref``, or the same extension in the new repo if ``ref`` is in the old one.

    ``pin`` is the branch, tag, SHA or ``:latest`` the user typed, if any.
    ``recorded`` is the ref an installed entry tracks: a branch carries over,
    but a release tag or SHA of the old repo names other code in the new one,
    so it is dropped. Without a pin, a shorthand gets the newest release, as it
    always has, and an ``org/repo`` or URL follows the default branch.
    """
    path = _old_extension_path(ref)
    if path is None:
        return ref
    if pin is None and recorded is not None and _is_branch(recorded):
        pin = recorded
    if pin is None and ref.kind == "first_party":
        pin = LATEST
    moved = parse_ref(f"{EXTENSIONS_REPO}#{_new_extension_path(path)}", ref_override=pin)
    _warn_moved("Extension source", ref.raw, moved.raw)
    return moved


def _is_branch(recorded: str) -> bool:
    return not (
        recorded in (UNPINNED_REF, LATEST)
        or is_commit_sha(recorded)
        or is_release_tag(recorded)
    )


def warn_if_installed_from_agents_cli_repo(
    extension_name: str, source: str | None
) -> None:
    """Warn when an extension recorded with an agents-cli repo source is used.

    Its pin is a commit of the old repo, so it is restored from there until
    `extension update` moves it.
    """
    if source is None:
        return
    try:
        ref = parse_ref(source)
    except RefParseError:
        # A source that does not parse names no repo, so it can't be the old one.
        return
    if _old_extension_path(ref) is None:
        return
    logging.warning(
        "Extension %r is installed from %s, which is deprecated; first-party "
        "extensions moved to %s. Move it there with:\n  agents-cli extension update %s",
        extension_name,
        source,
        _EXTENSIONS_REPO_URL,
        extension_name,
    )


def move_template_if_deprecated(agent_spec: str) -> str:
    """Return ``agent_spec``, or the new-repo spec if it names an old template.

    Warns when it moves the spec. The old spec's ref is dropped: its tags name
    agents-cli releases, which the new repo doesn't have, so the new spec
    follows the default branch.
    """
    moved = moved_template_spec(agent_spec)
    if moved is None:
        return agent_spec
    warn_template_moved(agent_spec, moved)
    return moved


def moved_template_spec(agent_spec: str) -> str | None:
    """The new-repo spec for a template under the agents-cli repo's extensions/.

    Returns None for any other spec. Doesn't warn, for a caller that may still
    render from the old spec; it calls `warn_template_moved` once it uses the
    new one.
    """
    try:
        ref = parse_ref(agent_spec)
    except RefParseError:
        return None
    # A bare name is a built-in template here, not an extension shorthand.
    path = None if ref.kind == "first_party" else _old_extension_path(ref)
    if path is None:
        return None
    return f"{EXTENSIONS_REPO}/{_new_extension_path(path)}"


def warn_template_moved(old_spec: str, new_spec: str) -> None:
    """Warn that ``new_spec`` is used instead of ``old_spec``, once per spec.

    enhance and upgrade read the same spec more than once in one command.
    """
    if old_spec in _warned_templates:
        return
    _warned_templates.add(old_spec)
    _warn_moved("Template", old_spec, new_spec)


def _old_extension_path(ref: ExtensionRef) -> str | None:
    """The path under extensions/ that ``ref`` selects in the agents-cli repo."""
    if ref.kind == "first_party":
        # A bare name, with any `#selector`, is relative to extensions/.
        return (ref.selector or "").strip("/") or None
    repo_path = _github_repo_path(ref)
    if repo_path is None:
        return None
    # A github path is `org/repo`, then, in the `--agent` forms, a path inside
    # the repo: `google/agents-cli/extensions/x` gives `google/agents-cli` and
    # `extensions/x`.
    parts = repo_path.split("/")
    org_repo, in_repo_parts = "/".join(parts[:2]), parts[2:]
    if org_repo.lower() != AGENTS_CLI_REPO:
        return None
    # A browse URL puts `tree/<branch>/` before the path, and a spec may end in
    # `@<ref>`. Either ref names an old release, which the new repo doesn't
    # have. Only a one-part branch is recognized: with more, the branch and the
    # path can't be told apart.
    if in_repo_parts[:1] == ["tree"]:
        in_repo_parts = in_repo_parts[2:]
    in_repo = "/".join(in_repo_parts).partition("@")[0]
    path = "/".join(part for part in (in_repo, ref.selector) if part)
    name = path.removeprefix(f"{_OLD_DIR}/").strip("/")
    if not path.startswith(f"{_OLD_DIR}/") or not name:
        return None
    return name


def _github_repo_path(ref: ExtensionRef) -> str | None:
    if ref.kind == "github":
        return ref.upstream
    if ref.kind == "git" and ref.url is not None:
        return github_path(ref.url)
    return None


def _new_extension_path(path: str) -> str:
    """``langchain`` and ``langchain/template`` both read core/langchain/template."""
    name, _, folder = path.strip("/").partition("/")
    return f"{_NEW_DIR}/{name}/{folder or TEMPLATE_DIR}"


def _warn_moved(what: str, old: str, new: str) -> None:
    logging.warning(
        "%s %s is deprecated: first-party extensions moved to %s. Using %s instead.",
        what,
        old,
        _EXTENSIONS_REPO_URL,
        new,
    )
