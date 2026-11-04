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

"""Cloud Run deployment for ``agents-cli deploy``."""

import json
import logging
import subprocess
import sys
from collections.abc import Mapping

import backoff
import click
import requests

from google.agents.cli import _tools
from google.agents.cli._gcp_project import (
    get_gcp_project_ancestors,
    get_gcp_project_number,
)
from google.agents.cli._project import (
    ProjectConfig,
    find_project_root,
    scaffold_older_than,
)
from google.agents.cli._runner import popen_resolved, run
from google.agents.cli.auth import get_access_token
from google.agents.cli.deploy._agent_identity import (
    AGENT_IDENTITY_ROLES,
    SECRET_ACCESSOR_ROLE,
    grant_roles_to_principal,
    grant_secret_access_to_principal,
    should_use_agent_identity,
)
from google.agents.cli.deploy._cloud_run_validation import validate_cloud_run_deployment
from google.agents.cli.deploy._utils import (
    KeyValueChanges,
    MachineShape,
    cloud_run_has_agent_identity,
    cloud_run_service_url,
    parse_secret_refs,
    print_table,
    redact_command,
    secret_resource_name,
)
from google.agents.cli.scaffold.utils.language import get_project_version

# Right after a project/repo/service is first created, the Cloud Run Service
# Agent often isn't yet allowed to pull the (cross-project) image, so the deploy
# fails with a 403 that gcloud itself flags as transient ("permissions might
# take a few minutes to propagate"). These clear on their own within minutes, so
# we retry them. Matching is intentionally narrow — we only match the image-pull
# propagation signatures so genuine permission misconfigurations (e.g. a deployer
# that permanently lacks a role) fail fast instead of burning the retry budget.
_CLOUD_RUN_TRANSIENT_DEPLOY_SIGNATURES = (
    "permissions might take a few minutes to propagate",
    "must have permission to read the image",
    "artifactregistry.repositories.downloadArtifacts",
)

# Retry budget for transient Cloud Run deploy failures. IAM propagation can take
# several minutes, so we retry until max_time with a capped exponential backoff:
# waits ramp 5s, 10s, 20s then hold at 30s (each full-jittered), i.e. steady
# ~30s polling until the deadline. max_tries is unset so max_time is the sole
# stop condition.
_CLOUD_RUN_DEPLOY_MAX_TIME = 600
_CLOUD_RUN_DEPLOY_BACKOFF_FACTOR = 5
_CLOUD_RUN_DEPLOY_BACKOFF_MAX_VALUE = 30

# The first version of Agents CLI that supports Agent Identity in Cloud Run deployments.
_CLOUD_RUN_AGENT_IDENTITY_VERSION = "1.13.0"


class _TransientCloudRunDeployError(click.ClickException):
    """A Cloud Run deploy failure expected to clear on retry (IAM propagation).

    Subclasses ClickException so that if every retry is exhausted, backoff
    re-raises it and the CLI still exits with a clean, actionable message.
    """


