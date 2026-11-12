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

from __future__ import annotations

import logging
import subprocess
import sys
import time

import requests

from google.agents.cli._output import Console
from google.agents.cli.auth import GCLOUD_LOGIN

from .command import run_gcloud_command
from .version import PACKAGE_NAME, get_current_version

console = Console()


_NO_CREDENTIALS_MESSAGE = (
    "No Google Cloud credentials found.\n"
    "Install the Google Cloud CLI if needed (https://cloud.google.com/sdk/docs/install), "
    f"then run: `{GCLOUD_LOGIN}`\n"
    "To use a Gemini API key instead, run: `agents-cli login -i`"
)
_NO_PROJECT_MESSAGE = (
    "No Google Cloud project is set.\n"
    "Run: `gcloud config set project YOUR_PROJECT_ID` "
    "or set the GOOGLE_CLOUD_PROJECT environment variable."
)
_RELOGIN_HINT = f"Run: `{GCLOUD_LOGIN}`"
_AIPLATFORM_SERVICE = "aiplatform.googleapis.com"


def get_user_agent(context: str | None = None) -> str:
    """Returns a custom user agent string."""
    version = get_current_version()
    prefix = "ag" if context == "agent-garden" else ""
    return f"{prefix}{version}-{PACKAGE_NAME}/{prefix}{version}-{PACKAGE_NAME}"


def get_x_goog_api_client_header(context: str | None = None) -> str:
    """Build x-goog-api-client header matching SDK format."""
    user_agent = get_user_agent(context)
    python_version = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    return f"{user_agent} gl-python/{python_version} gccl/{user_agent}"


def _get_credentials_and_token() -> tuple:
    """Get credentials, project, and valid token.

    Returns:
        Tuple of (credentials, project, token)
    """
    import google.auth.exceptions
    import google.auth.transport.requests

    from google.agents.cli.auth import get_adc_credentials

    credentials, project = get_adc_credentials()

    # Refresh credentials to get valid token
    auth_req = google.auth.transport.requests.Request()
    try:
        credentials.refresh(auth_req)
    except google.auth.exceptions.RefreshError as e:
        raise Exception(_refresh_error_message(credentials, e)) from e

    return credentials, project, credentials.token


def _refresh_error_message(credentials: object, error: Exception) -> str:
    """Explain why refreshing the credentials failed and how to fix it."""
    import google.auth.compute_engine
    import google.oauth2.credentials

    if isinstance(credentials, google.auth.compute_engine.Credentials):
        # Re-logging in doesn't help here: the VM's service account itself
        # can't get a token, usually because the VM lacks the cloud-platform scope.
        return (
            f"The VM's service account could not get an access token: {error}\n"
            "Give the VM the cloud-platform access scope, or sign in as a user "
            f"with `{GCLOUD_LOGIN}`."
        )
    if isinstance(credentials, google.oauth2.credentials.Credentials):
        return (
            f"Your Google Cloud credentials could not be refreshed "
            f"(they may be expired or revoked): {error}\n{_RELOGIN_HINT}"
        )
    # Service account keys, workload identity federation and impersonation:
    # signing in again as a user doesn't fix their configuration.
    return (
        f"Your Google Cloud credentials ({type(credentials).__name__}) could not "
        f"be refreshed: {error}\n"
        "Check the credentials that GOOGLE_APPLICATION_CREDENTIALS or your "
        "Application Default Credentials file points to."
    )


def _get_account_from_credentials(credentials: object) -> str | None:
    """Try to get account email from credentials object."""
    return getattr(credentials, "service_account_email", None) or getattr(
        credentials, "_account", None
    )


def _get_account_from_gcloud() -> str | None:
    """Try to get account from gcloud config."""
    try:
        result = run_gcloud_command(
            ["config", "get-value", "account"],
            check=False,
            capture_output=True,
            timeout=10,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def _test_vertex_connection(
    project: str, token: str, context: str | None = None
) -> tuple[bool, str | None]:
    """Test the Gemini platform connection using requests.

    Returns:
        Tuple of (success, error_message)
    """
    user_agent = get_user_agent(context)
    x_goog_api_client = get_x_goog_api_client_header(context)

    try:
        response = requests.post(
            f"https://us-east1-aiplatform.googleapis.com/v1beta1/projects/{project}/locations/global/publishers/google/models/gemini-2.5-flash:countTokens",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": user_agent,
                "x-goog-api-client": x_goog_api_client,
            },
            json={"contents": [{"role": "user", "parts": [{"text": "Hi"}]}]},
            timeout=10,
        )

        if response.status_code == 200:
            return True, None
        return False, _classify_http_error(response, project)
    except requests.exceptions.RequestException as e:
        return False, f"Network error: {e}"


