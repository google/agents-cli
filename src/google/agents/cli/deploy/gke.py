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

"""GKE deployment for ``agents-cli deploy``."""

import json
import logging
import time
from pathlib import Path

import click

from google.agents.cli import _tools
from google.agents.cli._project import find_project_root
from google.agents.cli._runner import run
from google.agents.cli.deploy._utils import (
    parse_key_value_pairs,
    print_table,
    read_project_dotenv,
    redact_command,
)
from google.agents.cli.infra._terraform import SINGLE_PROJECT_TF_DIR
from google.agents.cli.scaffold.utils.language import get_project_version

# Resources the targeted ``terraform apply`` creates for a local GKE deploy.
_GKE_DEPLOY_TARGETS = (
    "google_container_cluster.app",
    "google_artifact_registry_repository.docker_repo",
    "google_compute_router_nat.nat",
    "google_compute_firewall.allow_internal",
    "google_service_account.app_sa",
    "google_project_iam_member.app_sa_roles",
    "google_project_iam_member.default_compute_sa_storage_object_creator",
    "google_service_account_iam_member.workload_identity_binding",
    "google_project_service_identity.vertex_sa",
    "kubernetes_namespace_v1.app",
    "kubernetes_service_account_v1.app",
    "kubernetes_deployment_v1.app",
    "kubernetes_service_v1.app",
    "kubernetes_horizontal_pod_autoscaler_v2.app",
    "kubernetes_pod_disruption_budget_v1.app",
)
# Extra resources needed when the project stores sessions in Cloud SQL.
_GKE_CLOUD_SQL_TARGETS = (
    "random_password.db_password",
    "google_sql_database_instance.session_db",
    "google_sql_database.database",
    "google_sql_user.db_user",
    "google_secret_manager_secret.db_password",
    "google_secret_manager_secret_version.db_password",
    "kubernetes_secret_v1.db_password",
)


def deploy_gke(
    *,
    project,
    region,
    image,
    cluster_name,
    update_env_vars,
    dry_run,
    service_name,
    session_type,
):
    """GKE deployment: single linear flow with conditional steps.

    When ``image`` is provided (CI/CD mode), skips terraform and docker build.
    When ``image`` is None (local dev mode), runs targeted terraform + build flow.
    Both paths share cluster credentials, kubectl rollout, env-var injection
    (AGENT_VERSION, any --update-env-vars, and APP_URL), and external IP steps.

    ``session_type`` selects which optional resources the targeted apply must
    include; see ``_GKE_CLOUD_SQL_TARGETS``.
    """
    deploy_targets = list(_GKE_DEPLOY_TARGETS)
    if session_type == "cloud_sql":
        deploy_targets += _GKE_CLOUD_SQL_TARGETS
    _tools.require_tool("gcloud")
    _tools.require_tool("kubectl")
    cluster_name = cluster_name or service_name

    if not image:
        _tools.require_tool("terraform")

    if dry_run:
        if not image:
            tf_dir = SINGLE_PROJECT_TF_DIR.as_posix()
            click.echo(f"  Would run: terraform -chdir={tf_dir} init -input=false")
            click.echo(
                f"  Would run: terraform -chdir={tf_dir} apply -auto-approve -input=false"
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
        tf_dir = SINGLE_PROJECT_TF_DIR.as_posix()
        click.echo("\n🏗️  Provisioning infrastructure with Terraform...")
        # -input=false: report a missing variable instead of blocking on a prompt.
        run(
            ["terraform", f"-chdir={tf_dir}", "init", "-input=false"],
            check_err_msg="Terraform init failed",
        )
        apply_args = [
            "terraform",
            f"-chdir={tf_dir}",
            "apply",
            "-auto-approve",
            "-input=false",
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
            "--project",
            project,
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
    try:
        env_var_map.update(parse_key_value_pairs(update_env_vars))
    except ValueError as e:
        raise click.ClickException(f"argument --update-env-vars: {e}") from e

    # Skip the version read (and its warning) when the user has already supplied one.
    if "AGENT_VERSION" not in env_var_map:
        env_var_map["AGENT_VERSION"] = get_project_version(project_root)
    # Fail closed: ADK defaults content-in-spans to true; keep it off for bare deploys.
    env_var_map.setdefault("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "false")

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
        logging.warning(
            "Could not determine the service IP — skipping APP_URL injection."
        )

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
    try:
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
    except click.ClickException:
        _echo_rollout_diagnostics(service_name)
        raise

    # Step 7: Print summary
    click.echo("\n\n✅ GKE deployment complete!")
    if service_ip:
        click.echo(f"   Internal service IP: {service_ip}")
    click.echo(
        f"   For local access: kubectl port-forward svc/{service_name} 8080:8080 -n {service_name}"
    )


def list_gke_deployments() -> None:
    """List GKE deployments via kubectl."""
    _tools.require_tool("kubectl")

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

    data = json.loads(result.stdout) if result.stdout.strip() else {}
    items = data.get("items", [])

    if not items:
        click.echo("No GKE deployments found in the current cluster.")
        return

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

    print_table(table)


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


def _echo_rollout_diagnostics(service_name: str) -> None:
    """Print pod and event state for a deployment whose rollout did not finish."""
    click.echo("\n🔍 Rollout diagnostics (the rollout above did not complete):")
    diagnostics: list[tuple[str, list[str]]] = [
        ("Pods", ["kubectl", "get", "pods", "-o", "wide", "-n", service_name]),
        (
            "Pod details",
            [
                "kubectl",
                "describe",
                "pods",
                "-l",
                f"app={service_name}",
                "-n",
                service_name,
            ],
        ),
        (
            "Events",
            [
                "kubectl",
                "get",
                "events",
                "--sort-by=.lastTimestamp",
                "-n",
                service_name,
            ],
        ),
    ]
    for title, args in diagnostics:
        click.echo(f"\n--- {title} ---")
        # check=False: a diagnostic that fails must not replace the rollout error.
        run(args, print_cmd=False, check=False)