def deploy_cloud_run(
    *,
    cfg: ProjectConfig,
    project: str,
    region: str,
    service_name: str,
    image: str | None,
    shape: MachineShape,
    timeout: int | None,
    ingress: str | None,
    port: int | None,
    iap: bool,
    service_account: str | None,
    agent_identity: bool | None,
    env_var_changes: KeyValueChanges,
    secret_changes: KeyValueChanges,
    label_changes: KeyValueChanges,
    no_wait: bool,
    dry_run: bool,
) -> None:
    """Deploy with ``gcloud run deploy``, or print the command on dry run."""
    _tools.require_tool("gcloud")

    project_number = get_gcp_project_number(project)

    env_var_map = _cloud_run_env_vars(
        project=project,
        project_number=project_number,
        region=region,
        service_name=service_name,
        env_vars=env_var_changes.updates,
    )
    # Cloud Run merges secrets, so an empty --secrets (a clear on Agent Runtime)
    # sends nothing.
    secret_args = (
        _cloud_run_secret_args(secret_changes.updates, env_var_map)
        if secret_changes.updates
        else []
    )
    dry_run_validation_report, service_exists = None, None
    already_agent_identity = False
    if dry_run:
        dry_run_validation_report, service_exists = validate_cloud_run_deployment(
            project=project,
            region=region,
            service_name=service_name,
            service_account=service_account,
            agent_identity=agent_identity,
            identity_default=cfg.cloud_run_identity_default,
            secrets=secret_changes.updates,
            image=image,
            project_number=project_number,
        )
        creating = not service_exists
    else:
        existing_service = _get_cloud_run_service(
            project=project, region=region, service=service_name
        )
        creating = existing_service is None
        already_agent_identity = cloud_run_has_agent_identity(existing_service)

    # A new service follows the project's identity default when neither
    # --agent-identity nor --no-agent-identity is passed.
    agent_identity = should_use_agent_identity(
        agent_identity_flag=agent_identity,
        identity_default=cfg.cloud_run_identity_default,
        is_update=not creating,
    )
    if agent_identity:
        _warn_if_terraform_predates_agent_identity(cfg)
        if service_account:
            logging.warning(
                "Agent Identity overrides --service-account: the agent uses "
                "Agent Identity, so '%s' is ignored.",
                service_account,
            )
            service_account = None

    if agent_identity is False and already_agent_identity:
        raise click.ClickException(
            f"Cloud Run service '{service_name}' already runs with Agent "
            "Identity, which cannot be turned off: its functional type and "
            "identity type are immutable once set.\n"
            "  To go back to a service account, delete the service and "
            "redeploy without --agent-identity:\n"
            f"    gcloud run services delete {service_name} "
            f"--region {region} --project {project}"
        )
    if already_agent_identity and service_account:
        logging.warning(
            "Cloud Run service '%s' runs as its Agent Identity principal, so "
            "--service-account '%s' is only recorded and checked at deploy time; "
            "the agent doesn't run as it.",
            service_name,
            service_account,
        )

    args = _cloud_run_base_args(
        service_name=service_name,
        project=project,
        region=region,
        image=image,
        agent_identity=bool(agent_identity),
    )
    args.extend(_cloud_run_shape_args(shape, creating=creating))
    args.extend(
        _cloud_run_service_args(
            timeout=timeout,
            ingress=ingress,
            port=port,
            iap=iap,
            service_account=service_account,
            agent_identity=bool(agent_identity),
        )
    )
    args.extend(_cloud_run_env_var_args(env_var_map))
    args.extend(secret_args)
    args.extend(_cloud_run_label_args(label_changes))

    if no_wait:
        args.append("--async")

    display_cmd = redact_command(args)
    if dry_run_validation_report is not None:
        _print_cloud_run_plan(display_cmd, service_exists=service_exists, region=region)
        dry_run_validation_report.raise_if_failed()
        return

    agent_identity_principal = None
    if agent_identity or already_agent_identity:
        if already_agent_identity:
            # Only a revision carries the principal, so read it from the latest
            # ready one. None when there is none yet, e.g. after a failed first
            # deploy, and then it is built like for a new service.
            revision = (existing_service or {}).get("latestReadyRevision", "")
            if revision:
                agent_identity_principal = _get_agent_identity_principal(
                    project=project,
                    region=region,
                    service=service_name,
                    revision=revision.rpartition("/")[2],
                )
        if not agent_identity_principal:
            agent_identity_principal = _build_agent_identity_principal(
                project=project,
                project_number=project_number,
                region=region,
                service=service_name,
            )

        # Grant the roles upfront. This is allowed for Cloud Run Agent Identity
        # principals.
        if agent_identity_principal:
            # On a service that already has Agent Identity, the principal likely
            # already holds its roles, so a denied read only warns.
            grant_roles_to_principal(
                project,
                agent_identity_principal,
                denied_read_is_error=not already_agent_identity,
            )
        else:
            logging.warning(
                "Deploying with Agent Identity, but the agent's principal could "
                "not be worked out, so its baseline roles were not granted: %s.\n"
                "  Once the deploy finishes, `agents-cli deploy --status` prints "
                "the principal Cloud Run assigned; grant each role to it with "
                "`gcloud projects add-iam-policy-binding`.",
                ", ".join(AGENT_IDENTITY_ROLES),
            )

    # The principal reads the secrets when an instance starts. Cloud Run's deploy
    # check covers only the service account.
    if secret_changes.updates and (agent_identity or already_agent_identity):
        secrets = [
            secret_resource_name(project=project, secret=spec["secret"])
            for spec in parse_secret_refs(secret_changes.updates).values()
        ]
        if agent_identity_principal:
            grant_secret_access_to_principal(
                agent_identity_principal,
                secrets,
                denied_read_is_error=not already_agent_identity,
            )
        else:
            logging.warning(
                "Deploying with Agent Identity, but the agent's principal could "
                "not be worked out, so it was not granted %s on: %s.\n"
                "  The service can't start until it is. Once the deploy finishes, "
                "`agents-cli deploy --status` prints the principal; grant it with "
                "`gcloud secrets add-iam-policy-binding`.",
                SECRET_ACCESSOR_ROLE,
                ", ".join(dict.fromkeys(secrets)),
            )

    click.secho(f"  ▸ {display_cmd}", fg="cyan", dim=True)

    _run_cloud_run_deploy_with_retry(args, project=project)

    if not no_wait:
        _print_cloud_run_next_steps(
            service_name=service_name,
            region=region,
            project=project,
            service_url=env_var_map.get("APP_URL"),
            agent_identity_principal=agent_identity_principal,
            is_a2a=cfg.is_a2a,
        )