def _classify_http_error(response: requests.Response, project: str) -> str:
    """Map a failed countTokens response to "api_not_enabled" or a message."""
    if response.status_code == 401:
        return f"Google Cloud rejected your credentials.\n{_RELOGIN_HINT}"
    if response.status_code != 403:
        return f"Status {response.status_code}: {response.text}"

    # Proxies and firewalls can answer with HTML or a non-standard JSON body, so
    # check the shape before reading it and fall back to the raw text.
    try:
        payload = response.json()
    except ValueError:
        payload = None
    error_data = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error_data, dict):
        error_data = {}
    error_message = str(error_data.get("message") or response.text)
    details = error_data.get("details")
    # Google APIs put the machine-readable cause in google.rpc.ErrorInfo.reason.
    error_infos = [
        detail
        for detail in (details if isinstance(details, list) else [])
        if isinstance(detail, dict)
    ]
    reasons = {info.get("reason") for info in error_infos} - {None}
    # Interactive mode offers to enable aiplatform, so only report it as
    # disabled when the error names that service.
    aiplatform_disabled = any(
        info.get("reason") == "SERVICE_DISABLED"
        and isinstance(info.get("metadata"), dict)
        and info["metadata"].get("service") == _AIPLATFORM_SERVICE
        for info in error_infos
    )
    lowered = error_message.lower()
    if aiplatform_disabled or (
        not reasons
        and _AIPLATFORM_SERVICE in error_message
        and any(
            phrase in lowered
            for phrase in ("not enabled", "is disabled", "has not been used")
        )
    ):
        return "api_not_enabled"
    if "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in reasons:
        # ADC from the metadata server only gets the scopes the VM was created with.
        return (
            "Your access token lacks the cloud-platform scope. On a Compute Engine "
            "VM, give the VM the cloud-platform access scope, or sign in as a user "
            f"with `{GCLOUD_LOGIN}`."
        )
    if "BILLING_DISABLED" in reasons:
        return (
            f"Billing is not enabled for project '{project}'. "
            "Link a billing account to the project in the Google Cloud console."
        )
    return (
        f"Permission denied on project '{project}': {error_message}\n"
        "Check that the project ID is correct and that your account has the "
        "Vertex AI User role (roles/aiplatform.user)."
    )


def enable_vertex_ai_api(project_id: str, context: str | None = None) -> bool:
    """Enable the aiplatform.googleapis.com API and wait for propagation."""
    try:
        console.print("Enabling the aiplatform.googleapis.com API...")
        run_gcloud_command(
            [
                "services",
                "enable",
                "aiplatform.googleapis.com",
                "--project",
                project_id,
            ],
            check=True,
            capture_output=True,
        )
        console.print("✓ aiplatform.googleapis.com API enabled successfully")

        # Wait for API propagation
        console.print("⏳ Waiting for API availability to propagate...")
        max_wait_time = 180  # 3 minutes
        check_interval = 10  # 10 seconds
        start_time = time.time()

        # Get fresh token for retries
        _, _, token = _get_credentials_and_token()

        while time.time() - start_time < max_wait_time:
            success, _ = _test_vertex_connection(project_id, token, context)
            if success:
                console.print("✓ aiplatform.googleapis.com API is now available")
                return True
            time.sleep(check_interval)
            console.print("⏳ Still waiting for API propagation...")

        logging.warning("API propagation took longer than expected, but continuing...")
        return True

    except subprocess.CalledProcessError as e:
        console.print(
            f"Failed to enable the aiplatform.googleapis.com API: {e.stderr}",
            style="bold red",
        )
        return False


def verify_credentials_and_vertex(
    context: str | None = None,
    interactive: bool = False,
) -> dict:
    """Verify credentials and the Gemini platform connection.

    Uses google.auth + requests for lightweight verification.

    Args:
        context: Optional context for user agent (e.g., "agent-garden")
        interactive: If True and API not enabled, prompt user to enable it

    Returns:
        Dict with project and account info

    Raises:
        Exception on authentication or connection failure
    """
    import google.auth.exceptions
    from rich.prompt import Confirm

    try:
        # Get credentials and token
        credentials, project, token = _get_credentials_and_token()
        if not project:
            raise Exception(_NO_PROJECT_MESSAGE)

        # Test the Gemini platform connection
        success, error = _test_vertex_connection(project, token, context)

        # Only fetch account for interactive mode (when we need to display it)
        def get_account() -> str:
            account = _get_account_from_credentials(credentials)
            if not account:
                account = _get_account_from_gcloud() or "Unknown account"
            return account

        if success:
            # In non-interactive mode, skip account lookup since it's not displayed
            account = get_account() if interactive else "N/A"
            return {"project": project, "account": account}

        # Handle API not enabled
        if error == "api_not_enabled":
            if not interactive:
                raise Exception(
                    f"The aiplatform.googleapis.com API is not enabled in project '{project}'. "
                    f"Enable it with: gcloud services enable aiplatform.googleapis.com --project {project}"
                )
            else:
                # Interactive mode - offer to enable
                console.print(
                    f"The aiplatform.googleapis.com API is not enabled in project '{project}'.",
                    style="yellow",
                )
                if Confirm.ask(
                    "Do you want to enable the aiplatform.googleapis.com API now?",
                    default=True,
                ):
                    if enable_vertex_ai_api(project, context):
                        return {"project": project, "account": get_account()}
                    raise Exception("Failed to enable the aiplatform.googleapis.com API")
                else:
                    raise Exception(
                        "The aiplatform.googleapis.com API is not enabled and user declined to enable it"
                    )

        # Other errors
        raise Exception(f"Gemini platform connection failed: {error}")

    except google.auth.exceptions.DefaultCredentialsError as e:
        raise Exception(_NO_CREDENTIALS_MESSAGE) from e
    except google.auth.exceptions.TransportError as e:
        # Raised when refreshing the token can't reach the auth server (e.g.
        # offline); it is a network problem, not a credentials one.
        raise Exception(f"Network error connecting to Google Cloud: {e}") from e
    except google.auth.exceptions.GoogleAuthError as e:
        raise Exception(f"Google Cloud authentication failed: {e}") from e
    except requests.exceptions.RequestException as e:
        raise Exception(f"Network error connecting to Gemini platform: {e}") from e
