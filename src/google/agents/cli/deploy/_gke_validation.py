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

"""Read-only checks that a GKE deploy's Google Cloud requests would succeed."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import replace

import click

from google.agents.cli.auth import get_adc_credentials
from google.agents.cli.deploy._cloud_validation import (
    _AGENT_RUNTIME_API,
    _ARTIFACT_REGISTRY_API,
    _CLOUD_BUILD_API,
    _CLOUD_BUILD_PERMISSION,
    _DETAIL_INDENT,
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
    _combined_result,
    _fail_result,
    _gcloud_steps_identity,
    _get_token,
    _iam_permissions_result,
    _is_transient_status,
    _no_credentials_result,
    _pass_result,
    _Permissions,
    _report_results,
    _run_check,
    _warn_result,
    _with_api_message,
)
from google.agents.cli.infra._terraform import SINGLE_PROJECT_TF_DIR

_KUBERNETES_HINT = (
    "an RBAC role in the cluster can grant these instead; if the deploy fails, "
    "ask for roles/container.developer or an RBAC role"
)
_TERRAFORM_CREDENTIAL_VARIABLES = (
    "GOOGLE_CREDENTIALS",
    "GOOGLE_CLOUD_KEYFILE_JSON",
    "GCLOUD_KEYFILE_JSON",
    "GOOGLE_OAUTH_ACCESS_TOKEN",
    "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT",
)
_TERRAFORM_PROVIDER_CREDENTIALS_RE = re.compile(
    r'(?<![\w."])"?(credentials|access_token|impersonate_service_account|'
    r'external_credentials)(?:\s*=|"\s*:|\s*\{)'
)
_TERRAFORM_COMMENT_LINE_RE = re.compile(r"^[ \t]*(?:#|//).*$", re.MULTILINE)
_NO_USABLE_ADC = "no usable ADC"
_GKE_API = "container.googleapis.com"
_GET_CLUSTER_PERMISSION = "container.clusters.get"
_CREATE_CLUSTER_PERMISSION = "container.clusters.create"
_ENABLE_SERVICE_PERMISSION = "serviceusage.services.enable"
_GET_BUILD_PERMISSION = "cloudbuild.builds.get"
_GKE_TERRAFORM_APIS = (
    "serviceusage.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    _AGENT_RUNTIME_API,
    _CLOUD_BUILD_API,
    "bigquery.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "cloudtrace.googleapis.com",
    "telemetry.googleapis.com",
    "compute.googleapis.com",
    _GKE_API,
)
_GKE_CLOUD_SQL_APIS = ("sqladmin.googleapis.com", _SECRET_MANAGER_API)
_GKE_TERRAFORM_PERMISSIONS = (
    "serviceusage.services.use",
    "serviceusage.services.generateServiceIdentity",
    "compute.networks.create",
    "compute.subnetworks.create",
    "compute.firewalls.create",
    "compute.networks.updatePolicy",
    "compute.routers.create",
    "compute.routers.update",
    "artifactregistry.repositories.create",
    "storage.buckets.create",
    "iam.serviceAccounts.create",
    "iam.serviceAccounts.getIamPolicy",
    "iam.serviceAccounts.setIamPolicy",
    "resourcemanager.projects.get",
    _GET_PROJECT_IAM_POLICY_PERMISSION,
    _SET_PROJECT_IAM_POLICY_PERMISSION,
)
_GKE_CLOUD_SQL_PERMISSIONS = (
    "cloudsql.instances.create",
    "cloudsql.databases.create",
    "cloudsql.users.create",
    "secretmanager.secrets.create",
    "secretmanager.versions.add",
)
_GKE_TERRAFORM_KUBERNETES_PERMISSIONS = (
    "container.namespaces.create",
    "container.serviceAccounts.create",
    "container.services.create",
    "container.deployments.create",
    "container.horizontalPodAutoscalers.create",
    "container.podDisruptionBudgets.create",
)
_GKE_KUBECTL_PERMISSIONS = (
    "container.deployments.get",
    "container.deployments.update",
    "container.deployments.list",
)


def validate_gke_deployment(
    *,
    project: str,
    region: str,
    cluster_name: str,
    image: str | None,
    uses_cloud_sql: bool,
) -> ValidationReport:
    """Check what a GKE deploy needs, and print the results.

    Each check runs as the identity of its step: the gcloud account for gcloud,
    the ADC for Terraform, and for kubectl whichever of the two gcloud gives it.
    """
    click.echo(_VALIDATION_HEADER)
    token = _get_token()
    if token is None:
        return _report_results([_no_credentials_result()])
    identity = _gcloud_steps_identity(token, failed_checks="their failed checks")
    gcloud_check_token = identity.token
    other_gcloud_account = identity.other_account
    click.echo(f"  Checking the gcloud steps as {identity.checked_as}")
    token_is_adc = _is_adc_token(token)
    runs_terraform = image is None
    terraform_credential_source = (
        _terraform_credential_source(token_is_adc=token_is_adc)
        if runs_terraform
        else None
    )
    checks_terraform = runs_terraform and terraform_credential_source is None

    terraform_apis: tuple[str, ...] = ()
    if runs_terraform:
        terraform_apis = _GKE_TERRAFORM_APIS
        if uses_cloud_sql:
            terraform_apis += _GKE_CLOUD_SQL_APIS
        # Terraform does not enable Artifact Registry by name: it comes on with the
        # GKE and Cloud Build APIs, which depend on it, so it is not in terraform_apis.
        apis = (*terraform_apis, _ARTIFACT_REGISTRY_API)
        apis_result, disabled_apis = _check_apis(
            project=project,
            apis=apis,
            token=gcloud_check_token,
            enabled_by_deploy=apis,
            enabled_by_deploy_hint="during the deploy, Terraform enables any API "
            "that is off",
            passed=f"all {len(apis)} APIs that the deploy and its Terraform use",
        )
    else:
        apis_result, disabled_apis = _check_apis(
            project=project, apis=(_GKE_API,), token=gcloud_check_token
        )
    results = [apis_result]

    # When Terraform reads other credentials, gcloud is the first reader of the
    # cluster that the checks can run as.
    lookup_token = token if checks_terraform else gcloud_check_token
    cluster_exists: bool | None = None
    can_read_cluster = False
    if _GKE_API not in disabled_apis:
        cluster_result, cluster_exists, can_read_cluster = _check_gke_cluster(
            project=project,
            region=region,
            cluster_name=cluster_name,
            runs_terraform=runs_terraform,
            token=lookup_token,
            as_adc=checks_terraform
            and (token != gcloud_check_token or other_gcloud_account is not None),
            other_gcloud_account=None if checks_terraform else other_gcloud_account,
        )
        results.append(cluster_result)
    elif runs_terraform:
        cluster_exists = False
    gcloud_can_read_cluster = can_read_cluster
    if can_read_cluster and gcloud_check_token != lookup_token:
        _, _, gcloud_can_read_cluster = _check_gke_cluster(
            project=project,
            region=region,
            cluster_name=cluster_name,
            runs_terraform=runs_terraform,
            token=gcloud_check_token,
        )
    adc_can_read_cluster = can_read_cluster and token == lookup_token

    gcloud_permissions, adc_permissions = _gke_permissions(
        runs_terraform=runs_terraform,
        checks_terraform=checks_terraform,
        kubectl_uses_adc=token_is_adc and _kubectl_uses_adc(),
        other_gcloud_account=other_gcloud_account,
        cluster_exists=cluster_exists,
        gcloud_can_read_cluster=gcloud_can_read_cluster,
        adc_can_read_cluster=adc_can_read_cluster,
        enables_apis=any(api in disabled_apis for api in terraform_apis),
        uses_cloud_sql=uses_cloud_sql,
    )
    results.append(
        _check_gke_permissions(
            project=project,
            token=token,
            gcloud_token=gcloud_check_token,
            gcloud=gcloud_permissions,
            adc=adc_permissions,
            terraform_credential_source=terraform_credential_source,
        )
    )
    return _report_results(results)


def _is_adc_token(token: str) -> bool:
    """Whether ``token`` is the ADC, which Terraform reads too, and not the
    gcloud token that the checks fall back to."""
    try:
        credentials, _ = get_adc_credentials()
    except Exception as exc:
        logging.debug("Validation checks have no ADC", exc_info=exc)
        return False
    return credentials.token == token


def _terraform_credential_source(*, token_is_adc: bool) -> str | None:
    """Name where Terraform reads other credentials than the checks' ADC, or
    None when it reads the same ADC. Terraform ignores ``CLOUDSDK_CONFIG``, so
    it reads the ADC in the default gcloud folder.
    """
    if setting := _terraform_provider_credentials_setting():
        return setting
    for variable in _TERRAFORM_CREDENTIAL_VARIABLES:
        if os.environ.get(variable):
            return variable
    if os.environ.get("CLOUDSDK_CONFIG") and not os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS"
    ):
        return (
            "the ADC in ~/.config/gcloud (%APPDATA%\\gcloud on Windows; it ignores "
            "CLOUDSDK_CONFIG)"
        )
    return None if token_is_adc else _NO_USABLE_ADC


def _terraform_provider_credentials_setting() -> str | None:
    """Name the first credentials setting in a Terraform config file. Line
    comments are skipped, ``/* */`` ones are not: a false match only warns."""
    tf_files = [
        *SINGLE_PROJECT_TF_DIR.glob("*.tf"),
        *SINGLE_PROJECT_TF_DIR.glob("*.tf.json"),
    ]
    for tf_file in sorted(tf_files):
        try:
            text = tf_file.read_text(errors="replace")
        except OSError:
            continue
        if match := _TERRAFORM_PROVIDER_CREDENTIALS_RE.search(
            _TERRAFORM_COMMENT_LINE_RE.sub("", text)
        ):
            return f"the {match.group(1)} setting in {tf_file.as_posix()}"
    return None


def _check_gke_cluster(
    *,
    project: str,
    region: str,
    cluster_name: str,
    runs_terraform: bool,
    token: str,
    as_adc: bool = False,
    other_gcloud_account: str | None = None,
) -> tuple[CheckResult, bool | None, bool]:
    """Look up the cluster the deploy reads.

    Returns the result, whether the cluster exists (None when unknown), and
    whether ``token`` can read clusters: GKE answers 404 only to a caller that
    can. ``as_adc`` names the ADC in a failure. With ``other_gcloud_account``,
    the deploy reads the cluster as that account, so a rejection only warns.
    """
    exists: bool | None = None
    can_read = False
    as_who = " as the ADC" if as_adc else ""

    def check() -> CheckResult:
        nonlocal exists, can_read
        resp = _call_api(
            url=f"https://container.googleapis.com/v1/projects/{project}"
            f"/locations/{region}/clusters/{cluster_name}",
            token=token,
        )
        if resp.status_code == 404:
            exists, can_read = False, True
            missing = f"no cluster named '{cluster_name}' in {project}/{region}"
            if runs_terraform:
                return _warn_result(
                    "Target cluster",
                    f"{missing}; Terraform creates a cluster with the name and "
                    "region set in its variables — if they differ from "
                    "--cluster-name and --region, the deploy fails at "
                    "get-credentials",
                )
            return _fail_result(
                "Target cluster",
                f"{missing}, and --image skips the Terraform step that creates it — "
                "run the deploy without --image first, so that Terraform creates "
                "the cluster, or pass --cluster-name and --region of an existing "
                "cluster",
            )
        # gcloud can use a token that the API rejects, e.g. one bound to a certificate.
        if _is_transient_status(resp.status_code) or resp.status_code == 401:
            return _warn_result(
                "Target cluster", f"could not verify{as_who} — HTTP {resp.status_code}"
            )
        details = _api_error_field(resp, "details")
        reasons = (
            {d.get("reason") for d in details if isinstance(d, dict)}
            if isinstance(details, list)
            else set()
        )
        if resp.status_code == 403 and "SERVICE_DISABLED" in reasons:
            if runs_terraform:
                exists = False
                return _warn_result(
                    "Target cluster",
                    f"cannot look up the cluster while {_GKE_API} is not enabled; "
                    "Terraform creates one if its state has none",
                )
            return _fail_result(
                "Target cluster",
                f"{_GKE_API} is not enabled — enable with:{_DETAIL_INDENT}"
                f"gcloud services enable {_GKE_API} --project={project}",
            )
        if resp.status_code == 403 and "LOCATION_POLICY_VIOLATED" in reasons:
            return _fail_result(
                "Target cluster",
                _with_api_message(
                    f"GKE rejected region '{region}' (HTTP 403): it does not exist "
                    "or is not allowed in this project — check --region",
                    resp,
                ),
            )
        if not resp.ok:
            if other_gcloud_account and resp.status_code == 403:
                return _warn_result(
                    "Target cluster",
                    _with_api_message(
                        "GKE rejected the cluster lookup as the ADC (HTTP 403). gcloud "
                        f"reads this cluster as {other_gcloud_account}, which may have "
                        f"access — check that it has {_GET_CLUSTER_PERMISSION}, and "
                        "check --project, --region and --cluster-name",
                        resp,
                    ),
                )
            if other_gcloud_account:
                return _warn_result(
                    "Target cluster",
                    _with_api_message(
                        f"GKE rejected the cluster lookup as the ADC (HTTP "
                        f"{resp.status_code}) — check --project, --region and "
                        "--cluster-name",
                        resp,
                    ),
                )
            if as_adc:
                detail = (
                    f"GKE rejected the cluster lookup as the ADC (HTTP "
                    f"{resp.status_code}). Terraform reads this cluster as the ADC, "
                    "so the deploy would stop here — check --project, --region and "
                    f"--cluster-name, and that the ADC has {_GET_CLUSTER_PERMISSION}"
                )
            else:
                detail = (
                    f"GKE rejected the cluster lookup (HTTP {resp.status_code}). The "
                    "deploy reads this cluster too, so it would stop here — check "
                    "--project, --region and --cluster-name, and that you have "
                    f"{_GET_CLUSTER_PERMISSION}"
                )
            return _fail_result("Target cluster", _with_api_message(detail, resp))
        exists, can_read = True, True
        return _pass_result("Target cluster", f"deploys to cluster '{cluster_name}'")

    result = _run_check("Target cluster", check)
    return result, exists, can_read


def _kubectl_uses_adc() -> bool:
    """Whether gcloud sets up kubectl to run as the ADC. Only the environment
    variable is read: ``gcloud config`` would need one more gcloud call."""
    value = os.environ.get("CLOUDSDK_CONTAINER_USE_APPLICATION_DEFAULT_CREDENTIALS")
    return (value or "").lower() in ("1", "true", "yes", "on", "y")


def _gke_permissions(
    *,
    runs_terraform: bool,
    checks_terraform: bool,
    kubectl_uses_adc: bool,
    other_gcloud_account: str | None,
    cluster_exists: bool | None,
    gcloud_can_read_cluster: bool,
    adc_can_read_cluster: bool,
    enables_apis: bool,
    uses_cloud_sql: bool,
) -> tuple[_Permissions, _Permissions | None]:
    """The permissions to test as the gcloud account, and as the ADC (None when
    nothing checkable runs as the ADC)."""
    builds = (_CLOUD_BUILD_PERMISSION,) if runs_terraform else ()
    gcloud_required = (
        builds if gcloud_can_read_cluster else (_GET_CLUSTER_PERMISSION, *builds)
    )
    gcloud_maybe_required = (_GET_BUILD_PERMISSION,) if runs_terraform else ()
    kubectl = () if kubectl_uses_adc else _GKE_KUBECTL_PERMISSIONS
    if other_gcloud_account:
        gcloud = _Permissions(
            other_account=(*gcloud_required, *gcloud_maybe_required),
            other_account_kubernetes=kubectl,
            other_account_name=other_gcloud_account,
            kubernetes_hint=_KUBERNETES_HINT,
        )
    else:
        gcloud = _Permissions(
            required=gcloud_required,
            maybe_required=gcloud_maybe_required,
            kubernetes=kubectl,
            kubernetes_hint=_KUBERNETES_HINT,
        )
    adc_kubectl = _GKE_KUBECTL_PERMISSIONS if kubectl_uses_adc else ()
    if not checks_terraform:
        adc = _Permissions(kubernetes=adc_kubectl, kubernetes_hint=_KUBERNETES_HINT)
        return gcloud, adc if adc_kubectl else None
    required = () if adc_can_read_cluster else (_GET_CLUSTER_PERMISSION,)
    maybe_required: tuple[str, ...] = ()
    if enables_apis:
        required += (_ENABLE_SERVICE_PERMISSION,)
    else:
        maybe_required += (_ENABLE_SERVICE_PERMISSION,)
    if cluster_exists is False:
        required += (_CREATE_CLUSTER_PERMISSION,)
    else:
        maybe_required += (_CREATE_CLUSTER_PERMISSION,)
    maybe_required += _GKE_TERRAFORM_PERMISSIONS
    kubernetes = _GKE_TERRAFORM_KUBERNETES_PERMISSIONS
    if uses_cloud_sql:
        maybe_required += _GKE_CLOUD_SQL_PERMISSIONS
        kubernetes += ("container.secrets.create",)
    return gcloud, _Permissions(
        required=required,
        maybe_required=maybe_required,
        kubernetes=(*kubernetes, *adc_kubectl),
        kubernetes_hint=_KUBERNETES_HINT,
    )


def _check_gke_permissions(
    *,
    project: str,
    token: str,
    gcloud_token: str,
    gcloud: _Permissions,
    adc: _Permissions | None,
    terraform_credential_source: str | None,
) -> CheckResult:
    """Test each set of permissions as its identity, in one call when both use
    the same token, and label each part when the tokens differ. Terraform's are
    not checked when it reads other credentials."""
    two_identities = gcloud_token != token
    if adc and not two_identities:
        required = tuple(dict.fromkeys(gcloud.required + adc.required))
        gcloud = replace(
            gcloud,
            required=required,
            maybe_required=gcloud.maybe_required + adc.maybe_required,
            other_account=tuple(p for p in gcloud.other_account if p not in required),
            # kubectl runs after Terraform, so its permissions come last.
            kubernetes=adc.kubernetes + gcloud.kubernetes,
        )
        adc = None
    sections = []
    if gcloud.requested:
        sections.append(
            (
                "as the gcloud account",
                _iam_permissions_result(
                    project=project, token=gcloud_token, permissions=gcloud, resource=None
                ),
            )
        )
    if adc:
        sections.append(
            (
                "as the ADC",
                _iam_permissions_result(
                    project=project, token=token, permissions=adc, resource=None
                ),
            )
        )
    if terraform_credential_source:
        not_checked = (
            "could not check"
            if two_identities
            else "could not check the Terraform permissions"
        )
        reason = (
            "your Application Default Credentials do not work, and Terraform uses "
            "them by default — run `agents-cli login`, unless Terraform has other "
            "credentials"
            if terraform_credential_source == _NO_USABLE_ADC
            else f"Terraform reads its credentials from {terraform_credential_source} "
            "— make sure that identity can enable APIs and create the cluster"
        )
        sections.append(
            (
                "for Terraform",
                _warn_result("IAM permissions", f"{not_checked}, because {reason}"),
            )
        )
    parts = [
        (result, _labeled(label, result) if two_identities else result.detail)
        for label, result in sections
    ]
    return _combined_result(
        "IAM permissions",
        failures=[text for r, text in parts if r.status is CheckStatus.FAILED],
        warnings=[text for r, text in parts if r.status is CheckStatus.WARNED],
        passed=_DETAIL_INDENT.join(text for _, text in parts),
    )


def _labeled(label: str, result: CheckResult) -> str:
    return _DETAIL_INDENT.join(
        f"{label}: {line}" for line in result.detail.split(_DETAIL_INDENT)
    )
