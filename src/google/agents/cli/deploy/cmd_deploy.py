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

import click
from click.core import ParameterSource

from google.agents.cli._gcp_project import resolve_gcp_project
from google.agents.cli._project import (
    ProjectConfig,
    chdir_project_root,
    check_cli_version,
    find_project_root,
    read_project_config,
    require_deployment_target,
)
from google.agents.cli.deploy._cloud_validation import validate_agent_runtime_deployment
from google.agents.cli.deploy._utils import (
    DEFAULT_CONCURRENCY,
    DEFAULT_CPU,
    DEFAULT_MAX_INSTANCES,
    DEFAULT_MEMORY,
    DEFAULT_MIN_INSTANCES,
    MachineShape,
    parse_kv_flag,
    resolve_service_name,
    validate_deployment_region,
)
from google.agents.cli.deploy.agent_runtime import (
    build_psc_interface_config,
    check_agent_runtime_operation,
    deploy_agent_runtime,
    list_agent_runtime_deployments,
    print_agent_runtime_dry_run,
)
from google.agents.cli.deploy.cloud_run import (
    check_cloud_run_status,
    deploy_cloud_run,
    list_cloud_run_deployments,
)
from google.agents.cli.deploy.gke import deploy_gke, list_gke_deployments

_AGENT_RUNTIME = "agent_runtime"
_CLOUD_RUN = "cloud_run"
_GKE = "gke"
_DEPLOYMENT_TARGETS = (_AGENT_RUNTIME, _CLOUD_RUN, _GKE)
_TARGET_DISPLAY_NAMES = {
    _AGENT_RUNTIME: "Agent Runtime",
    _CLOUD_RUN: "Cloud Run",
    _GKE: "GKE",
}


_SIZING_HINT = (
    "On GKE, configure sizing via Terraform and the HorizontalPodAutoscaler "
    "under deployment/terraform/."
)
# Flags that only some deployment targets support, in the order they are checked:
# (flag, targets that support it, hint shown on the other targets).
_TARGET_ONLY_FLAGS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("--network-attachment", (_AGENT_RUNTIME,), ""),
    ("--dns-peering-domain", (_AGENT_RUNTIME,), ""),
    ("--dns-peering-project", (_AGENT_RUNTIME,), ""),
    ("--dns-peering-network", (_AGENT_RUNTIME,), ""),
    ("--agent-gateway-egress", (_AGENT_RUNTIME,), ""),
    ("--agent-gateway-ingress", (_AGENT_RUNTIME,), ""),
    ("--key", (_AGENT_RUNTIME,), ""),
    ("--agent-identity", (_AGENT_RUNTIME,), ""),
    ("--no-agent-identity", (_AGENT_RUNTIME,), ""),
    # TODO: b/555632530 - extend --update-only to Cloud Run and GKE, which have
    # the same "configuration owned by Terraform" problem but are untested for it.
    ("--update-only", (_AGENT_RUNTIME,), ""),
    (
        "--secrets",
        (_AGENT_RUNTIME, _CLOUD_RUN),
        "On GKE, mount secrets via Kubernetes Secrets or the Secret Manager CSI driver.",
    ),
    # Container-build flags split by target: Agent Runtime builds from the
    # project Dockerfile (--build-args); Cloud Run / GKE take a prebuilt --image.
    ("--build-args", (_AGENT_RUNTIME,), ""),
    ("--framework", (_AGENT_RUNTIME,), "Cloud Run and GKE record no framework."),
    (
        "--image",
        (_CLOUD_RUN, _GKE),
        "Agent Runtime builds from the project Dockerfile and does not support "
        "prebuilt images.",
    ),
    ("--timeout", (_CLOUD_RUN,), ""),
    ("--ingress", (_CLOUD_RUN,), ""),
    # On GKE these are owned by Terraform and the HorizontalPodAutoscaler; reject
    # them rather than silently ignoring them.
    ("--cpu", (_AGENT_RUNTIME, _CLOUD_RUN), _SIZING_HINT),
    ("--memory", (_AGENT_RUNTIME, _CLOUD_RUN), _SIZING_HINT),
    ("--min-instances", (_AGENT_RUNTIME, _CLOUD_RUN), _SIZING_HINT),
    ("--max-instances", (_AGENT_RUNTIME, _CLOUD_RUN), _SIZING_HINT),
    ("--concurrency", (_AGENT_RUNTIME, _CLOUD_RUN), _SIZING_HINT),
    (
        "--labels",
        (_AGENT_RUNTIME, _CLOUD_RUN),
        "On GKE, resource labels are managed via Terraform under deployment/terraform/.",
    ),
    ("--no-wait", (_AGENT_RUNTIME, _CLOUD_RUN), ""),
)


