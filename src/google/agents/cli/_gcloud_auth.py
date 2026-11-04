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

"""Let gcloud subprocesses fall back to Application Default Credentials.

gcloud reads only its own credential store, never ADC, so in an environment
that has ADC but no `gcloud auth login` (CI with a service account key, a
container with ADC mounted) every gcloud command fails with "You do not
currently have an active account selected". When gcloud has no account of its
own, agents-cli hands it an ADC access token instead.
"""

import json
import logging
import math
import os
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from google.agents.cli._runner import run_resolved
from google.agents.cli._tools import ToolNotExecutableError, ToolNotFoundError

_ACCESS_TOKEN_ENV = "CLOUDSDK_AUTH_ACCESS_TOKEN"
# Any of these means the user already chose how gcloud authenticates.
_GCLOUD_AUTH_OVERRIDE_ENVS = (
    _ACCESS_TOKEN_ENV,
    "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
    "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
)
# gcloud command groups that never send credentials, so they skip the account check.
_CREDENTIAL_FREE_GROUPS = frozenset(
    {"components", "config", "help", "info", "topic", "version"}
)
# gcloud's config folder defaults to one under these (~/.config/gcloud, or
# %APPDATA%\gcloud on Windows).
_GCLOUD_CONFIG_DIR_ENVS = frozenset({"HOME", "APPDATA"})
# A release track goes before the command group, as in `gcloud beta auth login`.
_RELEASE_TRACKS = frozenset({"alpha", "beta"})
# `gcloud auth` subcommands that only read credentials; any other one may change
# the account gcloud or ADC signs in as.
_READ_ONLY_AUTH_COMMANDS = frozenset(
    {"list", "print-access-token", "print-identity-token"}
)
# A missing or blocked gcloud, which matters when adc_token_for_gcloud() is
# called directly rather than from a gcloud call that already found gcloud.
_GCLOUD_RUN_ERRORS = (
    ToolNotFoundError,
    ToolNotExecutableError,
    OSError,
    subprocess.TimeoutExpired,
)
# gcloud doesn't say when a token it prints expires, so reuse it only briefly.
_GCLOUD_TOKEN_TTL_SECONDS = 300


@dataclass(frozen=True)
class _CachedToken:
    token: str | None
    expires_at: float  # A time.monotonic() deadline.


_cached_token: _CachedToken | None = None
# Whether gcloud has credentials of its own, per gcloud environment (see
# _gcloud_env_key). Cleared when a `gcloud auth` command may change accounts.
_own_credentials: dict[tuple[tuple[str, str], ...], bool] = {}


def fallback_token_env(
    gcloud_args: Sequence[str], env: Mapping[str, str] | None
) -> dict[str, str] | None:
    """Return the environment for a gcloud subprocess with an ADC token added.

    Returns None, meaning "run gcloud as is", when the command is a
    `gcloud auth` command or doesn't use credentials, or when
    `adc_token_for_gcloud` returns None.

    Args:
        gcloud_args: The gcloud arguments, without the leading "gcloud".
        env: The environment the caller passes to the subprocess, or None for
            the current process environment.
    """
    command = _command_path(gcloud_args)
    group = command[0] if command else None
    # `gcloud auth` commands manage gcloud's own credentials; a token would
    # override them (e.g. print-identity-token fails with one).
    if group == "auth":
        if not _READ_ONLY_AUTH_COMMANDS.intersection(command[1:]):
            # The account may change, so check it again on the next call.
            _clear_caches()
        return None
    if group is None or group in _CREDENTIAL_FREE_GROUPS:
        return None
    base_env = dict(os.environ if env is None else env)
    token = adc_token_for_gcloud(base_env)
    if token is None:
        return None
    return {**base_env, _ACCESS_TOKEN_ENV: token}


def adc_token_for_gcloud(env: Mapping[str, str] | None = None) -> str | None:
    """Return the ADC token that gcloud commands from agents-cli run with.

    Returns None when gcloud uses its own credentials: an active account, or
    an access token or credential file set in the environment or in gcloud's
    config. Also returns None when ADC is unavailable.

    Args:
        env: The environment gcloud runs with, or None for the current
            process environment.
    """
    base_env = os.environ if env is None else env
    if any(base_env.get(name) for name in _GCLOUD_AUTH_OVERRIDE_ENVS):
        return None
    if _gcloud_has_own_credentials(base_env):
        return None
    token = _get_adc_token()
    if token is not None:
        logging.debug("gcloud has no credentials of its own; passing it the ADC token.")
    return token


def _command_path(gcloud_args: Sequence[str]) -> list[str]:
    """The command group and subcommands, without flags or a release track.

    Global flags before the group must use the `--flag=value` form, or their
    value would be taken for the group.
    """
    command = [arg for arg in gcloud_args if not arg.startswith("-")]
    if command and command[0] in _RELEASE_TRACKS:
        return command[1:]
    return command


