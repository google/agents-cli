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

"""agents-cli deploy command — deploy the agent."""

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import backoff
import click
import requests

from google.agents.cli import _tools
from google.agents.cli._project import (
    ProjectConfig,
    chdir_project_root,
    check_cli_version,
    find_project_root,
    read_project_config,
    require_deployment_target,
    resolve_gcp_project,
)
from google.agents.cli._runner import popen_resolved, run, run_resolved
from google.agents.cli.auth import get_access_token
from google.agents.cli.deploy._utils import (
    DEFAULT_CONCURRENCY,
    DEFAULT_CPU,
    DEFAULT_MAX_INSTANCES,
    DEFAULT_MEMORY,
    DEFAULT_MIN_INSTANCES,
    parse_key_value_pairs,
    read_project_dotenv,
    redact_command,
    resolve_service_name,
)
from google.agents.cli.deploy.agent_runtime import (
    check_agent_runtime_operation,
    deploy_agent_runtime,
    parse_secrets,
)
from google.agents.cli.scaffold.utils.language import get_project_version


def _cloud_run_service_exists(*, project: str, region: str, service: str) -> bool:
    """True if a Cloud Run service already exists (Cloud Run Admin API v2 GET).

    Used to choose create (apply our conservative defaults) vs. update (send only
    the flags the user set, letting gcloud preserve the rest). Uses a REST GET
    rather than `gcloud run services describe` to avoid ~2s of gcloud startup and
    the run_v2 dependency, mirroring the REST pattern in publish/cmd_publish.py.
    """
    url = (
        f"https://{region}-run.googleapis.com/v2/projects/{project}"
        f"/locations/{region}/services/{service}"
    )
    resp = requests.get(
        url, headers={"Authorization": f"Bearer {get_access_token()}"}, timeout=30
    )
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    return True


def _build_psc_interface_config(
    *,
    network_attachment: str | None,
    dns_peering_domain: str | None,
    dns_peering_project: str | None,
    dns_peering_network: str | None,
) -> dict | None:
    """Build a PSC interface config dict from CLI flags.

    Returns None when no networking flags are set.
    Raises ClickException when DNS peering flags are used without --network-attachment.
    """
    has_dns_peering = any([dns_peering_domain, dns_peering_project, dns_peering_network])

    if not network_attachment and not has_dns_peering:
        return None

    if not network_attachment and has_dns_peering:
        raise click.ClickException(
            "--dns-peering-domain, --dns-peering-project, and --dns-peering-network "
            "require --network-attachment.\n"
            "  PSC DNS peering is only valid when a network attachment is configured."
        )

    config: dict = {"network_attachment": network_attachment}

    if has_dns_peering:
        if not all([dns_peering_domain, dns_peering_project, dns_peering_network]):
            missing = []
            if not dns_peering_domain:
                missing.append("--dns-peering-domain")
            if not dns_peering_project:
                missing.append("--dns-peering-project")
            if not dns_peering_network:
                missing.append("--dns-peering-network")
            raise click.ClickException(
                f"Incomplete DNS peering configuration — missing: {', '.join(missing)}.\n"
                "  All three flags (--dns-peering-domain, --dns-peering-project, "
                "--dns-peering-network) must be provided together."
            )
        config["dns_peering_configs"] = [
            {
                "domain": dns_peering_domain,
                "target_project": dns_peering_project,
                "target_network": dns_peering_network,
            }
        ]

    return config


def _load_deploy_config(
    deployment_target: str | None,
) -> tuple[ProjectConfig, bool]:
    """Resolve project config for a deploy.

    When --deployment-target is given, deploy can run without a manifest. A
    project root (when present) is chdir'd into because deploy builds from cwd
    (--source ., relative terraform dirs).

    Returns the config and whether a manifest was found; the caller warns about
    the fallback defaults (including the resolved service name) when it wasn't.
    """
    project_root = find_project_root()
    if project_root is None and deployment_target is None:
        raise click.ClickException(
            "No agents-cli-manifest.yaml found in the current directory or its parents.\n"
            "  Run this command from your project root, pass --deployment-target to\n"
            "  deploy without a manifest, or create a project first:\n"
            "    agents-cli create my-agent"
        )
    if project_root is not None:
        chdir_project_root(project_root)

    cfg = read_project_config()
    check_cli_version(cfg)
    if deployment_target:  # explicit flag overrides the manifest
        cfg.deployment_target = deployment_target
    require_deployment_target(cfg)

    return cfg, project_root is not None


