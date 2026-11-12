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

"""Fetch extensions published as wheels on a Python package index.

Wheels are pinned by sha256, since an index can re-upload a file under the same
version. Only wheels are read, never built, so installing runs no package code.
"""

from __future__ import annotations

import contextlib
import logging
import lzma
import os
import re
import stat
import tempfile
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import (
    unquote,
    unquote_to_bytes,
    urldefrag,
    urljoin,
    urlsplit,
    urlunsplit,
)

from packaging.utils import (
    BuildTag,
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

from google.agents.cli._download import (
    DownloadError,
    basic_auth,
    download_verified,
    refuse_https_downgrade,
    request_error_reason,
)
from google.agents.cli._fs import delete_path, stays_inside
from google.agents.cli._uv_index import PUBLIC_INDEX_URL
from google.agents.cli.extension._refs import PACKAGE_NAME_RE, PYPI_PREFIX, pypi_source
from google.agents.cli.extension._spec import EXTENSION_FILE

# uv's then pip's precedence, so extensions come from the index the machine already uses.
# https://docs.astral.sh/uv/reference/environment/
# https://pip.pypa.io/en/stable/topics/configuration/#environment-variables
_INDEX_ENV_VARS = ("UV_DEFAULT_INDEX", "UV_INDEX_URL", "PIP_INDEX_URL")
_JSON_TYPE = "application/vnd.pypi.simple.v1+json"
# PEP 691's type, plus the alias and plain JSON that some indexes answer with.
_JSON_TYPES = frozenset(
    {_JSON_TYPE, "application/vnd.pypi.simple.latest+json", "application/json"}
)
# PEP 691 JSON first; PEP 503 HTML for indexes that only serve that.
_ACCEPT = f"{_JSON_TYPE}, text/html;q=0.01"
_TIMEOUT_SECONDS = 30
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MAX_VERSIONS_SHOWN = 10
_EXECUTE_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
_READ_BITS = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
_DEFAULT_PORTS = {"https": 443, "http": 80}
_CREDENTIALS_FORMAT = (
    "Credentials go in the URL as https://<user>:<password>@host/..., with any "
    "'/', '?', '#' or '@' in them percent-encoded, or in ~/.netrc."
)
_SIMPLE_API_HINT = (
    "check that the index URL is a package index's simple API, which usually ends "
    "in /simple."
)


class PypiError(Exception):
    pass


@dataclass(frozen=True)
class _Index:
    # Printed only for the public index: unencoded '/', '?' or '#' leave credentials in it.
    url: str
    auth: tuple[bytes, bytes] | None
    # None for the public index.
    env_var: str | None

    @property
    def label(self) -> str:
        return f"the index in {self.env_var}" if self.env_var else self.url

    @property
    def source_hint(self) -> str:
        if self.env_var:
            return f"Check the URL in {self.env_var}."
        return f"To use another index, set {_INDEX_ENV_VARS[0]}."

    @property
    def access_hint(self) -> str:
        if self.env_var:
            # requests reads ~/.netrc when it is given no credentials:
            # https://requests.readthedocs.io/en/latest/user/authentication/#netrc-authentication
            return f"check the credentials in the URL in {self.env_var}. {_CREDENTIALS_FORMAT}"
        return (
            "PyPI needs no credentials, so a proxy or firewall may be blocking it. "
            f"{self.source_hint}"
        )


@dataclass(frozen=True)
class _IndexFile:
    filename: str
    url: str
    sha256: str
    # None if not yanked, "" if yanked without a reason.
    yanked_reason: str | None


@dataclass(frozen=True)
class _Wheel:
    filename: str
    url: str
    sha256: str
    version: Version
    build: BuildTag
    yanked_reason: str | None

    @property
    def yanked(self) -> bool:
        return self.yanked_reason is not None


@dataclass(frozen=True)
class _Listing:
    # Only `wheels` can be pinned; the index lists no sha256 for `unhashed`.
    wheels: list[_Wheel]
    unhashed: list[_Wheel]
    # Sdists, platform-specific wheels and other projects' files.
    unusable: int


@dataclass(frozen=True)
class _Origin:
    scheme: str
    host: str
    port: int | None


class _LinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.files: list[_IndexFile] = []
        self._base_url = base_url

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        href = attributes.get("href")
        if tag != "a" or not href:
            return
        joined = _join_url(self._base_url, href)
        if joined is None:
            return
        url, fragment = urldefrag(joined)
        sha256 = (
            fragment.removeprefix("sha256=") if fragment.startswith("sha256=") else ""
        )
        # Like pip, take the filename from the URL rather than the link text.
        filename = unquote(urlsplit(url).path.rpartition("/")[2])
        # PEP 592: the attribute's value is the reason, if any.
        yanked_reason = (
            (attributes["data-yanked"] or "").strip()
            if "data-yanked" in attributes
            else None
        )
        self.files.append(_IndexFile(filename, url, sha256, yanked_reason))


def index_label() -> str:
    """The index wheels come from, for messages: the variable that set it, or its URL."""
    return _index().label


def resolve_wheel_sha256(package: str, version: str | None) -> str:
    """Return the sha256 of the wheel to pin: ``version``, or the newest release."""
    index = _index()
    wheel = _pick_wheel(_list_wheels(index, package), index, package, version)
    logging.info("Using %s from %s.", wheel.filename, index.label)
    if wheel.yanked:
        reason = (
            f": {wheel.yanked_reason}" if wheel.yanked_reason else "; no reason given"
        )
        logging.warning("%s was yanked from %s%s", wheel.filename, index.label, reason)
    return wheel.sha256


def latest_wheel_version(package: str) -> str:
    """Return the version of the newest release that is not yanked or a pre-release."""
    index = _index()
    return str(_pick_wheel(_list_wheels(index, package), index, package, None).version)


@contextlib.contextmanager
def extension_from_wheel(
    package: str, sha256: str, selector: str | None
) -> Iterator[Path]:
    """Download the wheel with ``sha256``, unpack it, and yield its extension folder."""
    if not _SHA256_RE.fullmatch(sha256):
        raise PypiError(
            f"invalid wheel hash {sha256!r}: expected 64 lowercase hex characters. "
            "If it is the `sha:` recorded in agents-cli-extensions.yaml, fix it "
            "or remove and re-add the extension."
        )
    if selector is not None and not stays_inside(selector):
        raise PypiError(
            f"invalid extension selector {selector!r}: use a relative path inside "
            "the package, with '/' separators and no '..'."
        )
    index = _index()
    wheels = _list_wheels(index, package).wheels
    wheel = next((w for w in wheels if w.sha256 == sha256), None)
    if wheel is None:
        raise PypiError(
            f"{index.label} no longer serves the {package!r} wheel with sha256 "
            f"{sha256[:12]}…; update the extension to a version it serves with "
            f"`agents-cli extension update <name> --to <version>`. {index.source_hint}"
        )
    with tempfile.TemporaryDirectory(prefix="agents-cli-wheel-") as tmp:
        archive = Path(tmp) / "package.whl"
        _download_wheel(wheel, index, archive)
        contents = Path(tmp) / "contents"
        _unpack(archive, contents)
        _delete_wheel_metadata(contents, wheel)
        yield _find_extension_dir(contents, package, str(wheel.version), selector)


def _index() -> _Index:
    env_var, url = next(
        (
            (var, value)
            for var in _INDEX_ENV_VARS
            if (value := os.environ.get(var, "").strip())
        ),
        (None, PUBLIC_INDEX_URL),
    )
    try:
        url, auth = _split_credentials(url.rstrip("/"))
        parts = urlsplit(url)
    except ValueError as e:
        # Neither the URL nor the error is printed: both can hold credentials.
        raise PypiError(
            f"the index URL in {env_var} is not a valid URL; check it for typos."
        ) from e
    if parts.query or parts.fragment:
        # Package pages are built by appending to the path, so a query would land mid-URL.
        raise PypiError(
            f"the index URL in {env_var} has a query string or fragment, which "
            f"package indexes don't use; remove it. {_CREDENTIALS_FORMAT}"
        )
    return _Index(url, auth, env_var)


def _list_wheels(index: _Index, package: str) -> _Listing:
    if not PACKAGE_NAME_RE.fullmatch(package):
        raise PypiError(
            f"invalid package name {package!r}: use letters, digits, '.', '_' and "
            "'-', starting and ending with a letter or digit."
        )
    page = f"{index.url}/{canonicalize_name(package)}/"
    files = _fetch_index_files(index, page, package)
    if not files:
        raise PypiError(
            f"{index.label} lists no files for {package!r}. If the package has releases, "
            f"{_SIMPLE_API_HINT} {index.source_hint}"
        )
    wheels = [wheel for f in files if (wheel := _as_wheel(package, f))]
    return _Listing(
        wheels=[w for w in wheels if _SHA256_RE.fullmatch(w.sha256)],
        unhashed=[w for w in wheels if not _SHA256_RE.fullmatch(w.sha256)],
        unusable=len(files) - len(wheels),
    )


def _fetch_index_files(index: _Index, page: str, package: str) -> list[_IndexFile]:
    # Deferred: requests takes about 0.2s to import.
    import requests

    try:
        response = requests.get(
            page,
            headers={"Accept": _ACCEPT},
            auth=basic_auth(index.auth),
            timeout=_TIMEOUT_SECONDS,
            hooks={"response": [refuse_https_downgrade]},
        )
    except requests.RequestException as e:
        raise PypiError(
            f"could not reach {index.label} ({request_error_reason(e)}). Check your "
            f"network connection. {index.source_hint}"
        ) from e
    except DownloadError as e:
        raise PypiError(f"could not read {index.label}: {e}. {index.source_hint}") from e
    if response.status_code == 404:
        raise PypiError(
            f"{index.label} has no package {package!r} (HTTP 404). Check the package "
            f"name, and {_SIMPLE_API_HINT} {index.source_hint}"
        )
    if response.status_code in (401, 403):
        raise PypiError(
            f"{index.label} returned HTTP {response.status_code} for {package!r}; "
            f"{index.access_hint}"
        )
    if not response.ok:
        retry = "; try again later" if response.status_code >= 500 else ""
        raise PypiError(
            f"{index.label} returned HTTP {response.status_code} for {package!r}"
            f"{retry}. {index.source_hint}"
        )
    # Resolve file URLs against response.url: a redirect makes it differ from `page`.
    content_type = response.headers.get("Content-Type", "").split(";")[0]
    if content_type.strip().lower() not in _JSON_TYPES:
        return _files_from_html(response.text, response.url)
    try:
        data = response.json()
    except ValueError as e:
        raise PypiError(
            f"{index.label} returned JSON for {package!r} that cannot be read: {e}. "
            f"{index.source_hint}"
        ) from e
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise PypiError(
            f"{index.label} returned JSON for {package!r} without a `files` list. "
            f"{index.source_hint}"
        )
    return _files_from_json(data["files"], response.url)


def _files_from_html(text: str, base_url: str) -> list[_IndexFile]:
    parser = _LinkParser(base_url)
    parser.feed(text)
    return parser.files


def _files_from_json(files: list[Any], base_url: str) -> list[_IndexFile]:
    entries = [
        f
        for f in files
        if isinstance(f, dict)
        and (f.get("hashes") is None or isinstance(f.get("hashes"), dict))
    ]
    if len(entries) < len(files):
        logging.debug(
            "Skipping %d malformed entries in the index listing.",
            len(files) - len(entries),
        )
    return [
        _IndexFile(
            filename=str(f.get("filename") or ""),
            url=url,
            sha256=str((f.get("hashes") or {}).get("sha256") or ""),
            yanked_reason=_yanked_reason(f.get("yanked")),
        )
        for f in entries
        if (url := _join_url(base_url, str(f.get("url") or ""))) is not None
    ]


def _join_url(base_url: str, href: str) -> str | None:
    """Resolve a listed file's URL, or None (skipping the file) if it is invalid."""
    try:
        return urljoin(base_url, href)
    except ValueError as e:
        logging.debug("Skipping a file at an invalid URL in the index listing: %s", e)
        return None


def _yanked_reason(yanked: object) -> str | None:
    # PEP 691: either a flag or the reason it was yanked.
    if yanked is False or yanked is None:
        return None
    return yanked.strip() if isinstance(yanked, str) else ""


def _as_wheel(package: str, file: _IndexFile) -> _Wheel | None:
    if not file.filename.endswith(".whl"):
        return None
    try:
        name, version, build, tags = parse_wheel_filename(file.filename)
    except InvalidWheelFilename as e:
        logging.debug("Skipping %s: %s", file.filename, e)
        return None
    # An extension is data and scripts, so only a pure-Python 3 wheel fits.
    pure_python3 = any(
        t.interpreter.startswith("py3") and t.abi == "none" and t.platform == "any"
        for t in tags
    )
    if name != canonicalize_name(package) or not pure_python3:
        return None
    return _Wheel(
        file.filename, file.url, file.sha256.lower(), version, build, file.yanked_reason
    )


def _pick_wheel(
    listing: _Listing, index: _Index, package: str, version: str | None
) -> _Wheel:
    wheels = listing.wheels
    if version is None:
        candidates = [w for w in wheels if not w.yanked and not w.version.is_prerelease]
    else:
        try:
            wanted = Version(version)
        except InvalidVersion as e:
            raise PypiError(
                f"invalid version {version!r} for a PyPI extension: pass a version "
                "such as 1.2.0."
            ) from e
        # An exact pin may name a yanked release (PEP 592).
        candidates = [w for w in wheels if w.version == wanted]
    if not candidates:
        raise _no_wheel_error(listing, index, package, version)
    # Sort by filename so ties pick the same wheel whatever the listing order.
    picked = max(
        sorted(candidates, key=lambda w: w.filename),
        key=lambda w: (w.version, not w.yanked, w.build),
    )
    if version is None:
        _warn_if_newer_unhashed(listing, picked)
    return picked


def _no_wheel_error(
    listing: _Listing, index: _Index, package: str, version: str | None
) -> PypiError:
    which = f"version {version}" if version else "final, non-yanked release"
    message = (
        f"{index.label} has no {which} of {package!r} as a wheel for any platform "
        "(py3-none-any)."
    )
    if listing.wheels:
        message += f" Available versions: {_describe_versions(listing.wheels)}."
        if version is None:
            newest = max(w.version for w in listing.wheels)
            message += f" Pin one to use it, e.g. {PYPI_PREFIX}{package}=={newest}."
    if listing.unhashed:
        message += (
            " Skipped because the index lists no sha256 for them: "
            f"{_describe_versions(listing.unhashed)}; use an index that lists hashes."
        )
    if not listing.wheels and not listing.unhashed:
        files = f"{listing.unusable} file{'' if listing.unusable == 1 else 's'}"
        message += (
            f" It lists {files}, none of them a py3-none-any wheel; sdists and "
            "platform-specific wheels cannot be installed as an extension."
        )
    return PypiError(f"{message} {index.source_hint}")


def _describe_versions(wheels: list[_Wheel]) -> str:
    versions = sorted({w.version for w in wheels}, reverse=True)
    kept = {w.version for w in wheels if not w.yanked}
    shown = [
        str(v) if v in kept else f"{v} (yanked)" for v in versions[:_MAX_VERSIONS_SHOWN]
    ]
    more = ", …" if len(versions) > _MAX_VERSIONS_SHOWN else ""
    return ", ".join(shown) + more


def _warn_if_newer_unhashed(listing: _Listing, picked: _Wheel) -> None:
    newer = {
        w.version
        for w in listing.unhashed
        if w.version > picked.version and not w.yanked and not w.version.is_prerelease
    }
    if newer:
        logging.warning(
            "Skipped %s: the index lists no sha256 for it, so %s is used.",
            ", ".join(str(v) for v in sorted(newer, reverse=True)),
            picked.version,
        )


def _download_wheel(wheel: _Wheel, index: _Index, dest: Path) -> None:
    # Credentials an index puts in a file's link are used, but kept out of messages.
    url, link_auth = _split_credentials(wheel.url)
    try:
        on_index = _is_on_index(url, index)
    except ValueError as e:
        raise PypiError(
            f"{index.label} lists {wheel.filename} at a URL whose host or port is not "
            f"valid. {index.source_hint}"
        ) from e
    if urlsplit(index.url).scheme == "https" and urlsplit(url).scheme == "http":
        # requests would send any ~/.netrc credentials for the host in the clear.
        raise PypiError(
            f"{index.label} lists {wheel.filename} at a plain http URL; refusing to "
            f"download it. {index.source_hint}"
        )
    try:
        download_verified(
            url,
            dest,
            wheel.sha256,
            # Files on another host or over plain http must not get the index's credentials.
            auth=link_auth or (index.auth if on_index else None),
            timeout=_TIMEOUT_SECONDS,
        )
    except DownloadError as e:
        hint = _download_hint(e.status, url, index, on_index)
        raise PypiError(f"{e}; {hint}" if hint else str(e)) from e


def _split_credentials(url: str) -> tuple[str, tuple[bytes, bytes] | None]:
    parts = urlsplit(url)
    auth = (
        (unquote_to_bytes(parts.username or ""), unquote_to_bytes(parts.password or ""))
        if parts.username or parts.password
        else None
    )
    netloc = parts.netloc.rpartition("@")[2]
    return urlunsplit(parts._replace(netloc=netloc)), auth


def _is_on_index(url: str, index: _Index) -> bool:
    return _origin(url) == _origin(index.url)


def _origin(url: str) -> _Origin:
    parts = urlsplit(url)
    return _Origin(
        scheme=parts.scheme,
        host=(parts.hostname or "").lower(),
        port=parts.port or _DEFAULT_PORTS.get(parts.scheme),
    )


def _download_hint(status: int | None, url: str, index: _Index, on_index: bool) -> str:
    if status in (401, 403) and (on_index or index.env_var is None):
        return index.access_hint
    if status in (401, 403):
        host = urlsplit(url).hostname
        return (
            f"{index.label} serves this file from {host}, and credentials for the "
            f"index are not sent there; add credentials for {host} to ~/.netrc."
        )
    if status is not None and status >= 500:
        return "try again later."
    return ""


def _unpack(archive: Path, dest: Path) -> None:
    try:
        # Created up front, so an empty archive still leaves a folder to search.
        dest.mkdir()
        with zipfile.ZipFile(archive) as wheel:
            members = wheel.infolist()
            for info in members:
                _check_member(info)
            for info in members:
                _extract_member(wheel, info, dest)
    # zipfile raises these for encrypted, unsupported or corrupt members, and for
    # member names that are not valid UTF-8.
    except (
        zipfile.BadZipFile,
        OSError,
        RuntimeError,
        EOFError,
        UnicodeDecodeError,
        zlib.error,
        lzma.LZMAError,
    ) as e:
        raise PypiError(f"could not unpack the wheel: {e}") from e


def _check_member(info: zipfile.ZipInfo) -> None:
    member = info.filename
    if not stays_inside(member):
        raise PypiError(
            f"the wheel holds a file outside its own folder ({member!r}); "
            "refusing to unpack it."
        )
    # zipfile would write the link as a file holding its target path.
    if stat.S_ISLNK(info.external_attr >> 16):
        raise PypiError(f"the wheel holds a symlink ({member!r}); refusing to unpack it.")


def _extract_member(wheel: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path) -> None:
    extracted = Path(wheel.extract(info, dest))
    # zipfile drops file modes, so a bundled script would lose its executable bit.
    archived = (info.external_attr >> 16) & _EXECUTE_BITS
    if archived:
        mode = extracted.stat().st_mode
        # Add execute only where the archive set it and the umask left read access.
        readers = (mode & _READ_BITS) >> 2
        extracted.chmod(mode | (archived & readers))


def _delete_wheel_metadata(contents: Path, wheel: _Wheel) -> None:
    """Delete the wheel's install metadata, so only package files can be vendored."""
    # The wheel spec names both folders after the filename's {distribution}-{version}.
    prefix = "-".join(wheel.filename.split("-")[:2])
    for name in (f"{prefix}.dist-info", f"{prefix}.data"):
        delete_path(contents / name)


def _find_extension_dir(
    contents: Path, package: str, version: str, selector: str | None
) -> Path:
    if selector and (contents / selector / EXTENSION_FILE).is_file():
        return contents / selector
    if not selector and (contents / EXTENSION_FILE).is_file():
        return contents
    found = sorted(m.parent for m in contents.rglob(EXTENSION_FILE) if m.is_file())
    if not found:
        raise PypiError(
            f"{package!r} has no {EXTENSION_FILE}, so it is not an agents-cli "
            "extension; check the package name."
        )
    if selector:
        raise _pick_one_error(
            contents, package, version, found, f"has no {EXTENSION_FILE} in {selector}/"
        )
    if len(found) > 1:
        raise _pick_one_error(
            contents, package, version, found, "holds several extensions"
        )
    return found[0]


def _pick_one_error(
    contents: Path, package: str, version: str, found: list[Path], problem: str
) -> PypiError:
    # References, not `extension add` commands: update and install raise this too.
    choices = "\n".join(
        "  "
        + pypi_source(
            package,
            version,
            None if p == contents else p.relative_to(contents).as_posix(),
        )
        for p in found
    )
    return PypiError(f"{package!r} {problem}; use one of these references:\n{choices}")
