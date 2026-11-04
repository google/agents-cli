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

"""Read-only checks that a Cloud Run deploy's Google Cloud requests would succeed."""

from __future__ import annotations

from collections.abc import Mapping

import click

from google.agents.cli.deploy._agent_identity import should_use_agent_identity
from google.agents.cli.deploy._cloud_validation import (
    _ARTIFACT_REGISTRY_API,
    _CLOUD_BUILD_API,
    _CLOUD_BUILD_PERMISSION,
    _GET_PROJECT_IAM_POLICY_PERMISSION,
    _SECRET_MANAGER_API,
    _SET_PROJECT_IAM_POLICY_PERMISSION,
    _VALIDATION_HEADER,
    CheckResult,
    CheckStatus,
    ValidationReport,
    _api_error_field,
    _call_api,
    _check_apis,
    _check_service_account,
    _combined_result,
    _describe_problem,
    _fail_result,
    _gcloud_steps_identity,
    _get_token,
    _iam_permissions_result,
    _IamResource,
    _is_transient_status,
    _no_credentials_result,
    _pass_result,
    _Permissions,
    _report_results,
    _run_check,
    _secret_version_checks,
    _warn_result,
    _with_api_message,
)
from google.agents.cli.deploy._utils import (
    cloud_run_has_agent_identity,
    cloud_run_service_url,
    parse_secret_refs,
    secret_resource_name,
)

_CLOUD_RUN_API = "run.googleapis.com"
_CREATE_SERVICE_PERMISSION = "run.services.create"
_UPDATE_SERVICE_PERMISSION = "run.services.update"
_GET_SECRET_IAM_POLICY_PERMISSION = "secretmanager.secrets.getIamPolicy"
_SET_SECRET_IAM_POLICY_PERMISSION = "secretmanager.secrets.setIamPolicy"


def validate_cloud_run_deployment(
    *,
    project: str,
    region: str | None,
    service_name: str,
    service_account: str | None,
    agent_identity: bool | None,
    identity_default: str,
    secrets: Mapping[str, str],
    image: str | None,
    project_number: str | None,
) -> tuple[ValidationReport, bool | None]:
    """Check what a Cloud Run deploy needs, and print the results.

    ``image`` is a prebuilt container, so the deploy skips the build. With no
    ``region``, gcloud picks one. Also returns whether the service exists, or
    None when that is unknown.
    """
    click.echo(_VALIDATION_HEADER)
    token = _get_token()
    if token is None:
        return _report_results([_no_credentials_result()]), None
    # gcloud makes the deploy requests, so the checks run as its account. The
    # service lookup keeps the get_access_token() token, like the deploy's own
    # lookup in _is_cloud_run_create.
    identity = _gcloud_steps_identity(token)
    check_token = identity.token
    adc_differs = identity.other_account is not None
    click.echo(f"  Checking as {identity.checked_as}")

    builds_image = not image
    enabled_by_deploy = (_ARTIFACT_REGISTRY_API, _CLOUD_BUILD_API) if builds_image else ()
    apis = (_CLOUD_RUN_API, *enabled_by_deploy)
    if secrets and _has_secret_in_deploy_project(secrets):
        apis += (_SECRET_MANAGER_API,)
    apis_result, disabled_apis = _check_apis(
        project=project,
        apis=apis,
        token=check_token,
        enabled_by_deploy=enabled_by_deploy,
    )
    results = [apis_result]

    service_exists, service = None, None
    if not region:
        results.append(
            _warn_result(
                "Target service",
                "not looked up, because no region is set, so gcloud uses its "
                "run/region setting, or asks for a region when that is not set — "
                "set region in agents-cli-manifest.yaml, or pass --region",
            )
        )
    elif _CLOUD_RUN_API not in disabled_apis:
        service_result, service_exists, service = _check_cloud_run_service(
            project=project, region=region, service_name=service_name, token=token
        )
        results.append(service_result)
    already_agent_identity = cloud_run_has_agent_identity(service)
    # Resolved like the deploy does: a new service follows the project's identity
    # default, and Agent Identity drops --service-account.
    agent_identity = should_use_agent_identity(
        agent_identity_flag=agent_identity,
        identity_default=identity_default,
        is_update=bool(service_exists),
    )
    if agent_identity:
        service_account = None
    if agent_identity is False and already_agent_identity:
        results.append(
            _fail_result(
                "Agent Identity",
                f"service '{service_name}' already runs with Agent Identity, which "
                "cannot be turned off — to go back to a service account, delete the "
                "service and redeploy without --agent-identity",
            )
        )
    elif already_agent_identity and service_account:
        results.append(
            _warn_result(
                "Agent Identity",
                f"service '{service_name}' runs as its Agent Identity principal, so "
                f"--service-account '{service_account}' is only recorded and checked "
                "at deploy time — the agent doesn't run as it",
            )
        )
    uses_agent_identity = bool(agent_identity) or already_agent_identity

    # Every deploy with Agent Identity reads the project IAM policy to grant the
    # principal its roles. It writes the policy only for roles the principal
    # lacks, which a service that already has Agent Identity normally holds.
    if not uses_agent_identity:
        role_grant_permissions = _Permissions()
    elif already_agent_identity:
        role_grant_permissions = _Permissions(
            maybe_required=(
                _GET_PROJECT_IAM_POLICY_PERMISSION,
                _SET_PROJECT_IAM_POLICY_PERMISSION,
            ),
        )
    else:
        role_grant_permissions = _Permissions(
            required=(
                _GET_PROJECT_IAM_POLICY_PERMISSION,
                _SET_PROJECT_IAM_POLICY_PERMISSION,
            )
        )

    results.append(
        _iam_permissions_result(
            project=project,
            token=check_token,
            permissions=_cloud_run_permissions(
                service_exists=service_exists,
                builds_image=builds_image,
                role_grant_permissions=role_grant_permissions,
            ),
            resource=_IamResource(
                kind="service",
                url=cloud_run_service_url(
                    project=project, region=region, service=service_name
                )
                + ":testIamPermissions",
                permission=_UPDATE_SERVICE_PERMISSION,
            )
            if service_exists and region
            else None,
        )
    )
    # Cloud Run checks actAs on the account the service records, even with Agent
    # Identity.
    current_account = ((service or {}).get("template") or {}).get("serviceAccount")
    results.extend(
        _cloud_run_service_account_checks(
            service_account=service_account,
            current_account=current_account,
            service_exists=service_exists,
            project=project,
            project_number=project_number,
            token=check_token,
        )
    )
    if secrets:
        results.extend(
            _secret_version_checks(
                project=project,
                secrets=secrets,
                token=check_token,
                disabled_apis=disabled_apis,
                runtime_identity="the service account of the service",
            )
        )
        if uses_agent_identity and _SECRET_MANAGER_API not in disabled_apis:
            results.append(
                _run_check(
                    "Secret access",
                    lambda: _check_secret_grant_permissions(
                        project=project,
                        secrets=secrets,
                        token=check_token,
                        migrates=not already_agent_identity,
                    ),
                )
            )
    if adc_differs:
        # The service lookup used the same token as the deploy's own lookup, and
        # the Agent Identity check reads the service it returned.
        results = [
            _warn_result(r.name, r.detail)
            if r.status is CheckStatus.FAILED
            and r.name not in ("Target service", "Agent Identity")
            else r
            for r in results
        ]
    return _report_results(results), service_exists


