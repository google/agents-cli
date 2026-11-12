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

"""Pre-flight check of the model credentials a local agent server will use.

Without it, a missing credential crashes the server at startup or fails the
first model call with a raw google-auth error.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
from collections.abc import Iterator, Mapping
from pathlib import Path

from dotenv import dotenv_values

from google.agents.cli._auth_hints import with_auth_hint
from google.agents.cli._defaults import GCP_PROJECT_PLACEHOLDER
from google.agents.cli._project import find_nearest_dotenv
from google.agents.cli.auth import GCLOUD_LOGIN

# google-genai uses ENTERPRISE whenever it's set, ignoring VERTEXAI, and reads
# VERTEXAI only when ENTERPRISE is unset.
_GEMINI_PLATFORM_FLAGS = ("GOOGLE_GENAI_USE_ENTERPRISE", "GOOGLE_GENAI_USE_VERTEXAI")
_API_KEY_VARS = ("GOOGLE_API_KEY", "GEMINI_API_KEY")
_TRUE_VALUES = ("true", "1")
# google-auth waits up to 120s by default; a pre-flight shouldn't hang that long.
_TOKEN_TIMEOUT_SECONDS = 15
_DEFAULT_CREDENTIAL_SOURCE = "your Application Default Credentials"
# Every warning ends with this: the scaffolded .env selects the Gemini platform
# even for an agent that was later switched to another model provider.
_NOT_GEMINI_NOTE = (
    "\n  If the agent doesn't use Gemini, remove GOOGLE_GENAI_USE_ENTERPRISE and "
    "GOOGLE_GENAI_USE_VERTEXAI from .env to skip this check."
)


def warn_if_model_auth_broken(agent_dir: Path) -> None:
    """Warn when a local agent on the Gemini platform can't call its model.

    Reads the ``.env`` the agent server loads (the nearest one at or above the
    agent folder), with variables already set in the shell taking precedence,
    as they do in the server. Only warns, so an agent that kept the template's
    Gemini platform settings but calls another model still runs. Checks nothing
    for the Gemini API (API key) backend.
    """
    env = {**_read_nearest_dotenv(agent_dir), **os.environ}
    if not _uses_gemini_platform(env):
        return
    if env.get("GOOGLE_CLOUD_PROJECT") == GCP_PROJECT_PLACEHOLDER:
        logging.warning(
            "GOOGLE_CLOUD_PROJECT is still the placeholder '%s'.\n"
            "  Set it to your Google Cloud project ID in .env, or comment out the "
            "Gemini platform lines in .env and set GEMINI_API_KEY to use the "
            "Gemini API instead.%s",
            GCP_PROJECT_PLACEHOLDER,
            _NOT_GEMINI_NOTE,
        )
        return

    api_keys = [name for name in _API_KEY_VARS if env.get(name)]
    # google-genai uses an API key on the Gemini platform (express mode) only
    # when no project or location is set; otherwise it ignores the key.
    project_or_location = env.get("GOOGLE_CLOUD_PROJECT") or env.get(
        "GOOGLE_CLOUD_LOCATION"
    )
    if api_keys and not project_or_location:
        return
    if api_keys:
        logging.warning(
            "%s is set but unused: the Gemini platform settings in .env make the "
            "agent call the model with Application Default Credentials. To use "
            "the key, comment out GOOGLE_GENAI_USE_ENTERPRISE, "
            "GOOGLE_GENAI_USE_VERTEXAI, GOOGLE_CLOUD_PROJECT and "
            "GOOGLE_CLOUD_LOCATION in .env.",
            " and ".join(api_keys),
        )

    if env.get("GOOGLE_APPLICATION_CREDENTIALS") != os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS"
    ):
        # The server picks up a key file named only in .env; this process can't
        # see it, so checking ADC here would test the wrong credentials.
        return
    _warn_if_no_adc_token()


def _read_nearest_dotenv(agent_dir: Path) -> dict[str, str]:
    dotenv_path = find_nearest_dotenv(agent_dir)
    if dotenv_path is None:
        return {}
    return {
        key: value
        for key, value in dotenv_values(dotenv_path, encoding="utf-8").items()
        if value is not None
    }


def _uses_gemini_platform(env: Mapping[str, str]) -> bool:
    for flag in _GEMINI_PLATFORM_FLAGS:
        if flag in env:
            return env[flag].strip().lower() in _TRUE_VALUES
    return False


def _warn_if_no_adc_token() -> None:
    # Imported here to keep google-auth off the CLI startup path.
    import google.auth.exceptions
    from google.auth.transport.requests import Request as GoogleAuthRequest

    from google.agents.cli.auth import get_adc_credentials

    try:
        with _google_auth_logs_hidden():
            credentials, _ = get_adc_credentials()
    except google.auth.exceptions.DefaultCredentialsError as e:
        logging.warning(
            "%s%s",
            with_auth_hint(f"The agent can't call Gemini on Google Cloud: {e}"),
            _NOT_GEMINI_NOTE,
        )
        return
    except OSError as e:
        # google-auth lets an unreadable credentials file's OSError through.
        logging.warning(
            "The agent can't call Gemini on Google Cloud: could not read a "
            "credentials file: %s\n  Check that it exists and that you can read "
            "it, or point GOOGLE_APPLICATION_CREDENTIALS at another key file.%s",
            e,
            _NOT_GEMINI_NOTE,
        )
        return
    except google.auth.exceptions.GoogleAuthError as e:
        # The check is advisory, so an unexpected failure mustn't stop the command.
        logging.warning("Could not check your Google Cloud credentials: %s", e)
        return

    try:
        with _google_auth_logs_hidden():
            credentials.refresh(
                functools.partial(GoogleAuthRequest(), timeout=_TOKEN_TIMEOUT_SECONDS)
            )
    except google.auth.exceptions.RefreshError as e:
        source = _credential_source(credentials)
        # RefreshError carries the raw HTTP response as a second argument.
        cause = e.args[0] if e.args else e
        message = (
            f"The agent can't call Gemini on Google Cloud: {source} could not "
            f"get an access token.\n  {cause}"
        )
        hinted = with_auth_hint(message)
        if hinted == message:
            hinted += f"\n  Run `{GCLOUD_LOGIN}` to sign in again."
        logging.warning("%s%s", hinted, _NOT_GEMINI_NOTE)
    except (google.auth.exceptions.GoogleAuthError, OSError) as e:
        # Maybe a passing network problem; let the agent try for itself.
        logging.warning("Could not check your Google Cloud credentials: %s", e)


@contextlib.contextmanager
def _google_auth_logs_hidden() -> Iterator[None]:
    """Hide google-auth's warnings, such as one per metadata server probe.

    The check reports the outcome itself. Left alone at DEBUG.
    """
    logger = logging.getLogger("google.auth")
    saved_level = logger.level
    if logger.getEffectiveLevel() > logging.DEBUG:
        logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(saved_level)


def _credential_source(credentials: object) -> str:
    """Where the Application Default Credentials came from, for messages."""
    # Imported here to keep google-auth off the CLI startup path.
    from google.auth import compute_engine, external_account, impersonated_credentials
    from google.oauth2 import credentials as user_credentials
    from google.oauth2 import service_account

    sources = (
        (
            user_credentials.Credentials,
            "your user credentials in the Application Default Credentials file",
        ),
        (
            service_account.Credentials,
            "the service account key file in GOOGLE_APPLICATION_CREDENTIALS",
        ),
        (compute_engine.Credentials, "the Compute Engine metadata server"),
        (external_account.Credentials, "workload identity federation"),
        (impersonated_credentials.Credentials, "service account impersonation"),
    )
    for credentials_type, source in sources:
        if isinstance(credentials, credentials_type):
            return source
    return _DEFAULT_CREDENTIAL_SOURCE