def _resolve_deploy_service_name(
    cfg: ProjectConfig, service_name_override: str | None
) -> str:
    """Resolve the deployed service name, rejecting --service-name for GKE.

    GKE resource names (cluster, namespace, deployment, service, Artifact
    Registry repo) are owned by Terraform's var.project_name, which the CLI does
    not set at deploy time. An override would only rename the kubectl-side
    references, leaving them pointing at resources Terraform never created — so
    for GKE the override is rejected and the name stays pinned to the project.
    """
    if service_name_override and cfg.deployment_target == "gke":
        raise click.ClickException(
            "--service-name is not supported for GKE deployments.\n"
            "  GKE resource names are derived from the project name via "
            "Terraform (var.project_name) and cannot be overridden at deploy "
            "time.\n"
            "  Use Cloud Run or Agent Runtime to customize the service name."
        )
    return resolve_service_name(cfg, service_name_override)


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

# Retry budget for transient Cloud Run deploy failures. Propagation can take a
# few minutes, so we allow several attempts with exponential backoff + jitter.
_CLOUD_RUN_DEPLOY_MAX_TRIES = 5
_CLOUD_RUN_DEPLOY_MAX_TIME = 300


class _TransientCloudRunDeployError(click.ClickException):
    """A Cloud Run deploy failure expected to clear on retry (IAM propagation).

    Subclasses ClickException so that if every retry is exhausted, backoff
    re-raises it and the CLI still exits with a clean, actionable message.
    """


@backoff.on_exception(
    backoff.expo,
    _TransientCloudRunDeployError,
    max_tries=_CLOUD_RUN_DEPLOY_MAX_TRIES,
    max_time=_CLOUD_RUN_DEPLOY_MAX_TIME,
    jitter=backoff.full_jitter,
    on_backoff=lambda details: logging.warning(
        "Cloud Run deploy hit a transient IAM-propagation error; retrying in "
        "%.0fs (attempt %d/%d)...",
        details["wait"],
        details["tries"] + 1,  # the upcoming attempt; details['tries'] = ones done
        _CLOUD_RUN_DEPLOY_MAX_TRIES,
    ),
)
def _run_cloud_run_deploy_with_retry(args: list[str], *, project: str | None) -> None:
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