def _warn_if_terraform_predates_agent_identity(cfg: ProjectConfig) -> None:
    """Warn when the project's Terraform can't manage a service with Agent Identity."""
    if scaffold_older_than(cfg, _CLOUD_RUN_AGENT_IDENTITY_VERSION):
        logging.warning(
            "This project was scaffolded on an older version of Agents CLI that didn't "
            "support Agent Identity in Cloud Run deployments. Applying the Terraform "
            "configuration as-is might fail after this deployment. Upgrade your project "
            "with `agents-cli scaffold upgrade` to mitigate this problem.",
        )


def check_cloud_run_status(
    project: str,
    region: str,
    service_name: str,
) -> None:
    """Check the status of the Cloud Run service."""
    _tools.require_tool("gcloud")
    args = [
        "gcloud",
        "run",
        "services",
        "describe",
        service_name,
        "--format=json",
        "--project",
        project,
    ]
    if region:
        args.extend(["--region", region])

    result = run(args, capture=True, print_cmd=False, check=False)
    if result.returncode != 0:
        raise click.ClickException(
            f"Failed to describe Cloud Run service '{service_name}'.\n"
            "  The service may not exist yet or the deployment may have failed."
        )

    svc = json.loads(result.stdout)
    conditions = svc.get("status", {}).get("conditions", [])
    ready = any(
        c.get("type") == "Ready" and c.get("status") == "True" for c in conditions
    )

    if ready:
        url = svc.get("status", {}).get("url", "")
        click.echo(f"✅ Cloud Run service '{service_name}' is ready.")
        if url:
            click.echo(f"   URL: {url}")
        revision = svc.get("status", {}).get("latestReadyRevisionName")
        principal = (
            _get_agent_identity_principal(
                project=project, region=region, service=service_name, revision=revision
            )
            if revision
            else None
        )
        if principal:
            click.echo(f"   Agent Identity principal: {principal}")
    else:
        reason = ""
        for c in conditions:
            if c.get("type") == "Ready":
                reason = c.get("message", "")
                break
        click.echo(f"⏳ Cloud Run service '{service_name}' is not yet ready.")
        if reason:
            click.echo(f"   Reason: {reason}")


