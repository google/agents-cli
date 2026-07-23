#!/usr/bin/env python3
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

"""Utility to register an Agent Runtime to Gemini Enterprise."""

import json
import logging
import os
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import click
import requests
from packaging import version
from rich.console import Console
from rich.table import Table

from google.agents.cli._agent_runtime_a2a import build_agent_runtime_a2a_card_url
from google.agents.cli._gcp_project import (
    get_gcp_project_number,
    resolve_gcp_project,
)
from google.agents.cli._output import emit
from google.agents.cli._project import read_project_config
from google.agents.cli._runner import run_resolved
from google.agents.cli._tools import ToolNotFoundError
from google.agents.cli.auth import get_access_token, get_id_token
from google.agents.cli.scaffold.utils.gcp import (
    get_user_agent,
    get_x_goog_api_client_header,
)
from google.agents.cli.scaffold.utils.logging import display_welcome_banner

# All human-facing output (progress, tables, prompts, errors) goes to stderr so
# that stdout carries only machine-readable JSON from emit().
# highlight=False disables Rich's auto-coloring of numbers/URLs/IDs in our output.
console = Console(stderr=True, highlight=False)


def _strip_callback(
    _ctx: click.Context, _param: click.Parameter, value: str | None
) -> str | None:
    """Click callback to strip whitespace/newlines from option values."""
    return value.strip() if value else value


# SDK version that contains the fix for Gemini Enterprise session bug
# See: https://github.com/GoogleCloudPlatform/agent-starter-pack/issues/495
SDK_MIN_VERSION_FOR_GEMINI_ENTERPRISE = "1.128.0"

# Only Gemini Enterprise (intranet) apps can be connected via registration.
_GEMINI_ENTERPRISE_APP_TYPE = "APP_TYPE_INTRANET"

# SDK upgrade command constants
_SDK_UPGRADE_PACKAGE = (
    "google-cloud-aiplatform[adk,agent_engines] "
    "@ git+https://github.com/googleapis/python-aiplatform.git"
)
_SDK_UPGRADE_COMMAND = f'uv add "{_SDK_UPGRADE_PACKAGE}"'


def get_sdk_version_from_lock_file() -> tuple[str | None, bool]:
    """Get google-cloud-aiplatform version and source from uv.lock file.

    Returns:
        Tuple of (version string or None, is_from_git boolean).
        If from git, the fix is assumed to be applied regardless of version.
    """
    lock_path = Path("uv.lock")
    if not lock_path.exists():
        return None, False

    try:
        with open(lock_path, "rb") as f:
            lock_data = tomllib.load(f)

        for package in lock_data.get("package", []):
            if package.get("name") == "google-cloud-aiplatform":
                found_version = package.get("version")
                source = package.get("source")
                is_from_git = isinstance(source, dict) and "git" in source
                return found_version, is_from_git

        return None, False
    except (tomllib.TOMLDecodeError, OSError):
        return None, False


def _is_sdk_version_affected(current_version: str) -> bool:
    """Check if the SDK version is affected by the Gemini Enterprise bug."""
    return version.parse(current_version) <= version.parse(
        SDK_MIN_VERSION_FOR_GEMINI_ENTERPRISE
    )


def _print_sdk_compatibility_warning(current_version: str) -> None:
    """Print warning message about SDK compatibility issue."""
    console.print("\n" + "=" * 70)
    console.print("[yellow]⚠️  Agent Runtime SDK Compatibility Issue Detected[/yellow]")
    console.print("=" * 70)
    console.print(
        f"\nYour current google-cloud-aiplatform version ({current_version}) has a known"
    )
    console.print("issue with Agent Runtime that causes 'Session not found' errors when")
    console.print("registering to Gemini Enterprise.")
    console.print(
        "\nSee: https://github.com/GoogleCloudPlatform/agent-starter-pack/issues/495"
    )
    console.print(
        "\n[bold]The fix is available in the SDK git repository "
        "(will be in PyPI >1.128.0).[/bold]"
    )


def _run_sdk_upgrade() -> bool:
    """Execute the SDK upgrade command.

    Returns:
        True if upgrade succeeded, False otherwise.
    """
    console.print("\n[blue]Upgrading SDK from git (this may take a minute)...[/blue]")
    try:
        result = run_resolved(
            ["uv", "add", _SDK_UPGRADE_PACKAGE],
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
        )

        if result.returncode == 0:
            console.print("\n[green]✅ SDK upgraded successfully![/green]")
            console.print("\n[bold]Next steps:[/bold]")
            console.print(
                "  1. Redeploy your agent to pick up the fix: [cyan]agents-cli deploy[/cyan]"
            )
            console.print("  2. Re-run this command to register with Gemini Enterprise")
            return True

        console.print(f"\n[red]❌ Failed to upgrade SDK:[/red]\n{result.stderr}")
        console.print(f"\nYou can manually run:\n  {_SDK_UPGRADE_COMMAND}")
        return False

    except ToolNotFoundError:
        # run_resolved raises ToolNotFoundError if it cannot find the executable (e.g., 'uv')
        console.print(
            "\n[yellow]⚠️  'uv' command not found. Please run manually:[/yellow]"
        )
        console.print(f"  {_SDK_UPGRADE_COMMAND}")
        return False
    except subprocess.TimeoutExpired:
        console.print("\n[red]❌ Upgrade timed out.[/red]")
        return False


def check_and_upgrade_sdk_for_agent_runtime() -> bool:
    """Check if SDK version is compatible with Gemini Enterprise and offer to upgrade.

    For Agent Runtime deployments, there's a known issue with SDK versions <= 1.128.0
    that causes 'Session not found' errors. The fix is available in the git repo.

    Returns:
        True if SDK is compatible or user upgraded, False if user chose to abort.
    """
    try:
        current_version, is_from_git = get_sdk_version_from_lock_file()

        if not current_version:
            # No lock file or couldn't parse - skip check
            return True

        if is_from_git:
            # Installed from git - assume fix is applied
            return True

        if not _is_sdk_version_affected(current_version):
            return True  # Version is OK

        # Version is affected - warn user and offer upgrade
        _print_sdk_compatibility_warning(current_version)

        if click.confirm(
            "\nWould you like to upgrade to the fixed version from git now?",
            default=True,
        ):
            if _run_sdk_upgrade():
                return False  # User needs to redeploy and restart
            return click.confirm(
                "\nContinue anyway (may encounter errors)?", default=False
            )

        # User declined upgrade
        console.print(
            f"\nYou can manually upgrade later by running:\n  {_SDK_UPGRADE_COMMAND}"
        )
        return click.confirm("\nContinue anyway (may encounter errors)?", default=False)

    except Exception as e:
        # If we can't check the version, just continue
        console.print(f"[dim]Warning: Could not check SDK version: {e}[/dim]")
        return True


