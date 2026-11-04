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

"""GCP project resolution and lookup utilities."""

import json
import logging
import os
from dataclasses import dataclass

import click

from google.agents.cli._runner import run


def resolve_gcp_project(
    override_project: str | None = None, *, required: bool = False
) -> str:
    """Resolves the GCP project ID to use.

    The project ID is resolved in the following order of precedence:

    1.  The ``override_project`` argument if provided.
        It's expected this would come from a --project command line argument.
    2.  The ``GOOGLE_CLOUD_PROJECT`` environment variable.
    3.  Application Default Credentials via :func:`google.auth.default`,
        which itself checks (in order):

        a.  ``GOOGLE_APPLICATION_CREDENTIALS`` service account JSON file.
        b.  The gcloud SDK ADC file
            (``gcloud auth application-default login``); when this file
            exists but lacks a project, the gcloud SDK falls back to
            ``gcloud config get-value project``.
        c.  GAE / GCE / Cloud Run metadata service.

    Returns:
        The resolved GCP project ID, or an empty string if no project is found.
    """
    if override_project:
        return override_project
    env_project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if env_project:
        return env_project
    # Local import: avoids a circular import at module load time
    # (auth.py imports from _gcp_project transitively in some paths).
    from google.agents.cli.auth import _get_adc_project

    project = _get_adc_project() or ""
    if required and not project:
        raise click.ClickException(
            "Could not determine GCP project. Set one with:\n"
            "  * pass --project <PROJECT_ID>\n"
            "  * export GOOGLE_CLOUD_PROJECT=<PROJECT_ID>\n"
            "  * gcloud config set project <PROJECT_ID>"
        )
    return project


def get_gcp_project_number(project_id: str) -> str | None:
    """Get numeric GCP project number for a project ID or project number.

    Args:
        project_id: GCP project ID or project number (e.g., 'my-project' or '123456789').

    Returns:
        Project number string, or None if lookup fails.
    """
    res = run(
        [
            "gcloud",
            "projects",
            "describe",
            project_id,
            "--format=value(projectNumber)",
        ],
        check=False,
        capture=True,
    )
    if res.returncode == 0:
        return res.stdout.strip() or None

    # Maybe it's already a project number, return as-is
    if project_id.isdigit():
        return project_id
    return None


@dataclass(frozen=True)
class ProjectAncestor:
    """A resource a project sits under, as `gcloud projects get-ancestors` lists it."""

    id: str
    # "project", "folder" or "organization".
    type: str


def get_gcp_project_ancestors(project_id: str) -> list[ProjectAncestor] | None:
    """Get the resource hierarchy a project sits in.

    Args:
        project_id: GCP project ID or project number.

    Returns:
        The ancestry (possibly empty), or None if it can't be read.
    """
    res = run(
        ["gcloud", "projects", "get-ancestors", project_id, "--format=json"],
        check=False,
        capture=True,
        print_cmd=False,
    )
    if res.returncode != 0:
        logging.warning(
            "Could not read the ancestry of project %s: %s",
            project_id,
            res.stderr.strip(),
        )
        return None
    try:
        return [
            ProjectAncestor(id=entry["id"], type=entry["type"])
            for entry in json.loads(res.stdout)
        ]
    except (json.JSONDecodeError, TypeError, KeyError) as e:
        logging.warning("Could not read the ancestry of project %s: %s", project_id, e)
        return None
