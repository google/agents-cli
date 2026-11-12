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

"""Fix hints for Google Cloud auth and Gemini API errors.

Errors from a local agent server reach the CLI as text (an SSE error frame),
not as exceptions, so the hints match on the error text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from google.agents.cli._defaults import GCP_PROJECT_PLACEHOLDER
from google.agents.cli.auth import GCLOUD_LOGIN


@dataclass(frozen=True)
class _Hint:
    markers: tuple[str, ...]
    text: str
    # When set, the message must also mention one of these. Keeps a generic
    # status such as PERMISSION_DENIED from matching a tool's error.
    context: tuple[str, ...] = ()


# google-genai ignores an API key while the Gemini platform settings are set.
_USE_GEMINI_API = (
    "or switch to the Gemini API: comment out the Gemini platform lines in .env "
    "and set GEMINI_API_KEY (or GOOGLE_API_KEY)."
)
_GEMINI_PLATFORM_API = "aiplatform.googleapis.com"
_GEMINI_API = "generativelanguage.googleapis.com"

# First match wins, so the specific reasons come before the generic statuses
# that carry them (a disabled API or billing is also a 403 PERMISSION_DENIED).
_HINTS = (
    _Hint(
        (GCP_PROJECT_PLACEHOLDER,),
        f"GOOGLE_CLOUD_PROJECT is still the placeholder '{GCP_PROJECT_PLACEHOLDER}'. "
        "Set it to your Google Cloud project ID in .env.",
    ),
    _Hint(
        ("No service account scopes specified",),
        "The VM's service account has no cloud-platform access scope. Run "
        f"`{GCLOUD_LOGIN}`, give the VM the cloud-platform scope, {_USE_GEMINI_API}",
    ),
    _Hint(
        (
            # google-auth for Python and Java, then Go, then Node.js.
            "default credentials were not found",
            "could not find default credentials",
            "Could not load the default credentials",
        ),
        f"No Application Default Credentials found. Run `{GCLOUD_LOGIN}`, "
        + _USE_GEMINI_API,
    ),
    _Hint(
        # Java, then Node.js; Python raises a bare OSError.
        ("Error reading credential file", "Unable to read the credential file"),
        "The credentials file can't be read. Check that it exists and that you "
        "can read it, or point GOOGLE_APPLICATION_CREDENTIALS at another key file.",
    ),
    _Hint(
        ("invalid_grant", "Reauthentication is needed", "invalid_rapt"),
        f"Your Application Default Credentials have expired. Run `{GCLOUD_LOGIN}`.",
    ),
    _Hint(
        ("API_KEY_INVALID", "API key not valid", "API key expired"),
        "The API key was rejected. Check GEMINI_API_KEY (or GOOGLE_API_KEY) in "
        "your shell and in .env.",
    ),
    _Hint(
        ("SERVICE_DISABLED", "has not been used in project"),
        "An API the agent needs is disabled in your project. Enable it with "
        "`gcloud services enable {service}`.",
    ),
    _Hint(
        ("BILLING_DISABLED",),
        "Billing is disabled for your Google Cloud project. Link a billing "
        "account to it in the Cloud console.",
    ),
    _Hint(
        ("RESOURCE_EXHAUSTED",),
        "A quota was exceeded. Wait and retry, or request a quota increase.",
    ),
    _Hint(
        ("PERMISSION_DENIED",),
        "Your credentials can't call the model in this project. Check "
        "GOOGLE_CLOUD_PROJECT, and that your account has the roles/aiplatform.user "
        "role on it.",
        context=(_GEMINI_PLATFORM_API,),
    ),
    _Hint(
        ("PERMISSION_DENIED",),
        "The API key can't call the Gemini API. Check GEMINI_API_KEY (or "
        "GOOGLE_API_KEY) in your shell and in .env.",
        context=(_GEMINI_API,),
    ),
    _Hint(
        ("NOT_FOUND",),
        "The model was not found. Check the model name and, on the Gemini "
        "platform, that the model is available in GOOGLE_CLOUD_LOCATION.",
        context=("models/",),
    ),
)

# The ErrorInfo detail names the disabled service.
_SERVICE_FIELD_RE = re.compile(
    r"""['"]service['"]:\s*['"]([a-z0-9.-]+\.googleapis\.com)['"]"""
)
# type.googleapis.com prefixes error detail type names; it isn't a service.
_SERVICE_RE = re.compile(r"\b(?!type\.googleapis\.com)[a-z0-9-]+\.googleapis\.com\b")


def with_auth_hint(message: str) -> str:
    """Return ``message`` followed by a fix hint when it names a known auth error."""
    for hint in _HINTS:
        if not any(marker in message for marker in hint.markers):
            continue
        if hint.context and not any(context in message for context in hint.context):
            continue
        return f"{message}\n  {hint.text.format(service=_service_name(message))}"
    return message


def _service_name(message: str) -> str:
    field = _SERVICE_FIELD_RE.search(message)
    if field:
        return field.group(1)
    service = _SERVICE_RE.search(message)
    return service.group(0) if service else "<SERVICE>"
