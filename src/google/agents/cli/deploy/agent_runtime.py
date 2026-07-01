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

"""Deploy agents to Agent Runtime.

De-templatized deploy module. All cookiecutter conditionals replaced
with runtime checks via ProjectConfig.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import warnings
from pathlib import Path
from typing import Any

import click
import pathspec
import vertexai
from google.cloud import resourcemanager_v3
from google.iam.v1 import iam_policy_pb2, policy_pb2
from vertexai._genai import _agent_engines_utils
from vertexai._genai.types import AgentEngine, AgentEngineConfig, IdentityType

from google.agents.cli._agent_runtime_a2a import build_agent_runtime_a2a_card_url
from google.agents.cli._project import (
    ProjectConfig,
    find_project_root,
    scaffold_older_than,
)
from google.agents.cli.deploy._operation import (
    METADATA_FILE,
    clear_operation,
    read_operation,
    write_operation,
)
from google.agents.cli.deploy._utils import (
    DEFAULT_CONCURRENCY,
    DEFAULT_CPU,
    DEFAULT_MAX_INSTANCES,
    DEFAULT_MEMORY,
    DEFAULT_MIN_INSTANCES,
    parse_key_value_pairs,
    read_project_dotenv,
    resolve_service_name,
)
from google.agents.cli.scaffold.utils.language import get_project_version

# Suppress google-cloud-storage version compatibility warning
warnings.filterwarnings(
    "ignore", category=FutureWarning, module="google.cloud.aiplatform"
)


def parse_secrets(secrets_string: str | None) -> dict[str, dict[str, str]]:
    """Parse secrets from ENV_VAR=SECRET_ID or ENV_VAR=SECRET_ID:VERSION format."""
    raw = parse_key_value_pairs(secrets_string)
    result: dict[str, dict[str, str]] = {}
    for key, spec in raw.items():
        if ":" not in spec:
            secret_id, version = spec, "latest"
        else:
            secret_id, _, version = spec.rpartition(":")
        result[key] = {"secret": secret_id, "version": version}
    return result


def format_env_value(value: Any) -> str:
    """Format an env var value for display, masking secrets."""
    if isinstance(value, dict) and "secret" in value and "version" in value:
        return f"[secret:{value['secret']}:{value['version']}]"
    return str(value)


# Agent Runtime injects GOOGLE_CLOUD_PROJECT itself; setting it in
# deployment_spec.env is rejected with FAILED_PRECONDITION ("... is reserved").
# GOOGLE_CLOUD_LOCATION is NOT reserved (verified) — the LLM location can differ
# from the deploy region, so we keep it. Filtered from the propagated .env.
_AGENT_RUNTIME_RESERVED_ENV = frozenset({"GOOGLE_CLOUD_PROJECT"})


def _build_runtime_env_vars(
    *,
    set_env_vars: str | None,
    secrets: dict[str, dict[str, str]],
    port: int | None = None,
) -> dict[str, Any]:
    """Assemble the runtime env vars for the deployed Agent Runtime.

    Precedence (highest first): ``--update-env-vars`` / ``--set-secrets``, then the
    project ``.env``, then these overridable defaults. ``GOOGLE_CLOUD_PROJECT`` is
    dropped — Agent Runtime reserves it (the platform injects it) and rejects it.
    The backend defaults to Vertex AI (at ``GOOGLE_CLOUD_LOCATION=global``) unless
    the ``.env`` supplies an AI Studio API key:

    - ``AGENT_VERSION`` — the pyproject.toml version, read at runtime by the A2A
      agent card. Read only when the user hasn't supplied a value, so an override
      skips the pyproject read and its missing-version warning.
    - ``PORT`` — the container port, when one is supplied.
    - telemetry toggles — Cloud Trace export and prompt/response capture in spans.
    """
    # Project .env is the base layer; explicit --update-env-vars wins over it.
    env_vars: dict[str, Any] = read_project_dotenv(find_project_root() or Path.cwd())
    env_vars.update(parse_key_value_pairs(set_env_vars))
    env_vars.update(secrets)  # type: ignore[arg-type]
    # Agent Runtime injects these itself; including them in deployment_spec.env is
    # rejected with FAILED_PRECONDITION ("... is reserved").
    for reserved in _AGENT_RUNTIME_RESERVED_ENV & env_vars.keys():
        logging.warning(
            "Ignoring reserved Agent Runtime env var %s \u2014 it is set by the platform.",
            reserved,
        )
        del env_vars[reserved]
    if "AGENT_VERSION" not in env_vars:
        env_vars["AGENT_VERSION"] = get_project_version(find_project_root() or Path.cwd())
    if port:
        env_vars.setdefault("PORT", str(port))
    # agent_runtime is Vertex-native: default to Vertex (LLM at global) unless the
    # .env opted into AI Studio with an API key.
    if "GEMINI_API_KEY" not in env_vars and "GOOGLE_API_KEY" not in env_vars:
        env_vars.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
        env_vars.setdefault("GOOGLE_CLOUD_LOCATION", "global")
    env_vars.setdefault("GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY", "true")
    env_vars.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    return env_vars


def _existing_plain_env_vars(agent: Any) -> dict[str, str]:
    """Plain env vars on a deployed Agent Runtime, as ``{name: value}``.

    An update replaces the whole ``deployment_spec.env`` block, so re-sending
    these preserves vars set outside this deploy. Secrets are skipped: the API
    only touches them when secrets are supplied.
    """
    spec = getattr(agent.api_resource, "spec", None)
    deployment_spec = getattr(spec, "deployment_spec", None) if spec else None
    env = getattr(deployment_spec, "env", None) if deployment_spec else None
    result: dict[str, str] = {}
    for var in env or []:
        name = getattr(var, "name", None)
        if name:
            result[name] = getattr(var, "value", "") or ""
    return result


def _get_resource_name_from_operation(operation_name: str) -> str:
    """Extract ReasoningEngine resource name from long-running operation name.

    GCP long-running operations on specific resources are guaranteed by API
    standards to end with "/operations/{operation_id}". Extract the full
    resource name by partitioning on "/operations/".
    """
    resource_name, _, _ = operation_name.rpartition("/operations/")
    return resource_name


def write_deployment_metadata(
    remote_agent: Any,
    cfg: ProjectConfig,
) -> None:
    """Write deployment metadata to file."""
    metadata = {
        "remote_agent_runtime_id": remote_agent.api_resource.name,
        "deployment_target": "agent_runtime",
        "is_a2a": cfg.is_a2a,
        "agent_directory": cfg.agent_directory,
        "deployment_timestamp": datetime.datetime.now(tz=datetime.UTC).isoformat(),
    }

    with open(METADATA_FILE, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    logging.info(f"Agent Runtime ID written to {METADATA_FILE}")


def print_deployment_success(
    remote_agent: Any,
    location: str,
    project: str,
    cfg: ProjectConfig,
) -> None:
    """Print deployment success message with console URL."""
    resource_name_parts = remote_agent.api_resource.name.split("/")
    agent_runtime_id = resource_name_parts[-1]
    project_number = resource_name_parts[1]

    if cfg.is_a2a:
        print("\n✅ Deployment successful!")
        agent_card_url = build_agent_runtime_a2a_card_url(
            location, remote_agent.api_resource.name, cfg.agent_directory
        )
        print(f"🪪 Agent Card URL: {agent_card_url}")
    else:
        print("\n✅ Deployment successful!")

    print(f"Agent Runtime ID: {remote_agent.api_resource.name}")

    spec = getattr(remote_agent.api_resource, "spec", None)
    service_account = getattr(spec, "service_account", None) if spec else None
    if service_account:
        print(f"Service Account: {service_account}")
    else:
        default_sa = (
            f"service-{project_number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
        )
        print(f"Service Account: {default_sa}")

    console_url = (
        f"https://console.cloud.google.com/vertex-ai/agents/agent-engines/"
        f"locations/{location}/agent-engines/{agent_runtime_id}?project={project}"
    )
    print(f"\n📊 View in Console: {console_url}\n")


def setup_agent_identity(client: Any, project: str, display_name: str) -> Any:
    """Create agent with identity and grant required IAM roles."""
    click.echo(f"\n🔧 Creating agent identity for: {display_name}")
    agent = client.agent_engines.create(
        config={
            "identity_type": IdentityType.AGENT_IDENTITY,
            "display_name": display_name,
        }
    )

    roles = [
        "roles/aiplatform.user",
        "roles/serviceusage.serviceUsageConsumer",
        "roles/browser",
        "roles/cloudapiregistry.viewer",
        "roles/logging.logWriter",
        "roles/monitoring.metricWriter",
    ]
    principal = f"principal://{agent.api_resource.spec.effective_identity}"
    click.echo(f"🔐 Granting IAM roles to: {principal}")
    proj_client = resourcemanager_v3.ProjectsClient()
    policy = proj_client.get_iam_policy(
        request=iam_policy_pb2.GetIamPolicyRequest(resource=f"projects/{project}")
    )
    for role in roles:
        policy.bindings.append(policy_pb2.Binding(role=role, members=[principal]))
    proj_client.set_iam_policy(
        request=iam_policy_pb2.SetIamPolicyRequest(
            resource=f"projects/{project}", policy=policy
        )
    )
    click.echo("  ✅ Agent identity ready")
    return agent


# agent_runtime switched from reasoning-engine introspection to a container
# build (which requires a Dockerfile) in this release. Projects scaffolded
# before it never shipped a Dockerfile, so a missing one means the project
# predates the container model.
_CONTAINER_RUNTIME_VERSION = "0.6.0"


def _missing_dockerfile_error(cfg: ProjectConfig) -> str:
    """Actionable message for an agent_runtime deploy with no Dockerfile.

    The usual cause is a project scaffolded before agent_runtime switched to a
    container build: those projects deployed via reasoning-engine introspection
    and never shipped a Dockerfile, so a newer CLI cannot build them.
    """
    lines = [
        "Dockerfile not found in the project root directory.",
        "  agent_runtime deploys a container image, which requires a Dockerfile.",
    ]
    if scaffold_older_than(cfg, _CONTAINER_RUNTIME_VERSION):
        version = cfg.acli_version
        lines += [
            "",
            f"  This project was scaffolded with agents-cli {version}, before",
            "  agent_runtime used containers. Either:",
            "    • migrate the project:  agents-cli scaffold upgrade",
            "    • or deploy with the version it was built for:",
            f"        uvx google-agents-cli@{version} deploy",
        ]
    else:
        lines += [
            "  Run `agents-cli scaffold upgrade` to regenerate it, or recreate",
            "  the project with `agents-cli create`.",
        ]
    return "\n".join(lines)


# Always ignored, mirroring gcloud's generated .gcloudignore defaults.
_DEFAULT_IGNORE_LINES = (".git", ".gcloudignore", ".gitignore")
_INCLUDE_DIRECTIVE = "#!include:"


def _ignore_lines(root: Path) -> list[str]:
    """Gitignore-style patterns from ``root``'s ignore file.

    Uses ``.gcloudignore`` if present, else ``.gitignore``, plus
    :data:`_DEFAULT_IGNORE_LINES`. A top-level ``#!include:<file>`` line is
    expanded once.
    """
    lines = list(_DEFAULT_IGNORE_LINES)
    source = root / ".gcloudignore"
    if not source.exists():
        source = root / ".gitignore"
    if not source.exists():
        return lines
    for raw in source.read_text(encoding="utf-8").splitlines():
        directive = raw.strip()
        if directive.startswith(_INCLUDE_DIRECTIVE):
            included = root / directive.removeprefix(_INCLUDE_DIRECTIVE).strip()
            if included.exists():
                lines += included.read_text(encoding="utf-8").splitlines()
        else:
            lines.append(raw)
    return lines


def _packaged_files(root: Path) -> list[str]:
    """``./``-prefixed paths of files under ``root``, excluding anything ignored
    per :func:`_ignore_lines`.
    """
    spec = pathspec.PathSpec.from_lines("gitwildmatch", _ignore_lines(root))
    files: list[str] = []
    # Sort dirs/files for a deterministic, reproducible archive order.
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        # Prune ignored directories in place so os.walk never descends into them
        # (the trailing slash tells gitwildmatch to match directory patterns).
        dirnames[:] = sorted(
            d for d in dirnames if not spec.match_file((rel_dir / d).as_posix() + "/")
        )
        # A file in a kept directory can still be individually ignored (e.g.
        # ``*.secret``), so check each one even after pruning its parent.
        for name in sorted(filenames):
            rel = (rel_dir / name).as_posix()
            if not spec.match_file(rel):
                files.append(f"./{rel}")
    return files


def deploy_agent_runtime(
    *,
    cfg: ProjectConfig,
    project: str,
    display_name: str | None = None,
    location: str = "us-east1",
    description: str | None = None,
    source_packages: list[str] | None = None,
    set_env_vars: str | None = None,
    set_secrets: str | None = None,
    labels: str | None = None,
    service_account: str | None = None,
    min_instances: int | None = None,
    max_instances: int | None = None,
    cpu: str | None = None,
    memory: str | None = None,
    container_concurrency: int | None = None,
    agent_identity: bool = False,
    no_wait: bool = False,
    psc_interface_config: dict | None = None,
    build_args: str | None = None,
    port: int | None = None,
) -> AgentEngine | None:
    """Deploy the agent to Vertex AI Agent Runtime.

    Args:
        cfg: Project configuration from pyproject.toml.
        project: GCP project ID. Defaults to ADC project.
        display_name: Display name for the agent engine.
        location: GCP region.
        description: Description of the agent.
        source_packages: Source packages to deploy.
        set_env_vars: Comma-separated KEY=VALUE env vars.
        set_secrets: Comma-separated ENV_VAR=SECRET_ID pairs.
        labels: Comma-separated KEY=VALUE labels.
        service_account: Service account email.
        min_instances: Minimum number of instances.
        max_instances: Maximum number of instances.
        cpu: CPU limit.
        memory: Memory limit.
        container_concurrency: Container concurrency.
        agent_identity: Enable agent identity.
        no_wait: If True, start the deployment and return immediately.
        psc_interface_config: PSC interface configuration dict for private
            VPC connectivity. Contains ``network_attachment`` and optionally
            ``dns_peering_configs``.
        build_args: Comma-separated KEY=VALUE build args.
        port: Container port.

    Returns:
        The deployed AgentEngine instance, or None when no_wait is True.
    """
    if location == "global":
        raise click.ClickException(
            "Region 'global' is not supported for Agent Runtime deployments.\n"
            "  Please specify a regional location (e.g., 'us-central1', 'us-east1') via --region or in your project config."
        )

    display_name = display_name or resolve_service_name(cfg, None)

    # Agent Runtime builds from the project's Dockerfile, so upload the tree.
    auto_packaged = not source_packages
    source_packages = source_packages or _packaged_files(Path.cwd())

    if not os.path.exists("Dockerfile"):
        raise click.ClickException(_missing_dockerfile_error(cfg))
    # A present-but-ignored Dockerfile passes the check above yet breaks the
    # build, so surface it clearly instead of failing opaquely in Agent Engine.
    if auto_packaged and "./Dockerfile" not in source_packages:
        raise click.ClickException(
            "Dockerfile is present but excluded by .gcloudignore/.gitignore.\n"
            "  Remove the matching ignore pattern so the deploy can package it."
        )

    # Parse CLI environment variables, secrets, and labels
    secrets = parse_secrets(set_secrets)
    labels_dict = parse_key_value_pairs(labels)

    env_vars = _build_runtime_env_vars(
        set_env_vars=set_env_vars,
        secrets=secrets,
        port=port,
    )

    # Initialize vertexai client
    http_options = {"api_version": "v1beta1"} if agent_identity else None
    client = vertexai.Client(
        project=project,
        location=location,
        http_options=http_options,
    )
    vertexai.init(project=project, location=location)

    # Check for existing agent
    existing_agents = list(client.agent_engines.list())
    matching_agents = [
        agent
        for agent in existing_agents
        if agent.api_resource.display_name == display_name
    ]

    # Pre-existence flag must be computed before setup_agent_identity: that call
    # creates a bare identity agent (no deployment spec), but it's still a
    # first-time spec deploy so the conservative defaults must apply.
    is_update = bool(matching_agents)

    # Setup agent identity on first deployment
    if agent_identity and not matching_agents:
        matching_agents = [setup_agent_identity(client, project, display_name)]
    if not is_update:
        # Create: no existing spec to preserve; apply the conservative shape.
        min_instances = DEFAULT_MIN_INSTANCES if min_instances is None else min_instances
        max_instances = DEFAULT_MAX_INSTANCES if max_instances is None else max_instances
        cpu = DEFAULT_CPU if cpu is None else cpu
        memory = DEFAULT_MEMORY if memory is None else memory
        container_concurrency = (
            DEFAULT_CONCURRENCY
            if container_concurrency is None
            else container_concurrency
        )

    if matching_agents:
        resource_name = matching_agents[0].api_resource.name
        # list() may return a summary without deployment_spec; get() guarantees
        # the full env/resource_limits are populated.
        existing = client.agent_engines.get(name=resource_name)
        # Preserve env vars set outside this deploy; CLI/user values still win.
        for key, value in _existing_plain_env_vars(existing).items():
            env_vars.setdefault(key, value)
        # Point the A2A agent card at the real Agent Engine HTTP passthrough
        # instead of localhost; needs the existing engine's resource name, so a
        # first-time create picks it up on the next deploy.
        env_vars.setdefault(
            "APP_URL",
            f"https://{location}-aiplatform.googleapis.com/reasoningEngines/v1/"
            f"{resource_name}/api",
        )
        # When only one of cpu/memory is set, fill the other half from the live
        # spec so config_kwargs["resource_limits"] gets a complete pair. When both
        # are None a plain redeploy must omit resource_limits to preserve the live
        # value — only fill when exactly one side was explicitly supplied.
        if (cpu is None) ^ (memory is None):
            dep = getattr(existing.api_resource.spec, "deployment_spec", None)
            limits = getattr(dep, "resource_limits", None) or {}
            existing_cpu = (
                limits.get("cpu")
                if isinstance(limits, dict)
                else getattr(limits, "cpu", None)
            )
            existing_memory = (
                limits.get("memory")
                if isinstance(limits, dict)
                else getattr(limits, "memory", None)
            )
            cpu = cpu if cpu is not None else existing_cpu
            memory = memory if memory is not None else existing_memory
            if cpu is None or memory is None:
                logging.warning(
                    "Could not resolve the existing %s to pair with the supplied value; "
                    "resource_limits left unchanged for this update.",
                    "memory" if memory is None else "cpu",
                )

    click.echo("\n🤖 Deploying agent to Agent Runtime...\n")

    # Log deployment parameters
    click.echo("\n📋 Deployment Parameters:")

    def _shown(v: Any) -> Any:
        return v if v is not None else "(unchanged)"

    params = [
        ("Project", project),
        ("Location", location),
        ("Display Name", display_name),
        ("Min Instances", _shown(min_instances)),
        ("Max Instances", _shown(max_instances)),
        ("CPU", _shown(cpu)),
        ("Memory", _shown(memory)),
        ("Container Concurrency", _shown(container_concurrency)),
    ]
    if service_account:
        params.append(("Service Account", service_account))
    if agent_identity:
        params.append(("Agent Identity", "Enabled (Preview)"))
    if psc_interface_config:
        params.append(
            ("Network Attachment", psc_interface_config.get("network_attachment", "—"))
        )
        for i, dc in enumerate(psc_interface_config.get("dns_peering_configs", [])):
            params.append(
                (
                    f"DNS Peering [{i}]",
                    f"{dc.get('domain', '')} → {dc.get('target_project', '')}/{dc.get('target_network', '')}",
                )
            )
    if port:
        params.append(("Port", port))
    if build_args:
        params.append(("Build Args", build_args))
    for name, value in params:
        click.echo(f"  {name}: {value}")

    if env_vars:
        click.echo("\n🌍 Environment Variables:")
        for key, value in sorted(env_vars.items()):
            click.echo(f"  {key}: {format_env_value(value)}")

    source_packages_list = list(source_packages)

    config_kwargs: dict[str, Any] = {
        "display_name": display_name,
        "source_packages": source_packages_list,
        "env_vars": env_vars,
        "service_account": service_account,
        "identity_type": IdentityType.AGENT_IDENTITY if agent_identity else None,
        "description": description,
        "labels": labels_dict if labels_dict else None,
        "min_instances": min_instances,
        "max_instances": max_instances,
        "container_concurrency": container_concurrency,
        "resource_limits": {"cpu": cpu, "memory": memory}
        if (cpu is not None and memory is not None)
        else None,
    }

    # Agent Engine builds and serves the container over HTTP, so no entrypoint
    # module or class-method spec is needed — just the image build config.
    image_spec_dict: dict[str, Any] = {}
    build_args_dict = parse_key_value_pairs(build_args)
    if port:
        build_args_dict.setdefault("PORT", str(port))
    if build_args_dict:
        image_spec_dict["build_args"] = build_args_dict
    config_kwargs["image_spec"] = image_spec_dict

    # The Console uses agent_framework to decide which playground to render.
    # The unified app is a google-adk container (it serves the native ADK
    # reasoning_engine contract), matching the terraform deploy path.
    config_kwargs["agent_framework"] = "google-adk"

    if psc_interface_config is not None:
        config_kwargs["psc_interface_config"] = psc_interface_config

    config = AgentEngineConfig(**config_kwargs)

    # Deploy (create or update)
    action = "Updating" if matching_agents else "Creating"

    if no_wait:
        click.echo(f"\n🚀 {action} agent: {display_name} (returning immediately)...")
        operation = _start_and_record_operation(
            client, config, matching_agents, project, location
        )
        click.echo(f"\n📋 Operation started: {operation.name}")
        click.echo("   Check status with: agents-cli deploy --status")
        return None

    click.echo(f"\n🚀 {action} agent: {display_name} (this can take 5-10 minutes)...")

    operation = _start_and_record_operation(
        client, config, matching_agents, project, location
    )
    click.echo(f"   Operation: {operation.name}")
    click.echo(
        "   If this command is interrupted, run 'google-agents deploy --status' to check progress."
    )

    # Block until the operation completes
    _agent_engines_utils._await_operation(
        operation_name=operation.name,
        get_operation_fn=client.agent_engines._get_agent_operation,
    )

    # Build AgentEngine from completed operation
    completed_op = client.agent_engines._get_agent_operation(
        operation_name=operation.name,
    )
    if completed_op.error:
        clear_operation()
        raise click.ClickException(f"Deployment failed: {completed_op.error}")

    # Retrieve the newly created/updated agent engine using the public client.agent_engines.get()
    # to ensure all fields (including the api_resource name) are fully loaded and populated.
    resource_name = _get_resource_name_from_operation(operation.name)
    remote_agent = client.agent_engines.get(name=resource_name)

    # Clear secrets if explicitly set to empty
    if (
        set_secrets is not None
        and not secrets
        and matching_agents
        and remote_agent.api_resource
    ):
        clear_op = client.agent_engines._update(
            name=remote_agent.api_resource.name,
            config={
                "spec": {"deployment_spec": {"secret_env": []}},
                "update_mask": "spec.deployment_spec.secret_env",
            },
        )
        _agent_engines_utils._await_operation(
            operation_name=clear_op.name,
            get_operation_fn=client.agent_engines._get_agent_operation,
        )

    write_deployment_metadata(remote_agent, cfg)
    print_deployment_success(remote_agent, location, project, cfg)
    clear_operation()

    return remote_agent


def _start_deploy_operation(
    client: Any,
    config: AgentEngineConfig,
    matching_agents: list[Any],
    action: str,
) -> Any:
    """Start a create or update operation without waiting for completion.

    Replicates the first half of the public create()/update() methods —
    builds the API config and fires the request — but skips the blocking
    ``_await_operation()`` call.

    Returns:
        An ``AgentEngineOperation`` with ``.name`` and ``.done`` fields.
    """
    api_config = client.agent_engines._create_config(
        mode=action,
        display_name=config.display_name,
        description=config.description,
        source_packages=config.source_packages,
        entrypoint_module=config.entrypoint_module,
        entrypoint_object=config.entrypoint_object,
        class_methods=config.class_methods,
        env_vars=config.env_vars,
        service_account=config.service_account,
        requirements_file=config.requirements_file,
        labels=config.labels,
        min_instances=config.min_instances,
        max_instances=config.max_instances,
        resource_limits=config.resource_limits,
        container_concurrency=config.container_concurrency,
        identity_type=config.identity_type,
        agent_framework=config.agent_framework,
        psc_interface_config=config.psc_interface_config,
        image_spec=config.image_spec,
    )

    if matching_agents:
        return client.agent_engines._update(
            name=matching_agents[0].api_resource.name,
            config=api_config,
        )
    return client.agent_engines._create(config=api_config)


def _start_and_record_operation(
    client: Any,
    config: AgentEngineConfig,
    matching_agents: list[Any],
    project: str,
    location: str,
) -> Any:
    """Start the create/update operation and persist it so ``deploy --status``
    can recover it if the command is interrupted."""
    operation = _start_deploy_operation(
        client,
        config,
        matching_agents,
        action="update" if matching_agents else "create",
    )
    write_operation(
        operation_name=operation.name,
        project=project,
        location=location,
        deployment_target="agent_runtime",
    )
    return operation


def check_agent_runtime_operation(
    cfg: ProjectConfig,
    project: str,
    location: str = "us-east1",
) -> None:
    """Check the status of a pending Agent Runtime deploy operation."""
    op_data = read_operation()
    if not op_data:
        raise click.ClickException(
            "No pending deployment operation found.\n"
            "  Run 'agents-cli deploy' or 'agents-cli deploy --no-wait' first."
        )

    operation_name = op_data["operation_name"]
    location = location if location != "us-east1" else op_data.get("location", location)
    started_at = op_data.get("started_at", "")

    client = vertexai.Client(project=project, location=location)
    operation = client.agent_engines._get_agent_operation(
        operation_name=operation_name,
    )

    if operation.done:
        if operation.error:
            clear_operation()
            raise click.ClickException(f"Deployment failed: {operation.error}")

        # Retrieve the newly created/updated agent engine using the public client.agent_engines.get()
        # to ensure all fields (including the api_resource name) are fully loaded and populated.
        resource_name = _get_resource_name_from_operation(operation_name)
        remote_agent = client.agent_engines.get(name=resource_name)

        write_deployment_metadata(remote_agent, cfg)
        print_deployment_success(remote_agent, location, project, cfg)
        clear_operation()
    else:
        elapsed = ""
        if started_at:
            start = datetime.datetime.fromisoformat(started_at)
            delta = datetime.datetime.now(tz=datetime.UTC) - start
            minutes = int(delta.total_seconds() // 60)
            seconds = int(delta.total_seconds() % 60)
            elapsed = f" ({minutes}m {seconds}s elapsed)"

        click.echo(f"⏳ Deployment still in progress{elapsed}")
        click.echo(f"   Operation: {operation_name}")
        click.echo("   Run 'agents-cli deploy --status' again to check.")
