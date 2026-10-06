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

"""Read-only checks that the Google Cloud requests a deploy will make would succeed."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

import click
import requests

from google.agents.cli._agent_platform import AgentPlatformClient
from google.agents.cli.auth import get_access_token
from google.agents.cli.deploy._utils import parse_secrets
from google.agents.cli.deploy.agent_runtime import _resolve_target_agent

# (connect, read) seconds, so an unreachable host fails fast.
_REQUEST_TIMEOUT = (5, 30)
_RETRY_STATUS_CODES = (429, 503)
_RETRY_DELAY_SECONDS = 1

_SECRET_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,255}")

# Continues a multi-line detail under its check in the report.
_DETAIL_INDENT = "\n      "

_AGENT_RUNTIME_API = "aiplatform.googleapis.com"
_SECRET_MANAGER_API = "secretmanager.googleapis.com"
_CREATE_ENGINE_PERMISSION = "aiplatform.reasoningEngines.create"
_UPDATE_ENGINE_PERMISSION = "aiplatform.reasoningEngines.update"
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


def validate_agent_runtime_deployment(
    *,
    project: str,
    region: str,
    display_name: str,
    service_account: str | None,
    secrets: str | None,
    update_only: bool,
    agent_identity: bool | None,
) -> None:
    """Check what an Agent Runtime deploy needs, and stop if a check fails."""
    click.echo("\n🔎 Validating the deployment (no resources are changed)...")
    secret_ids_result = (
        _check_agent_runtime_secret_ids(project=project, secrets=secrets)
        if secrets
        else None
    )
    token = _get_token()
    if token is None:
        _report_results(
            [
                *filter(None, [secret_ids_result]),
                _warn_result(
                    "Google Cloud credentials",
                    "no access token, so the Google Cloud checks were skipped — "
                    "run `agents-cli login`",
                ),
            ]
        )
        return

    apis = (_AGENT_RUNTIME_API,)
    if secrets:
        apis += (_SECRET_MANAGER_API,)
    apis_result, disabled_apis = _check_apis(project=project, apis=apis, token=token)
    results = [apis_result]

    engine, engine_known = None, False
    if _AGENT_RUNTIME_API not in disabled_apis:
        engine_result, engine, engine_known = _check_target_engine(
            project=project,
            region=region,
            display_name=display_name,
            update_only=update_only,
        )
        results.append(engine_result)

    permissions = _deploy_permissions(
        creates=(engine is None) if engine_known else None,
        update_only=update_only,
        agent_identity=agent_identity,
    )
    results.append(
        _run_check(
            "IAM permissions",
            lambda: _check_permissions(
                project=project,
                token=token,
                permissions=permissions,
                region=region,
                engine=engine,
            ),
        )
    )

    # With Agent Identity the agent is its own principal and the deploy drops
    # --service-account. Checking that account would fail on one the deploy
    # never uses.
    if service_account and not agent_identity:
        results.append(
            _run_check(
                "Service account",
                lambda: _check_service_account(
                    service_account=service_account, token=token
                ),
            )
        )

    # Secret Manager answers 403 while its API is disabled, and the APIs check
    # already fails on that.
    if secret_ids_result:
        results.append(secret_ids_result)
    elif secrets and _SECRET_MANAGER_API not in disabled_apis:
        results.append(
            _run_check(
                "Secrets",
                lambda: _check_secret_versions(
                    project=project, secrets=secrets, token=token
                ),
            )
        )
    _report_results(results)


_STATUS_ICONS = {
    CheckStatus.PASSED: "✅",
    CheckStatus.WARNED: "⚠️ ",
    CheckStatus.FAILED: "❌",
}


@dataclass(frozen=True)
class _Permissions:
    """Permissions to test. A missing ``required`` one fails, a missing
    ``maybe_required`` one only warns."""

    required: tuple[str, ...] = ()
    maybe_required: tuple[str, ...] = ()


@dataclass
class _States:
    """Labels sorted by what a batch of state lookups returned."""

    not_found: list[str] = field(default_factory=list)
    not_enabled: dict[str, str] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)


def _pass_result(name: str, detail: str) -> CheckResult:
    return CheckResult(name, CheckStatus.PASSED, detail)


def _warn_result(name: str, detail: str) -> CheckResult:
    return CheckResult(name, CheckStatus.WARNED, detail)


def _fail_result(name: str, detail: str) -> CheckResult:
    return CheckResult(name, CheckStatus.FAILED, detail)


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
        elif not resp.ok:
            states.unreadable.append(f"{label} (HTTP {resp.status_code})")
        elif (state := resp.json().get("state")) != "ENABLED":
            states.not_enabled[label] = state
    return states


def _get_token() -> str | None:
    """Return the access token, or None when there are no credentials."""
    try:
        return get_access_token()
    except Exception as exc:
        logging.debug("Validation checks have no access token", exc_info=exc)
        return None


def _check_apis(
    *, project: str, apis: tuple[str, ...], token: str
) -> tuple[CheckResult, list[str]]:
    """Return the check result and the APIs found disabled."""
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
        return _combined_result(
            "APIs enabled",
            failures=_describe_problem(
                prefix="not enabled",
                items=disabled,
                hint=f"enable with:{_DETAIL_INDENT}"
                f"gcloud services enable {' '.join(disabled)} --project={project}",
            ),
            warnings=_describe_problem(
                prefix="could not verify",
                items=[
                    *states.unreadable,
                    *(f"{api} (HTTP 404)" for api in states.not_found),
                ],
            ),
            passed=", ".join(apis),
        )

    return _run_check("APIs enabled", check), disabled


def _check_target_engine(
    *, project: str, region: str, display_name: str, update_only: bool
) -> tuple[CheckResult, str | None, bool]:
    """Find the engine the deploy will update, with the deploy's own lookup.

    Returns the result, the engine to update (None to create one), and whether
    the lookup gave an answer.
    """
    found: list[str] = []

    def check() -> CheckResult:
        client = AgentPlatformClient(project=project, location=region)
        found.extend(
            agent.api_resource.name
            for agent in _resolve_target_agent(client, display_name)
        )
        if found:
            return _pass_result("Target engine", f"updates {found[0]}")
        if update_only:
            return _fail_result(
                "Target engine",
                f"no engine named '{display_name}' in {project}/{region}, and "
                "--update-only forbids creating one",
            )
        return _pass_result(
            "Target engine", f"creates a new engine named '{display_name}'"
        )

    result = _run_check("Target engine", check)
    engine = found[0] if found else None
    return result, engine, result.status is CheckStatus.PASSED


def _deploy_permissions(
    *, creates: bool | None, update_only: bool, agent_identity: bool | None
) -> _Permissions:
    """The permissions the deploy needs. ``creates`` is None when it is unknown.

    When it is unknown, create and update only warn: the engine check has
    already warned that the lookup could not run. Agent Identity creates a new engine bare, updates it, and then edits the
    project IAM policy to grant the agent its roles. An existing engine edits
    the policy only when it moves to Agent Identity, so that only warns.
    """
    iam_policy = (_GET_PROJECT_IAM_POLICY_PERMISSION, _SET_PROJECT_IAM_POLICY_PERMISSION)
    if creates is None and not update_only:
        if agent_identity:
            return _Permissions(
                required=(_UPDATE_ENGINE_PERMISSION,),
                maybe_required=(_CREATE_ENGINE_PERMISSION, *iam_policy),
            )
        return _Permissions(
            maybe_required=(_CREATE_ENGINE_PERMISSION, _UPDATE_ENGINE_PERMISSION)
        )
    if creates and agent_identity:
        return _Permissions(
            required=(_CREATE_ENGINE_PERMISSION, _UPDATE_ENGINE_PERMISSION, *iam_policy)
        )
    if creates:
        return _Permissions(required=(_CREATE_ENGINE_PERMISSION,))
    return _Permissions(
        required=(_UPDATE_ENGINE_PERMISSION,),
        maybe_required=iam_policy if agent_identity else (),
    )


def _check_permissions(
    *,
    project: str,
    token: str,
    permissions: _Permissions,
    region: str,
    engine: str | None,
) -> CheckResult:
    """Test the permissions on the project.

    Update granted on ``engine`` itself also counts: Agent Runtime supports IAM
    on a single engine, so a user can hold update there and not on the project.
    """
    requested = permissions.required + permissions.maybe_required
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
    # testIamPermissions grants nothing on a project that does not exist.
    ask_admin = "ask a project admin for a role that grants it"
    if not granted:
        ask_admin = f"check that '{project}' is the right project ID, or {ask_admin}"
    engine_problem = None
    if engine and _UPDATE_ENGINE_PERMISSION in set(requested) - granted:
        engine_granted, engine_problem = _engine_grants(
            region=region, engine=engine, token=token
        )
        granted |= engine_granted
    unverified = [_UPDATE_ENGINE_PERMISSION] if engine_problem else []
    return _combined_result(
        "IAM permissions",
        failures=_describe_problem(
            prefix=f"missing on '{project}'",
            items=[
                p
                for p in permissions.required
                if p not in granted and p not in unverified
            ],
            hint=ask_admin,
        ),
        warnings=[
            *_describe_problem(
                prefix=f"missing on '{project}'",
                items=[p for p in permissions.maybe_required if p not in granted],
                hint="the deploy may fail if it needs these permissions",
            ),
            *_describe_problem(
                prefix=f"missing on '{project}'",
                items=unverified,
                hint="not granted on the project, and the engine could not be "
                f"checked ({engine_problem})",
            ),
        ],
        passed=f"{', '.join(requested)} granted",
    )


def _engine_grants(
    *, region: str, engine: str, token: str
) -> tuple[set[str], str | None]:
    """Return update if granted on ``engine``, and why the engine gave no answer."""
    try:
        resp = _call_api(
            url=f"https://{region}-aiplatform.googleapis.com/v1/{engine}:testIamPermissions",
            token=token,
            json={"permissions": [_UPDATE_ENGINE_PERMISSION]},
        )
    except Exception as exc:
        logging.debug("Engine permission check could not run", exc_info=exc)
        return set(), type(exc).__name__
    if not resp.ok:
        return set(), f"HTTP {resp.status_code}"
    return set(resp.json().get("permissions", [])), None


def _check_service_account(*, service_account: str, token: str) -> CheckResult:
    """Check that the account exists and that you may attach it to the agent.

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
            "Service account", f"{service_account} does not exist — check the email"
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