def _check_cloud_run_service(
    *, project: str, region: str, service_name: str, token: str
) -> tuple[CheckResult, bool | None, dict | None]:
    """Look up the service the deploy writes to.

    Returns the result, whether the service exists (None when unknown), and the
    service as the Admin API v2 returns it, when it exists.
    """
    exists: bool | None = None
    service: dict | None = None

    def check() -> CheckResult:
        nonlocal exists, service
        resp = _call_api(
            url=cloud_run_service_url(
                project=project, region=region, service=service_name
            ),
            token=token,
        )
        if resp.status_code == 404:
            # A host for a region that does not exist answers with an HTML 404.
            if _api_error_field(resp, "status") != "NOT_FOUND":
                return _fail_result(
                    "Target service",
                    f"Cloud Run has no region '{region}' — check --region",
                )
            exists = False
            return _pass_result(
                "Target service", f"creates a new service named '{service_name}'"
            )
        if _is_transient_status(resp.status_code):
            return _warn_result(
                "Target service", f"could not verify — HTTP {resp.status_code}"
            )
        if not resp.ok:
            detail = (
                f"Cloud Run rejected the service lookup (HTTP {resp.status_code}). "
                "The deploy does the same lookup first, so it would stop here too "
                "— check --project and --region, and that you have run.services.get"
            )
            return _fail_result("Target service", _with_api_message(detail, resp))
        service = resp.json()
        exists = True
        return _pass_result("Target service", f"updates service '{service_name}'")

    result = _run_check("Target service", check)
    return result, exists, service


def _cloud_run_permissions(
    *,
    service_exists: bool | None,
    builds_image: bool,
    role_grant_permissions: _Permissions,
) -> _Permissions:
    """Create for a new service, update for an existing one, either when unknown.

    A source deploy also builds the image.
    """
    build = (_CLOUD_BUILD_PERMISSION,) if builds_image else ()
    if service_exists is None:
        return _Permissions(
            required=(*build, *role_grant_permissions.required),
            maybe_required=(
                _CREATE_SERVICE_PERMISSION,
                _UPDATE_SERVICE_PERMISSION,
                *role_grant_permissions.maybe_required,
            ),
        )
    write = _UPDATE_SERVICE_PERMISSION if service_exists else _CREATE_SERVICE_PERMISSION
    return _Permissions(
        required=(write, *build, *role_grant_permissions.required),
        maybe_required=role_grant_permissions.maybe_required,
    )