def _get_agent_identity_principal(
    *, project: str, region: str, service: str, revision: str
) -> str | None:
    """The principal a Cloud Run revision runs as, or None if it has no agent identity.

    Read rather than derived, so it reports what the service actually runs as.
    Only a revision carries the principal, and one running as a service account
    carries none. Returns None on any failure: this only annotates the status
    output, so it must not fail the command.
    """
    url = (
        f"https://{region}-run.googleapis.com/v2/projects/{project}"
        f"/locations/{region}/services/{service}/revisions/{revision}"
    )
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {get_access_token()}"},
            timeout=30,
        )
        resp.raise_for_status()
        identity = resp.json().get("workloadIdentityConfig", {}).get("identity")
    except (requests.RequestException, ValueError, RuntimeError) as e:
        logging.warning("Could not read the identity of revision %s: %s", revision, e)
        return None
    # The API returns the identity already prefixed with "//", so the IAM member
    # is "principal:" + identity, which renders as principal://agents.global...
    return f"principal:{identity}" if identity else None


def list_cloud_run_deployments(project: str, region: str | None) -> None:
    """List Cloud Run services via gcloud."""
    _tools.require_tool("gcloud")

    args = [
        "gcloud",
        "run",
        "services",
        "list",
        "--format=json",
        "--project",
        project,
    ]
    if region:
        args.extend(["--region", region])

    result = run(args, capture=True, print_cmd=False, check=False)
    if result.returncode != 0:
        raise click.ClickException("Failed to list Cloud Run services.")

    services = json.loads(result.stdout) if result.stdout.strip() else []

    if not services:
        location_label = f" in {region}" if region else ""
        click.echo(f"No Cloud Run services found{location_label} ({project}).")
        return

    from rich.table import Table

    title_parts = ["Cloud Run Services", f"— {project}"]
    if region:
        title_parts.append(f"({region})")
    table = Table(title=" ".join(title_parts))
    table.add_column("Service Name", style="bold")
    table.add_column("Region")
    table.add_column("URL", style="dim")
    table.add_column("Last Deployed")

    for svc in services:
        metadata = svc.get("metadata", {})
        status = svc.get("status", {})
        name = metadata.get("name", "—")
        labels = metadata.get("labels", {})
        svc_region = labels.get("cloud.googleapis.com/location", "—")
        url = status.get("url", "—")
        # Cloud Run uses metadata.creationTimestamp or status conditions
        conditions = status.get("conditions", [])
        ready_time = "—"
        for cond in conditions:
            if cond.get("type") == "Ready" and cond.get("lastTransitionTime"):
                ready_time = cond["lastTransitionTime"][:16].replace("T", " ")
                break
        table.add_row(name, svc_region, url, ready_time)

    print_table(table)


def _cloud_run_base_args(
    *,
    service_name: str,
    project: str,
    region: str,
    image: str | None,
    agent_identity: bool,
) -> list[str]:
    """``gcloud run deploy`` with the target service and image or source."""
    # --functional-type and --identity-type only exist on the beta track.
    track = ["gcloud", "beta"] if agent_identity else ["gcloud"]
    args = [*track, "run", "deploy", service_name, "--project", project]
    if region:
        args.extend(["--region", region])
    if image:
        args.extend(["--image", image])
    else:
        args.extend(["--source", "."])
    return args