def _check_agent_runtime_secret_ids(*, project: str, secrets: str) -> CheckResult | None:
    """Fail on a ``--secrets`` value the deploy would reject, or return None.

    It needs no network call, so it runs even without credentials. Agent Runtime
    accepts only the bare ID of a secret in the deploy project, and its API
    rejects anything else, a path such as ``projects/p/secrets/s`` included.
    """
    try:
        specs = parse_secrets(secrets).values()
    except click.ClickException as exc:
        return _fail_result("Secrets", exc.format_message())
    invalid = [
        spec["secret"] for spec in specs if not _SECRET_ID_RE.fullmatch(spec["secret"])
    ]
    if not invalid:
        return None
    return _fail_result(
        "Secrets",
        _describe_problem(
            prefix="not a bare secret ID",
            items=invalid,
            hint="Agent Runtime only reads secrets from the deploy project; pass the "
            f"ID of a secret in '{project}' (letters, digits, - and _)",
        )[0],
    )


def _check_secret_versions(*, project: str, secrets: str, token: str) -> CheckResult:
    """Look up the exact secret *version* the deploy will mount.

    Reading the version catches a secret or version that does not exist, and a
    version that is disabled or destroyed. The deploy would only hit these at
    runtime. A 403 is not a failure: ``roles/secretmanager.secretAccessor`` lets
    a principal use the secret but not read its metadata, so a GET can be denied
    for a secret that works fine.
    """
    states = _read_states(
        urls={
            f"{spec['secret']}:{spec['version']}": "https://secretmanager.googleapis.com"
            f"/v1/projects/{project}/secrets/{spec['secret']}/versions/{spec['version']}"
            for spec in parse_secrets(secrets).values()
        },
        token=token,
    )
    return _combined_result(
        "Secrets",
        failures=[
            *_describe_problem(
                prefix=f"not found in '{project}'",
                items=states.not_found,
                hint="check the secret ID and version, or create them",
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
            items=states.unreadable,
            hint="the deploy still works if the runtime principal holds "
            "roles/secretmanager.secretAccessor",
        ),
        passed="all referenced secret versions exist and are enabled",
    )


def _report_results(results: list[CheckResult]) -> None:
    """Print the check results, and stop the command if a check failed."""
    for result in results:
        click.echo(f"  {_STATUS_ICONS[result.status]} {result.name}: {result.detail}")

    failures = [r for r in results if r.status is CheckStatus.FAILED]
    if failures:
        raise click.ClickException(
            f"Pre-deploy validation failed ({len(failures)} of {len(results)} checks): "
            f"{', '.join(r.name for r in failures)}.\n"
            "  Fix the items above, then run the deploy."
        )
    if any(r.status is CheckStatus.WARNED for r in results):
        click.echo("  ⚠️  Some checks only warned, so the deploy may still fail.")
