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
from typing import Literal, TypeAlias
from urllib.parse import urlsplit

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from google.agents.cli._fs import stays_inside

_FIRST_PARTY_ORG = "google"
# The repo a bare extension name resolves to.
_SHORTHAND_REPO = f"{_FIRST_PARTY_ORG}/agents-cli"
# Installed without a trust prompt.
_VERIFIED_REPOS = frozenset(
    {"google/adk-recipes", _SHORTHAND_REPO, "google/agents-cli-extensions"}
)

# "verified" is a repo in `_VERIFIED_REPOS` or a package in `_VERIFIED_PACKAGES`,
# "first_party" any other repo in the `google` GitHub org.
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

PYPI_PREFIX = "pypi@"
# Normalized PyPI project names installed without a trust prompt. List only
# names Google already owns: anyone can register a free name and be trusted.
_VERIFIED_PACKAGES: frozenset[str] = frozenset()
# https://packaging.python.org/en/latest/specifications/name-normalization/
PACKAGE_NAME_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?")
# Version operators, checked after the supported `==` is split off. A `!` alone
# is allowed after the first character: it separates an epoch, as in 1!2.0.
_SPECIFIER_RE = re.compile(r"^!|[<>=~,]")

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")
# scp-style, as offered by most hosts' "clone with SSH" button: git@host:org/repo.
_SCP_RE = re.compile(r"^[A-Za-z0-9._-]+@(?P<host>[A-Za-z0-9._-]+):(?P<path>.+)$")


class RefParseError(Exception):
    """Raised when an extension reference cannot be parsed."""


@dataclass(frozen=True)
class ExtensionRef:
    raw: str
    kind: str  # "first_party" | "github" | "git" | "local" | "pypi"
    tier: Tier
    # The org/repo of a git ref, the package name of a pypi ref, or a local path.
    upstream: str
    # Clone URL, set when the ref was given as one. Otherwise the ref names a
    # repo on github.com and the resolver builds the URL from it.
    url: str | None
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
    if not stays_inside(selector):
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
      - ``pypi@acme-extensions==1.2.0#core/soc2``  a wheel on a Python package
        index, with an optional version and extension selector
      - pass ``ref_override`` to pin a branch, tag, or SHA (a version for pypi@)
    """
    raw = (raw or "").strip()
    if not raw:
        raise RefParseError("empty extension reference.")

    if is_pypi_reference(raw):
        return _parse_pypi(PYPI_PREFIX + raw[len(PYPI_PREFIX) :], ref_override)

    if raw.startswith("local@"):
        path_part, selector = _split_selector(raw[len("local@") :])
        if not path_part:
            raise RefParseError(f"local reference missing a path: {raw!r}.")
        return ExtensionRef(
            raw=raw,
            kind="local",
            tier="third_party",
            upstream=path_part,
            url=None,
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
            upstream=_repo_identity(url),
            url=url,
            selector=selector,
            ref=ref_override,
        )

    if "/" in base:
        return ExtensionRef(
            raw=raw,
            kind="github",
            tier=_tier(base),
            upstream=base,
            url=None,
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
        upstream=_SHORTHAND_REPO,
        url=None,
        selector=selector or base,
        ref=ref_override,
    )


def is_pypi_reference(raw: str) -> bool:
    """Whether ``raw`` is a ``pypi@`` reference, in any letter case."""
    raw = raw.strip()
    # `pypi@host:org/repo` is an ssh clone URL for a user named pypi.
    scp = _SCP_RE.match(raw)
    if scp is not None and "/" in scp.group("path"):
        return False
    return raw[: len(PYPI_PREFIX)].lower() == PYPI_PREFIX


def strip_pin(value: str) -> str:
    """Return a --to or --ref value without surrounding spaces or a leading `==`."""
    return value.strip().removeprefix("==").strip()


def _parse_pypi(raw: str, ref_override: str | None) -> ExtensionRef:
    # Checked once stripped: " /etc" passes as a relative path until its space goes.
    spec, _, selector = raw[len(PYPI_PREFIX) :].partition("#")
    selector = selector.strip() or None
    if selector is not None:
        _check_selector(selector)
    if selector is not None and "==" in selector:
        raise RefParseError(
            f"version after the selector in {raw!r}: write it before '#', e.g. "
            f"'{PYPI_PREFIX}acme-extensions==1.2.0#core/soc2'."
        )
    if ref_override is not None:
        # Not parsed as a Version: sync passes the recorded sha256 here.
        ref_override = strip_pin(ref_override)
        if _SPECIFIER_RE.search(ref_override):
            raise RefParseError(
                f"unsupported version {ref_override!r}: pin an exact version, "
                "e.g. 1.2.0 or ==1.2.0."
            )
    package, version = _split_package_spec(raw, spec)
    pinned = ref_override or version or None
    return ExtensionRef(
        # Rebuilt with any --ref/--to version, since it is recorded as the source.
        raw=pypi_source(package, pinned, selector),
        kind="pypi",
        tier="verified"
        if canonicalize_name(package) in _VERIFIED_PACKAGES
        else "third_party",
        upstream=package,
        url=None,
        selector=selector,
        ref=pinned,
    )


def _split_package_spec(raw: str, spec: str) -> tuple[str, str]:
    """Split ``package==version`` into its checked parts; the version may be empty."""
    package, has_version, version = (part.strip() for part in spec.partition("=="))
    example = f"e.g. '{PYPI_PREFIX}acme-extensions==1.2.0'"
    if not package:
        raise RefParseError(f"no package name in {raw!r}: {example}.")
    if "[" in package:
        raise RefParseError(
            f"extras are not supported in {raw!r}: drop the '[...]', {example}."
        )
    if has_version and not version:
        raise RefParseError(f"no version after '==' in {raw!r}: {example}.")
    if _SPECIFIER_RE.search(package) or (has_version and not _is_version(version)):
        raise RefParseError(
            f"unsupported version in {raw!r}: pin an exact version with '==', {example}."
        )
    if not PACKAGE_NAME_RE.fullmatch(package):
        raise RefParseError(
            f"invalid package name {package!r} in {raw!r}: use a PyPI project "
            f"name, {example}."
        )
    return package, version


def _is_version(value: str) -> bool:
    try:
        Version(value)
    except InvalidVersion:
        return False
    return True


def pypi_source(package: str, version: str | None, selector: str | None) -> str:
    pin = f"=={version}" if version else ""
    return f"{PYPI_PREFIX}{package}{pin}{f'#{selector}' if selector else ''}"


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