def _cloud_run_service_account_checks(
    *,
    service_account: str | None,
    current_account: str | None,
    service_exists: bool | None,
    project: str,
    project_number: str | None,
    token: str,
) -> list[CheckResult]:
    """Check the account the service will run as, when it is known."""
    if service_account:
        return [
            _run_check(
                "Service account",
                lambda: _check_cloud_run_service_account(
                    account=service_account, project=project, token=token
                ),
            )
        ]
    # Cloud Run checks actAs on the runtime account for every new revision, even
    # when the deploy keeps the service's current account.
    if current_account:
        return [
            _run_check(
                "Service account",
                lambda: _check_service_account(
                    service_account=current_account,
                    token=token,
                    not_found_hint="the service runs as this account; pass "
                    "--service-account to replace it",
                ),
            )
        ]
    if service_exists is not False:
        return []
    return [
        _run_check(
            "Service account",
            lambda: _check_default_service_account(
                project_number=project_number, token=token
            ),
        )
    ]


def _check_cloud_run_service_account(
    *, account: str, project: str, token: str
) -> CheckResult:
    """Check the account the service runs as. An ID without ``@`` is an account
    in the deploy project.

    The email holds the project ID, so it cannot be built from a project number
    or a domain-scoped project ID.
    """
    if "@" in account:
        return _check_service_account(service_account=account, token=token)
    if project.isdigit() or ":" in project:
        return _warn_result(
            "Service account",
            f"'{account}' was not checked, because its email cannot be built from "
            f"project '{project}' — pass the full email to check it",
        )
    return _check_service_account(
        service_account=f"{account}@{project}.iam.gserviceaccount.com",
        token=token,
        not_found_hint="check the account ID",
    )


def _check_default_service_account(
    *, project_number: str | None, token: str
) -> CheckResult:
    """Check the default compute account, which a new service runs as when no
    ``--service-account`` is set."""
    if not project_number:
        return _warn_result(
            "Service account",
            "could not read the project number, so the default compute service "
            "account was not checked",
        )
    return _check_service_account(
        service_account=f"{project_number}-compute@developer.gserviceaccount.com",
        token=token,
        not_found_hint="a new service runs as this default account; pass "
        "--service-account",
    )


def _check_secret_grant_permissions(
    *, project: str, secrets: Mapping[str, str], token: str, migrates: bool
) -> CheckResult:
    """Check that you can grant the Agent Identity principal access to each secret.

    The deploy reads each secret's IAM policy, and writes it when the principal
    can't read the secret yet.
    """
    permissions = (_GET_SECRET_IAM_POLICY_PERMISSION, _SET_SECRET_IAM_POLICY_PERMISSION)
    required = permissions if migrates else ()
    maybe_required = () if migrates else permissions
    missing: list[str] = []
    maybe_missing: list[str] = []
    unverified: list[str] = []
    names = dict.fromkeys(
        secret_resource_name(project=project, secret=spec["secret"])
        for spec in parse_secret_refs(secrets).values()
    )
    for name in names:
        resp = _call_api(
            url=f"https://secretmanager.googleapis.com/v1/{name}:testIamPermissions",
            token=token,
            json={"permissions": [*required, *maybe_required]},
        )
        if not resp.ok:
            unverified.append(f"{name} (HTTP {resp.status_code})")
            continue
        granted = set(resp.json().get("permissions", []))
        missing += [f"{p} on {name}" for p in required if p not in granted]
        maybe_missing += [f"{p} on {name}" for p in maybe_required if p not in granted]
    return _combined_result(
        "Secret access",
        failures=_describe_problem(
            prefix="missing",
            items=missing,
            hint="the deploy grants the Agent Identity principal "
            "roles/secretmanager.secretAccessor on each secret; ask for "
            "roles/secretmanager.admin on it",
        ),
        warnings=[
            *_describe_problem(
                prefix="missing",
                items=maybe_missing,
                hint="needed only if the Agent Identity principal can't read the "
                "secret yet",
            ),
            *_describe_problem(prefix="could not verify", items=unverified),
        ],
        passed="you can grant the Agent Identity principal access to every secret",
    )


def _has_secret_in_deploy_project(secrets: Mapping[str, str]) -> bool:
    """True when any secret is a bare ID, which Cloud Run reads from the deploy
    project.

    A full ``projects/<p>/secrets/<s>`` path points at its own project, so it
    does not need Secret Manager enabled in the deploy project.
    """
    return any(
        not spec["secret"].startswith("projects/")
        for spec in parse_secret_refs(secrets).values()
    )