@click.command("deploy")
@click.option("--project", default=None, help="GCP project ID.")
@click.option("--region", default=None, help="GCP region.")
@click.option(
    "--deployment-target",
    "-d",
    type=click.Choice(["agent_runtime", "cloud_run", "gke"]),
    default=None,
    help="Deployment target. Overrides agents-cli-manifest.yaml and lets deploy "
    "run without a manifest.",
)
@click.option(
    "--secrets",
    default=None,
    help="Comma-separated ENV=SECRET or ENV=SECRET:VERSION pairs "
    "(Agent Runtime, Cloud Run).",
)
@click.option(
    "--agent-identity", is_flag=True, default=False, help="Enable agent identity."
)
@click.option(
    "--update-env-vars", default=None, help="Comma-separated KEY=VALUE env vars."
)
@click.option(
    "--iap",
    is_flag=True,
    default=False,
    help="Enable Identity-Aware Proxy (Cloud Run).",
)
@click.option(
    "--port",
    default=None,
    type=int,
    help="Container port (Cloud Run / Agent Runtime).",
)
@click.option(
    "--memory",
    default=None,
    help=f"Memory limit (Agent Runtime, Cloud Run). Default: {DEFAULT_MEMORY}.",
)
# --cpu is a string (not int): CPU values may be fractional/suffixed.
@click.option(
    "--cpu",
    default=None,
    help=f"CPU limit (Agent Runtime, Cloud Run). Default: {DEFAULT_CPU}.",
)
@click.option(
    "--min-instances",
    default=None,
    type=int,
    help="Minimum number of instances (Agent Runtime, Cloud Run). "
    f"Default: {DEFAULT_MIN_INSTANCES}.",
)
@click.option(
    "--max-instances",
    default=None,
    type=int,
    help="Maximum number of instances (Agent Runtime, Cloud Run). "
    f"Default: {DEFAULT_MAX_INSTANCES}.",
)
@click.option(
    "--concurrency",
    default=None,
    type=int,
    help="Concurrent requests per container (Agent Runtime, Cloud Run). "
    f"Default: {DEFAULT_CONCURRENCY}.",
)
@click.option("--service-account", default=None, help="Service account email.")
@click.option(
    "--service-name",
    "service_name_override",
    default=None,
    help="Override the deployed service name (Cloud Run service or Agent Runtime "
    "display name); defaults to the project name. Not supported for GKE. If you "
    "override it, consider updating your Terraform and CI (if present) — they "
    "derive resource names from the project name.",
)
@click.option(
    "--image",
    default=None,
    help="Container image URI (Cloud Run / GKE). Skips source build.",
)
@click.option(
    "--build-args",
    default=None,
    help="Comma-separated KEY=VALUE args passed to the container image build "
    "(Agent Runtime).",
)
@click.option(
    "--cluster-name",
    default=None,
    help="Cluster name (GKE).",
)
@click.option(
    "--dry-run",
    "--dryrun",
    "-n",
    is_flag=True,
    default=False,
    help="Print what would be executed without running it.",
)
@click.option(
    "--list",
    "list_deployments",
    is_flag=True,
    default=False,
    help="List existing deployments and exit.",
)
@click.option(
    "--no-wait",
    "no_wait",
    is_flag=True,
    default=False,
    help="Start the deployment and return immediately.",
)
@click.option(
    "--status",
    "status",
    is_flag=True,
    default=False,
    help="Check the status of a pending --no-wait deployment.",
)
@click.option(
    "--interactive",
    "-i",
    is_flag=True,
    default=False,
    help="Enable interactive prompts for underlying tooling (gcloud, etc).",
)
@click.option(
    "--no-confirm-project",
    is_flag=True,
    default=False,
    help="Skip project confirmation prompt.",
)
@click.option(
    "--network-attachment",
    default=None,
    help="Network attachment resource name for PSC interface (Agent Runtime). "
    "Enables private VPC connectivity. "
    "Format: projects/PROJECT/regions/REGION/networkAttachments/NAME",
)
@click.option(
    "--dns-peering-domain",
    default=None,
    help="DNS peering domain suffix, e.g. 'my-internal.corp.' (Agent Runtime, requires --network-attachment).",
)
@click.option(
    "--dns-peering-project",
    default=None,
    help="Project ID hosting the Cloud DNS managed zone for DNS peering (Agent Runtime, requires --network-attachment).",
)
@click.option(
    "--dns-peering-network",
    default=None,
    help="VPC network name in the target project for DNS peering (Agent Runtime, requires --network-attachment).",
)
def cmd_deploy(
    *,
    project,
    region,
    deployment_target,
    secrets,
    agent_identity,
    update_env_vars,
    iap,
    port,
    memory,
    cpu,
    min_instances,
    max_instances,
    concurrency,
    service_account,
    service_name_override,
    image,
    cluster_name,
    dry_run,
    list_deployments,
    no_wait,
    status,
    interactive,
    no_confirm_project,
    network_attachment,
    dns_peering_domain,
    dns_peering_project,
    dns_peering_network,
    build_args,
):
    """Deploy the agent.

    \b
    Dispatches by deployment target configured in agents-cli-manifest.yaml:
      agent_runtime → Agent Runtime deployment
      cloud_run    → gcloud beta run deploy
      gke          → terraform + docker build + kubectl apply

    \b
    Pass --deployment-target to override the manifest, or to deploy without a
    manifest (e.g. from a built container or CI):
      agents-cli deploy --deployment-target cloud_run

    \b
    Use --list to show existing deployments:
      agents-cli deploy --list

    \b
    Use --no-wait to start a deployment and return immediately:
      agents-cli deploy --no-wait

    \b
    Use --status to check on a --no-wait deployment:
      agents-cli deploy --status
    """
    cfg, has_manifest = _load_deploy_config(deployment_target)

    region = region or cfg.region
    service_name = _resolve_deploy_service_name(cfg, service_name_override)

    if not has_manifest:
        # No manifest: surface the defaults in play — including the resolved
        # service name — and the cwd we're building from.
        logging.warning(
            "No agents-cli-manifest.yaml found — deploying with defaults:\n"
            "    • service name:    %s\n"
            "    • agent directory: %s\n"
            "    • building from:   %s\n"
            "  Pass --service-name to set the service name, run from a scaffolded "
            "project, or see `agents-cli deploy --help` for all flags.",
            service_name,
            cfg.agent_directory,
            os.getcwd(),
        )

    project_explicitly_passed = bool(project)
    # Resolve project once upfront — all deployment targets need it
    project = resolve_gcp_project(project, required=True)

    if status:
        _check_deploy_status(cfg, project, region, service_name)
        return

    if list_deployments:
        _list_deployments(cfg, project, region)
        return

    # Prompt for confirmation if project was resolved automatically and not skipping
    confirm_project = not project_explicitly_passed and not no_confirm_project
    if confirm_project:
        if not interactive:
            raise click.ClickException(
                f"About to deploy to Google Cloud project '{project}' (resolved from `gcloud config`) — confirmation required.\n"
                "  To proceed, either:\n"
                f"    • Pass it explicitly:    --project {project}\n"
                "    • Skip the prompt:       --no-confirm-project\n"
                "    • Run interactively:     -i"
            )
        if not click.confirm(
            f"Deploying to Google Cloud project '{project}'. Proceed?", default=True
        ):
            raise click.ClickException("Aborted by user.")

    # Build PSC interface config from networking flags
    psc_interface_config = _build_psc_interface_config(
        network_attachment=network_attachment,
        dns_peering_domain=dns_peering_domain,
        dns_peering_project=dns_peering_project,
        dns_peering_network=dns_peering_network,
    )

    if psc_interface_config and cfg.deployment_target != "agent_runtime":
        raise click.ClickException(
            "--network-attachment and --dns-peering-* flags are only supported "
            f"for Agent Runtime deployments (current target: {cfg.deployment_target})."
        )

    if secrets and cfg.deployment_target not in ("agent_runtime", "cloud_run"):
        raise click.ClickException(
            "--secrets is only supported for Agent Runtime and Cloud Run deployments "
            f"(current target: {cfg.deployment_target}).\n"
            "  For GKE, mount secrets via Kubernetes Secrets or the Secret Manager "
            "CSI driver."
        )

    # Container-build flags split by target: Agent Runtime builds from the
    # project Dockerfile (--build-args); Cloud Run / GKE take a prebuilt --image.
    if build_args and cfg.deployment_target != "agent_runtime":
        raise click.ClickException(
            "The --build-args flag is only supported for Agent Runtime deployments."
        )
    if image and cfg.deployment_target == "agent_runtime":
        raise click.ClickException(
            "The --image flag is only supported for Cloud Run and GKE deployments. "
            "Agent Runtime does not support prebuilt images."
        )

    # CPU / memory / instance / concurrency sizing works on Agent Runtime and
    # Cloud Run, but on GKE these are configured via Terraform and the
    # HorizontalPodAutoscaler — reject them rather than silently ignoring.
    if cfg.deployment_target == "gke":
        gke_unsupported = {
            "--cpu": cpu,
            "--memory": memory,
            "--min-instances": min_instances,
            "--max-instances": max_instances,
            "--concurrency": concurrency,
        }
        misused = [flag for flag, value in gke_unsupported.items() if value is not None]
        if misused:
            raise click.ClickException(
                f"{', '.join(misused)} {'is' if len(misused) == 1 else 'are'} not "
                "supported for GKE deployments — configure sizing via Terraform and "
                "the HorizontalPodAutoscaler under deployment/terraform/."
            )

    # The sizing flags (cpu/memory/min/max/concurrency) are left as-is (possibly
    # None) so each deployment target can decide how to handle unset values:
    #   - Agent Runtime: pass None → FieldMask omits the field → preserves on update
    #   - Cloud Run: apply defaults on create, omit on update (see _shape_flag below)
    if cfg.deployment_target == "agent_runtime":
        if dry_run:
            # Fill defaults FOR DISPLAY ONLY — the real call passes raw (possibly-None)
            # values so Agent Runtime's FieldMask can preserve existing settings on update.
            runtime_shape = {
                "cpu": cpu if cpu is not None else DEFAULT_CPU,
                "memory": memory if memory is not None else DEFAULT_MEMORY,
                "min_instances": min_instances
                if min_instances is not None
                else DEFAULT_MIN_INSTANCES,
                "max_instances": max_instances
                if max_instances is not None
                else DEFAULT_MAX_INSTANCES,
                "container_concurrency": concurrency
                if concurrency is not None
                else DEFAULT_CONCURRENCY,
            }
            msg = f"  Would deploy to Agent Runtime: project={project}, region={region}"
            for key, value in runtime_shape.items():
                msg += f"\n  {key}: {value}"
            msg += "\n  (defaults apply on create; existing values preserved on update)"
            if psc_interface_config:
                msg += f"\n  PSC network attachment: {psc_interface_config['network_attachment']}"
                for dc in psc_interface_config.get("dns_peering_configs", []):
                    msg += (
                        f"\n  DNS peering: {dc['domain']}"
                        f" → {dc['target_project']}/{dc['target_network']}"
                    )
            click.echo(msg)
            return
        deploy_agent_runtime(
            cfg=cfg,
            project=project,
            location=region,
            display_name=service_name,
            set_env_vars=update_env_vars,
            set_secrets=secrets,
            service_account=service_account,
            agent_identity=agent_identity,
            no_wait=no_wait,
            psc_interface_config=psc_interface_config,
            build_args=build_args,
            port=port,
            cpu=cpu,
            memory=memory,
            min_instances=min_instances,
            max_instances=max_instances,
            container_concurrency=concurrency,
        )

    elif cfg.deployment_target == "cloud_run":
        _tools.require_tool(
            "gcloud",
            "Install the Google Cloud SDK: https://cloud.google.com/sdk/docs/install",
        )

        args = ["gcloud", "run", "deploy", service_name]
        if project:
            args.extend(["--project", project])
        if region:
            args.extend(["--region", region])
        if image:
            args.extend(["--image", image])
        else:
            args.extend(["--source", "."])

        # For sizing flags: user value always wins; on create fall back to our
        # conservative defaults; on update omit so gcloud preserves the live value.
        # Dry-run skips the network call and treats the deploy as a create for display.
        creating = (
            True
            if dry_run
            else not _cloud_run_service_exists(
                project=project, region=region, service=service_name
            )
        )

        def _shape_flag(flag: str, value, default):
            if value is not None:
                args.extend([flag, str(value)])
            elif creating:
                args.extend([flag, str(default)])

        _shape_flag("--memory", memory, DEFAULT_MEMORY)
        _shape_flag("--cpu", cpu, DEFAULT_CPU)
        _shape_flag("--min-instances", min_instances, DEFAULT_MIN_INSTANCES)
        _shape_flag("--max-instances", max_instances, DEFAULT_MAX_INSTANCES)
        _shape_flag("--concurrency", concurrency, DEFAULT_CONCURRENCY)
        args.append("--no-allow-unauthenticated")
        args.append("--no-cpu-throttling")
        if port:
            args.extend(["--port", str(port)])
        if iap:
            args.append("--iap")
        if service_account:
            args.extend(["--service-account", service_account])

        # Inject environment variables. Precedence: --update-env-vars > .env > defaults.
        project_root = find_project_root() or "."
        env_var_map = read_project_dotenv(project_root)
        env_var_map.update(parse_key_value_pairs(update_env_vars))
        env_var_map.setdefault("AGENT_VERSION", get_project_version(project_root))

        # Set APP_URL so the service knows its own URL (used by A2A agent cards, etc.)
        if "APP_URL" not in env_var_map and project:
            try:
                result = run_resolved(
                    [
                        "gcloud",
                        "projects",
                        "describe",
                        project,
                        "--format=value(projectNumber)",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                project_number = result.stdout.strip()
                env_var_map["APP_URL"] = (
                    f"https://{service_name}-{project_number}.{region}.run.app"
                )
            except (subprocess.CalledProcessError, OSError):
                click.echo(
                    "  ⚠️  Could not determine project number — skipping APP_URL injection."
                )
        env_var_str = ",".join(f"{k}={v}" for k, v in env_var_map.items())
        args.extend(["--update-env-vars", env_var_str])

        # Mount secrets as env vars (ENV=SECRET[:VERSION], version defaults to latest).
        # Use --update-secrets (merge) to match the --update-env-vars semantics above,
        # rather than --set-secrets, which would drop any not listed here.
        if secrets:
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
            args.extend(["--update-secrets", secret_str])

        # Add default labels
        args.extend(["--labels", "created-by=adk"])

        if no_wait:
            args.append("--async")

        display_cmd = redact_command(args)
        if dry_run:
            click.echo(f"  Would run: {display_cmd}")
            return
        click.secho(f"  ▸ {display_cmd}", fg="cyan", dim=True)

        _run_cloud_run_deploy_with_retry(args, project=project)

    elif cfg.deployment_target == "gke":
        if no_wait:
            raise click.ClickException("--no-wait is not supported for GKE deployments.")
        _deploy_gke(
            project=project,
            region=region,
            image=image,
            cluster_name=cluster_name,
            update_env_vars=update_env_vars,
            dry_run=dry_run,
            service_name=service_name,
        )

    else:
        raise click.ClickException(
            f"Unknown deployment target: {cfg.deployment_target}. "
            "Set deployment_target in agents-cli-manifest.yaml."
        )


def _enable_cloud_run_apis(project: str | None) -> None:
    """Enable Cloud Build and Cloud Run APIs, then retry the deployment."""
    click.echo("Enabling required APIs (Cloud Build, Cloud Run)...")
    enable_base = ["gcloud", "services", "enable"]
    if project:
        enable_base.extend(["--project", project])
    for api in ("cloudbuild.googleapis.com", "run.googleapis.com"):
        run([*enable_base, api], capture=True, print_cmd=False, check=False)


def _check_deploy_status(
    cfg: ProjectConfig,
    project: str,
    region: str,
    service_name: str,
) -> None:
    """Check the status of a pending --no-wait deployment."""
    if cfg.deployment_target == "agent_runtime":
        check_agent_runtime_operation(
            cfg=cfg,
            project=project,
            location=region,
        )
    elif cfg.deployment_target == "cloud_run":
        _check_cloud_run_status(project, region, service_name)
    elif cfg.deployment_target == "gke":
        raise click.ClickException("--status is not supported for GKE deployments.")
    else:
        raise click.ClickException(f"Unknown deployment target: {cfg.deployment_target}")


def _check_cloud_run_status(
    project: str | None,
    region: str,
    service_name: str,
) -> None:
    """Check the status of the Cloud Run service."""
    _tools.require_tool(
        "gcloud",
        "Install the Google Cloud SDK: https://cloud.google.com/sdk/docs/install",
    )
    args = [
        "gcloud",
        "run",
        "services",
        "describe",
        service_name,
        "--format=json",
    ]
    if project:
        args.extend(["--project", project])
    if region:
        args.extend(["--region", region])

    result = run(args, capture=True, print_cmd=False, check=False)
    if result.returncode != 0:
        raise click.ClickException(
            f"Failed to describe Cloud Run service '{service_name}'.\n"
            "  The service may not exist yet or the deployment may have failed."
        )

    import json

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


def _build_image_with_cloud_build(*, image: str, project: str) -> None:
    """Build and push an image with Cloud Build, polling for the result.

    Submits with ``--async`` and polls ``builds describe`` instead of streaming
    logs. gcloud's default log streaming exits non-zero when the caller can't
    read the default logs bucket (e.g. under VPC-SC, or without project Viewer)
    even though the build itself succeeds; polling works in every environment.
    The build is viewable at the printed console URL.
    """
    submit = run(
        [
            "gcloud",
            "builds",
            "submit",
            "--tag",
            image,
            "--project",
            project,
            "--async",
            "--format=value(id)",
        ],
        capture=True,
        check_err_msg="Failed to submit Cloud Build",
    )
    build_id = (submit.stdout or "").strip()
    if not build_id:
        raise click.ClickException("Cloud Build did not return a build ID.")
    build_url = f"https://console.cloud.google.com/cloud-build/builds/{build_id}?project={project}"
    click.echo(f"  Build {build_id} — logs: {build_url}")

    deadline = time.monotonic() + 1800  # 30 min safety cap
    while True:
        if time.monotonic() > deadline:
            raise click.ClickException(
                f"Timed out waiting for Cloud Build {build_id}. See {build_url}"
            )
        time.sleep(5)
        status = run(
            [
                "gcloud",
                "builds",
                "describe",
                build_id,
                "--project",
                project,
                "--format=value(status)",
            ],
            capture=True,
            print_cmd=False,
            check=False,
        ).stdout.strip()
        if status == "SUCCESS":
            click.echo("  ✅ Build succeeded.")
            return
        if status in {"FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED", "EXPIRED"}:
            raise click.ClickException(
                f"Cloud Build {build_id} ended with status {status}. See {build_url}"
            )


def _deploy_gke(
    *,
    project,
    region,
    image,
    cluster_name,
    update_env_vars,
    dry_run,
    service_name,
):
    """GKE deployment: single linear flow with conditional steps.

    When ``image`` is provided (CI/CD mode), skips terraform and docker build.
    When ``image`` is None (local dev mode), runs targeted terraform + build flow.
    Both paths share cluster credentials, kubectl rollout, env-var injection
    (AGENT_VERSION, any --update-env-vars, and APP_URL), and external IP steps.
    """
    deploy_targets = [
        "google_container_cluster.app",
        "google_artifact_registry_repository.docker_repo",
        "google_compute_router_nat.nat",
        "google_compute_firewall.allow_internal",
        "google_service_account.app_sa",
        "google_project_iam_member.app_sa_roles",
        "google_project_iam_member.default_compute_sa_storage_object_creator",
        "google_service_account_iam_member.workload_identity_binding",
        "kubernetes_namespace_v1.app",
        "kubernetes_service_account_v1.app",
        "kubernetes_deployment_v1.app",
        "kubernetes_service_v1.app",
        "kubernetes_horizontal_pod_autoscaler_v2.app",
        "kubernetes_pod_disruption_budget_v1.app",
    ]
    _tools.require_tool(
        "gcloud",
        "Install the Google Cloud SDK: https://cloud.google.com/sdk/docs/install",
    )
    _tools.require_tool(
        "kubectl", "Install kubectl: https://kubernetes.io/docs/tasks/tools/"
    )
    cluster_name = cluster_name or service_name

    if not image:
        _tools.require_tool(
            "terraform",
            "Install Terraform: https://developer.hashicorp.com/terraform/install",
        )

    if dry_run:
        if not image:
            tf_dir = "deployment/terraform/single-project"
            click.echo(f"  Would run: terraform -chdir={tf_dir} init")
            click.echo(
                f"  Would run: terraform -chdir={tf_dir} apply -auto-approve"
                f" -target=({len(deploy_targets)} targets)"
            )
            click.echo(
                "  Would run: gcloud builds submit --tag ... --async (then poll status)"
            )
        click.echo("  Would run: gcloud container clusters get-credentials ...")
        click.echo(
            f"  Would run: kubectl set image ... {image or f'{region}-docker.pkg.dev/{project}/{service_name}/{service_name}:latest'}"
        )
        click.echo("  Would run: kubectl get svc ... (service IP)")
        click.echo("  Would run: kubectl set env ... AGENT_VERSION=... APP_URL=...")
        click.echo("  Would run: kubectl rollout status ...")
        return

    # Step 1: Targeted Terraform (local dev only)
    if not image:
        tf_dir = "deployment/terraform/single-project"
        click.echo("\n🏗️  Provisioning infrastructure with Terraform...")
        run(
            ["terraform", f"-chdir={tf_dir}", "init"],
            check_err_msg="Terraform init failed",
        )
        apply_args = [
            "terraform",
            f"-chdir={tf_dir}",
            "apply",
            "-auto-approve",
            f"-var=project_id={project}",
        ]
        for target in deploy_targets:
            apply_args.extend(["-target", target])
        run(apply_args, check_err_msg="Terraform apply failed")

    # Step 2: Get cluster credentials
    click.echo("\n🔑 Getting cluster credentials...")
    run(
        [
            "gcloud",
            "container",
            "clusters",
            "get-credentials",
            cluster_name,
            "--region",
            region,
            *(["--project", project] if project else []),
        ],
        check_err_msg="Failed to get cluster credentials",
    )

    # Step 3: Build and push container image (local dev only)
    if not image:
        image = f"{region}-docker.pkg.dev/{project}/{service_name}/{service_name}:latest"
        click.echo(f"\n🐳 Building container image: {image}")
        _build_image_with_cloud_build(image=image, project=project)

    # Step 4: Update container image
    click.echo("\n🔄 Rolling out deployment...")
    run(
        [
            "kubectl",
            "set",
            "image",
            f"deployment/{service_name}",
            f"{service_name}={image}",
            "-n",
            service_name,
        ],
        check_err_msg="kubectl set image failed",
    )

    # Step 5: Inject runtime env vars (AGENT_VERSION, --update-env-vars, APP_URL).
    # A user-supplied value (via --update-env-vars) takes precedence over the
    # CLI-derived defaults, matching the Cloud Run and Agent Runtime paths.
    project_root = find_project_root() or Path.cwd()
    env_var_map = read_project_dotenv(project_root)
    env_var_map.update(parse_key_value_pairs(update_env_vars))
    env_var_map.setdefault("AGENT_VERSION", get_project_version(project_root))

    click.echo("\n🌐 Getting service IP...")
    ip_result = run(
        [
            "kubectl",
            "get",
            "service",
            service_name,
            "-n",
            service_name,
            "-o",
            "jsonpath={.status.loadBalancer.ingress[0].ip}",
        ],
        capture=True,
        print_cmd=False,
        check=False,
    )
    service_ip = ip_result.stdout.strip() if ip_result.returncode == 0 else ""
    if service_ip:
        click.echo(f"  Service IP: {service_ip}")
        # APP_URL is used by A2A agents for the agent card URL.
        env_var_map.setdefault("APP_URL", f"http://{service_ip}:8080")
    else:
        click.echo("  ⚠️  Could not determine service IP — skipping APP_URL injection.")

    kubectl_env_args = [
        "kubectl",
        "set",
        "env",
        f"deployment/{service_name}",
        *(f"{k}={v}" for k, v in env_var_map.items()),
        "-n",
        service_name,
    ]
    click.secho(f"  ▸ {redact_command(kubectl_env_args)}", fg="cyan", dim=True)
    run(
        kubectl_env_args,
        print_cmd=False,
        check_err_msg="Failed to set environment variables",
    )

    # Step 6: Wait for rollout
    run(
        [
            "kubectl",
            "rollout",
            "status",
            f"deployment/{service_name}",
            "-n",
            service_name,
            "--timeout=600s",
        ],
        check_err_msg="Rollout failed",
    )

    # Step 7: Print summary
    click.echo("\n\n✅ GKE deployment complete!")
    if service_ip:
        click.echo(f"   Internal service IP: {service_ip}")
    click.echo(
        f"   For local access: kubectl port-forward svc/{service_name} 8080:8080 -n {service_name}"
    )


def _list_deployments(cfg: ProjectConfig, project: str | None, region: str) -> None:
    """List existing deployments for the current project's deployment target."""
    if cfg.deployment_target == "agent_runtime":
        _list_agent_runtime_deployments(project, region)
    elif cfg.deployment_target == "cloud_run":
        _list_cloud_run_deployments(project, region)
    elif cfg.deployment_target == "gke":
        _list_gke_deployments()
    else:
        raise click.ClickException(f"Unknown deployment target: {cfg.deployment_target}")


def _list_agent_runtime_deployments(project: str | None, location: str) -> None:
    """List Agent Runtime deployments via the Vertex AI SDK."""
    import warnings

    import vertexai

    from google.agents.cli.auth import get_adc_credentials

    warnings.filterwarnings(
        "ignore", category=FutureWarning, module="google.cloud.aiplatform"
    )

    if not project:
        _, project = get_adc_credentials()
    if not project:
        raise click.ClickException(
            "Could not determine GCP project. Pass --project or set a default project."
        )

    client = vertexai.Client(project=project, location=location)
    agents = list(client.agent_engines.list())

    if not agents:
        click.echo(f"No Agent Runtime deployments found in {project} ({location}).")
        return

    from rich.console import Console
    from rich.table import Table

    table = Table(title=f"Agent Runtime Deployments — {project} ({location})")
    table.add_column("Display Name", style="bold")
    table.add_column("Resource Name", style="dim")
    table.add_column("Create Time")

    for agent in agents:
        res = agent.api_resource
        display_name = getattr(res, "display_name", None) or "—"
        name = getattr(res, "name", None) or "—"
        create_time = getattr(res, "create_time", None)
        time_str = create_time.strftime("%Y-%m-%d %H:%M") if create_time else "—"
        table.add_row(display_name, name, time_str)

    console = Console()
    console.print()
    console.print(table)


def _list_cloud_run_deployments(project: str | None, region: str | None) -> None:
    """List Cloud Run services via gcloud."""
    _tools.require_tool(
        "gcloud",
        "Install the Google Cloud SDK: https://cloud.google.com/sdk/docs/install",
    )

    args = [
        "gcloud",
        "run",
        "services",
        "list",
        "--format=json",
    ]
    if project:
        args.extend(["--project", project])
    if region:
        args.extend(["--region", region])

    result = run(args, capture=True, print_cmd=False, check=False)
    if result.returncode != 0:
        raise click.ClickException("Failed to list Cloud Run services.")

    import json

    services = json.loads(result.stdout) if result.stdout.strip() else []

    if not services:
        location_label = f" in {region}" if region else ""
        project_label = f" ({project})" if project else ""
        click.echo(f"No Cloud Run services found{location_label}{project_label}.")
        return

    from rich.console import Console
    from rich.table import Table

    title_parts = ["Cloud Run Services"]
    if project:
        title_parts.append(f"— {project}")
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

    console = Console()
    console.print()
    console.print(table)


def _list_gke_deployments() -> None:
    """List GKE deployments via kubectl."""
    _tools.require_tool(
        "kubectl", "Install kubectl: https://kubernetes.io/docs/tasks/tools/"
    )

    result = run(
        ["kubectl", "get", "deployments", "-o", "json"],
        capture=True,
        print_cmd=False,
        check=False,
    )
    if result.returncode != 0:
        raise click.ClickException(
            "Failed to list GKE deployments.\n"
            "  Ensure kubectl is configured with cluster credentials."
        )

    import json

    data = json.loads(result.stdout) if result.stdout.strip() else {}
    items = data.get("items", [])

    if not items:
        click.echo("No GKE deployments found in the current cluster.")
        return

    from rich.console import Console
    from rich.table import Table

    table = Table(title="GKE Deployments")
    table.add_column("Name", style="bold")
    table.add_column("Ready")
    table.add_column("Namespace")
    table.add_column("Created")

    for dep in items:
        metadata = dep.get("metadata", {})
        status = dep.get("status", {})
        name = metadata.get("name", "—")
        namespace = metadata.get("namespace", "—")
        ready = f"{status.get('readyReplicas', 0)}/{status.get('replicas', 0)}"
        created = metadata.get("creationTimestamp", "—")[:16].replace("T", " ")
        table.add_row(name, ready, namespace, created)

    console = Console()
    console.print()
    console.print(table)
