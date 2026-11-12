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

"""Authentication module for agents-cli.

Supports 3 authentication methods:
- Google Cloud credentials (Application Default Credentials)
- Gemini API Key from AI Studio (GEMINI_API_KEY)
- Express Mode / Gemini platform API key (GOOGLE_API_KEY)

Authentication is based on environment variables and gcloud ADC.
For API key methods, we print instructions for the user to
export and persist the environment variable themselves.
"""

import enum
import logging
import os
import shlex
import subprocess
import warnings
import webbrowser
from typing import Any

import click

from google.agents.cli._runner import run_resolved
from google.agents.cli._tools import ToolNotFoundError, require_tool


class AuthType(enum.Enum):
    """Supported authentication methods."""

    GOOGLE_CLOUD = "google_cloud"
    GEMINI_API_KEY = "gemini_api_key"
    EXPRESS_MODE = "express_mode"


def _api_key_instructions(var_name):
    """Print instructions for the user to export the API_KEY env var.

    Args:
        var_name: Environment variable name (e.g. GEMINI_API_KEY).
    """
    click.echo()
    click.echo("  To use this API key in your current session, run:")
    click.echo()
    click.secho(f'    export {var_name}="YOUR_API_KEY"', bold=True)
    click.echo()
    click.echo(
        "  To persist this variable across sessions, you can add it to your shell profile."
    )
    click.echo("  For example, add the export command above to your shell's startup file")
    click.echo("  (e.g., ~/.bashrc, ~/.zshrc, or ~/.profile).")
    click.echo()
    click.echo(
        "  If you create an ADK project, you can also store the API key under .env in the agent folder."
    )
    click.echo()
    logging.warning(
        "Be aware that when you export API keys or service account paths in "
        "your shell configuration file, any process launched from that shell "
        "can read them."
    )
    click.echo()


# ── Google Cloud (ADC) ──────────────────────────────────────────────


_adc_credentials = None

# Signs in gcloud and ADC with one browser login. agents-cli calls Google Cloud
# through both, so every sign-in hint suggests this command.
_GCLOUD_LOGIN_CMD = ("gcloud", "auth", "login", "--update-adc")
GCLOUD_LOGIN = shlex.join(_GCLOUD_LOGIN_CMD)
# Prints a URL to open on another machine instead of launching a local browser.
# https://cloud.google.com/sdk/gcloud/reference/auth/login
_GCLOUD_HEADLESS_LOGIN_CMD = (*_GCLOUD_LOGIN_CMD, "--no-launch-browser")

_QUOTA_PROJECT_WARNING_PREFIX = (
    "Your application has authenticated using end user credentials"
)


def get_adc_credentials() -> tuple[Any, str | None]:
    """Get Application Default Credentials, caching the result as a singleton.

    The credentials object already handles token expiration / refresh etc
    internally so this is safe.

    The credentials returned are Any-typed because they can actually take
    quite a few forms in practice. e.g. if authed as a service account
    `service_account_email` will be present, but if authed as a user
    it will not be.

    Returns:
        A tuple of (credentials, project_id).
    """
    global _adc_credentials
    if _adc_credentials is None:
        _adc_credentials = default_credentials(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )

    return _adc_credentials


def reset_adc_credentials() -> None:
    """Forget the cached ADC, e.g. after a `gcloud auth` command changed them."""
    global _adc_credentials
    _adc_credentials = None


def default_credentials(**kwargs: Any) -> tuple[Any, str | None]:
    """``google.auth.default()`` without its missing-quota-project warning.

    The warning is advice for developers of apps that call Google APIs with
    gcloud user credentials; printed by agents-cli it only alarms users.
    """
    import google.auth

    # Not catch_warnings(): it isn't thread-safe, and telemetry calls this from
    # a background thread. Repeat calls don't stack duplicate filters.
    warnings.filterwarnings(
        "ignore", message=_QUOTA_PROJECT_WARNING_PREFIX, category=UserWarning
    )
    return google.auth.default(**kwargs)


