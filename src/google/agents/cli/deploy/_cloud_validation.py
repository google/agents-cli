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

"""Shared parts of the read-only Google Cloud checks of a deploy dry run."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum

import click
import requests

from google.agents.cli import _gcloud_auth
from google.agents.cli.auth import get_access_token, get_gcloud_access_token
from google.agents.cli.deploy._utils import parse_secret_refs, secret_resource_name
from google.agents.cli.scaffold.utils.command import run_gcloud_command

# (connect, read) seconds, so an unreachable host fails fast.
_REQUEST_TIMEOUT = (5, 30)
_RETRY_STATUS_CODES = (429, 503)
_RETRY_DELAY_SECONDS = 1
_VALIDATION_HEADER = "\n🔎 Validating the deployment (no resources are changed)..."
# Continues a multi-line detail under its check in the report.
_DETAIL_INDENT = "\n      "
_AGENT_RUNTIME_API = "aiplatform.googleapis.com"
_CLOUD_BUILD_API = "cloudbuild.googleapis.com"
_ARTIFACT_REGISTRY_API = "artifactregistry.googleapis.com"
_SECRET_MANAGER_API = "secretmanager.googleapis.com"
_CLOUD_BUILD_PERMISSION = "cloudbuild.builds.create"
_GET_PROJECT_IAM_POLICY_PERMISSION = "resourcemanager.projects.getIamPolicy"
_SET_PROJECT_IAM_POLICY_PERMISSION = "resourcemanager.projects.setIamPolicy"
_ACT_AS_PERMISSION = "iam.serviceAccounts.actAs"


class CheckStatus(Enum):
    """Outcome of a single pre-deploy check."""

    PASSED = "passed"
    WARNED = "warned"
    FAILED = "failed"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True)
class ValidationReport:
    """The check results, already printed. A dry run prints its plan, then
    calls ``raise_if_failed``."""

    results: tuple[CheckResult, ...]

    def raise_if_failed(self) -> None:
        """Stop the command if a check failed, else warn if a check only warned."""
        failures = [r for r in self.results if r.status is CheckStatus.FAILED]
        if failures:
            raise click.ClickException(
                f"Pre-deploy validation failed ({len(failures)} of "
                f"{len(self.results)} checks): "
                f"{', '.join(r.name for r in failures)}.\n"
                "  Fix the ❌ items, then run the dry run again."
            )
        if any(r.status is CheckStatus.WARNED for r in self.results):
            logging.warning("Some checks only warned, so the deploy may still fail.")


_STATUS_ICONS = {
    CheckStatus.PASSED: "✅",
    CheckStatus.WARNED: "⚠️ ",
    CheckStatus.FAILED: "❌",
}


@dataclass(frozen=True)
class _Permissions:
    """Permissions to test. A missing ``required`` one fails; the others only
    warn. ``maybe_required`` ones may not be needed, ``kubernetes`` ones an RBAC
    role can grant instead of IAM (``kubernetes_hint`` says how), and
    ``other_account`` ones are needed by steps that run as
    ``other_account_name``, which the checks cannot run as.
    ``other_account_kubernetes`` ones are both.
    """

    required: tuple[str, ...] = ()
    maybe_required: tuple[str, ...] = ()
    other_account: tuple[str, ...] = ()
    kubernetes: tuple[str, ...] = ()
    other_account_kubernetes: tuple[str, ...] = ()
    other_account_name: str = ""
    kubernetes_hint: str = ""

    @property
    def requested(self) -> tuple[str, ...]:
        return (
            self.required
            + self.maybe_required
            + self.other_account
            + self.kubernetes
            + self.other_account_kubernetes
        )


@dataclass(frozen=True)
class _IamResource:
    """A resource that can grant ``permission`` on itself, apart from the project."""

    kind: str
    url: str
    permission: str


@dataclass
class _States:
    """Labels sorted by what a batch of state lookups returned.

    ``denied`` holds the labels of a 403, ``unreadable`` the labels of any other
    error, with the HTTP code.
    """

    not_found: list[str] = field(default_factory=list)
    not_enabled: dict[str, str] = field(default_factory=dict)
    denied: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)


def _pass_result(name: str, detail: str) -> CheckResult:
    return CheckResult(name, CheckStatus.PASSED, detail)


def _warn_result(name: str, detail: str) -> CheckResult:
    return CheckResult(name, CheckStatus.WARNED, detail)


def _fail_result(name: str, detail: str) -> CheckResult:
    return CheckResult(name, CheckStatus.FAILED, detail)


def _no_credentials_result() -> CheckResult:
    return _warn_result(
        "Google Cloud credentials",
        "no access token, so the Google Cloud checks were skipped — run "
        "`agents-cli login`",
    )


def _combined_result(
    name: str, *, failures: list[str], warnings: list[str], passed: str
) -> CheckResult:
    """Fail if anything failed, else warn if anything warned, else pass.

    A failure also lists the warnings, so one run shows everything to fix.
    """
    if failures:
        return _fail_result(name, _DETAIL_INDENT.join(failures + warnings))
    if warnings:
        return _warn_result(name, _DETAIL_INDENT.join(warnings))
    return _pass_result(name, passed)


def _describe_problem(
    *, prefix: str, items: list[str], hint: str | None = None
) -> list[str]:
    """One report line naming ``items``, or no line when there are none."""
    if not items:
        return []
    line = f"{prefix}: {', '.join(items)}"
    return [f"{line} — {hint}" if hint else line]


def _run_check(name: str, check: Callable[[], CheckResult]) -> CheckResult:
    """Run one check. A ``ClickException`` fails it, any other error only warns.

    The deploy raises the same ``ClickException`` for the same problem. Any
    other error, such as no network, says nothing about the deploy.
    """
    try:
        return check()
    except click.ClickException as exc:
        return _fail_result(name, exc.format_message())
    except Exception as exc:
        logging.debug("Validation check %r could not run", name, exc_info=exc)
        return _warn_result(name, f"could not verify — {type(exc).__name__}: {exc}")


def _call_api(*, url: str, token: str, json: dict | None = None) -> requests.Response:
    """GET ``url``, or POST ``json`` to it when given. Retries once when throttled."""
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(2):
        if json is None:
            resp = requests.get(url, headers=headers, timeout=_REQUEST_TIMEOUT)
        else:
            resp = requests.post(
                url, headers=headers, json=json, timeout=_REQUEST_TIMEOUT
            )
        if attempt or resp.status_code not in _RETRY_STATUS_CODES:
            break
        time.sleep(_RETRY_DELAY_SECONDS)
    if not resp.ok:
        logging.debug("Validation request to %s returned HTTP %s", url, resp.status_code)
    return resp


def _read_states(*, urls: dict[str, str], token: str) -> _States:
    """GET each labeled URL and sort the label by the ``state`` it reports."""
    states = _States()
    for label, url in urls.items():
        resp = _call_api(url=url, token=token)
        if resp.status_code == 404:
            states.not_found.append(label)
        elif resp.status_code == 403:
            states.denied.append(label)
        elif not resp.ok:
            states.unreadable.append(f"{label} (HTTP {resp.status_code})")
        elif (state := resp.json().get("state")) != "ENABLED":
            states.not_enabled[label] = state
    return states


def _api_error_field(resp: requests.Response, key: str) -> str | None:
    """Read ``error.<key>`` from a Google API error body, or None."""
    try:
        return resp.json()["error"][key]
    except (ValueError, KeyError, TypeError):
        return None


def _with_api_message(detail: str, resp: requests.Response) -> str:
    message = _api_error_field(resp, "message")
    return f"{detail}{_DETAIL_INDENT}{message}" if message else detail


def _get_token() -> str | None:
    """Return the access token, or None when there are no credentials."""
    try:
        return get_access_token()
    except Exception as exc:
        logging.debug("Validation checks have no access token", exc_info=exc)
        return None


@dataclass(frozen=True)
class _GcloudStepsIdentity:
    """Who the checks of the gcloud steps run as.

    ``other_account`` is the account gcloud deploys as, when the checks fall
    back to an ADC that may be another account; None otherwise.
    """

    token: str
    checked_as: str
    other_account: str | None


def _gcloud_steps_identity(
    token: str, *, failed_checks: str = "failed checks"
) -> _GcloudStepsIdentity:
    """Check the gcloud steps as gcloud's account, or as the ADC when gcloud's
    token does not work for direct API calls."""
    gcloud_token, account = _get_gcloud_identity()
    if account and gcloud_token:
        return _GcloudStepsIdentity(gcloud_token, f"{account} (gcloud account)", None)
    if token == gcloud_token:
        return _GcloudStepsIdentity(
            token,
            "your gcloud account, but its token does not work for direct API "
            "calls, and Application Default Credentials are not set up — run "
            "`agents-cli login`",
            None,
        )
    adc_email = _token_email(token)
    checked_as = (
        f"{adc_email} (Application Default Credentials)"
        if adc_email
        else "your Application Default Credentials"
    )
    # Without credentials of its own, gcloud deploys with the ADC token too.
    if _gcloud_auth.adc_token_for_gcloud() is not None:
        return _GcloudStepsIdentity(token, checked_as, None)
    deploy_account = _gcloud_deploy_account()
    if not adc_email or adc_email.lower() != (deploy_account or "").lower():
        other = deploy_account or "an unknown account"
        return _GcloudStepsIdentity(
            token,
            f"{checked_as}, but gcloud deploys as {other}, so {failed_checks} only warn",
            other,
        )
    return _GcloudStepsIdentity(token, checked_as, None)


def _get_gcloud_identity() -> tuple[str | None, str | None]:
    """Return the gcloud token and its email.

    The email is None when gcloud has no token or tokeninfo rejects it.
    """
    try:
        token = get_gcloud_access_token()
    except Exception as exc:
        logging.debug("gcloud has no access token", exc_info=exc)
        return None, None
    if not token:
        return None, None
    # A gcloud token bound to an mTLS client certificate fails plain REST calls.
    email = _token_email(token)
    return token, None if email is None else email or "an unknown account"


def _token_email(token: str) -> str | None:
    """Return the email of ``token``, "" when tokeninfo names none, or None
    when tokeninfo rejects it."""
    try:
        tokeninfo = requests.post(
            "https://oauth2.googleapis.com/tokeninfo",
            data={"access_token": token},
            timeout=_REQUEST_TIMEOUT,
        )
        if tokeninfo.ok:
            return tokeninfo.json().get("email", "")
        logging.debug("tokeninfo rejected the token (HTTP %s)", tokeninfo.status_code)
    except Exception as exc:
        logging.debug("Could not check the token", exc_info=exc)
    return None


def _gcloud_deploy_account() -> str | None:
    """Return the account gcloud deploys as, or None when it is unknown."""
    try:
        config = json.loads(
            run_gcloud_command(
                ["config", "list", "--format=json"], capture_output=True, timeout=5
            ).stdout
        )
        return (config.get("auth") or {}).get("impersonate_service_account") or (
            config.get("core") or {}
        ).get("account")
    except Exception as exc:
        logging.debug("Could not read the gcloud account", exc_info=exc)
        return None


def _check_apis(
    *,
    project: str,
    apis: tuple[str, ...],
    token: str,
    enabled_by_deploy: tuple[str, ...] = (),
    enabled_by_deploy_hint: str = "gcloud asks to enable them during the deploy",
    passed: str | None = None,
) -> tuple[CheckResult, list[str]]:
    """Return the check result and the APIs found disabled.

    An API in ``enabled_by_deploy`` only warns, because the deploy turns it on,
    as ``enabled_by_deploy_hint`` says. ``passed`` replaces the list of APIs on
    success.
    """
    disabled: list[str] = []

    def check() -> CheckResult:
        states = _read_states(
            urls={
                api: f"https://serviceusage.googleapis.com/v1/projects/{project}/services/{api}"
                for api in apis
            },
            token=token,
        )
        disabled.extend(states.not_enabled)
        must_enable = [api for api in disabled if api not in enabled_by_deploy]
        offered = [api for api in disabled if api in enabled_by_deploy]
        return _combined_result(
            "APIs enabled",
            failures=_describe_problem(
                prefix="not enabled",
                items=must_enable,
                hint=f"enable with:{_DETAIL_INDENT}"
                f"gcloud services enable {' '.join(must_enable)} --project={project}",
            ),
            warnings=[
                *_describe_problem(
                    prefix="not enabled",
                    items=offered,
                    hint=enabled_by_deploy_hint,
                ),
                *_describe_problem(
                    prefix="could not verify",
                    items=[f"{api} (HTTP 403)" for api in states.denied],
                    hint="check --project, or ask for "
                    "roles/serviceusage.serviceUsageViewer so the dry run can check "
                    "the APIs",
                ),
                *_describe_problem(
                    prefix="could not verify",
                    items=[
                        *states.unreadable,
                        *(f"{api} (HTTP 404)" for api in states.not_found),
                    ],
                ),
            ],
            passed=passed or ", ".join(apis),
        )

    return _run_check("APIs enabled", check), disabled


def _is_transient_status(code: int) -> bool:
    """Throttled (429) or server-side (5xx), which says nothing about the request."""
    return code == 429 or code >= 500


def _iam_permissions_result(
    *,
    project: str,
    token: str,
    permissions: _Permissions,
    resource: _IamResource | None,
) -> CheckResult:
    return _run_check(
        "IAM permissions",
        lambda: _check_permissions(
            project=project,
            token=token,
            permissions=permissions,
            resource=resource,
        ),
    )


def _check_permissions(
    *,
    project: str,
    token: str,
    permissions: _Permissions,
    resource: _IamResource | None,
) -> CheckResult:
    """Test the permissions on the project.

    ``resource.permission`` granted on the resource itself also counts: a user
    can hold it on a single resource and not on the project.
    """
    requested = permissions.requested
    resp = _call_api(
        url=f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}:testIamPermissions",
        token=token,
        json={"permissions": list(requested)},
    )
    if not resp.ok:
        return _warn_result(
            "IAM permissions", f"could not verify — HTTP {resp.status_code}"
        )
    granted = set(resp.json().get("permissions", []))
    unverified: list[str] = []
    unverified_hint = None
    if resource and resource.permission in set(requested) - granted:
        resource_granted, problem = _resource_grants(resource=resource, token=token)
        granted |= resource_granted
        if problem:
            unverified = [resource.permission]
            unverified_hint = (
                f"not granted on the project, and the {resource.kind} could not "
                f"be checked ({problem})"
            )
    # testIamPermissions grants nothing on a project that does not exist.
    ask_admin = "ask a project admin for a role that grants it"
    if not granted:
        ask_admin = f"check that '{project}' is the right project ID, or {ask_admin}"
    missing = [
        p for p in permissions.required if p not in granted and p not in unverified
    ]
    missing_on = f"missing on '{project}'"
    return _combined_result(
        "IAM permissions",
        failures=_describe_problem(prefix=missing_on, items=missing, hint=ask_admin),
        warnings=[
            *_describe_problem(
                prefix=missing_on,
                items=[p for p in permissions.maybe_required if p not in granted],
                hint="the deploy may fail if it needs these permissions",
            ),
            *_describe_problem(
                prefix=missing_on,
                items=[p for p in permissions.other_account if p not in granted],
                hint="the gcloud steps need these, but they run as "
                f"{permissions.other_account_name}, which the dry run cannot check; "
                "if that account lacks them, ask a project admin for a role that "
                "grants them",
            ),
            *_describe_problem(
                prefix=missing_on,
                items=[p for p in permissions.kubernetes if p not in granted],
                hint=permissions.kubernetes_hint,
            ),
            *_describe_problem(
                prefix=missing_on,
                items=[
                    p for p in permissions.other_account_kubernetes if p not in granted
                ],
                hint=f"kubectl runs as {permissions.other_account_name}, which the "
                f"dry run cannot check; {permissions.kubernetes_hint}",
            ),
            *_describe_problem(prefix=missing_on, items=unverified, hint=unverified_hint),
        ],
        passed=f"all {len(requested)} permissions granted"
        if len(requested) > 1
        else f"{requested[0]} granted",
    )


def _resource_grants(
    *, resource: _IamResource, token: str
) -> tuple[set[str], str | None]:
    """Return the permission if granted on ``resource``, and why it gave no answer."""
    try:
        resp = _call_api(
            url=resource.url, token=token, json={"permissions": [resource.permission]}
        )
    except Exception as exc:
        logging.debug("%s permission check could not run", resource.kind, exc_info=exc)
        return set(), type(exc).__name__
    if not resp.ok:
        return set(), f"HTTP {resp.status_code}"
    return set(resp.json().get("permissions", [])), None


def _check_service_account(
    *,
    service_account: str,
    token: str,
    not_found_hint: str = "check the email",
) -> CheckResult:
    """Check that the account exists and that you may deploy with it.

    ``projects/-`` lets IAM find the account's project from the email, so an
    account from another project is found too.
    """
    resp = _call_api(
        url=f"https://iam.googleapis.com/v1/projects/-/serviceAccounts/{service_account}:testIamPermissions",
        token=token,
        json={"permissions": [_ACT_AS_PERMISSION]},
    )
    if resp.status_code == 404:
        return _fail_result(
            "Service account", f"{service_account} does not exist — {not_found_hint}"
        )
    if not resp.ok:
        return _warn_result(
            "Service account",
            f"could not verify {service_account} — HTTP {resp.status_code}",
        )
    if _ACT_AS_PERMISSION not in resp.json().get("permissions", []):
        return _fail_result(
            "Service account",
            f"missing {_ACT_AS_PERMISSION} on {service_account} — ask for "
            "roles/iam.serviceAccountUser on that account",
        )
    return _pass_result(
        "Service account", f"{service_account} exists and you can act as it"
    )


def _secret_version_checks(
    *,
    project: str,
    secrets: Mapping[str, str],
    token: str,
    disabled_apis: list[str],
    runtime_identity: str,
) -> list[CheckResult]:
    """Look up the secret versions, unless the Secret Manager API is disabled.

    Secret Manager answers 403 while its API is disabled, and the APIs check
    already fails on that.
    """
    if _SECRET_MANAGER_API in disabled_apis:
        logging.warning(
            "Could not verify the secrets: the Secret Manager API is disabled in "
            "'%s'. Enable it, then run the dry run again.",
            project,
        )
        return []
    return [
        _run_check(
            "Secrets",
            lambda: _check_secret_versions(
                project=project,
                secrets=secrets,
                token=token,
                runtime_identity=runtime_identity,
            ),
        )
    ]


def _check_secret_versions(
    *, project: str, secrets: Mapping[str, str], token: str, runtime_identity: str
) -> CheckResult:
    """Look up the exact secret *version* the deploy will mount.

    Reading the version catches a secret or version that does not exist, and a
    version that is disabled or destroyed. The deploy would only hit these at
    runtime. A 403 is not a failure: ``roles/secretmanager.secretAccessor`` lets
    ``runtime_identity`` use the secret but not read its metadata, so a GET can
    be denied for a secret that works fine.
    """
    states = _read_states(
        urls={
            f"{spec['secret']}:{spec['version']}": "https://secretmanager.googleapis.com"
            f"/v1/{secret_resource_name(project=project, secret=spec['secret'])}"
            f"/versions/{spec['version']}"
            for spec in parse_secret_refs(secrets).values()
        },
        token=token,
    )
    return _combined_result(
        "Secrets",
        failures=[
            *_describe_problem(
                prefix=f"not found in '{project}'",
                items=[s for s in states.not_found if not s.startswith("projects/")],
                hint="check the secret ID and version, or create them",
            ),
            *_describe_problem(
                prefix="not found",
                items=[s for s in states.not_found if s.startswith("projects/")],
                hint="check the secret path and version, or create them",
            ),
            *_describe_problem(
                prefix="not enabled",
                items=[
                    f"{label} ({state or 'no state'})"
                    for label, state in states.not_enabled.items()
                ],
                hint="enable the version, or point --secrets at an enabled one",
            ),
        ],
        warnings=_describe_problem(
            prefix="could not verify",
            items=[
                *(f"{label} (HTTP 403)" for label in states.denied),
                *states.unreadable,
            ],
            hint=f"the deploy still works if {runtime_identity} holds "
            "roles/secretmanager.secretAccessor",
        ),
        passed="all referenced secret versions exist and are enabled",
    )


def _report_results(results: list[CheckResult]) -> ValidationReport:
    """Print the check results."""
    for result in results:
        click.echo(f"  {_STATUS_ICONS[result.status]} {result.name}: {result.detail}")
    return ValidationReport(tuple(results))
