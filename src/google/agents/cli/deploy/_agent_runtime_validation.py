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

"""Read-only checks that an Agent Runtime deploy's Google Cloud requests would succeed."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping

import click
from google.genai.errors import APIError

from google.agents.cli._agent_platform import AgentPlatformClient
from google.agents.cli.deploy._agent_identity import should_use_agent_identity
from google.agents.cli.deploy._cloud_validation import (
    _AGENT_RUNTIME_API,
    _GET_PROJECT_IAM_POLICY_PERMISSION,
    _SECRET_MANAGER_API,
    _SET_PROJECT_IAM_POLICY_PERMISSION,
    _VALIDATION_HEADER,
    CheckResult,
    CheckStatus,
    ValidationReport,
    _check_apis,
    _check_service_account,
    _describe_problem,
    _fail_result,
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
)
from google.agents.cli.deploy._utils import parse_secret_refs
from google.agents.cli.deploy.agent_runtime import _resolve_target_agent

_SECRET_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,255}")
_CREATE_ENGINE_PERMISSION = "aiplatform.reasoningEngines.create"
_UPDATE_ENGINE_PERMISSION = "aiplatform.reasoningEngines.update"


def validate_agent_runtime_deployment(
    *,
    project: str,
    region: str,
    display_name: str,
    service_account: str | None,
    secrets: Mapping[str, str],
    update_only: bool,
    agent_identity: bool | None,
    identity_type_default: str,
) -> ValidationReport:
    """Check what an Agent Runtime deploy needs, and print the results."""
    click.echo(_VALIDATION_HEADER)
    secret_ids_result = (
        _check_agent_runtime_secret_ids(project=project, secrets=secrets)
        if secrets
        else None
    )
    token = _get_token()
    if token is None:
        return _report_results(
            [*filter(None, [secret_ids_result]), _no_credentials_result()]
        )

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
    # None when the engine lookup could not run.
    will_create = (engine is None) if engine_known else None

    uses_agent_identity = should_use_agent_identity(
        agent_identity_flag=agent_identity,
        identity_default=identity_type_default,
        is_update=not will_create,
    )

    results.append(
        _iam_permissions_result(
            project=project,
            token=token,
            permissions=_agent_runtime_permissions(
                creates=will_create,
                update_only=update_only,
                agent_identity=uses_agent_identity,
            ),
            resource=_engine_iam_resource(region=region, engine=engine)
            if engine
            else None,
        )
    )

    # With Agent Identity the agent is its own principal and the deploy drops
    # --service-account. Checking that account would fail on one the deploy
    # never uses.
    if service_account and not uses_agent_identity:
        results.append(
            _run_check(
                "Service account",
                lambda: _check_service_account(
                    service_account=service_account, token=token
                ),
            )
        )

    if secret_ids_result:
        results.append(secret_ids_result)
    elif secrets:
        results.extend(
            _secret_version_checks(
                project=project,
                secrets=secrets,
                token=token,
                disabled_apis=disabled_apis,
                runtime_identity="the runtime principal",
            )
        )
    return _report_results(results)


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
        try:
            agents = _resolve_target_agent(client, display_name)
        except click.ClickException as exc:
            code = _transient_api_error_code(exc)
            if code is None:
                raise
            logging.debug("Target engine lookup returned HTTP %s", code, exc_info=exc)
            return _warn_result("Target engine", f"could not verify — HTTP {code}")
        found.extend(agent.api_resource.name for agent in agents)
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


def _transient_api_error_code(exc: click.ClickException) -> int | None:
    """The HTTP code when ``exc`` wraps a transient API error.

    ``APIError.code`` is None when the error response has no code.
    """
    cause = exc.__cause__
    if (
        isinstance(cause, APIError)
        and cause.code is not None
        and _is_transient_status(cause.code)
    ):
        return cause.code
    return None


def _agent_runtime_permissions(
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


def _engine_iam_resource(*, region: str, engine: str) -> _IamResource:
    return _IamResource(
        kind="engine",
        url=f"https://{region}-aiplatform.googleapis.com/v1/{engine}:testIamPermissions",
        permission=_UPDATE_ENGINE_PERMISSION,
    )


def _check_agent_runtime_secret_ids(
    *, project: str, secrets: Mapping[str, str]
) -> CheckResult | None:
    """Fail on a ``--secrets`` value the deploy would reject, or return None.

    It needs no network call, so it runs even without credentials. Agent Runtime
    accepts only the bare ID of a secret in the deploy project, and its API
    rejects anything else, a path such as ``projects/p/secrets/s`` included.
    """
    invalid = [
        spec["secret"]
        for spec in parse_secret_refs(secrets).values()
        if not _SECRET_ID_RE.fullmatch(spec["secret"])
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