def get_discovery_engine_endpoint(location: str) -> str:
    """Get the appropriate Discovery Engine API endpoint for the given location.

    Args:
        location: The location/region (e.g., 'global', 'us', 'eu')

    Returns:
        The Discovery Engine API endpoint base URL

    Examples:
        >>> get_discovery_engine_endpoint('global')
        'https://discoveryengine.googleapis.com'
        >>> get_discovery_engine_endpoint('eu')
        'https://eu-discoveryengine.googleapis.com'
        >>> get_discovery_engine_endpoint('us')
        'https://us-discoveryengine.googleapis.com'
    """
    if location == "global":
        return "https://discoveryengine.googleapis.com"
    else:
        # Regional endpoints use the format: https://{region}-discoveryengine.googleapis.com
        return f"https://{location}-discoveryengine.googleapis.com"


def parse_agent_runtime_id(agent_runtime_id: str) -> dict[str, str] | None:
    """Parse an Agent Runtime resource name to extract components.

    Args:
        agent_runtime_id: Agent Runtime resource name
            (e.g., projects/PROJECT_NUM/locations/REGION/reasoningEngines/ENGINE_ID)

    Returns:
        Dictionary with 'project', 'location', 'engine_id' keys, or None if invalid format
    """
    parts = agent_runtime_id.split("/")
    if (
        len(parts) == 6
        and parts[0] == "projects"
        and parts[2] == "locations"
        and parts[4] == "reasoningEngines"
    ):
        return {
            "project": parts[1],
            "location": parts[3],
            "engine_id": parts[5],
        }
    return None


def parse_gemini_enterprise_app_id(app_id: str) -> dict[str, str] | None:
    """Parse Gemini Enterprise app resource name to extract components.

    Args:
        app_id: Gemini Enterprise app resource name
            (e.g., projects/{project_number}/locations/{location}/collections/{collection}/engines/{engine_id})

    Returns:
        Dictionary with 'project_number', 'location', 'collection', 'engine_id' keys, or None if invalid format
    """
    parts = app_id.split("/")
    if (
        len(parts) == 8
        and parts[0] == "projects"
        and parts[2] == "locations"
        and parts[4] == "collections"
        and parts[6] == "engines"
    ):
        return {
            "project_number": parts[1],
            "location": parts[3],
            "collection": parts[5],
            "engine_id": parts[7],
        }
    return None


def _build_api_headers(
    access_token: str,
    project_id: str,
    content_type: bool = False,
) -> dict[str, str | bytes]:
    """Build headers for Discovery Engine API requests with user-agent.

    Args:
        access_token: Google Cloud access token
        project_id: GCP project ID or number for billing
        content_type: Whether to include Content-Type header (for POST/PATCH)

    Returns:
        Headers dictionary
    """
    headers: dict[str, str | bytes] = {
        "Authorization": f"Bearer {access_token}",
        "x-goog-user-project": project_id,
        "User-Agent": get_user_agent(),
        "x-goog-api-client": get_x_goog_api_client_header(),
    }
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


