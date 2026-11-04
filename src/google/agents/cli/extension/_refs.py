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

"""Parse extension references for `agents-cli extension add`."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, TypeAlias
from urllib.parse import urlsplit

_FIRST_PARTY_ORG = "google"
# The repo a bare extension name resolves to.
_SHORTHAND_REPO = f"{_FIRST_PARTY_ORG}/agents-cli"
# Installed without a trust prompt.
_VERIFIED_REPOS = frozenset(
    {"google/adk-recipes", _SHORTHAND_REPO, "google/agents-cli-extensions"}
)

# "verified" is a repo in `_VERIFIED_REPOS`, "first_party" any other repo in
# the `google` GitHub org.
Tier: TypeAlias = Literal["verified", "first_party", "third_party"]

# Never recorded as a pin; git rejects it as a ref name, so it can't shadow a ref.
LATEST = ":latest"
# Recorded as the ref of an extension added without one.
UNPINNED_REF = "HEAD"

# Schemes an extension may be cloned from: the set `--agent` already accepts for
# a template repo (scaffold/utils/remote_template.py), plus ssh://, which is how
# a self-hosted host is usually reached, and file://, for a bare repo on a shared
# mount — `local@` copies a working tree and cannot pin a commit against one.
# `git://` is left out deliberately: it is unauthenticated, and an extension's
# commands run arbitrary code on the machine that installs it.
GIT_URL_SCHEMES = ("https", "http", "ssh", "file")

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")
# scp-style, as offered by most hosts' "clone with SSH" button: git@host:org/repo.
_SCP_RE = re.compile(r"^[A-Za-z0-9._-]+@(?P<host>[A-Za-z0-9._-]+):(?P<path>.+)$")


class RefParseError(Exception):
    """Raised when an extension reference cannot be parsed."""


@dataclass(frozen=True)
class ExtensionRef:
    raw: str
    kind: str  # "first_party" | "github" | "git" | "local"
    tier: Tier
    repo: str | None
    # Clone URL, set when the ref was given as one. Otherwise the ref names a
    # repo on github.com and the resolver builds the URL from it.
    url: str | None
    local_path: Path | None
    selector: str | None
    ref: str | None


def _split_selector(value: str) -> tuple[str, str | None]:
    if "#" in value:
        base, selector = value.split("#", 1)
        if selector:
            _check_selector(selector)
        return base, (selector or None)
    return value, None


def _check_selector(selector: str) -> None:
    """Reject a selector that would point outside the source it selects from.

    The resolver joins the selector onto the checkout with pathlib, where an
    absolute selector replaces the checkout entirely and `..` climbs out of it.
    Either would vendor a directory the ref's kind, pin and trust decision do not
    describe: `langchain#/tmp/x` would install /tmp/x as verified, unprompted.
    """
    if (
        not PurePosixPath(selector).parts
        or "\\" in selector
        or PurePosixPath(selector).is_absolute()
        or PureWindowsPath(selector).drive
        or ".." in PurePosixPath(selector).parts
    ):
        raise RefParseError(
            f"invalid extension selector {selector!r}: use a relative path inside "
            "the source, with '/' separators and no '..', e.g. 'soc2' or "
            "'extensions/soc2'."
        )


def _as_git_url(base: str) -> str | None:
    """Return ``base`` unchanged if it is a clonable git URL, else None.

    A string carrying a scheme git cannot clone is an error rather than a None,
    so that `ftp://host/org/repo` is reported as a bad scheme instead of falling
    through and being read as the `org/repo` shorthand `ftp:/host/org`.
    """
    scheme = _SCHEME_RE.match(base)
    if scheme is not None:
        if scheme.group(1).lower() not in GIT_URL_SCHEMES:
            raise RefParseError(
                f"unsupported scheme in {base!r}: use "
                f"{', '.join(s + '://' for s in GIT_URL_SCHEMES)}, a 'git@host:org/repo' "
                "URL, an 'org/repo' shorthand, or 'local@<path>'."
            )
        return base
    return base if _SCP_RE.match(base) else None


def _repo_identity(url: str) -> str:
    """Return an ``org/repo`` label for a clone URL.

    Used for messages, for the vendored directory's fallback name and for the
    readable part of the cache directory. The clone itself always uses the URL
    as given, so nothing here has to round-trip.
    """
    scp = _SCP_RE.match(url)
    path = scp.group("path") if scp is not None else urlsplit(url).path
    parts = [p for p in path.split("/") if p]
    if not parts:
        raise RefParseError(f"no repository path in {url!r}.")
    if parts[-1].endswith(".git"):
        parts[-1] = parts[-1].removesuffix(".git")
    if not parts[-1]:
        raise RefParseError(f"no repository name in {url!r}.")
    return "/".join(parts[-2:])


def parse_ref(raw: str, *, ref_override: str | None = None) -> ExtensionRef:
    """Parse an extension reference string into a structured ExtensionRef.

    Supported forms:
      - ``soc2``                            shorthand for google/agents-cli
      - ``acme/acli-extensions``            any org/repo on github.com
      - ``acme/acli-extensions#soc2``       org/repo with extension selector
      - ``https://git.example.com/acme/acli-extensions``   any git host
      - ``git@git.example.com:acme/acli-extensions``       the same, over ssh
      - ``local@../my-extension``           local path
      - pass ``ref_override`` to pin a branch, tag, or SHA
    """
    raw = (raw or "").strip()
    if not raw:
        raise RefParseError("empty extension reference.")

    if raw.startswith("local@"):
        path_part, selector = _split_selector(raw[len("local@") :])
        if not path_part:
            raise RefParseError(f"local reference missing a path: {raw!r}.")
        return ExtensionRef(
            raw=raw,
            kind="local",
            tier="third_party",
            repo=None,
            url=None,
            local_path=Path(path_part),
            selector=selector,
            ref=ref_override,
        )

    base, selector = _split_selector(raw)

    # Checked before the org/repo shorthand: a URL contains slashes too, and
    # would otherwise be silently mangled into one.
    url = _as_git_url(base)
    if url is not None:
        return ExtensionRef(
            raw=raw,
            kind="git",
            tier=_tier(_github_repo(url)),
            repo=_repo_identity(url),
            url=url,
            local_path=None,
            selector=selector,
            ref=ref_override,
        )

    if "/" in base:
        return ExtensionRef(
            raw=raw,
            kind="github",
            tier=_tier(base),
            repo=base,
            url=None,
            local_path=None,
            selector=selector,
            ref=ref_override,
        )

    # Shorthand: a bare name selects that extension from `_SHORTHAND_REPO`
    # (google/agents-cli). The name becomes the selector, so it
    # needs the same check: `..` or `C:\tmp\evil` has no `/` to make it a repo.
    # Checked even when a `#selector` replaces it, so a junk name is not
    # silently recorded as a verified source.
    _check_selector(base)
    return ExtensionRef(
        raw=raw,
        kind="first_party",
        tier="verified",
        repo=_SHORTHAND_REPO,
        url=None,
        local_path=None,
        selector=selector or base,
        ref=ref_override,
    )


def _github_repo(url: str) -> str | None:
    """Return ``org/repo`` if ``url`` clones a github.com repo over https or ssh."""
    scp = _SCP_RE.match(url)
    if scp is not None:
        host, path = scp.group("host"), scp.group("path")
    else:
        parts = urlsplit(url)
        # Plain http could be tampered with, so it never names a trusted repo.
        if parts.scheme.lower() not in ("https", "ssh"):
            return None
        host, path = parts.hostname or "", parts.path
    if host.lower().removeprefix("www.") != "github.com":
        return None
    return path.strip("/").removesuffix(".git")


def _tier(repo: str | None) -> Tier:
    """Classify a github.com ``org/repo`` (None for any other source)."""
    if repo is None:
        return "third_party"
    repo = repo.lower()
    if repo in _VERIFIED_REPOS:
        return "verified"
    if repo.partition("/")[0] == _FIRST_PARTY_ORG:
        return "first_party"
    return "third_party"