def _clear_caches() -> None:
    # Imported here: auth imports _runner, which imports this module lazily.
    from google.agents.cli.auth import reset_adc_credentials

    global _cached_token
    _cached_token = None
    _own_credentials.clear()
    reset_adc_credentials()


def _gcloud_has_own_credentials(env: Mapping[str, str]) -> bool:
    # The checks run with the gcloud call's own environment: a CLOUDSDK_CONFIG
    # or CLOUDSDK_ACTIVE_CONFIG_NAME there selects other accounts and config.
    key = _gcloud_env_key(env)
    if key not in _own_credentials:
        check_env = dict(env)
        _own_credentials[key] = _gcloud_has_active_account(
            check_env
        ) or _gcloud_auth_configured(check_env)
    return _own_credentials[key]


def _gcloud_env_key(env: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """The variables that change which accounts and config gcloud reads."""
    return tuple(
        sorted(
            (name, value)
            for name, value in env.items()
            if name.startswith("CLOUDSDK_") or name in _GCLOUD_CONFIG_DIR_ENVS
        )
    )


def _gcloud_has_active_account(env: dict[str, str]) -> bool:
    try:
        result = run_resolved(
            [
                "gcloud",
                "auth",
                "list",
                "--filter=status:ACTIVE",
                "--format=value(account)",
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except _GCLOUD_RUN_ERRORS as e:
        logging.debug("Could not list gcloud accounts: %s", e)
        return True
    # If gcloud can't tell, leave it alone rather than override an account it may have.
    return result.returncode != 0 or bool(result.stdout.strip())


def _gcloud_auth_configured(env: dict[str, str]) -> bool:
    """Whether gcloud's config sets an access token file or credential file.

    `gcloud auth list` shows no account for these, and the ADC token would
    take precedence over them.
    """
    try:
        result = run_resolved(
            ["gcloud", "config", "list", "--format=json(auth)"],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except _GCLOUD_RUN_ERRORS as e:
        logging.debug("Could not read the gcloud config: %s", e)
        return True
    if result.returncode != 0:
        return True
    try:
        # gcloud prints `{"auth": {...}}`, or `null` when no auth property is set.
        auth = (json.loads(result.stdout.strip() or "null") or {}).get("auth") or {}
        return bool(auth.get("access_token_file") or auth.get("credential_file_override"))
    except (json.JSONDecodeError, AttributeError) as e:
        logging.debug("Could not parse the gcloud auth config: %s", e)
        return True


def _get_adc_token() -> str | None:
    global _cached_token
    if _cached_token is not None and time.monotonic() < _cached_token.expires_at:
        return _cached_token.token

    # Imported here: auth imports _runner, which imports this module lazily.
    import google.auth.exceptions
    from google.auth.transport.requests import Request as GoogleAuthRequest

    from google.agents.cli.auth import get_adc_credentials

    try:
        credentials, _ = get_adc_credentials()
        # The cached credentials refresh only when expired, so a long command
        # doesn't pay for a token request on every gcloud call.
        if not credentials.valid:
            credentials.refresh(GoogleAuthRequest())
        return credentials.token
    except (google.auth.exceptions.DefaultCredentialsError, OSError) as e:
        # gcloud reads the same ADC file, so it can't find a token either. An
        # unreadable GOOGLE_APPLICATION_CREDENTIALS file raises OSError.
        logging.debug("No Application Default Credentials for gcloud: %s", e)
        _cached_token = _CachedToken(None, math.inf)
        return None
    except google.auth.exceptions.RefreshError as e:
        # google-auth can't refresh some user credentials that gcloud can, e.g.
        # when the organization requires reauthentication.
        logging.debug("google-auth could not refresh ADC: %s", e)
    except google.auth.exceptions.GoogleAuthError as e:
        # Not cached: a network error may pass before the next gcloud call.
        logging.debug("Could not get an ADC access token: %s", e)
        return None

    token = _print_adc_token_with_gcloud()
    ttl = _GCLOUD_TOKEN_TTL_SECONDS if token else math.inf
    _cached_token = _CachedToken(token, time.monotonic() + ttl)
    return token


def _print_adc_token_with_gcloud() -> str | None:
    try:
        result = run_resolved(
            ["gcloud", "auth", "application-default", "print-access-token", "--quiet"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except _GCLOUD_RUN_ERRORS as e:
        logging.debug("Could not print the ADC access token: %s", e)
        return None
    if result.returncode != 0 or not result.stdout.strip():
        # gcloud then fails with its own "run gcloud auth login" message.
        logging.debug("No Application Default Credentials for gcloud: %s", result.stderr)
        return None
    return result.stdout.strip()