def _requires_access_token(url: str) -> bool:
    """Return True if the URL is a googleapis.com endpoint.

    googleapis.com endpoints require access tokens. All other URLs
    receive audience-scoped ID tokens instead.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    return hostname.endswith(".googleapis.com")


def fetch_agent_card_from_url(url: str) -> dict | None:
    """Fetch and return an agent card from a URL, or None on failure.

    Uses an access token for googleapis.com URLs and an audience-scoped
    ID token for all other URLs.
    """
    try:
        headers = {}

        if _requires_access_token(url):
            # googleapis.com requires access tokens for API authentication
            access_token = get_access_token()
            headers["Authorization"] = f"Bearer {access_token}"
        else:
            # All other URLs get an audience-scoped ID token.
            # ID tokens are bound to the target audience, making them safe
            # even if the URL points to an untrusted endpoint.
            parsed = urlparse(url)
            if parsed.scheme != "https":
                console.print(
                    f"⚠️  URL uses {parsed.scheme}:// — credentials may be exposed in transit. "
                    "Use https:// for production endpoints.",
                    style="yellow",
                )
            audience = f"{parsed.scheme}://{parsed.netloc}"
            try:
                identity_token = get_id_token(audience)
                headers["Authorization"] = f"Bearer {identity_token}"
            except Exception as auth_err:
                console.print(
                    f"⚠️  Could not obtain ID token: {auth_err}",
                    style="yellow",
                )
                console.print(
                    "  Proceeding without authentication. "
                    "If the endpoint requires auth, the request will fail.",
                    style="yellow",
                )

        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        return response.json()
    except requests.exceptions.HTTPError as e:
        console.print(
            f"⚠️  HTTP error fetching agent card from {url}: {e}",
            style="yellow",
        )
        if e.response is not None and e.response.status_code in (401, 403):
            console.print(
                "  Authentication failed. Ensure you are logged in with 'gcloud auth application-default login'",
                style="yellow",
            )
        return None
    except Exception as e:
        console.print(
            f"⚠️  Could not fetch agent card from {url}: {e}",
            style="yellow",
        )
        return None


def construct_agent_card_url_from_metadata(
    metadata: dict,
) -> str | None:
    """Construct agent card URL from deployment metadata (Agent Runtime only).

    Args:
        metadata: Deployment metadata dictionary

    Returns:
        Agent card URL if construction succeeds, None otherwise
    """
    deployment_target = metadata.get("deployment_target")

    if deployment_target == "agent_runtime":
        # For Agent Runtime: construct URL from remote_agent_runtime_id
        remote_agent_runtime_id = metadata.get("remote_agent_runtime_id")
        if remote_agent_runtime_id and remote_agent_runtime_id != "None":
            parsed = parse_agent_runtime_id(remote_agent_runtime_id)
            if parsed:
                location = parsed["location"]
                # Prefer the agent_directory recorded at deploy time so publish
                # works from outside the project dir; fall back to local config
                # for metadata written before it was persisted.
                agent_directory = metadata.get("agent_directory")
                if not agent_directory:
                    agent_directory = read_project_config().agent_directory
                    logging.warning(
                        "deployment_metadata.json has no 'agent_directory'; using "
                        "'%s' from the local project config. If you are publishing "
                        "from outside the project directory, the A2A card URL may be "
                        "wrong — re-deploy to record agent_directory in the metadata.",
                        agent_directory,
                    )
                return build_agent_runtime_a2a_card_url(
                    location, remote_agent_runtime_id, agent_directory
                )

    return None


def prompt_for_agent_card_url_with_auto_construct(
    metadata: dict | None,
    default_url: str | None = None,
) -> str:
    """Get agent card URL with automatic construction from deployment metadata.

    Args:
        metadata: Deployment metadata dictionary (can be None)
        default_url: Default agent card URL (e.g., from CLI arg)

    Returns:
        Agent card URL
    """
    # If default URL provided, show as smart default
    if default_url:
        console.print("\nAgent card URL provided:")
        console.print(f"  [bold]{default_url}[/]")
        use_default = click.confirm(
            "Use this agent card URL?", default=True, show_default=True
        )
        if use_default:
            return default_url

    # Try to auto-construct from metadata (Agent Runtime only)
    if metadata:
        auto_url = construct_agent_card_url_from_metadata(metadata)

        if auto_url:
            # Successfully constructed from Agent Runtime metadata
            console.print(
                "\n✅ Found Agent Runtime deployment in deployment_metadata.json"
            )
            console.print(f"   Agent card URL: [bold]{auto_url}[/]")

            use_auto = click.confirm(
                "\nUse this agent card URL?", default=True, show_default=True
            )

            if use_auto:
                return auto_url

    # Fallback: manual entry
    console.print("\n[blue]" + "=" * 70 + "[/]")
    console.print("[blue]A2A AGENT CARD URL[/]")
    console.print("[blue]" + "=" * 70 + "[/]")
    console.print(
        "\nEnter your agent card URL manually"
        "\n[blue]Example: https://your-service.run.app/a2a/app/.well-known/agent-card.json[/]"
    )

    agent_card_url = click.prompt(
        "\nAgent card URL",
        type=str,
    ).strip()

    return agent_card_url


def get_agent_runtime_metadata(agent_runtime_id: str) -> tuple[str | None, str | None]:
    """Fetch display_name and description from deployed Agent Runtime.

    Args:
        agent_runtime_id: Agent Runtime resource name

    Returns:
        Tuple of (display_name, description) - either can be None if not found
    """
    parts = agent_runtime_id.split("/")
    if len(parts) < 6:
        return None, None

    project_id = parts[1]
    location = parts[3]

    try:
        import vertexai

        client = vertexai.Client(project=project_id, location=location)
        agent_runtime = client.agent_engines.get(name=agent_runtime_id)

        display_name = getattr(agent_runtime.api_resource, "display_name", None)
        description = getattr(agent_runtime.api_resource, "description", None)

        return display_name, description
    except Exception as e:
        console.print(f"Warning: Could not fetch metadata from Agent Runtime: {e}")
        return None, None


def prompt_for_agent_runtime_id(default_from_metadata: str | None) -> str:
    """Prompt user for Agent Runtime ID with optional default.

    Args:
        default_from_metadata: Default value from deployment_metadata.json if available

    Returns:
        The Agent Runtime resource name
    """
    if default_from_metadata:
        console.print("\nFound Agent Runtime ID from deployment_metadata.json:")
        console.print(f"  [bold]{default_from_metadata}[/]")
        use_default = click.confirm(
            "Use this Agent Runtime ID?", default=True, show_default=True
        )
        if use_default:
            return default_from_metadata

    console.print(
        "\nEnter your Agent Runtime resource name"
        "\n[blue]Example: projects/123456789/locations/us-east1/reasoningEngines/1234567890[/]"
        "\n(You can find this in the Agent Builder Console or deployment_metadata.json)"
    )

    while True:
        agent_runtime_id = click.prompt("Agent Runtime ID", type=str).strip()
        parsed = parse_agent_runtime_id(agent_runtime_id)
        if parsed:
            return agent_runtime_id
        else:
            console.print(
                "❌ Invalid format. Expected: projects/{project}/locations/{location}/reasoningEngines/{id}",
                style="bold red",
            )


def list_gemini_enterprise_apps(
    project_number: str,
    location: str = "global",
) -> list[dict] | None:
    """List available Gemini Enterprise apps in a project.

    Args:
        project_number: GCP project number
        location: Location (global, us, or eu)

    Returns:
        List of engine dictionaries with 'name' and 'displayName' keys, or None on error
    """
    try:
        access_token = get_access_token()
        base_endpoint = get_discovery_engine_endpoint(location)
        url = (
            f"{base_endpoint}/v1alpha/projects/{project_number}/"
            f"locations/{location}/collections/default_collection/engines"
        )
        headers = _build_api_headers(access_token, project_number)

        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        data = response.json()
        engines = data.get("engines", [])

        # Filter for Gemini Enterprise apps
        return [e for e in engines if e.get("appType") == _GEMINI_ENTERPRISE_APP_TYPE]

    except requests.exceptions.HTTPError as e:
        if (
            hasattr(e, "response")
            and e.response is not None
            and e.response.status_code == 404
        ):
            # No engines found or collection doesn't exist
            return []
        error_code = (
            e.response.status_code
            if hasattr(e, "response") and e.response is not None
            else "unknown"
        )
        console.print(
            f"⚠️  Could not list Gemini Enterprise apps: HTTP {error_code}",
            style="yellow",
        )
        return None
    except Exception as e:
        console.print(
            f"⚠️  Could not list Gemini Enterprise apps: {e}",
            style="yellow",
        )
        return None


def prompt_for_gemini_enterprise_components(
    default_project: str | None = None,
) -> str:
    """Prompt user for Gemini Enterprise resource components and construct full ID.

    Attempts to list available apps across all common locations in the project.
    Falls back to manual entry if listing fails or user chooses custom entry.

    Args:
        default_project: Default project number from Agent Runtime ID (unused, kept for compatibility)

    Returns:
        Full Gemini Enterprise app resource name
    """
    console.print("\n[blue]" + "=" * 70 + "[/]")
    console.print("[blue]GEMINI ENTERPRISE CONFIGURATION[/]")
    console.print("[blue]" + "=" * 70 + "[/]")

    # Get current project ID from auth defaults
    current_project_id = resolve_gcp_project()
    project_id = None
    project_number = None

    if current_project_id:
        console.print(f"\n✓ Current project: {current_project_id}")
        use_current = click.confirm(
            "\nUse this project for Gemini Enterprise?", default=True
        )
        if use_current:
            project_id = current_project_id
        else:
            project_id = click.prompt("Enter project ID", type=str).strip()
    else:
        console.print(
            "\nYou need to provide the Gemini Enterprise app details."
            "\nFind these in: Google Cloud Console → Gemini Enterprise → Apps"
        )
        project_id = click.prompt("Project ID", type=str).strip()

    # Convert project ID to project number
    console.print(f"[dim]Looking up project number for '{project_id}'...[/]")
    project_number = get_gcp_project_number(project_id)
    if not project_number:
        console.print(
            f"⚠️  Could not find project number for '{project_id}'",
            style="yellow",
        )
        console.print("Please enter the project number directly:")
        project_number = click.prompt("Project number", type=str).strip()
    else:
        console.print(f"✓ Project number: {project_number}")

    # Search across all common locations
    console.print(f"\n[dim]Searching for Gemini Enterprise apps in {project_id}...[/]")
    all_engines = []
    common_locations = ["global", "us", "eu"]

    for location in common_locations:
        engines = list_gemini_enterprise_apps(project_number, location)
        if engines:
            # Add location info to each engine for display
            for engine in engines:
                engine["_location"] = location
            all_engines.extend(engines)

    # Show results if any apps found
    if len(all_engines) > 0:
        console.print(f"\n✓ Found {len(all_engines)} Gemini Enterprise app(s):\n")

        # Display available apps with numbers
        for idx, engine in enumerate(all_engines, 1):
            display_name = engine.get("displayName", "N/A")
            location = engine.get("_location", "N/A")
            # Extract short ID from full name
            full_name = engine.get("name", "")
            parts = full_name.split("/")
            short_id = parts[-1] if parts else "N/A"

            console.print(f"  [{idx}] {display_name} [dim]({location})[/]")
            console.print(f"      ID: {short_id}")

        # Add option for custom entry
        console.print("\n  [0] Enter a custom Gemini Enterprise ID\n")

        # Prompt for selection
        while True:
            try:
                selection = click.prompt(
                    f"Select an app (0-{len(all_engines)})",
                    type=int,
                    default=1 if len(all_engines) == 1 else None,
                )

                if 0 <= selection <= len(all_engines):
                    break
                else:
                    console.print(
                        f"Please enter a number between 0 and {len(all_engines)}"
                    )
            except (ValueError, click.exceptions.Abort):
                console.print("Invalid input. Please enter a number.")
                raise

        # If user selected an existing app
        if selection > 0:
            selected_engine = all_engines[selection - 1]
            full_id = selected_engine.get("name")

            console.print("\n✓ Selected Gemini Enterprise App:")
            console.print(f"  [bold]{full_id}[/]")
            confirmed = click.confirm("Use this app?", default=True)

            if confirmed:
                return full_id

            # If not confirmed, restart the whole process
            console.print("\nLet's try again...")
            return prompt_for_gemini_enterprise_components(default_project)

        # If user selected custom entry (0), fall through to manual entry

    else:
        console.print(f"\n⚠️  No Gemini Enterprise apps found in project {project_number}")
        console.print("You can enter the details manually or try a different project.\n")
        retry = click.confirm("Try a different project?", default=False)
        if retry:
            return prompt_for_gemini_enterprise_components(None)

    # Manual entry flow
    console.print("\n[blue]Manual Configuration[/]")
    console.print(
        "\nEnter your Gemini Enterprise app details."
        "\nFind these in: Google Cloud Console → Gemini Enterprise → Apps"
    )

    # Get location for manual entry
    console.print("\nGemini Enterprise apps are typically in: global, us, or eu")
    location = click.prompt(
        "Location/Region",
        type=str,
        default="global",
        show_default=True,
    ).strip()

    # Get short ID
    console.print(
        "\nEnter your Gemini Enterprise ID (from the 'ID' column in the Apps table)."
        "\n[blue]Example: gemini-enterprise-123456_1234567890[/]"
    )
    ge_short_id = click.prompt("Gemini Enterprise ID", type=str).strip()

    # Construct full resource name
    full_id = f"projects/{project_number}/locations/{location}/collections/default_collection/engines/{ge_short_id}"

    console.print("\nConstructed Gemini Enterprise App ID:")
    console.print(f"  [bold]{full_id}[/]")
    confirmed = click.confirm("Is this correct?", default=True)

    if confirmed:
        return full_id

    # If not confirmed, restart
    console.print("\nLet's try again...")
    return prompt_for_gemini_enterprise_components(default_project)


def get_gemini_enterprise_console_url(
    gemini_enterprise_app_id: str, project_id: str
) -> str | None:
    """Construct Gemini Enterprise console URL.

    Args:
        gemini_enterprise_app_id: Full Gemini Enterprise app resource name
        project_id: GCP project ID (not number)

    Returns:
        Console URL string, or None if parsing fails
    """
    parsed = parse_gemini_enterprise_app_id(gemini_enterprise_app_id)
    if not parsed:
        return None

    location = parsed["location"]
    engine_id = parsed["engine_id"]

    return (
        f"https://console.cloud.google.com/gemini-enterprise/locations/{location}/"
        f"engines/{engine_id}/overview/dashboard?project={project_id}"
    )


def _match_adk_agent(agent: dict, agent_runtime_id: str) -> bool:
    """Whether ``agent`` is the ADK registration for ``agent_runtime_id``."""
    adk_def = agent.get("adkAgentDefinition") or agent.get("adk_agent_definition") or {}
    prov_re = (
        adk_def.get("provisionedReasoningEngine")
        or adk_def.get("provisioned_reasoning_engine")
        or {}
    )
    re_name = prov_re.get("reasoningEngine") or prov_re.get("reasoning_engine") or ""
    return re_name == agent_runtime_id


def _match_a2a_agent(agent: dict, card_url: str | None) -> bool:
    """Whether ``agent``'s stored card has the same ``url`` as ``card_url``.

    Compares the card's ``url`` (the RPC endpoint, e.g. ``.../a2a/<dir>``), not
    the ``.well-known/agent-card.json`` fetch URL: GE stores the card body, so
    matching on the fetch URL never hits and re-publish would duplicate.
    """
    if not card_url:
        return False
    a2a_def = agent.get("a2aAgentDefinition") or agent.get("a2a_agent_definition") or {}
    raw_card = a2a_def.get("jsonAgentCard") or a2a_def.get("json_agent_card") or "{}"
    try:
        return json.loads(raw_card).get("url") == card_url
    except json.JSONDecodeError:
        return False


def _find_existing_agent(
    *,
    list_url: str,
    headers: dict[str, str | bytes],
    match: Callable[[dict], bool],
) -> dict | None:
    """Return the first registered agent satisfying ``match``, scanning all pages."""
    params = {"pageSize": 100}
    while True:
        resp = requests.get(list_url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        existing = next((a for a in data.get("agents", []) if match(a)), None)
        if existing or not data.get("nextPageToken"):
            return existing
        params["pageToken"] = data["nextPageToken"]


def _upsert_agent(
    *,
    list_url: str,
    base_endpoint: str,
    headers: dict[str, str | bytes],
    payload: dict,
    match: Callable[[dict], bool],
) -> tuple[dict, str]:
    """Create the registration, or PATCH the existing match.

    The agents.create endpoint assigns a server-side ID and allows duplicates, so
    we look up an existing registration first (scanning all pages) and update it in
    place when found. The lookup runs before the write block so a list failure
    fails closed (propagates) rather than falling through to create a duplicate.

    Returns:
        Tuple of (API response dict, action) where action is "created" or "updated".
    """
    existing_agent = _find_existing_agent(list_url=list_url, headers=headers, match=match)

    try:
        if existing_agent:
            update_url = f"{base_endpoint}/v1alpha/{existing_agent['name']}"
            console.print(f"  Updating existing agent: {existing_agent['name']}")
            response = requests.patch(
                update_url, headers=headers, json=payload, timeout=30
            )
            action = "updated"
        else:
            console.print("  No matching agent found; creating a new registration.")
            response = requests.post(list_url, headers=headers, json=payload, timeout=30)
            action = "created"
        response.raise_for_status()
    except requests.exceptions.HTTPError as err:
        # Fold the API response body into the error so the ClickException raised by
        # the caller surfaces it; the caller logs it once (avoids log-and-throw).
        body = err.response.text if err.response is not None else ""
        raise requests.exceptions.HTTPError(f"{err}\n   Response: {body}") from err

    result = response.json()
    console.print(f"\n✅ Successfully {action} agent registration!")
    console.print(f"   Agent Name:\n   {result.get('name', 'N/A')}")
    return result, action


def register_a2a_agent(
    *,
    agent_card: dict,
    agent_card_url: str,
    gemini_enterprise_app_id: str,
    display_name: str,
    description: str,
    project_id: str | None = None,
    authorization_id: str | None = None,
) -> tuple[dict, str]:
    """Register an A2A agent to Gemini Enterprise, updating in place if it exists.

    Looks up an existing registration for the same agent card URL and PATCHes it;
    otherwise creates a new one.

    Args:
        agent_card: Agent card dictionary fetched from the agent
        agent_card_url: URL where the agent card was fetched from
        gemini_enterprise_app_id: Full Gemini Enterprise app resource name
        display_name: Display name for the agent in Gemini Enterprise
        description: Description of the agent
        project_id: Optional GCP project ID for billing
        authorization_id: Optional OAuth authorization ID

    Returns:
        Tuple of (API response dict, action) where action is "created" or "updated".

    Raises:
        requests.HTTPError: If the API request fails
        ValueError: If gemini_enterprise_app_id format is invalid
    """
    parsed = parse_gemini_enterprise_app_id(gemini_enterprise_app_id)
    if not parsed:
        raise ValueError(
            f"Invalid GEMINI_ENTERPRISE_APP_ID format. Expected: "
            f"projects/{{project_number}}/locations/{{location}}/collections/{{collection}}/engines/{{engine_id}}, "
            f"got: {gemini_enterprise_app_id}"
        )

    project_number = parsed["project_number"]
    as_location = parsed["location"]
    collection = parsed["collection"]
    engine_id = parsed["engine_id"]

    # Use provided project ID or fallback to project number from GE app
    if not project_id:
        project_id = project_number

    access_token = get_access_token()
    base_endpoint = get_discovery_engine_endpoint(as_location)
    url = (
        f"{base_endpoint}/v1alpha/projects/{project_number}/"
        f"locations/{as_location}/collections/{collection}/engines/{engine_id}/"
        "assistants/default_assistant/agents"
    )
    headers = _build_api_headers(access_token, project_id, content_type=True)

    # Build payload with A2A agent definition
    payload = {
        "displayName": display_name,
        "description": description,
        "icon": {
            "uri": "https://fonts.gstatic.com/s/i/short-term/release/googlesymbols/smart_toy/default/24px.svg"
        },
        "a2aAgentDefinition": {"jsonAgentCard": json.dumps(agent_card)},
    }

    # Add authorization config if provided
    if authorization_id:
        payload["authorizationConfig"] = {"agentAuthorization": authorization_id}

    console.print("\n[blue]Registering A2A agent to Gemini Enterprise...[/]")
    console.print(f"  Agent Card URL: {agent_card_url}")
    console.print(f"  Gemini Enterprise App: {gemini_enterprise_app_id}")
    console.print(f"  Display Name: {display_name}")

    return _upsert_agent(
        list_url=url,
        base_endpoint=base_endpoint,
        headers=headers,
        payload=payload,
        match=lambda agent: _match_a2a_agent(agent, agent_card.get("url")),
    )


def register_agent(
    *,
    agent_runtime_id: str,
    gemini_enterprise_app_id: str,
    display_name: str,
    description: str,
    tool_description: str,
    project_id: str | None = None,
    authorization_id: str | None = None,
) -> tuple[dict, str]:
    """Register an agent engine to Gemini Enterprise, updating in place if it exists.

    Looks up an existing registration for the same reasoning engine and PATCHes it;
    otherwise creates a new one.

    Args:
        agent_runtime_id: Agent engine resource name (e.g., projects/.../reasoningEngines/...)
        gemini_enterprise_app_id: Full Gemini Enterprise app resource name
            (e.g., projects/{project_number}/locations/{location}/collections/{collection}/engines/{engine_id})
        display_name: Display name for the agent in Gemini Enterprise
        description: Description of the agent
        tool_description: Description of what the tool does
        project_id: Optional GCP project ID for billing (extracted from agent_runtime_id if not provided)
        authorization_id: Optional OAuth authorization ID
            (e.g., projects/{project_number}/locations/global/authorizations/{auth_id})

    Returns:
        Tuple of (API response dict, action) where action is "created" or "updated".

    Raises:
        requests.HTTPError: If the API request fails
        ValueError: If gemini_enterprise_app_id format is invalid
    """
    parsed = parse_gemini_enterprise_app_id(gemini_enterprise_app_id)
    if not parsed:
        raise ValueError(
            f"Invalid GEMINI_ENTERPRISE_APP_ID format. Expected: "
            f"projects/{{project_number}}/locations/{{location}}/collections/{{collection}}/engines/{{engine_id}}, "
            f"got: {gemini_enterprise_app_id}"
        )

    project_number = parsed["project_number"]
    as_location = parsed["location"]
    collection = parsed["collection"]
    engine_id = parsed["engine_id"]

    # Use project from agent engine if not explicitly provided (for billing header)
    if not project_id:
        parsed_agent = parse_agent_runtime_id(agent_runtime_id)
        if parsed_agent:
            project_id = parsed_agent["project"]
        else:
            project_id = project_number

    # Get access token
    access_token = get_access_token()

    # Build API endpoint with regional support
    base_endpoint = get_discovery_engine_endpoint(as_location)
    url = (
        f"{base_endpoint}/v1alpha/projects/{project_number}/"
        f"locations/{as_location}/collections/{collection}/engines/{engine_id}/"
        "assistants/default_assistant/agents"
    )

    # Request headers
    headers = _build_api_headers(access_token, project_id, content_type=True)

    # Request body
    payload: dict = {
        "displayName": display_name,
        "description": description,
        "icon": {
            "uri": "https://fonts.gstatic.com/s/i/short-term/release/googlesymbols/smart_toy/default/24px.svg"
        },
        "adk_agent_definition": {
            "tool_settings": {"tool_description": tool_description},
            "provisioned_reasoning_engine": {"reasoning_engine": agent_runtime_id},
        },
    }

    # Add OAuth authorization if provided (at top level, not inside adk_agent_definition)
    if authorization_id:
        payload["authorization_config"] = {"tool_authorizations": [authorization_id]}

    console.print("\n[blue]Registering agent to Gemini Enterprise...[/]")
    console.print(f"  Agent Runtime: {agent_runtime_id}")
    console.print(f"  Gemini Enterprise App: {gemini_enterprise_app_id}")
    console.print(f"  Display Name: {display_name}")

    return _upsert_agent(
        list_url=url,
        base_endpoint=base_endpoint,
        headers=headers,
        payload=payload,
        match=lambda agent: _match_adk_agent(agent, agent_runtime_id),
    )


def _list_gemini_enterprise_apps(project_id: str | None, interactive: bool) -> None:
    """List Gemini Enterprise apps across all locations for the given project.

    Renders a rich table in interactive mode; emits a JSON ``{"apps": [...]}``
    line in non-interactive mode for LLM/CI consumers.
    """
    resolved_project_id = resolve_gcp_project(project_id)
    if not resolved_project_id:
        raise click.ClickException(
            "Could not determine GCP project.\n"
            "  Pass --project-id or set a default project with:\n"
            "    gcloud config set project PROJECT_ID"
        )

    pn = get_gcp_project_number(resolved_project_id)
    if not pn:
        raise click.ClickException(
            f"Could not resolve project number for '{resolved_project_id}'."
        )

    if interactive:
        console.print(
            f"[dim]Searching for Gemini Enterprise apps in {resolved_project_id}...[/]"
        )

    locations = ["global", "us", "eu"]
    all_engines: list[tuple[str, dict]] = []
    for loc in locations:
        engines = list_gemini_enterprise_apps(pn, loc)
        if engines:
            for engine in engines:
                all_engines.append((loc, engine))

    if not interactive:
        emit(
            {
                "apps": [
                    {
                        "display_name": engine.get("displayName"),
                        "location": loc,
                        "name": engine.get("name"),
                    }
                    for loc, engine in all_engines
                ]
            }
        )
        return

    if not all_engines:
        console.print(
            f"\nNo Gemini Enterprise apps found in project {resolved_project_id}."
        )
        return

    table = Table(title=f"Gemini Enterprise Apps — {resolved_project_id}")
    table.add_column("Display Name", style="bold")
    table.add_column("Location")
    table.add_column("Resource Name", style="dim")

    for loc, engine in all_engines:
        table.add_row(
            engine.get("displayName", "—"),
            loc,
            engine.get("name", "—"),
        )

    console.print()
    console.print(table)


def default_registration_type(
    *, deployment_target: str | None, is_a2a: bool, has_agent_card_url: bool
) -> str:
    """Pick the Gemini Enterprise registration path.

    An explicit agent card URL forces A2A. Agent Runtime defaults to native ADK
    invocation (``:streamQuery``); Cloud Run / GKE have no reasoning engine, so
    they register over A2A.
    """
    if has_agent_card_url:
        return "a2a"
    if deployment_target == "agent_runtime":
        return "adk"
    if is_a2a:
        return "a2a"
    return "adk"


def _finalize_registration(
    *,
    result: dict,
    action: str,
    registration_type: str,
    gemini_enterprise_app_id: str,
    project_id: str | None,
    interactive: bool,
) -> None:
    """Show the console link and, in non-interactive mode, emit a JSON result."""
    console_url = None
    console_project_id = resolve_gcp_project(project_id)
    if console_project_id:
        console_url = get_gemini_enterprise_console_url(
            gemini_enterprise_app_id, console_project_id
        )
        if console_url:
            console.print(
                f"\n🔗 View in Console:\n   [link={console_url}]{console_url}[/link]"
            )

    if not interactive:
        emit(
            {
                "status": "ok",
                "action": action,
                "registration_type": registration_type,
                "agent_name": result.get("name"),
                "gemini_enterprise_app_id": gemini_enterprise_app_id,
                "console_url": console_url,
            }
        )


@click.command("gemini-enterprise")
@click.option(
    "--agent-runtime-id",
    envvar="AGENT_RUNTIME_ID",
    callback=_strip_callback,
    help="Agent Runtime resource name (e.g., projects/.../reasoningEngines/...). "
    "If not provided, reads from deployment_metadata.json.",
)
@click.option(
    "--metadata-file",
    default="deployment_metadata.json",
    help="Path to deployment metadata file (default: deployment_metadata.json).",
)
@click.option(
    "--gemini-enterprise-app-id",
    callback=_strip_callback,
    help="Gemini Enterprise app full resource name "
    "(e.g., projects/{project_number}/locations/{location}/collections/{collection}/engines/{engine_id}). "
    "If not provided, the command will prompt you interactively. "
    "Can also be set via ID or GEMINI_ENTERPRISE_APP_ID env var.",
)
@click.option(
    "--display-name",
    envvar="GEMINI_DISPLAY_NAME",
    callback=_strip_callback,
    help="Display name for the agent.",
)
@click.option(
    "--description",
    envvar="GEMINI_DESCRIPTION",
    callback=_strip_callback,
    help="Description of the agent.",
)
@click.option(
    "--tool-description",
    envvar="GEMINI_TOOL_DESCRIPTION",
    callback=_strip_callback,
    help="Description of what the tool does.",
)
@click.option(
    "--project-id",
    "--project",
    envvar="GOOGLE_CLOUD_PROJECT",
    callback=_strip_callback,
    help="GCP project ID (extracted from agent-runtime-id if not provided).",
)
@click.option(
    "--authorization-id",
    envvar="GEMINI_AUTHORIZATION_ID",
    callback=_strip_callback,
    help="OAuth authorization resource name "
    "(e.g., projects/{project_number}/locations/global/authorizations/{auth_id}).",
)
@click.option(
    "--agent-card-url",
    envvar="AGENT_CARD_URL",
    callback=_strip_callback,
    help="URL to fetch the agent card for A2A agents "
    "(e.g., https://your-service.run.app/a2a/app/.well-known/agent-card.json). "
    "If provided, registers as an A2A agent instead of ADK agent.",
)
@click.option(
    "--deployment-target",
    envvar="DEPLOYMENT_TARGET",
    type=click.Choice(["agent_runtime", "cloud_run", "gke"], case_sensitive=False),
    help="Deployment target (agent_runtime, cloud_run, or gke).",
)
@click.option(
    "--project-number",
    envvar="PROJECT_NUMBER",
    callback=_strip_callback,
    help="GCP project number. Used as default when prompting for Gemini Enterprise configuration.",
)
@click.option(
    "--registration-type",
    envvar="REGISTRATION_TYPE",
    type=click.Choice(["a2a", "adk"], case_sensitive=False),
    help="Registration type: 'a2a' for A2A agents (requires agent card URL), "
    "'adk' for ADK agents on Agent Runtime (requires agent engine ID). "
    "If not provided, auto-detected from metadata or prompted.",
)
@click.option(
    "--interactive",
    "-i",
    is_flag=True,
    default=False,
    help="Enable interactive prompts for human use.",
)
@click.option(
    "--list",
    "list_apps",
    is_flag=True,
    default=False,
    help="List Gemini Enterprise apps in the current project and exit.",
)
def register_gemini_enterprise(
    *,
    agent_runtime_id: str | None,
    metadata_file: str,
    gemini_enterprise_app_id: str | None,
    display_name: str | None,
    description: str | None,
    tool_description: str | None,
    project_id: str | None,
    authorization_id: str | None,
    agent_card_url: str | None,
    deployment_target: str | None,
    project_number: str | None,
    registration_type: str | None,
    interactive: bool,
    list_apps: bool,
) -> None:
    r"""Register a deployed agent with Gemini Enterprise.

    All required parameters must be supplied as flags in programmatic mode.
    Pass --interactive to be guided through missing values interactively.

    \b
    Use --list to list Gemini Enterprise apps in the current project:
      agents-cli publish gemini-enterprise --list
    """
    if list_apps:
        _list_gemini_enterprise_apps(project_id, interactive)
        return

    # The banner is human decoration printed to stdout; in non-interactive mode
    # stdout must stay pure JSON for machine consumers, so only show it interactively.
    if interactive:
        display_welcome_banner(register_mode=True)

    # Read metadata file once to determine agent type and deployment target
    metadata = None
    try:
        metadata_path = Path(metadata_file)
        if metadata_path.exists():
            with open(metadata_path, encoding="utf-8") as f:
                metadata = json.load(f)
    except (json.JSONDecodeError, KeyError, FileNotFoundError):
        pass

    provided_agent_card_url = agent_card_url or (
        os.getenv("AGENT_CARD_URL", "").strip() or None
    )

    # Determine registration type (a2a vs adk)
    resolved_registration_type = registration_type
    if not resolved_registration_type:
        if provided_agent_card_url or metadata:
            resolved_registration_type = default_registration_type(
                deployment_target=(
                    deployment_target
                    or (metadata.get("deployment_target") if metadata else None)
                ),
                is_a2a=metadata.get("is_a2a", False) if metadata else False,
                has_agent_card_url=bool(provided_agent_card_url),
            )
        elif interactive:
            # No metadata, no agent card URL - prompt user to choose
            console.print("[blue]No deployment metadata found.[/]")
            console.print(
                "\nSelect registration type:\n"
                "  [1] A2A - Agent-to-Agent protocol (requires agent card URL)\n"
                "  [2] ADK - Agent Development Kit on Agent Runtime (requires agent engine ID)\n"
            )
            choice = click.prompt(
                "Registration type (1 or 2)",
                type=click.Choice(["1", "2"]),
                default="1",
            )
            resolved_registration_type = "a2a" if choice == "1" else "adk"
        else:
            raise click.UsageError(
                "--registration-type is required in programmatic mode. "
                "Pass --interactive for interactive mode or --registration-type to specify."
            )

    # Log the registration type
    if resolved_registration_type == "a2a":
        console.print("[blue]→ A2A registration mode[/]")
    else:
        console.print("[blue]→ ADK registration mode[/]")

    # Set up agent_card_url for A2A mode
    if resolved_registration_type == "a2a":
        if interactive:
            if not provided_agent_card_url:
                agent_card_url = prompt_for_agent_card_url_with_auto_construct(
                    metadata, None
                )
            else:
                agent_card_url = prompt_for_agent_card_url_with_auto_construct(
                    metadata, provided_agent_card_url
                )
        else:
            # Non-interactive (--yes or strict programmatic): use provided URL or auto-construct
            if provided_agent_card_url:
                agent_card_url = provided_agent_card_url
                console.print(f"Using agent card URL: {agent_card_url}")
            elif metadata:
                # Try to auto-construct from metadata
                auto_url = construct_agent_card_url_from_metadata(metadata)
                if auto_url:
                    agent_card_url = auto_url
                    console.print(
                        f"Using auto-constructed agent card URL: {agent_card_url}"
                    )
                else:
                    raise click.ClickException(
                        "Agent card URL is required for A2A registration. "
                        "Set the AGENT_CARD_URL environment variable or pass --interactive for interactive mode."
                    )
            else:
                raise click.ClickException(
                    "Agent card URL is required for A2A registration. "
                    "Set the AGENT_CARD_URL environment variable or pass --interactive for interactive mode."
                )
        if not deployment_target:
            deployment_target = (
                metadata.get("deployment_target", "cloud_run")
                if metadata
                else "cloud_run"
            )

        if deployment_target == "agent_runtime":
            logging.warning(
                "A2A registration on Agent Runtime is not recommended for "
                "Gemini Enterprise; prefer ADK registration "
                "(--registration-type adk), which Gemini Enterprise invokes "
                "natively via :streamQuery. Proceeding because A2A was requested "
                "explicitly."
            )
    else:
        # ADK mode - no agent_card_url needed
        agent_card_url = None

    # A2A registration
    if agent_card_url:
        agent_card = fetch_agent_card_from_url(agent_card_url)
        if not agent_card:
            raise click.ClickException(
                f"Failed to fetch agent card from {agent_card_url}. "
                "Please verify the URL is correct and the agent is running."
            )

        console.print(f"✓ Fetched agent card: {agent_card.get('name', 'Unknown')}")

        resolved_gemini_enterprise_app_id = (
            gemini_enterprise_app_id
            or (os.getenv("ID", "").strip() or None)
            or (os.getenv("GEMINI_ENTERPRISE_APP_ID", "").strip() or None)
        )

        if not resolved_gemini_enterprise_app_id:
            if interactive:
                default_project = project_number
                if (
                    not default_project
                    and metadata
                    and metadata.get("deployment_target") == "agent_runtime"
                ):
                    remote_agent_runtime_id = metadata.get("remote_agent_runtime_id")
                    if remote_agent_runtime_id:
                        parsed = parse_agent_runtime_id(remote_agent_runtime_id)
                        if parsed:
                            default_project = parsed["project"]

                resolved_gemini_enterprise_app_id = (
                    prompt_for_gemini_enterprise_components(
                        default_project=default_project
                    )
                )
            else:
                raise click.ClickException(
                    "Gemini Enterprise App ID is required. "
                    "Set the ID or GEMINI_ENTERPRISE_APP_ID environment variable, "
                    "or pass --interactive for interactive mode."
                )

        # Get display name and description with smart defaults from agent card
        if not display_name:
            default_display_name = agent_card.get("name") or "My A2A Agent"
            if interactive:
                resolved_display_name = click.prompt(
                    "Display name", default=default_display_name
                )
            else:
                resolved_display_name = default_display_name
        else:
            resolved_display_name = display_name

        if not description:
            default_description = agent_card.get("description") or "AI Agent"
            if interactive:
                resolved_description = click.prompt(
                    "Description", default=default_description
                )
            else:
                resolved_description = default_description
        else:
            resolved_description = description

        # Register as A2A agent
        try:
            result, action = register_a2a_agent(
                agent_card=agent_card,
                agent_card_url=agent_card_url,
                gemini_enterprise_app_id=resolved_gemini_enterprise_app_id,
                display_name=resolved_display_name,
                description=resolved_description,
                project_id=project_id,
                authorization_id=authorization_id,
            )
            _finalize_registration(
                result=result,
                action=action,
                registration_type="a2a",
                gemini_enterprise_app_id=resolved_gemini_enterprise_app_id,
                project_id=project_id,
                interactive=interactive,
            )
        except Exception as e:
            raise click.ClickException(f"Error during A2A registration: {e}") from e

    # ADK
    else:
        # Check SDK version compatibility for Agent Runtime deployments
        # See: https://github.com/GoogleCloudPlatform/agent-starter-pack/issues/495
        # Only show interactive upgrade prompts in interactive mode
        if interactive and not check_and_upgrade_sdk_for_agent_runtime():
            console.print("\n[yellow]Registration aborted.[/yellow]")
            return

        # Step 1: Get Agent Runtime ID
        resolved_agent_runtime_id = agent_runtime_id

        if not resolved_agent_runtime_id:
            env_id = os.getenv("AGENT_ENGINE_ID", "").strip() or None
            if env_id:
                resolved_agent_runtime_id = env_id
            else:
                metadata_id = (
                    metadata.get("remote_agent_runtime_id") if metadata else None
                )
                if metadata_id:
                    # Use metadata value directly without prompting in non-interactive mode
                    resolved_agent_runtime_id = metadata_id
                    console.print(f"Using Agent Runtime ID from metadata: {metadata_id}")
                elif interactive:
                    resolved_agent_runtime_id = prompt_for_agent_runtime_id(None)
                else:
                    raise click.ClickException(
                        "Agent Runtime ID is required. "
                        "Set the AGENT_ENGINE_ID environment variable, use --agent-engine-id, "
                        "or pass --interactive for interactive mode."
                    )
        # Validate and parse Agent Runtime ID
        parsed_ae = parse_agent_runtime_id(resolved_agent_runtime_id)
        if not parsed_ae:
            raise click.ClickException(
                f"Invalid Agent Runtime ID format: {resolved_agent_runtime_id}\n"
                "Expected: projects/{{project}}/locations/{{location}}/reasoningEngines/{{id}}"
            )

        # Step 2: Get Gemini Enterprise App ID
        resolved_gemini_enterprise_app_id = (
            gemini_enterprise_app_id
            or (os.getenv("ID", "").strip() or None)
            or (os.getenv("GEMINI_ENTERPRISE_APP_ID", "").strip() or None)
        )

        if not resolved_gemini_enterprise_app_id:
            if interactive:
                resolved_gemini_enterprise_app_id = (
                    prompt_for_gemini_enterprise_components(
                        default_project=parsed_ae["project"]
                    )
                )
            else:
                raise click.ClickException(
                    "Gemini Enterprise App ID is required. "
                    "Set the ID or GEMINI_ENTERPRISE_APP_ID environment variable, "
                    "or pass --interactive for interactive mode."
                )

        # Step 3: Get display name and description
        auto_display_name, auto_description = get_agent_runtime_metadata(
            resolved_agent_runtime_id
        )

        resolved_display_name = display_name or auto_display_name or "My Agent"
        resolved_description = description or auto_description or "AI Agent"
        resolved_tool_description = tool_description or resolved_description

        # Step 4: Register as ADK agent
        try:
            result, action = register_agent(
                agent_runtime_id=resolved_agent_runtime_id,
                gemini_enterprise_app_id=resolved_gemini_enterprise_app_id,
                display_name=resolved_display_name,
                description=resolved_description,
                tool_description=resolved_tool_description,
                project_id=project_id,
                authorization_id=authorization_id,
            )
            _finalize_registration(
                result=result,
                action=action,
                registration_type="adk",
                gemini_enterprise_app_id=resolved_gemini_enterprise_app_id,
                project_id=project_id,
                interactive=interactive,
            )
        except Exception as e:
            raise click.ClickException(f"Error during ADK registration: {e}") from e
