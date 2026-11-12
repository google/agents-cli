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

"""Reading ``google.rpc`` error details (https://google.aip.dev/193)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_ERROR_INFO_TYPE = "/google.rpc.ErrorInfo"
_HELP_TYPE = "/google.rpc.Help"


def format_error_details(body: Any) -> str:
    """The reasons, logs link and help links in a failed request's response body.

    One indented line each, each starting with a newline. Empty when the error
    carries none of them.
    """
    lines = []
    for info in _details(body, _ERROR_INFO_TYPE):
        if info.get("reason"):
            lines.append(f"Reason: {info['reason']}")
        metadata = info.get("metadata")
        if isinstance(metadata, Mapping) and metadata.get("logs_url"):
            lines.append(f"Logs: {metadata['logs_url']}")
    for help_detail in _details(body, _HELP_TYPE):
        for link in help_detail.get("links") or []:
            if isinstance(link, Mapping) and link.get("url"):
                lines.append(f"Troubleshooting: {link['url']}")
    return "".join(f"\n  {line}" for line in lines)


def _details(body: Any, type_suffix: str) -> list[Mapping[str, Any]]:
    # An APIError's details are the whole response body, whose "error" field is
    # the google.rpc.Status.
    status = body.get("error") if isinstance(body, Mapping) else None
    details = status.get("details") if isinstance(status, Mapping) else None
    if not isinstance(details, list):
        return []
    return [
        detail
        for detail in details
        if isinstance(detail, Mapping)
        and str(detail.get("@type", "")).endswith(type_suffix)
    ]