def _setup_google_cloud_adc():
    """Guide the user through Google Cloud ADC setup.

    Checks for existing ADC. If not found, checks for gcloud CLI
    and offers to run ``gcloud auth login --update-adc``, which signs in
    both gcloud itself and ADC: agents-cli calls Google Cloud through both.

    Returns:
        Dict with project and account info on success, or None
        if the user needs to retry after manual setup.
    """

    # No ADC found — guide the user
    click.echo()
    click.echo("  No valid Application Default Credentials found.")
    click.echo()

    # Check if gcloud is installed
    try:
        require_tool("gcloud")
    except ToolNotFoundError:
        click.echo("  The Google Cloud CLI (gcloud) is not installed.")
        click.echo()
        click.echo("  To install it:")
        click.echo()
        click.secho(
            "    https://cloud.google.com/sdk/docs/install",
            fg="cyan",
        )
        click.echo()
        click.echo("  After installing, run:")
        click.echo()
        click.secho(
            f"    {GCLOUD_LOGIN}",
            bold=True,
        )
        click.echo()
        click.echo("  Then re-run setup.")
        return None

    # gcloud is installed — offer to run login
    click.echo(
        "  This will open a browser window to authenticate with your Google account."
    )
    click.echo("  Answer 'n' if this machine has no browser (for example over SSH).")
    click.echo()

    if not click.confirm(
        f"  Run '{GCLOUD_LOGIN}' now",
        default=True,
    ):
        click.echo()
        click.echo("  Run this command manually when ready:")
        click.echo()
        click.secho(
            f"    {GCLOUD_LOGIN}",
            bold=True,
        )
        click.echo()
        _print_headless_login_hint()
        return None

    click.echo()
    click.echo("  Opening browser for authentication...")
    click.echo()

    try:
        result = run_resolved(
            list(_GCLOUD_LOGIN_CMD),
            timeout=120,
        )
        if result.returncode != 0:
            click.secho(
                "  gcloud login did not complete successfully.",
                fg="red",
            )
            _print_headless_login_hint()
            return None
    except subprocess.TimeoutExpired:
        click.secho(
            "  Authentication timed out. Run the command manually.",
            fg="red",
        )
        _print_headless_login_hint()
        return None
    except Exception as e:
        click.secho(f"  Failed to run gcloud: {e}", fg="red")
        return None

    # Verify ADC is now available
    if not _check_valid_adc(report_problems=True):
        click.secho(
            "  Authentication completed but credentials could not be verified.",
            fg="red",
        )
        return None

    return {"project": _get_adc_project(), "account": _get_gcloud_account()}


def _print_headless_login_hint():
    click.echo(
        "  No browser on this machine (for example over SSH)? Run this instead and"
    )
    click.echo("  open the printed URL on another machine:")
    click.echo()
    click.secho(f"    {shlex.join(_GCLOUD_HEADLESS_LOGIN_CMD)}", bold=True)