def _get_cloud_run_service(
    *, project: str, region: str | None, service: str
) -> dict | None:
    """A Cloud Run service via the Admin API v2 GET, or None when it doesn't exist.

    Without a region, gcloud picks one, so the service can't be looked up and
    the deploy is treated as creating it.

    Uses a REST GET rather than `gcloud run services describe` to avoid ~2s of gcloud
    startup and the run_v2 dependency, mirroring the REST pattern in publish/cmd_publish.py.
    """
    if not region:
        return None
    url = cloud_run_service_url(project=project, region=region, service=service)
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {get_access_token()}"},
        timeout=30,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _build_agent_identity_principal(
    *, project: str, project_number: str | None, region: str, service: str
) -> str | None:
    """Build the IAM member Cloud Run gives this service's Agent Identity.

    Derived according to https://docs.cloud.google.com/run/docs/ai/authenticate-agents.

    Returns None without a project number, or if the ancestry can't be read.
    """
    if not project_number:
        return None
    ancestors = get_gcp_project_ancestors(project)
    if ancestors is None:
        return None
    organization = next(
        (a.id for a in ancestors if a.type == "organization"),
        None,
    )
    # A project outside an organization gets a project-scoped trust domain.
    trust_domain = f"org-{organization}" if organization else f"project-{project_number}"
    return (
        f"principal://agents.global.{trust_domain}.system.id.goog"
        f"/resources/run/projects/{project_number}/locations/{region}"
        f"/services/{service}"
    )


def _cloud_run_shape_args(shape: MachineShape, *, creating: bool) -> list[str]:
    """Sizing flags for ``gcloud run deploy``.

    User value always wins; on create fall back to our conservative defaults; on
    update omit so gcloud preserves the live value.
    """
    effective_shape = shape.with_defaults() if creating else shape
    args: list[str] = []
    for flag, value in (
        ("--memory", effective_shape.memory),
        ("--cpu", effective_shape.cpu),
        ("--min-instances", effective_shape.min_instances),
        ("--max-instances", effective_shape.max_instances),
        ("--concurrency", effective_shape.concurrency),
    ):
        if value is not None:
            args.extend([flag, str(value)])
    return args


def _cloud_run_service_args(
    *,
    timeout: int | None,
    ingress: str | None,
    port: int | None,
    iap: bool,
    service_account: str | None,
    agent_identity: bool,
) -> list[str]:
    """Access, networking, and identity flags for ``gcloud run deploy``."""
    args = ["--no-allow-unauthenticated", "--no-cpu-throttling"]
    if timeout is not None:
        args.extend(["--timeout", str(timeout)])
    if ingress:
        args.extend(["--ingress", ingress])
    if port:
        args.extend(["--port", str(port)])
    if iap:
        args.append("--iap")
    if agent_identity:
        args.extend(["--functional-type", "agent", "--identity-type", "agent-identity"])
    if service_account:
        args.extend(["--service-account", service_account])
    return args