@click.command("deploy")
@click.option("--project", default=None, help="GCP project ID.")
@click.option("--region", default=None, help="GCP region.")
@click.option(
    "--deployment-target",
    "-d",
    type=click.Choice(_DEPLOYMENT_TARGETS),
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
    "--agent-identity/--no-agent-identity",
    default=None,
    help="Enable or disable Agent Identity. Passing neither leaves an existing "
    "agent's identity untouched (on update) or disables Agent Identity (on create).",
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
    "--framework",
    default=None,
    show_default="the framework recorded in agents-cli-manifest.yaml",
    help=(
        "Framework the deployed container implements (Agent Runtime only). "
        "Sets agent_framework on the Agent Runtime resource, which the Google "
        "Cloud console reads to pick a playground. The API owns the accepted "
        "set and quietly falls back to 'custom' for anything else."
    ),
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
@click.option(
    "--timeout",
    default=None,
    type=click.IntRange(1, 3600),
    help="Request timeout in seconds (Cloud Run). Raise it for requests or "
    "connections that outlive Cloud Run's 300s default, which a new service "
    "gets. Left unset, an existing service keeps the timeout it has.",
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
    "--labels",
    default=None,
    help="Comma-separated KEY=VALUE resource labels (Agent Runtime, Cloud Run). "
    "Additive: adds/updates the labels you name; labels you don't name are "
    "preserved.",
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
    help="Print what would be executed without running it. For Agent Runtime, "
    "also run read-only Google Cloud checks.",
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
    "--update-only",
    "update_only",
    is_flag=True,
    default=False,
    help="Update an existing deployment, and fail if there is none to update "
    "instead of creating one (Agent Runtime).",
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
@click.option(
    "--agent-gateway-egress",
    default=None,
    help="Full resource name of an existing Agent Gateway to route the agent's "
    "outbound traffic through (Agent Runtime). The gateway must have "
    "governedAccessPath=AGENT_TO_ANYWHERE. Pass an empty value to unbind. "
    "Omit the flag to leave the current binding alone.",
)
@click.option(
    "--agent-gateway-ingress",
    default=None,
    help="Full resource name of an existing Agent Gateway to route the agent's "
    "inbound traffic through (Agent Runtime). The gateway must have "
    "governedAccessPath=CLIENT_TO_AGENT. Pass an empty value to unbind. "
    "Omit the flag to leave the current binding alone.",
)
@click.option(
    "--key",
    default=None,
    help="Cloud KMS key for customer-managed encryption (Agent Runtime). "
    "Format: projects/PROJECT/locations/LOCATION/keyRings/RING/cryptoKeys/KEY. "
    "Set on create only; cannot be changed later.",
)
@click.option(
    "--ingress",
    type=click.Choice(["all", "internal", "internal-and-cloud-load-balancing"]),
    default=None,
    help="Ingress traffic allowed to the service (Cloud Run).",
)
@click.pass_context
def cmd_deploy(
    ctx: click.Context,
    *,
    project: str | None,
    region: str | None,
    deployment_target: str | None,
    secrets: str | None,
    agent_identity: bool | None,
    update_env_vars: str | None,
    iap: bool,
    ingress: str | None,
    port: int | None,
    framework: str | None,
    memory: str | None,
    cpu: str | None,
    min_instances: int | None,
    max_instances: int | None,
    concurrency: int | None,
    timeout: int | None,
    service_account: str | None,
    service_name_override: str | None,
    image: str | None,
    cluster_name: str | None,
    dry_run: bool,
    list_deployments: bool,
    no_wait: bool,
    update_only: bool,
    status: bool,
    interactive: bool,
    no_confirm_project: bool,
    network_attachment: str | None,
    dns_peering_domain: str | None,
    dns_peering_project: str | None,
    dns_peering_network: str | None,
    agent_gateway_egress: str | None,
    agent_gateway_ingress: str | None,
    build_args: str | None,
    labels: str | None,
    key: str | None,
) -> None:
    """Deploy the agent.

    \b
    Dispatches by deployment target configured in agents-cli-manifest.yaml:
      agent_runtime → Agent Runtime deployment
      cloud_run    → gcloud run deploy
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
    validate_deployment_region(region, cfg.deployment_target)
    service_name = _resolve_deploy_service_name(cfg, service_name_override)

    if not has_manifest:
        _warn_deploying_without_manifest(cfg, service_name)

    project_explicitly_passed = bool(project)
    # Resolve project once upfront — all deployment targets need it
    project = resolve_gcp_project(project, required=True)

    if status:
        _check_deploy_status(cfg, project, region, service_name)
        return

    if list_deployments:
        _list_deployments(cfg, project, region)
        return

    # Build PSC interface config from networking flags
    psc_interface_config = build_psc_interface_config(
        network_attachment=network_attachment,
        dns_peering_domain=dns_peering_domain,
        dns_peering_project=dns_peering_project,
        dns_peering_network=dns_peering_network,
    )

    shape = MachineShape(
        cpu=cpu,
        memory=memory,
        min_instances=min_instances,
        max_instances=max_instances,
        concurrency=concurrency,
    )
    _validate_flags_for_target(cfg.deployment_target, _passed_flags(ctx))
    framework = cfg.framework if framework is None else framework

    parsed_labels = parse_kv_flag("--labels", labels)

    # Prompt only once every flag is known to be valid, so a bad flag never
    # costs the user a confirmation.
    if not project_explicitly_passed and not no_confirm_project:
        _confirm_resolved_project(project, interactive=interactive)

    if cfg.deployment_target == _AGENT_RUNTIME:
        if dry_run:
            validate_agent_runtime_deployment(
                project=project,
                region=region,
                display_name=service_name,
                service_account=service_account,
                secrets=secrets,
                update_only=update_only,
                agent_identity=agent_identity,
            )
            print_agent_runtime_dry_run(
                project=project,
                region=region,
                shape=shape,
                update_only=update_only,
                psc_interface_config=psc_interface_config,
                agent_gateway_egress=agent_gateway_egress,
                agent_gateway_ingress=agent_gateway_ingress,
                kms_key=key,
            )
            return
        deploy_agent_runtime(
            cfg=cfg,
            project=project,
            location=region,
            display_name=service_name,
            set_env_vars=update_env_vars,
            set_secrets=secrets,
            labels=parsed_labels or None,
            service_account=service_account,
            agent_identity=agent_identity,
            no_wait=no_wait,
            update_only=update_only,
            psc_interface_config=psc_interface_config,
            agent_gateway_egress=agent_gateway_egress,
            agent_gateway_ingress=agent_gateway_ingress,
            build_args=build_args,
            port=port,
            framework=framework,
            cpu=shape.cpu,
            memory=shape.memory,
            min_instances=shape.min_instances,
            max_instances=shape.max_instances,
            container_concurrency=shape.concurrency,
            kms_key=key,
        )

    elif cfg.deployment_target == _CLOUD_RUN:
        deploy_cloud_run(
            project=project,
            region=region,
            service_name=service_name,
            image=image,
            shape=shape,
            timeout=timeout,
            ingress=ingress,
            port=port,
            iap=iap,
            service_account=service_account,
            update_env_vars=update_env_vars,
            secrets=secrets,
            labels=parsed_labels,
            no_wait=no_wait,
            dry_run=dry_run,
        )

    elif cfg.deployment_target == _GKE:
        deploy_gke(
            project=project,
            region=region,
            image=image,
            cluster_name=cluster_name,
            update_env_vars=update_env_vars,
            dry_run=dry_run,
            service_name=service_name,
            session_type=cfg.session_type,
        )

    else:
        raise click.ClickException(
            f"Unknown deployment target: {cfg.deployment_target}. "
            "Set deployment_target in agents-cli-manifest.yaml."
        )


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
    if cfg.deployment_target not in _DEPLOYMENT_TARGETS:
        raise click.ClickException(
            f"Unknown deployment target '{cfg.deployment_target}'.\n"
            "  Set create_params.deployment_target in agents-cli-manifest.yaml to one "
            f"of: {', '.join(_DEPLOYMENT_TARGETS)}, or pass --deployment-target."
        )

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
    if service_name_override and cfg.deployment_target == _GKE:
        raise click.ClickException(
            "--service-name is not supported for GKE deployments.\n"
            "  GKE resource names are derived from the project name via "
            "Terraform (var.project_name) and cannot be overridden at deploy "
            "time.\n"
            "  Use Cloud Run or Agent Runtime to customize the service name."
        )
    return resolve_service_name(cfg, service_name_override)


def _warn_deploying_without_manifest(cfg: ProjectConfig, service_name: str) -> None:
    """Warn which defaults (service name, agent dir, build cwd) a bare deploy uses."""
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


def _check_deploy_status(
    cfg: ProjectConfig,
    project: str,
    region: str,
    service_name: str,
) -> None:
    """Check the status of a pending --no-wait deployment."""
    if cfg.deployment_target == _AGENT_RUNTIME:
        check_agent_runtime_operation(
            cfg=cfg,
            project=project,
            location=region,
        )
    elif cfg.deployment_target == _CLOUD_RUN:
        check_cloud_run_status(project, region, service_name)
    elif cfg.deployment_target == _GKE:
        raise click.ClickException("--status is not supported for GKE deployments.")
    else:
        raise click.ClickException(f"Unknown deployment target: {cfg.deployment_target}")


def _list_deployments(cfg: ProjectConfig, project: str, region: str) -> None:
    """List existing deployments for the current project's deployment target."""
    if cfg.deployment_target == _AGENT_RUNTIME:
        list_agent_runtime_deployments(project, region)
    elif cfg.deployment_target == _CLOUD_RUN:
        list_cloud_run_deployments(project, region)
    elif cfg.deployment_target == _GKE:
        list_gke_deployments()
    else:
        raise click.ClickException(f"Unknown deployment target: {cfg.deployment_target}")


def _confirm_resolved_project(project: str, *, interactive: bool) -> None:
    """Confirm a project that was resolved automatically rather than passed.

    Without --interactive there is no one to answer a prompt, so fail with the
    ways to proceed instead of deploying to a project the user never named.
    """
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


def _passed_flags(ctx: click.Context) -> set[str]:
    """CLI names of the options typed on the command line, e.g. ``{"--timeout"}``.

    Asks Click where each value came from instead of inspecting values, so an
    explicitly empty value (``--key ""``) counts as passed while a default does not.
    Values from an env var, a ``default_map``, or ``ctx.invoke`` are not
    COMMANDLINE and so are not checked; deploy uses none of those today.
    """
    passed = set()
    for param in ctx.command.params:
        name = param.name
        if (
            name is None
            or ctx.get_parameter_source(name) is not ParameterSource.COMMANDLINE
        ):
            continue
        # --agent-identity/--no-agent-identity: name the form the user typed.
        if param.secondary_opts and ctx.params[name] is False:
            passed.add(param.secondary_opts[0])
        # The table only lists -- names, so a param without one can't match a row.
        elif opt := next((opt for opt in param.opts if opt.startswith("--")), None):
            passed.add(opt)
    return passed


def _validate_flags_for_target(deployment_target: str, passed_flags: set[str]) -> None:
    """Reject flags that the deployment target does not support."""
    rejected = [
        (flag, targets, hint)
        for flag, targets, hint in _TARGET_ONLY_FLAGS
        if flag in passed_flags and deployment_target not in targets
    ]
    if not rejected:
        return
    # Report the first rejected flag together with any others that share its
    # supported targets and hint, e.g. "--cpu, --memory are only supported ...".
    _, targets, hint = rejected[0]
    flags = [flag for flag, t, h in rejected if (t, h) == (targets, hint)]
    raise click.ClickException(
        _unsupported_flags_message(flags, targets, hint, deployment_target)
    )


def _unsupported_flags_message(
    flags: list[str], targets: tuple[str, ...], hint: str, deployment_target: str
) -> str:
    """'<flags> is/are only supported for <targets> deployments (current target: <t>).'"""
    names = [_TARGET_DISPLAY_NAMES[t] for t in targets]
    supported = (
        names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    )
    verb = "is" if len(flags) == 1 else "are"
    message = (
        f"{', '.join(flags)} {verb} only supported for {supported} deployments "
        f"(current target: {deployment_target})."
    )
    return f"{message}\n  {hint}" if hint else message