def _get_gcloud_account():
    """Get the active gcloud account, if available."""
    try:
        result = run_resolved(
            ["gcloud", "config", "get-value", "account"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


# ── Gemini API Key ──────────────────────────────────────────────────


def _setup_gemini_api_key():
    """Guide the user through Gemini API key setup.

    Checks for GEMINI_API_KEY env var. If not set, opens AI Studio in the
    browser and prints the shell and project `.env` changes the user (or
    their coding agent) must make. The key is never read or stored.

    Returns:
        Dict if GEMINI_API_KEY is already set, otherwise None.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        return {"env_var": "GEMINI_API_KEY", "from_env": True}

    # Not set — guide the user
    click.echo()
    click.echo("  GEMINI_API_KEY is not set in your environment.")
    click.echo()

    url = "https://aistudio.google.com/apikey"
    click.echo("  We'll open Google AI Studio in your browser.")
    click.echo("  Create or copy an API key, then come back here for more instructions.")
    click.echo()
    click.secho(f"  {url}", fg="cyan")
    click.echo()

    click.pause("  Press Enter to open the browser...")
    webbrowser.open(url)

    click.echo()

    _api_key_instructions("GEMINI_API_KEY")
    _gemini_dotenv_instructions()

    return None


def _gemini_dotenv_instructions():
    """Print the `.env` edits that switch a generated project to the Gemini API.

    Generated projects default to Gemini platform (Vertex) in `.env`, and that
    flag wins over GEMINI_API_KEY, so exporting the key alone is not enough.
    """
    click.secho("  To use the API key in an agents-cli project:", bold=True)
    click.echo()
    click.echo("  Edit the .env file in the project root (no other files need changes):")
    click.echo()
    click.echo("    1. Set the API key:")
    click.secho("         GEMINI_API_KEY=YOUR_API_KEY", bold=True)
    click.echo("    2. Turn off Gemini platform mode. Python projects:")
    click.secho("         GOOGLE_GENAI_USE_ENTERPRISE=false", bold=True)
    click.echo("       Go, Java, TypeScript and Kotlin projects:")
    click.secho("         GOOGLE_GENAI_USE_VERTEXAI=false", bold=True)
    click.echo("    3. GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION are not needed")
    click.echo("       for local runs in this mode; you can comment them out.")
    click.echo()
    click.echo("  A coding agent can make these .env edits for you. Keep .env out of")
    click.echo("  version control.")
    click.echo()


# ── Express Mode ────────────────────────────────────────────────────


# TODO(b/495501218): Re-enable Express Mode in the login menu once launched.
def _setup_express_mode():
    """Guide the user through setting up Express Mode in Gemini platform.

    Checks for GOOGLE_API_KEY env var. If not set, opens the
    API keys page and prompts the user to paste a key.
    Persists the key by appending to the user's shell profile.

    Returns:
        Dict on success, or None if cancelled.
    """
    api_key = os.environ.get("GOOGLE_API_KEY")
    if api_key:
        return {"env_var": "GOOGLE_API_KEY", "from_env": True}

    # Not set — guide the user
    click.echo()
    click.echo("  GOOGLE_API_KEY is not set in your environment.")
    click.echo()

    url = "https://cloud.google.com/vertex-ai/generative-ai/docs/start/api-keys"
    click.echo("  We'll open the API keys page in your browser.")
    click.echo("  Create or copy an API key, then come back here for more instructions.")
    click.echo()
    click.secho(f"  {url}", fg="cyan")
    click.echo()

    click.pause("  Press Enter to open the browser...")
    webbrowser.open(url)

    click.echo()

    _api_key_instructions("GOOGLE_API_KEY")

    return None


def run_auth_step(show_header=True):
    """Run the interactive authentication step.

    Checks if already authenticated; if so, displays status.
    Otherwise, presents auth method selection menu and guides
    the user through the chosen method.

    Args:
        show_header: If True, print the "Authentication" header.
            Set to False when the caller already provides a section header.

    Returns:
        True if authenticated (or skipped), False on hard failure.
    """
    # Check if already authenticated
    authed, display = is_authenticated()
    if authed:
        if show_header:
            click.secho("Authentication", bold=True)
            click.echo()
        click.secho(f"  Authenticated as {display}", fg="green")
        click.echo()
        return True

    if show_header:
        click.secho("Authentication", bold=True)
        click.echo()
    click.echo("  Choose an authentication method:")
    click.echo()
    click.secho("  1. Google Cloud (ADC)", bold=True)
    click.echo("     Use gcloud Application Default Credentials.")
    click.echo("     Best for: production workloads, full GCP access")
    click.echo()
    click.secho("  2. Gemini API Key", bold=True)
    click.echo("     Enter an API key from AI Studio (aistudio.google.com).")
    click.echo("     Best for: getting started, personal projects")
    click.echo()
    click.secho("  3. Skip", dim=True)
    click.echo()

    choice = click.prompt("  Enter your choice", type=click.IntRange(1, 3), default=1)

    if choice == 3:
        click.echo()
        click.secho(
            "  Skipped. Run 'agents-cli login' later to authenticate.",
            fg="yellow",
        )
        click.echo()
        return True

    # Each method handles its own guided flow and UI
    result = None
    try:
        if choice == 1:
            result = _setup_google_cloud_adc()
        elif choice == 2:
            result = _setup_gemini_api_key()
    except Exception as e:
        click.echo()
        click.secho(f"  {e}", fg="red")

    click.echo()

    if result is not None:
        # Show success — re-read the saved display string
        _, display = is_authenticated()
        if display:
            click.secho(f"  Authenticated as {display}", fg="green")
        else:
            click.secho("  Credentials saved", fg="green")
    elif choice == 2:
        click.secho(
            "  Finish the Gemini API key setup with the steps above, then run"
            " 'agents-cli login --status' to confirm.",
            fg="yellow",
        )
    else:
        click.secho(
            "  Continuing without authentication. Run 'agents-cli login' later.",
            fg="yellow",
        )

    click.echo()
    return True


# ── Shared Helpers ──────────────────────────────────────────────────


def is_authenticated(*, report_problems: bool = True):
    """Check if valid credentials exist by inspecting env vars and ADC.

    With valid ADC, also checks that they can call the Gemini platform (a
    network call) and warns if not, e.g. when no project is set.

    Args:
        report_problems: Log why ADC is unusable or can't reach the Gemini
            platform. Pass False to re-check status without repeating that.

    Returns:
        Tuple of (bool, str_or_None) — authenticated status and
        a human-readable display string.
    """
    # Check GEMINI_API_KEY (instant)
    if os.environ.get("GEMINI_API_KEY"):
        return True, "Gemini API Key (GEMINI_API_KEY)"

    # Check GOOGLE_API_KEY (instant)
    if os.environ.get("GOOGLE_API_KEY"):
        return True, "Express Mode (GOOGLE_API_KEY)"

    # Check ADC validity
    if not _check_valid_adc(report_problems=report_problems):
        return False, None

    # Get project and account for display
    project = _get_adc_project()
    account = _get_gcloud_account()
    display = f"{account or 'Google Cloud'}{f' for project {project}' if project else ''} (Application Default Credentials)"
    if report_problems:
        _warn_if_vertex_unreachable()
    return True, display


def _check_valid_adc(*, report_problems: bool):
    # google.auth.default() doesn't actually validate credentials
    # until you try to refresh them. That refresh can be extremely slow
    # if credentials are missing or invalid, so we use the gcloud version
    # which goes much faster.
    try:
        run_resolved(
            ["gcloud", "auth", "application-default", "print-access-token", "--quiet"],
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        return True
    except ToolNotFoundError:
        # If gcloud is not installed, fallback to the slower google.auth.default() means of checking
        import google.auth.exceptions
        from google.auth.transport.requests import Request as GoogleAuthRequest

        try:
            credentials, _ = default_credentials()
            credentials.refresh(GoogleAuthRequest())
            return True
        except google.auth.exceptions.DefaultCredentialsError as e:
            # Nothing is set up yet, and every caller already says so.
            logging.debug("No Application Default Credentials: %s", e)
            return False
        except Exception as e:
            if report_problems:
                logging.info("Application Default Credentials are not usable: %s", e)
            return False
    except subprocess.CalledProcessError as e:
        reason = (e.stderr or "").strip()
        if "default credentials were not found" in reason.lower():
            # Same as above; its doc link also suggests a different login command.
            logging.debug("gcloud reported: %s", reason)
        elif reason and report_problems:
            logging.info("gcloud reported: %s", reason)
        return False


def _get_adc_project():
    """Return the ADC project"""
    try:
        _, project = get_adc_credentials()
        return project
    except Exception:
        return None


def _warn_if_vertex_unreachable():
    """Warn when ADC is valid but can't call the Gemini platform yet."""
    # Lazy: gcp pulls in requests and imports this module back.
    from google.agents.cli.scaffold.utils.gcp import verify_credentials_and_vertex

    try:
        verify_credentials_and_vertex()
    except Exception as e:
        # It raises a bare Exception whose message says what to fix; signing in
        # still succeeded, so report it without failing the command.
        logging.warning("%s", e)


# ── Token Helpers ──────────────────────────────────────────────────


def get_access_token() -> str:
    """Get a Google Cloud access token (for googleapis.com endpoints).

    Tries ADC with ``cloud-platform`` scope first, then falls back to
    ``gcloud auth print-access-token``.

    Raises:
        RuntimeError: If both paths fail.
    """
    from google.auth.transport.requests import Request as GoogleAuthRequest

    try:
        credentials, _ = get_adc_credentials()
        credentials.refresh(GoogleAuthRequest())
        if credentials.token:
            return credentials.token
    except Exception:
        pass

    try:
        return get_gcloud_access_token()
    except Exception as exc:
        raise RuntimeError("Failed to get access token") from exc


def get_gcloud_access_token() -> str:
    """Get the access token of the gcloud account (``gcloud auth print-access-token``)."""
    from google.agents.cli.scaffold.utils.command import run_gcloud_command

    result = run_gcloud_command(
        ["auth", "print-access-token"],
        capture_output=True,
        check=True,
        timeout=5,
    )
    return result.stdout.strip()


def get_id_token(audience: str) -> str:
    """Get a Google Cloud ID token for the given audience.

    Tries the IAM ``generateIdToken`` API first (works with WIF, SA keys,
    and ADC), then falls back to ``gcloud auth print-identity-token``
    for local development with user credentials.

    Raises:
        subprocess.CalledProcessError: If both paths fail.
    """
    import requests
    from google.auth.transport.requests import Request as GoogleAuthRequest

    from google.agents.cli.scaffold.utils.command import run_gcloud_command

    try:
        credentials, _ = get_adc_credentials()
        credentials.refresh(GoogleAuthRequest())
        sa_email = credentials.service_account_email
        iam_response = requests.post(
            f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{sa_email}:generateIdToken",
            headers={"Authorization": f"Bearer {credentials.token}"},
            json={"audience": audience, "includeEmail": True},
        )
        iam_response.raise_for_status()
        return iam_response.json()["token"]
    except (AttributeError, requests.exceptions.HTTPError):
        pass

    # Fallback for local dev with gcloud auth login (no SA credentials).
    # --audiences scopes the token to the target service, but user
    # credentials don't support it — retry without if it fails.
    try:
        result = run_gcloud_command(
            ["auth", "print-identity-token", f"--audiences={audience}"],
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        result = run_gcloud_command(
            ["auth", "print-identity-token"],
            capture_output=True,
            check=True,
        )
    return result.stdout.strip()
