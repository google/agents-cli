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

"""Consent gate for running third-party extension code."""

from __future__ import annotations

import click

from google.agents.cli.extension._pypi import PypiError, index_label
from google.agents.cli.extension._refs import ExtensionRef


def confirm_trust(ref: ExtensionRef, *, auto_approve: bool) -> bool:
    """Return True if it's OK to install/run this extension's code.

    Verified extensions are trusted. Everything else prompts unless
    `auto_approve`.
    """
    if ref.tier == "verified" or auto_approve:
        return True
    click.echo()
    click.secho(
        f"  Extension source {ref.raw!r}{_origin(ref)} is not verified. It can run "
        "arbitrary code on your machine when its commands are invoked.",
        fg="yellow",
    )
    return click.confirm("  Install and trust this extension?", default=False)


def _origin(ref: ExtensionRef) -> str:
    """Where the code comes from, beyond what ``ref.raw`` already says."""
    if ref.kind != "pypi":
        return f" at {ref.ref}" if ref.ref else ""
    try:
        return f" from {index_label()}"
    except PypiError:
        # Resolving it reports the problem; the prompt can do without it.
        return ""
