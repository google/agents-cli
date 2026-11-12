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

"""Download files whose sha256 is known in advance."""

from __future__ import annotations

import hashlib
import ssl
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    import requests
    from requests.auth import HTTPBasicAuth

_CHUNK_SIZE = 1 << 16


class DownloadError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def download_verified(
    url: str,
    dest: Path,
    sha256: str,
    *,
    auth: tuple[bytes, bytes] | None,
    timeout: float,
) -> None:
    """Download ``url`` to ``dest`` and check it against the hex digest ``sha256``.

    On ``DownloadError``, ``dest`` is deleted.
    """
    # Deferred: requests takes about 0.2s to import.
    import requests

    # Messages name the file, not the URL: it can carry credentials or a signed token.
    name = unquote(urlsplit(url).path.rpartition("/")[2]) or "the file"
    digest = hashlib.sha256()
    try:
        with (
            requests.get(
                url,
                auth=basic_auth(auth),
                stream=True,
                timeout=timeout,
                hooks={"response": [refuse_https_downgrade]},
            ) as response,
            open(dest, "wb") as f,
        ):
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                digest.update(chunk)
                f.write(chunk)
    except requests.RequestException as e:
        dest.unlink(missing_ok=True)
        status = e.response.status_code if e.response is not None else None
        raise DownloadError(
            f"could not download {name} ({request_error_reason(e)})", status
        ) from e
    except (DownloadError, OSError) as e:
        dest.unlink(missing_ok=True)
        raise DownloadError(f"could not download {name}: {e}") from e
    if digest.hexdigest() != sha256.lower():
        dest.unlink(missing_ok=True)
        raise DownloadError(
            f"{name} does not match the expected sha256; refusing to use it."
        )


def basic_auth(credentials: tuple[bytes, bytes] | None) -> HTTPBasicAuth | None:
    """Takes bytes, since requests encodes str credentials as Latin-1."""
    from requests.auth import HTTPBasicAuth

    return HTTPBasicAuth(*credentials) if credentials else None


def request_error_reason(error: requests.RequestException) -> str:
    """Why a request failed, without the URL that requests puts in its own message."""
    # Deferred: requests takes about 0.2s to import.
    import requests

    # Checked before the response, which holds the last redirect.
    if isinstance(error, requests.TooManyRedirects):
        return "too many redirects"
    if error.response is not None:
        return f"HTTP {error.response.status_code}"
    if isinstance(error, requests.exceptions.SSLError):
        if _is_certificate_error(error):
            return (
                "the server's TLS certificate could not be verified; if a proxy "
                "intercepts TLS, point REQUESTS_CA_BUNDLE at its CA bundle"
            )
        return "TLS error"
    if isinstance(error, requests.Timeout):
        return "timed out"
    if isinstance(error, requests.exceptions.InvalidURL):
        return "the URL is not valid"
    if isinstance(error, requests.ConnectionError):
        return "connection failed"
    return type(error).__name__


def _is_certificate_error(error: BaseException) -> bool:
    # requests chains the ssl error below its own and urllib3's errors.
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, ssl.SSLCertVerificationError):
            return True
        cause = cause.__cause__ or cause.__context__
    return False


def refuse_https_downgrade(response: requests.Response, **_: Any) -> None:
    """A requests response hook that refuses to follow a redirect from https to http.

    requests drops credentials on such a redirect, but then adds any from ~/.netrc.
    """
    location = response.headers.get("location")
    if (
        response.is_redirect
        and location is not None
        and urlsplit(response.url).scheme == "https"
        and urlsplit(location).scheme == "http"
    ):
        response.close()
        raise DownloadError("it redirected to plain http, which was refused")
