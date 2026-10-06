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

import backoff
import click
import requests

from google.agents.cli import _tools
from google.agents.cli._gcp_project import get_gcp_project_number
from google.agents.cli._project import find_project_root
from google.agents.cli._runner import popen_resolved, run
from google.agents.cli.auth import get_access_token
from google.agents.cli.deploy._utils import (
    MachineShape,
    parse_kv_flag,
    parse_secrets,
    print_table,
    read_project_dotenv,
    redact_command,
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


class _TransientCloudRunDeployError(click.ClickException):
    """A Cloud Run deploy failure expected to clear on retry (IAM propagation).

    Subclasses ClickException so that if every retry is exhausted, backoff
    re-raises it and the CLI still exits with a clean, actionable message.
    """


def deploy_cloud_run(
    *,
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
    update_env_vars: str | None,
    secrets: str | None,
    labels: dict[str, str],
    no_wait: bool,
    dry_run: bool,
) -> None:
    """Deploy with ``gcloud run deploy``, or print the command on dry run."""
    _tools.require_tool("gcloud")

    args = _cloud_run_base_args(
        service_name=service_name, project=project, region=region, image=image
    )
    creating = _is_cloud_run_create(
        dry_run=dry_run, project=project, region=region, service=service_name
    )
    args.extend(_cloud_run_shape_args(shape, creating=creating))
    args.extend(
        _cloud_run_service_args(
            timeout=timeout,
            ingress=ingress,
            port=port,
            iap=iap,
            service_account=service_account,
        )
    )

    env_var_map = _cloud_run_env_vars(
        project=project,
        region=region,
        service_name=service_name,
        update_env_vars=update_env_vars,
    )
    args.extend(_cloud_run_env_var_args(env_var_map))
    if secrets:
        args.extend(_cloud_run_secret_args(secrets, env_var_map))
    args.extend(_cloud_run_label_args(labels))

    if no_wait:
        args.append("--async")

    display_cmd = redact_command(args)
    if dry_run:
        click.echo(f"  Would run: {display_cmd}")
        return
    click.secho(f"  ▸ {display_cmd}", fg="cyan", dim=True)

    _run_cloud_run_deploy_with_retry(args, project=project)

    if not no_wait:
        _print_cloud_run_next_steps(
            service_name=service_name,
            region=region,
            project=project,
            service_url=env_var_map.get("APP_URL"),
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
    else:
        reason = ""
        for c in conditions:
            if c.get("type") == "Ready":
                reason = c.get("message", "")
                break
        click.echo(f"⏳ Cloud Run service '{service_name}' is not yet ready.")
        if reason:
            click.echo(f"   Reason: {reason}")


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
    *, service_name: str, project: str, region: str, image: str | None
) -> list[str]:
    """``gcloud run deploy`` with the target service and image or source."""
    args = ["gcloud", "run", "deploy", service_name, "--project", project]
    if region:
        args.extend(["--region", region])
    if image:
        args.extend(["--image", image])
    else:
        args.extend(["--source", "."])
    return args


def _is_cloud_run_create(
    *, dry_run: bool, project: str, region: str, service: str
) -> bool:
    """True if the deploy creates the service, False if it updates an existing one.

    Chooses create (apply our conservative defaults) vs. update (send only the
    flags the user set, letting gcloud preserve the rest). Dry run skips the
    network call and treats the deploy as a create for display. Uses a REST GET
    (Cloud Run Admin API v2) rather than `gcloud run services describe` to avoid
    ~2s of gcloud startup and the run_v2 dependency, mirroring the REST pattern in
    publish/cmd_publish.py.
    """
    if dry_run:
        return True
    url = (
        f"https://{region}-run.googleapis.com/v2/projects/{project}"
        f"/locations/{region}/services/{service}"
    )
    resp = requests.get(
        url, headers={"Authorization": f"Bearer {get_access_token()}"}, timeout=30
    )
    if resp.status_code == 404:
        return True
    resp.raise_for_status()
    return False


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
    if service_account:
        args.extend(["--service-account", service_account])
    return args


def _cloud_run_env_vars(
    *,
    project: str,
    region: str,
    service_name: str,
    update_env_vars: str | None,
) -> dict[str, str]:
    """Runtime env vars. Precedence: --update-env-vars > .env > defaults."""
    project_root = find_project_root() or "."
    env_var_map = read_project_dotenv(project_root)
    env_var_map.update(parse_kv_flag("--update-env-vars", update_env_vars))

    # Skip the version read (and its warning) when the user has already supplied one.
    if "AGENT_VERSION" not in env_var_map:
        env_var_map["AGENT_VERSION"] = get_project_version(project_root)
    # Fail closed: ADK defaults content-in-spans to true; keep it off for bare deploys.
    env_var_map.setdefault("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "false")

    # Set APP_URL so the service knows its own URL (used by A2A agent cards, etc.)
    if "APP_URL" not in env_var_map:
        app_url = _cloud_run_app_url(
            project=project, region=region, service_name=service_name
        )
        if app_url:
            env_var_map["APP_URL"] = app_url
    return env_var_map


def _cloud_run_app_url(*, project: str, region: str, service_name: str) -> str | None:
    """The service's deterministic ``run.app`` URL, or None if unresolvable."""
    project_number = get_gcp_project_number(project)
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


def _cloud_run_secret_args(secrets: str, env_var_map: dict[str, str]) -> list[str]:
    """``--update-secrets`` for ENV=SECRET[:VERSION] pairs (version defaults to latest).

    Uses --update-secrets (merge) to match the --update-env-vars semantics,
    rather than --set-secrets, which would drop any not listed here.
    """
    parsed_secrets = parse_secrets(secrets)
    overlap = parsed_secrets.keys() & env_var_map.keys()
    if overlap:
        raise click.ClickException(
            f"{', '.join(sorted(overlap))} cannot be set as both a plain "
            "environment variable and a secret. Cloud Run requires each key "
            "to be one or the other — rename it or drop it from --update-env-vars."
        )
    secret_str = ",".join(
        f"{env}={spec['secret']}:{spec['version']}"
        for env, spec in parsed_secrets.items()
    )
    return ["--update-secrets", secret_str]


def _cloud_run_label_args(labels: dict[str, str]) -> list[str]:
    """``--update-labels`` with the user labels plus the reserved ``created-by``."""
    # Merge user labels with the default ones, seeded LAST so
    # user-supplied ones can't override it.
    if labels.get("created-by") not in (None, "agents-cli"):
        logging.warning(
            "Ignoring --labels created-by=%s: 'created-by' is currently reserved.",
            labels["created-by"],
        )
    cr_labels = {**labels, "created-by": "agents-cli"}
    # --update-labels merges (preserves existing labels), consistent with
    # --update-env-vars / --update-secrets; emit exactly one flag.
    return ["--update-labels", ",".join(f"{k}={v}" for k, v in cr_labels.items())]


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
) -> None:
    """Print copy-pasteable next steps for talking to a deployed Cloud Run agent.

    gcloud already prints the Service URL and a proxy hint, but nothing about
    how to actually interact with the agent — which is exactly where users got
    stuck (b/557288939). ``agents-cli run`` handles the identity token that
    ``--no-allow-unauthenticated`` requires, so surface both the direct call
    (when the URL is known) and the local-proxy flow.
    """
    url = (service_url or "").rstrip("/")
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
    click.echo("\nTalk to your agent:")
    if url:
        click.echo(f'  agents-cli run --url {url} --mode a2a "hello"')
    else:
        click.echo('  agents-cli run --url <SERVICE_URL> --mode a2a "hello"')
        click.echo(
            "  (find <SERVICE_URL> in the Service URL above or via "
            "`agents-cli deploy --status`)"
        )
    click.echo("\nOr proxy locally, then use the proxy URL:")
    click.echo(f"  {' '.join(proxy_parts)}")
    click.echo('  agents-cli run --url http://127.0.0.1:8080 --mode a2a "hello"')
    click.echo(
        "\nFor ADK agents, the ADK HTTP API is also served — swap --mode a2a for --mode adk."
    )
