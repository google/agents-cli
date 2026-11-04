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

"""Agent Identity role grants."""

from __future__ import annotations

import logging
from collections.abc import Iterable

import click
import requests
from google.api_core import exceptions as api_exceptions
from google.cloud import resourcemanager_v3
from google.iam.v1 import iam_policy_pb2, policy_pb2

from google.agents.cli._project import AGENT_IDENTITY
from google.agents.cli.auth import get_access_token

AGENT_IDENTITY_ROLES = (
    "roles/aiplatform.user",
    "roles/serviceusage.serviceUsageConsumer",
    "roles/browser",
    "roles/cloudapiregistry.viewer",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
)

SECRET_ACCESSOR_ROLE = "roles/secretmanager.secretAccessor"

_SECRET_MANAGER_URL = "https://secretmanager.googleapis.com/v1"
_IAM_POLICY_VERSION = 3


def should_use_agent_identity(
    *, agent_identity_flag: bool | None, identity_default: str, is_update: bool
) -> bool | None:
    """Whether the deploy uses Agent Identity, or None to keep the engine's identity.

    The --agent-identity flag wins. Without it, a new engine follows the
    manifest default.
    """
    if agent_identity_flag is None and not is_update:
        return identity_default == AGENT_IDENTITY
    return agent_identity_flag


def grant_roles_to_principal(
    project: str, principal: str, *, denied_read_is_error: bool = True
) -> None:
    """Grant `AGENT_IDENTITY_ROLES` to `principal` on `project`.

    `principal` is a full IAM member string, e.g.
    `principal://agents.global.org-123.system.id.goog/resources/...`. Roles it
    already holds unconditionally are skipped, and the policy is not written
    when it holds them all.

    If the current policy cannot be read, click.ClickException is raised, unless
    `denied_read_is_error` is False.
    """
    click.echo(f"🔐 Granting IAM roles to: {principal}")
    proj_client = resourcemanager_v3.ProjectsClient()
    try:
        policy = proj_client.get_iam_policy(
            request=iam_policy_pb2.GetIamPolicyRequest(resource=f"projects/{project}")
        )
    except api_exceptions.PermissionDenied as err:
        message = _roles_grant_failed_message(
            project=project,
            principal=principal,
            missing_permission="resourcemanager.projects.getIamPolicy",
            roles=AGENT_IDENTITY_ROLES,
        )
        if denied_read_is_error:
            raise click.ClickException(message) from err
        _warn_grant_not_checked(message)
        return
    held = {
        binding.role
        for binding in policy.bindings
        if principal in binding.members and not binding.condition.expression
    }
    missing = [role for role in AGENT_IDENTITY_ROLES if role not in held]
    if not missing:
        click.echo("  ✅ Agent identity ready")
        return
    for role in missing:
        policy.bindings.append(policy_pb2.Binding(role=role, members=[principal]))
    try:
        proj_client.set_iam_policy(
            request=iam_policy_pb2.SetIamPolicyRequest(
                resource=f"projects/{project}", policy=policy
            )
        )
    except api_exceptions.PermissionDenied as err:
        raise click.ClickException(
            _roles_grant_failed_message(
                project=project,
                principal=principal,
                missing_permission="resourcemanager.projects.setIamPolicy",
                roles=missing,
            )
        ) from err
    click.echo("  ✅ Agent identity ready")


def _roles_grant_failed_message(
    *, project: str, principal: str, missing_permission: str, roles: Iterable[str]
) -> str:
    commands = "\n".join(
        f"    gcloud projects add-iam-policy-binding {project} "
        f"--member='{principal}' --role={role}"
        for role in roles
    )
    return (
        f"Could not grant the agent's principal its roles: {missing_permission} is "
        f"missing on project '{project}'.\n"
        "  Ask a project admin for it, or to grant the roles:\n"
        f"{commands}\n"
        "  Or, for a new agent, deploy with --no-agent-identity to run it as a "
        "service account."
    )


def grant_secret_access_to_principal(
    principal: str, secrets: Iterable[str], *, denied_read_is_error: bool
) -> None:
    """Grant `SECRET_ACCESSOR_ROLE` on each secret to `principal`.

    `secrets` are Secret Manager resource names, `projects/P/secrets/S`. A secret
    that already grants the role to `principal` unconditionally is skipped, and
    its policy is not written.

    If a secret's policy cannot be read, click.ClickException is raised, unless
    `denied_read_is_error` is False.
    """
    headers = {"Authorization": f"Bearer {get_access_token()}"}
    for secret in dict.fromkeys(secrets):
        url = f"{_SECRET_MANAGER_URL}/{secret}"
        resp = requests.get(
            f"{url}:getIamPolicy",
            headers=headers,
            params={"options.requestedPolicyVersion": _IAM_POLICY_VERSION},
            timeout=30,
        )
        if resp.status_code == 403:
            message = _secret_denied_message(
                secret=secret,
                principal=principal,
                problem="secretmanager.secrets.getIamPolicy is missing",
            )
            if denied_read_is_error:
                raise click.ClickException(message)
            _warn_grant_not_checked(message)
            continue
        _raise_for_secret_grant_error(resp, secret=secret, principal=principal)
        policy = resp.json()
        bindings = policy.setdefault("bindings", [])
        binding = next(
            (
                b
                for b in bindings
                if b.get("role") == SECRET_ACCESSOR_ROLE and not b.get("condition")
            ),
            None,
        )
        if binding is None:
            bindings.append({"role": SECRET_ACCESSOR_ROLE, "members": [principal]})
        elif principal in binding.get("members", []):
            continue
        else:
            binding.setdefault("members", []).append(principal)
        click.echo(f"🔐 Granting {SECRET_ACCESSOR_ROLE} on {secret} to: {principal}")
        policy["version"] = _IAM_POLICY_VERSION
        resp = requests.post(
            f"{url}:setIamPolicy", headers=headers, json={"policy": policy}, timeout=30
        )
        _raise_for_secret_grant_error(resp, secret=secret, principal=principal)


def _raise_for_secret_grant_error(
    resp: requests.Response, *, secret: str, principal: str
) -> None:
    if resp.ok:
        return
    problem = (
        "secretmanager.secrets.setIamPolicy is missing"
        if resp.status_code == 403
        else f"HTTP {resp.status_code}"
    )
    raise click.ClickException(
        _secret_denied_message(secret=secret, principal=principal, problem=problem)
    )


def _secret_denied_message(*, secret: str, principal: str, problem: str) -> str:
    return (
        f"Could not grant {SECRET_ACCESSOR_ROLE} on {secret} to the agent's "
        f"principal: {problem}. Without it the service can't read the secret "
        "when an instance starts.\n"
        "  Ask for the permission on the secret, or have the role granted:\n"
        f"    gcloud secrets add-iam-policy-binding {secret} "
        f"--member='{principal}' --role={SECRET_ACCESSOR_ROLE}"
    )


def _warn_grant_not_checked(message: str) -> None:
    logging.warning(
        "%s\n  The agent already runs with Agent Identity, so its principal "
        "normally already has access; the deploy continues.",
        message,
    )