def _cloud_run_env_vars(
    *,
    project: str,
    project_number: str | None,
    region: str,
    service_name: str,
    env_vars: Mapping[str, str],
) -> dict[str, str]:
    """Runtime env vars: ``env_vars`` (the project .env with --env-vars on top),
    then defaults."""
    project_root = find_project_root() or "."
    env_var_map = dict(env_vars)

    # Skip the version read (and its warning) when the user has already supplied one.
    if "AGENT_VERSION" not in env_var_map:
        env_var_map["AGENT_VERSION"] = get_project_version(project_root)
    # Fail closed: ADK defaults content-in-spans to true; keep it off for bare deploys.
    env_var_map.setdefault("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "false")

    # Set APP_URL so the service knows its own URL (used by A2A agent cards, etc.)
    if "APP_URL" not in env_var_map:
        app_url = _cloud_run_app_url(
            project=project,
            project_number=project_number,
            region=region,
            service_name=service_name,
        )
        if app_url:
            env_var_map["APP_URL"] = app_url
    return env_var_map


def _cloud_run_app_url(
    *, project: str, project_number: str | None, region: str, service_name: str
) -> str | None:
    """The service's deterministic ``run.app`` URL, or None if unresolvable."""
    if not project_number:
        logging.warning(
            "Could not determine the project number of %s — skipping APP_URL injection.",
            project,
        )
        return None
    return f"https://{service_name}-{project_number}.{region}.run.app"


def _cloud_run_env_var_args(env_var_map: dict[str, str]) -> list[str]:
    """``--update-env-vars`` for the runtime env vars."""
    return ["--update-env-vars", ",".join(f"{k}={v}" for k, v in env_var_map.items())]


def _cloud_run_secret_args(
    secrets: Mapping[str, str], env_var_map: dict[str, str]
) -> list[str]:
    """``--update-secrets`` for ENV=SECRET[:VERSION] pairs (version defaults to latest).

    Uses --update-secrets (merge) to match the --update-env-vars semantics,
    rather than --set-secrets, which would drop any not listed here.
    """
    parsed_secrets = parse_secret_refs(secrets)
    overlap = parsed_secrets.keys() & env_var_map.keys()
    if overlap:
        raise click.ClickException(
            f"{', '.join(sorted(overlap))} cannot be set as both a plain "
            "environment variable and a secret. Cloud Run requires each key "
            "to be one or the other — rename it or drop it from --env-vars."
        )
    secret_str = ",".join(
        f"{env}={spec['secret']}:{spec['version']}"
        for env, spec in parsed_secrets.items()
    )
    return ["--update-secrets", secret_str]


def _cloud_run_label_args(label_changes: KeyValueChanges) -> list[str]:
    """Label flags for gcloud, always re-applying the reserved ``created-by``.

    ``created-by`` is reserved, so a user can neither override its value via
    --labels nor drop it via --remove-labels / --clear-labels.
    """
    if label_changes.updates.get("created-by") not in (None, "agents-cli"):
        logging.warning(
            "Ignoring --labels created-by=%s: 'created-by' is currently reserved.",
            label_changes.updates["created-by"],
        )
    cr_remove_labels = list(label_changes.removals)
    if "created-by" in cr_remove_labels:
        cr_remove_labels.remove("created-by")
        logging.warning(
            "Ignoring --remove-labels created-by: 'created-by' is currently reserved."
        )

    # Emit removals before the additive --update-labels. gcloud applies
    # --clear-labels before --update-labels, so the user's --labels and the
    # reserved created-by we add below survive a clear. (--remove-labels and
    # --update-labels touch disjoint keys — a key can't be in both --labels
    # and --remove-labels — so their relative order doesn't matter.)
    args = []
    if label_changes.clear:
        args.append("--clear-labels")
    elif cr_remove_labels:
        args.extend(["--remove-labels", ",".join(cr_remove_labels)])

    cr_labels = {**label_changes.updates, "created-by": "agents-cli"}
    # --update-labels merges (preserves existing labels), consistent with
    # --update-env-vars / --update-secrets; emit exactly one flag.
    args.extend(["--update-labels", ",".join(f"{k}={v}" for k, v in cr_labels.items())])
    return args


def _print_cloud_run_plan(
    display_cmd: str, *, service_exists: bool | None, region: str | None
) -> None:
    """Print the command a dry run would run, and note a guessed create."""
    click.echo(f"  Would run: {display_cmd}")
    if service_exists is not None:
        return
    if region:
        sizing = (
            "If the service exists, the deploy leaves out --memory, --cpu, "
            "--min-instances, --max-instances and --concurrency, unless you set them."
        )
    else:
        sizing = (
            "With no region set, the deploy runs this command whether or not the "
            "service exists. On an existing service, it replaces the --memory, "
            "--cpu, --min-instances, --max-instances and --concurrency values."
        )
    click.echo(
        "  The dry run could not tell if the service exists, so this plan assumes "
        f"a new service. {sizing}"
    )


@backoff.on_exception(
    backoff.expo,
    _TransientCloudRunDeployError,
    factor=_CLOUD_RUN_DEPLOY_BACKOFF_FACTOR,
    max_value=_CLOUD_RUN_DEPLOY_BACKOFF_MAX_VALUE,
    max_tries=None,
    max_time=_CLOUD_RUN_DEPLOY_MAX_TIME,
    jitter=backoff.full_jitter,
    on_backoff=lambda details: logging.warning(
        "Cloud Run deploy hit a transient IAM-propagation error; retrying in "
        "%.0fs (attempt %d, %.0fs/%ds elapsed)...",
        details["wait"],
        details["tries"] + 1,  # the upcoming attempt; details['tries'] = ones done
        details["elapsed"],
        _CLOUD_RUN_DEPLOY_MAX_TIME,
    ),
)
def _run_cloud_run_deploy_with_retry(args: list[str], *, project: str) -> None:
    """Run ``gcloud run deploy``, streaming output, retrying transient IAM errors.

    Streams stderr to the terminal in real time (char by char, since gcloud
    renders progress with carriage returns rather than newlines) while capturing
    it for error classification. Raises:
      * ``_TransientCloudRunDeployError`` for cross-project IAM propagation
        failures, which backoff retries.
      * ``click.ClickException`` for all other failures, which fail fast.
    """
    process = popen_resolved(args, stderr=subprocess.PIPE, text=True)

    assert process.stderr is not None
    stderr_chars = []
    while True:
        char = process.stderr.read(1)
        if not char:
            break
        sys.stderr.write(char)
        sys.stderr.flush()
        stderr_chars.append(char)

    process.wait()

    if process.returncode == 0:
        return

    stderr = "".join(stderr_chars)
    if "SERVICE_DISABLED" in stderr:
        raise click.ClickException(
            "Cloud Run or Cloud Build API is not enabled.\n"
            "Please enable them by running:\n"
            f"  gcloud services enable cloudbuild.googleapis.com run.googleapis.com --project={project}"
        )
    if any(sig in stderr for sig in _CLOUD_RUN_TRANSIENT_DEPLOY_SIGNATURES):
        raise _TransientCloudRunDeployError(
            "Cloud Run deployment failed due to a transient IAM-propagation error "
            f"(exit code {process.returncode}). This usually clears within a few "
            "minutes after a project, repository, or service is first created."
        )
    raise click.ClickException(
        f"Cloud Run deployment failed (exit code {process.returncode})"
    )


def _print_cloud_run_next_steps(
    *,
    service_name: str,
    region: str,
    project: str,
    service_url: str | None,
    is_a2a: bool,
    agent_identity_principal: str | None = None,
) -> None:
    """Print copy-pasteable next steps for talking to a deployed Cloud Run agent.

    gcloud already prints the Service URL and a proxy hint, but nothing about
    how to actually interact with the agent — which is exactly where users got
    stuck (b/557288939). ``agents-cli run`` handles the identity token that
    ``--no-allow-unauthenticated`` requires, so surface both the direct call
    (when the URL is known) and the local-proxy flow.
    """
    url = (service_url or "").rstrip("/")
    # Projects without A2A (e.g. ADK TypeScript) only serve the ADK HTTP API.
    mode = "a2a" if is_a2a else "adk"
    proxy_parts = [
        "gcloud",
        "run",
        "services",
        "proxy",
        service_name,
        "--region",
        region,
        "--project",
        project,
    ]

    click.secho("\n✅ Deployed to Cloud Run.", fg="green")
    if agent_identity_principal:
        click.echo(f"\nAgent Identity principal: {agent_identity_principal}")
    click.echo("\nTalk to your agent:")
    if url:
        click.echo(f'  agents-cli run --url {url} --mode {mode} "hello"')
    else:
        click.echo(f'  agents-cli run --url <SERVICE_URL> --mode {mode} "hello"')
        click.echo(
            "  (find <SERVICE_URL> in the Service URL above or via "
            "`agents-cli deploy --status`)"
        )
    click.echo("\nOr proxy locally, then use the proxy URL:")
    click.echo(f"  {' '.join(proxy_parts)}")
    click.echo(f'  agents-cli run --url http://127.0.0.1:8080 --mode {mode} "hello"')
    if is_a2a:
        click.echo(
            "\nFor ADK agents, the ADK HTTP API is also served — swap --mode a2a for --mode adk."
        )
