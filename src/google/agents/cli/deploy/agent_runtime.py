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

"""Deploy agents to Agent Runtime in Gemini platform.

De-templatized deploy module. All cookiecutter conditionals replaced
with runtime checks via ProjectConfig.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import warnings
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import agentplatform
import backoff
import click
import pathspec
import requests
from google.genai.errors import APIError
from google.genai.types import EncryptionSpec

from google.agents.cli import _sdk_compat
from google.agents.cli._agent_platform import AgentPlatformClient
from google.agents.cli._agent_platform_types import types
from google.agents.cli._project import (
    DEFAULT_FRAMEWORK,
    ProjectConfig,
    find_project_root,
    scaffold_older_than,
)
from google.agents.cli._remote import (
    FetchedAgentCard,
    build_agent_runtime_card_url,
    build_agent_runtime_passthrough_url,
    build_remote_headers,
    fetch_agent_card,
    is_transient_request_error,
)
from google.agents.cli.deploy import _build_logs
from google.agents.cli.deploy._agent_identity import (
    AGENT_IDENTITY_ROLES,
    grant_roles_to_principal,
    should_use_agent_identity,
)
from google.agents.cli.deploy._operation import (
    METADATA_FILE,
    METADATA_LOCK_FILE,
    OperationClaim,
    claim_operation,
    clear_operation,
    complete_operation,
    is_starting_claim_stale,
    parse_started_at,
    read_operation,
    read_remote_agent_runtime_id,
    write_operation,
)
from google.agents.cli.deploy._utils import (
    DEFAULT_CONCURRENCY,
    DEFAULT_CPU,
    DEFAULT_MAX_INSTANCES,
    DEFAULT_MEMORY,
    DEFAULT_MIN_INSTANCES,
    KeyValueChanges,
    MachineShape,
    parse_kv_flag,
    parse_secret_refs,
    print_table,
    resolve_service_name,
    validate_deployment_region,
)
from google.agents.cli.scaffold.utils.language import (
    a2a_base_path,
    dispatch_language,
    get_language_config,
    get_project_version,
)

# Suppress google-cloud-storage version compatibility warning
warnings.filterwarnings(
    "ignore", category=FutureWarning, module="google.cloud.aiplatform"
)


# Agent Runtime injects GOOGLE_CLOUD_PROJECT itself; setting it in
# deployment_spec.env is rejected with FAILED_PRECONDITION ("... is reserved").
# GOOGLE_CLOUD_LOCATION is NOT reserved (verified) — the LLM location can differ
# from the deploy region, so we keep it. Filtered from the propagated .env.
_AGENT_RUNTIME_RESERVED_ENV = frozenset({"GOOGLE_CLOUD_PROJECT"})
# A prebuilt image gets PORT from container_spec.port; the API rejects it in env.
_PREBUILT_IMAGE_RESERVED_ENV = _AGENT_RUNTIME_RESERVED_ENV | {"PORT"}

# Default for deploy_agent_runtime's env_var_changes, secret_changes and
# label_changes.
_NO_CHANGES = KeyValueChanges()


# The shape of spec.effective_identity that denotes Agent Identity.
# Accepts strings that follow the pattern
# agents.global.{org}.system.id.goog/resources/aiplatform/projects/{project}/locations/{location}/reasoningEngines/{engine}.
_AGENT_IDENTITY_PRINCIPAL_RE = re.compile(
    r"^agents\.global\.[^/]+\.system\.id\.goog/resources/aiplatform/"
    r"projects/[^/]+/locations/[^/]+/reasoningEngines/[^/]+$"
)


_GATEWAY_RESOURCE_RE = re.compile(r"^projects/[^/]+/locations/[^/]+/agentGateways/[^/]+$")

# Update mask paths for Agent Gateway-related fields.
_AG_UPDATE_MASK_PATH = "spec.deployment_spec.agent_gateway_config"


_AG_EGRESS_UPDATE_MASK_PATH = f"{_AG_UPDATE_MASK_PATH}.agent_to_anywhere_config"

_AG_INGRESS_UPDATE_MASK_PATH = f"{_AG_UPDATE_MASK_PATH}.client_to_agent_config"

# agent_runtime switched from reasoning-engine introspection to a container
# build (which requires a Dockerfile) in this release. Projects scaffolded
# before it never shipped a Dockerfile, so a missing one means the project
# predates the container model.
_CONTAINER_RUNTIME_VERSION = "0.6.0"

# The generated reasoning_engine adapter serves agentplatform.frameworks.adk.AdkApp
# from this release. Older adapters wrap the 1.x AdkApp, which has no artifact
# operations and answers them with 404, so those projects don't advertise them.
_ADK_ARTIFACT_METHODS_VERSION = "1.11.0"
_ADK_ARTIFACT_METHODS = frozenset(
    {
        "async_save_artifact",
        "async_load_artifact",
        "async_list_artifact_keys",
        "async_list_artifact_versions",
        "async_get_artifact_version",
        "async_delete_artifact",
        "async_list_versions",
    }
)


# Always ignored, mirroring gcloud's generated .gcloudignore defaults.
_DEFAULT_IGNORE_LINES = (".git", ".gcloudignore", ".gitignore", METADATA_LOCK_FILE)


_INCLUDE_DIRECTIVE = "#!include:"

_SERVICE_AGENT_RE = re.compile(r"[\w.-]+@[\w.-]+\.iam\.gserviceaccount\.com")

# Both service agents need the key, but the error names only one.
_AIPLATFORM_AGENT_SUFFIX = "@gcp-sa-aiplatform.iam.gserviceaccount.com"


_RUNTIME_AGENT_SUFFIX = "@gcp-sa-aiplatform-re.iam.gserviceaccount.com"

_KMS_KEY_RE = re.compile(
    r"^projects/[^/]+/locations/([^/]+)/keyRings/[^/]+/cryptoKeys/[^/]+$"
)

_AGENT_CARD_UPDATE_MASK = "spec.agent_card"

# CLI-created engines default to min_instances=0, so allow for a cold start.
_AGENT_CARD_WAIT_SECONDS = 300
_AGENT_CARD_REQUEST_TIMEOUT_SECONDS = 60
_AGENT_CARD_MAX_RETRY_DELAY_SECONDS = 10


def build_psc_interface_config(
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


def print_agent_runtime_dry_run(
    *,
    project: str,
    region: str,
    shape: MachineShape,
    update_only: bool,
    psc_interface_config: dict | None,
    agent_gateway_egress: str | None,
    agent_gateway_ingress: str | None,
    kms_key: str | None,
    label_changes: KeyValueChanges,
    agent_card_sync: bool,
    image: str | None,
) -> None:
    """Print what an Agent Runtime deploy would create or update."""
    # Fill defaults FOR DISPLAY ONLY — the real call passes raw (possibly-None)
    # values so Agent Runtime's FieldMask can preserve existing settings on update.
    shown = shape.with_defaults()
    runtime_shape = {
        "cpu": shown.cpu,
        "memory": shown.memory,
        "min_instances": shown.min_instances,
        "max_instances": shown.max_instances,
        "container_concurrency": shown.concurrency,
    }
    msg = f"  Would deploy to Agent Runtime in Gemini platform: project={project}, region={region}"
    for key, value in runtime_shape.items():
        msg += f"\n  {key}: {value}"
    msg += "\n  (defaults apply on create; existing values preserved on update)"
    if image:
        msg += f"\n  Image: {image} (prebuilt; skips the source build)"
    if update_only:
        msg += (
            "\n  --update-only: an absent engine fails the deploy "
            "rather than being created."
        )
    if psc_interface_config:
        msg += f"\n  PSC network attachment: {psc_interface_config['network_attachment']}"
        for dc in psc_interface_config.get("dns_peering_configs", []):
            msg += (
                f"\n  DNS peering: {dc['domain']}"
                f" → {dc['target_project']}/{dc['target_network']}"
            )
    if kms_key:
        msg += (
            f"\n  Encryption key: {kms_key}"
            "\n  (applied on create only; an existing agent's key cannot change)"
        )
    for label, gateway in (
        ("egress", agent_gateway_egress),
        ("ingress", agent_gateway_ingress),
    ):
        if gateway:
            msg += f"\n  Agent Gateway {label}: {gateway}"
        elif gateway is not None:
            msg += f"\n  Agent Gateway {label}: (cleared)"
    # Clear/remove only act on an existing engine's labels.
    if label_changes.clear:
        msg += "\n  Labels: all cleared (no effect on create)"
    if label_changes.updates:
        pairs = ", ".join(f"{k}={v}" for k, v in label_changes.updates.items())
        msg += f"\n  Labels to add/update: {pairs}"
    if label_changes.removals:
        removals = ", ".join(label_changes.removals)
        msg += f"\n  Labels to remove: {removals} (no effect on create)"
    if agent_card_sync:
        msg += "\n  A2A agent card: written to Agent Registry after the deploy"
    click.echo(msg)


def deploy_agent_runtime(
    *,
    cfg: ProjectConfig,
    project: str,
    display_name: str | None = None,
    location: str = "us-east1",
    description: str | None = None,
    source_packages: list[str] | None = None,
    env_var_changes: KeyValueChanges = _NO_CHANGES,
    secret_changes: KeyValueChanges = _NO_CHANGES,
    label_changes: KeyValueChanges = _NO_CHANGES,
    service_account: str | None = None,
    min_instances: int | None = None,
    max_instances: int | None = None,
    cpu: str | None = None,
    memory: str | None = None,
    container_concurrency: int | None = None,
    agent_identity: bool | None = None,
    no_wait: bool = False,
    update_only: bool = False,
    psc_interface_config: dict | None = None,
    agent_gateway_egress: str | None = None,
    agent_gateway_ingress: str | None = None,
    build_args: str | None = None,
    image: str | None = None,
    port: int | None = None,
    framework: str = DEFAULT_FRAMEWORK,
    kms_key: str | None = None,
) -> types.Runtime | None:
    """Deploy the agent to Agent Runtime in Gemini platform.

    Args:
        cfg: Project configuration from pyproject.toml.
        project: GCP project ID. Defaults to ADC project.
        display_name: Display name for the Agent Runtime agent.
        location: GCP region.
        description: Description of the agent.
        source_packages: Source packages to deploy.
        env_var_changes: Env var changes; the updates hold the project .env with
            --env-vars on top.
        secret_changes: Secret changes, with ENV_VAR: SECRET_ID[:VERSION]
            updates. The updates replace the agent's secrets; a clear with no
            updates removes them all.
        label_changes: Label changes. Removals and a clear act on the live labels, so
            they have no effect on create.
        service_account: Service account email.
        min_instances: Minimum number of instances.
        max_instances: Maximum number of instances.
        cpu: CPU limit.
        memory: Memory limit.
        container_concurrency: Container concurrency.
        agent_identity: Enable or disable Agent Identity. When a new agent is
          created, None falls back to the project's
          agent_runtime_identity_default.
          When an existing agent is updated, None leaves the current identity
          unchanged.
        no_wait: If True, start the deployment and return immediately.
        update_only: If True, fail instead of creating an engine that does not
            already exist.
        psc_interface_config: PSC interface configuration dict for private
            VPC connectivity. Contains ``network_attachment`` and optionally
            ``dns_peering_configs``.
        agent_gateway_egress: Agent Gateway to route outbound traffic through.
            A full gateway resource name binds it, an empty value unbinds it,
            and ``None`` leaves the current binding untouched.
        agent_gateway_ingress: Agent Gateway to route inbound traffic through,
            with the same three states as ``agent_gateway_egress``.
        build_args: Comma-separated KEY=VALUE build args.
        image: Prebuilt Artifact Registry image URI. Deploys it as-is instead
            of uploading the source and building from the Dockerfile.
        port: Container port.
        framework: Framework label recorded on the deployment. The Console
            reads it to pick a playground, and it selects which runtime
            contract the deployment declares.
        kms_key: Cloud KMS key for CMEK. Applied on create only; a different
            key on update fails.

    Returns:
        The deployed Runtime instance, or None when no_wait is True.
    """
    validate_deployment_region(location, "Agent Runtime")

    if kms_key:
        kms_key = _validate_kms_key(kms_key, location=location)

    display_name = display_name or resolve_service_name(cfg, None)

    if not image:
        # Agent Runtime builds from the project's Dockerfile, so upload the tree.
        auto_packaged = not source_packages
        source_packages = source_packages or _packaged_files(Path.cwd())

        if not os.path.exists("Dockerfile"):
            raise click.ClickException(_missing_dockerfile_error(cfg))
        # A present-but-ignored Dockerfile passes the check above yet breaks the
        # build, so surface it clearly instead of failing opaquely in Agent Runtime.
        if auto_packaged and "./Dockerfile" not in source_packages:
            raise click.ClickException(
                "Dockerfile is present but excluded by .gcloudignore/.gitignore.\n"
                "  Remove the matching ignore pattern so the deploy can package it."
            )
    # Only binding an egress gateway needs the CA; an empty value unbinds.
    if agent_gateway_egress:
        _require_gateway_ready_project(cfg)

    secrets = parse_secret_refs(secret_changes.updates)
    agent_gateway_config = _build_agent_gateway_config(
        egress_gateway_name=agent_gateway_egress,
        ingress_gateway_name=agent_gateway_ingress,
    )

    runtime_env_vars = _build_runtime_env_vars(
        env_vars=env_var_changes.updates,
        secrets=secrets,
        # A prebuilt image takes the port via container_spec.port instead.
        port=None if image else port,
        language=cfg.language,
        reserved=_PREBUILT_IMAGE_RESERVED_ENV if image else _AGENT_RUNTIME_RESERVED_ENV,
    )

    # Parse build args early during local preparation
    image_spec_dict: dict[str, Any] = {}
    build_args_dict = parse_kv_flag("--build-args", build_args)

    if port:
        build_args_dict.setdefault("PORT", str(port))
    if build_args_dict:
        image_spec_dict["build_args"] = build_args_dict

    # Initialize agentplatform client, v1beta1 for the identity_type field.
    client = AgentPlatformClient(
        project=project,
        location=location,
        api_version="v1beta1",
    )
    agentplatform.init(project=project, location=location)

    # Atomically claim ownership before target revalidation and mutation
    claim = claim_operation(
        project=project,
        location=location,
        deployment_target="agent_runtime",
    )

    with claim:
        matching_agents = _resolve_target_agent(client, display_name)

        # Pre-existence flag must be computed before setup_agent_identity: that call
        # creates a bare identity agent (no deployment spec), but it's still a
        # first-time spec deploy so the conservative defaults must apply.
        is_update = bool(matching_agents)

        agent_identity = should_use_agent_identity(
            agent_identity_flag=agent_identity,
            identity_default=cfg.agent_runtime_identity_default,
            is_update=is_update,
        )

        if agent_identity:
            if service_account:
                # The API rejects setting both at the same time.
                logging.warning(
                    "Agent Identity overrides --service-account: the agent runs as "
                    "its own principal, so the service account '%s' is ignored and "
                    "cleared from the agent. Drop --service-account, or pass "
                    "--no-agent-identity, to silence this warning.",
                    service_account,
                )
            service_account = ""

        # An engine whose configuration is owned elsewhere (Terraform, a platform
        # template) is unusable when this deploy creates it instead: it comes up
        # without the env vars its owner would have set. Refuse rather than create.
        if update_only and not is_update:
            raise click.ClickException(
                f"No Agent Runtime deployment named '{display_name}' exists in "
                f"{project}/{location}, and --update-only forbids creating one.\n"
                "  Create it first (for example with `agents-cli infra single-project "
                "--apply`), or drop --update-only to let this deploy create it."
            )

        if not is_update:
            # Create: no existing spec to preserve; apply the conservative shape.
            min_instances = (
                DEFAULT_MIN_INSTANCES if min_instances is None else min_instances
            )
            max_instances = (
                DEFAULT_MAX_INSTANCES if max_instances is None else max_instances
            )
            cpu = DEFAULT_CPU if cpu is None else cpu
            memory = DEFAULT_MEMORY if memory is None else memory
            container_concurrency = (
                DEFAULT_CONCURRENCY
                if container_concurrency is None
                else container_concurrency
            )

        # Set it to true if an agent without Agent Identity is being re-deployed
        # with Agent Identity.
        migrates_to_agent_identity = False

        # Shown on redeploys that omit --key.
        existing_kms_key: str | None = None

        # A bare identity agent has no deployment until this deploy adds one.
        has_deployment = False
        resource_name: str | None = None

        # None leaves labels out of the update mask, so the live ones are preserved.
        resolved_labels = dict(label_changes.updates) or None

        if matching_agents:
            matching_api_resource = matching_agents[0].api_resource
            assert matching_api_resource is not None, "listed agent has no api_resource"
            resource_name = matching_api_resource.name
            # list() may return a summary without deployment_spec; get() guarantees
            # the full env/resource_limits are populated.
            existing = client.runtimes.get(name=resource_name)
            existing_spec = existing.api_resource.spec
            existing_kms_key = _existing_kms_key(existing)
            if kms_key:
                _check_kms_key_unchanged(
                    requested=kms_key,
                    existing=existing_kms_key,
                    display_name=display_name,
                )
            _check_deployment_source_unchanged(
                existing_spec, image=image, display_name=display_name
            )
            has_deployment = bool(
                existing_spec
                and (
                    existing_spec.source_code_spec
                    or existing_spec.package_spec
                    or existing_spec.container_spec
                )
            )
            # container_spec is replaced whole, so keep a port set by an earlier deploy.
            if image and port is None and existing_spec and existing_spec.container_spec:
                port = existing_spec.container_spec.port
            migrates_to_agent_identity = (
                agent_identity
                and is_update
                and not _is_agent_identity_principal(
                    existing_spec.effective_identity if existing_spec else None
                )
            )
            # Preserve env vars set outside this deploy; CLI/user values still win.
            for key, value in _existing_plain_env_vars(existing).items():
                runtime_env_vars.setdefault(key, value)
            # The labels mask path replaces the whole map, so send it in full.
            if label_changes.updates or label_changes.removals or label_changes.clear:
                resolved_labels = label_changes.apply(_existing_labels(existing))
            # Point the A2A agent card at the real Agent Runtime HTTP passthrough
            # instead of localhost; needs the existing engine's resource name, so a
            # first-time create picks it up on the next deploy.
            runtime_env_vars.setdefault(
                "APP_URL", build_agent_runtime_passthrough_url(location, resource_name)
            )
            # When only one of cpu/memory is set, fill the other half from the live
            # spec so config_kwargs["resource_limits"] gets a complete pair. When both
            # are None a plain redeploy must omit resource_limits to preserve the live
            # value — only fill when exactly one side was explicitly supplied.
            if (cpu is None) ^ (memory is None):
                dep = existing_spec.deployment_spec if existing_spec else None
                limits = (dep.resource_limits if dep else None) or {}
                existing_cpu = limits.get("cpu")
                existing_memory = limits.get("memory")
                cpu = cpu if cpu is not None else existing_cpu
                memory = memory if memory is not None else existing_memory
                if cpu is None or memory is None:
                    logging.warning(
                        "Could not resolve the existing %s to pair with the supplied value; "
                        "resource_limits left unchanged for this update.",
                        "memory" if memory is None else "cpu",
                    )

        config_kwargs: dict[str, Any] = {
            "display_name": display_name,
            "env_vars": runtime_env_vars,
            "service_account": service_account,
            "identity_type": _resolve_identity_type(agent_identity),
            "description": description,
            "labels": resolved_labels,
            "min_instances": min_instances,
            "max_instances": max_instances,
            "container_concurrency": container_concurrency,
            "resource_limits": {"cpu": cpu, "memory": memory}
            if (cpu is not None and memory is not None)
            else None,
            # The Console uses agent_framework to decide which playground to render.
            # It also selects which runtime contract the deployment declares.
            "agent_framework": framework,
        }

        # Agent Runtime serves the container over HTTP, so no entrypoint module is
        # needed: either a prebuilt image or the source plus its build config.
        container_spec: types.ReasoningEngineSpecContainerSpecDict | None = None
        if image:
            container_spec = {"image_uri": image}
            if port:
                container_spec["port"] = port
            config_kwargs["container_spec"] = container_spec
        else:
            config_kwargs["source_packages"] = list(source_packages or [])
            config_kwargs["image_spec"] = image_spec_dict

        if framework == DEFAULT_FRAMEWORK:
            class_methods_builder = dispatch_language(
                "deploy", CLASS_METHODS_BUILDERS, cfg.language
            )
            class_methods = class_methods_builder()
            if scaffold_older_than(cfg, _ADK_ARTIFACT_METHODS_VERSION):
                class_methods = [
                    m for m in class_methods if m["name"] not in _ADK_ARTIFACT_METHODS
                ]
                logging.info(
                    "Skipping artifact methods: this project was scaffolded with "
                    "agents-cli %s. Run `agents-cli scaffold upgrade` to enable them.",
                    cfg.acli_version,
                )
            config_kwargs["class_methods"] = class_methods

        if psc_interface_config is not None:
            config_kwargs["psc_interface_config"] = psc_interface_config

        if agent_gateway_config is not None:
            config_kwargs["agent_gateway_config"] = agent_gateway_config

        # Validate local SDK request-config construction prior to any remote mutation
        action_mode = "update" if (matching_agents or agent_identity) else "create"

        # encryption_spec is immutable; keep it out of the update mask. A new
        # Agent Identity agent gets the key from setup_agent_identity instead.
        if kms_key and action_mode == "create":
            config_kwargs["encryption_spec"] = EncryptionSpec(kms_key_name=kms_key)

        config = types.AgentRuntimeConfig(**config_kwargs)
        api_config = _create_api_config(
            client=client,
            config=config,
            action=action_mode,
            agent_gateway_egress=agent_gateway_egress,
            agent_gateway_ingress=agent_gateway_ingress,
        )

        click.echo("\n🤖 Deploying agent to Agent Runtime in Gemini platform...\n")

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
        if agent_identity is not None:
            params.append(("Agent Identity", "Enabled" if agent_identity else "Disabled"))
        if psc_interface_config:
            params.append(
                (
                    "Network Attachment",
                    psc_interface_config.get("network_attachment", "—"),
                )
            )
            for i, dc in enumerate(psc_interface_config.get("dns_peering_configs", [])):
                params.append(
                    (
                        f"DNS Peering [{i}]",
                        f"{dc.get('domain', '')} → {dc.get('target_project', '')}/{dc.get('target_network', '')}",
                    )
                )
        if agent_gateway_config:
            if agent_gateway_egress is not None:
                egress_cfg = agent_gateway_config.agent_to_anywhere_config
                new_val = egress_cfg.agent_gateway if egress_cfg else "(cleared)"
                params.append(("Agent Gateway (egress)", new_val))
            if agent_gateway_ingress is not None:
                ingress_cfg = agent_gateway_config.client_to_agent_config
                new_val = ingress_cfg.agent_gateway if ingress_cfg else "(cleared)"
                params.append(("Agent Gateway (ingress)", new_val))
        if image:
            params.append(("Image", image))
        if port:
            params.append(("Port", port))
        if build_args:
            params.append(("Build Args", build_args))
        if kms_key or existing_kms_key:
            params.append(("Encryption Key", kms_key or existing_kms_key))
        for name, value in params:
            click.echo(f"  {name}: {value}")

        if runtime_env_vars:
            click.echo("\n🌍 Environment Variables:")
            for key, value in sorted(runtime_env_vars.items()):
                click.echo(f"  {key}: {format_env_value(value)}")

        # Setup agent identity on first deployment (remote identity creation pre-step)
        if agent_identity and not matching_agents:
            claim.mark_mutation_started()
            matching_agents = [
                setup_agent_identity(client, project, display_name, kms_key)
            ]
            api_resource = matching_agents[0].api_resource
            assert api_resource is not None, "identity agent has no api_resource"
            resource_name = api_resource.name
            if not resource_name:
                raise click.ClickException(
                    f"Agent Runtime created the agent identity for {display_name} "
                    "but returned no resource name. Rerun `agents-cli deploy` to retry."
                )
            runtime_env_vars["APP_URL"] = build_agent_runtime_passthrough_url(
                location, resource_name
            )
            config_kwargs["env_vars"] = runtime_env_vars
            config = types.AgentRuntimeConfig(**config_kwargs)
            api_config = _create_api_config(
                client=client,
                config=config,
                action="update",
                agent_gateway_egress=agent_gateway_egress,
                agent_gateway_ingress=agent_gateway_ingress,
            )

        if container_spec and resource_name and not has_deployment:
            try:
                _deploy_image_first(
                    client,
                    matching_agents=matching_agents,
                    container_spec=container_spec,
                    project=project,
                    location=location,
                    claim=claim,
                )
            except click.ClickException:
                claim.completed = True
                clear_operation(owner_id=claim.owner_id)
                raise

        # Remote submit mutation
        action = "Updating" if matching_agents else "Creating"
        wait_note = (
            "not waiting for completion" if no_wait else "this can take a few minutes"
        )
        click.echo(f"\n🚀 {action} agent: {display_name} ({wait_note})...")

        claim.mark_mutation_started()
        started_at = datetime.datetime.now(tz=datetime.UTC)
        operation_name = _start_and_record_operation(
            client=client,
            matching_agents=matching_agents,
            project=project,
            location=location,
            api_config=api_config,
            config=config,
            agent_gateway_egress=agent_gateway_egress,
            agent_gateway_ingress=agent_gateway_ingress,
            claim=claim,
        )
        logs_url = build_agent_engine_logs_url(operation_name, project)

        click.echo(f"   Operation: {operation_name}\n   Monitor deploy logs: {logs_url}")
        if no_wait:
            click.echo("   Check status with: agents-cli deploy --status")
            claim.completed = True
            return None
        click.echo(
            "   If this command is interrupted, run 'agents-cli deploy --status' to check progress."
        )

        completed_op = _sdk_compat.wait_for_operation(
            client, operation_name=operation_name
        )
        if completed_op.error:
            claim.completed = True
            clear_operation(owner_id=claim.owner_id)
            raise click.ClickException(
                _deployment_failed_message(
                    completed_op.error,
                    operation_name=operation_name,
                    project=project,
                    started_at=started_at,
                )
            )

        # Retrieve the newly created/updated Agent Runtime agent using the public client.runtimes.get()
        # to ensure all fields (including the api_resource name) are fully loaded and populated.
        resource_name = _get_resource_name_from_operation(operation_name)
        remote_agent = client.runtimes.get(name=resource_name)

        if migrates_to_agent_identity:
            grant_agent_identity_roles(project, remote_agent)

        # The update leaves secret_env alone when it sets no secrets, so a clear
        # with nothing to set needs its own update.
        if (
            secret_changes.clear
            and not secrets
            and matching_agents
            and remote_agent.api_resource
        ):
            clear_op_name = _sdk_compat.start_update(
                client,
                name=remote_agent.api_resource.name,
                config={
                    "spec": {"deployment_spec": {"secret_env": []}},
                    "update_mask": "spec.deployment_spec.secret_env",
                },
            )
            _sdk_compat.wait_for_operation(client, operation_name=clear_op_name)

        write_deployment_metadata(remote_agent, cfg, owner_id=claim.owner_id)
        claim.completed = True
        if cfg.is_a2a:
            _sync_agent_card(
                client, remote_agent, cfg, resource_name=resource_name, location=location
            )
        print_deployment_success(remote_agent, location, project, cfg)

        return remote_agent


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

    operation_name = op_data.get("operation_name")
    if not operation_name:
        state = op_data.get("state", "starting")
        started_at = op_data.get("started_at", "unknown")
        owner_id = op_data.get("owner_id", "unknown")
        if is_starting_claim_stale(op_data):
            clear_operation(owner_id=owner_id)
            click.echo(
                f"Cleared stale deployment claim '{owner_id}' (state='{state}', started at {started_at}) "
                "because the deploying process is no longer running."
            )
            return
        raise click.ClickException(
            f"Deployment operation is in '{state}' state (owner: {owner_id}, started at {started_at}) "
            "without a confirmed remote operation name.\n"
            "  The deployment process may still be initiating the remote operation, or was interrupted.\n"
            "  Reconcile deployment_metadata.json or check Google Cloud Console before retrying."
        )

    location = location if location != "us-east1" else op_data.get("location", location)
    started_at = op_data.get("started_at", "")
    owner_id = op_data.get("owner_id")

    client = AgentPlatformClient(project=project, location=location)
    operation = _sdk_compat.get_operation(client, operation_name=operation_name)

    if operation.done:
        if operation.error:
            clear_operation(owner_id=owner_id)
            raise click.ClickException(
                _deployment_failed_message(
                    operation.error,
                    operation_name=operation_name,
                    project=project,
                    started_at=parse_started_at(started_at),
                )
            )

        # Retrieve the newly created/updated Agent Runtime agent using the public client.runtimes.get()
        # to ensure all fields (including the api_resource name) are fully loaded and populated.
        resource_name = _get_resource_name_from_operation(operation_name)
        remote_agent = client.runtimes.get(name=resource_name)

        write_deployment_metadata(remote_agent, cfg, owner_id=owner_id)
        if cfg.is_a2a:
            _sync_agent_card(
                client, remote_agent, cfg, resource_name=resource_name, location=location
            )
        print_deployment_success(remote_agent, location, project, cfg)
        clear_operation(owner_id=owner_id)
    else:
        elapsed = ""
        start = parse_started_at(started_at)
        if start is not None:
            delta = datetime.datetime.now(tz=datetime.UTC) - start
            minutes = int(delta.total_seconds() // 60)
            seconds = int(delta.total_seconds() % 60)
            elapsed = f" ({minutes}m {seconds}s elapsed)"

        click.echo(
            f"⏳ Deployment still in progress{elapsed}\n"
            f"   Operation: {operation_name}\n"
            f"   Monitor deploy logs: {build_agent_engine_logs_url(operation_name, project)}\n"
            "   Run 'agents-cli deploy --status' again to check."
        )


def list_agent_runtime_deployments(project: str, location: str) -> None:
    """List Agent Runtime deployments via the Gemini platform SDK."""
    client = AgentPlatformClient(project=project, location=location)
    agents = list(client.runtimes.list())

    if not agents:
        click.echo(f"No Agent Runtime deployments found in {project} ({location}).")
        return

    from rich.table import Table

    table = Table(
        title=f"Agent Runtime deployments in Gemini platform — {project} ({location})"
    )
    table.add_column("Display Name", style="bold")
    table.add_column("Resource Name", style="dim")
    table.add_column("Create Time")
    table.add_column("Encryption")

    for agent in agents:
        res = agent.api_resource
        display_name = (res.display_name if res else None) or "—"
        name = (res.name if res else None) or "—"
        create_time = res.create_time if res else None
        time_str = create_time.strftime("%Y-%m-%d %H:%M") if create_time else "—"
        encryption_spec = res.encryption_spec if res else None
        kms_key = encryption_spec.kms_key_name if encryption_spec else None
        encryption = kms_key.rsplit("/", 1)[-1] if kms_key else "Google-managed"
        table.add_row(display_name, name, time_str, encryption)

    print_table(table)


def _validate_kms_key(kms_key: str, *, location: str) -> str:
    """Return the stripped ``kms_key``; raise if malformed or not in ``location``."""
    kms_key = kms_key.strip()
    match = _KMS_KEY_RE.match(kms_key)
    if not match:
        raise click.ClickException(
            f"Invalid value for --key: {kms_key!r}.\n"
            "  Expected a full Cloud KMS crypto key resource name:\n"
            "    projects/PROJECT/locations/LOCATION/keyRings/RING/cryptoKeys/KEY"
        )
    key_location = match.group(1)
    if key_location != location:
        raise click.ClickException(
            f"The --key is in '{key_location}' but the agent deploys to "
            f"'{location}'.\n"
            "  Agent Runtime requires a single-region key in the same region as the "
            "agent\n"
            "  (multi-region, dual-region and global keys are not supported).\n"
            f"  Use a key in '{location}', or deploy to '{key_location}' with "
            f"--region {key_location}."
        )
    return kms_key


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


def _require_gateway_ready_project(cfg: ProjectConfig) -> None:
    """Check the project was scaffolded with Agent Gateway support."""
    if not cfg.agent_gateway:
        raise click.ClickException(
            "This project was not scaffolded with Agent Gateway support.\n"
            "  An egress gateway terminates TLS, so the image must trust its root CA.\n"
            "  Add that setup to the Dockerfile:\n"
            "    agents-cli scaffold enhance . --agent-gateway"
        )


def _build_agent_gateway_config(
    *, egress_gateway_name: str | None, ingress_gateway_name: str | None
) -> types.ReasoningEngineSpecDeploymentSpecAgentGatewayConfig | None:
    """Build the reasoning engine's ``agent_gateway_config``."""
    if egress_gateway_name is None and ingress_gateway_name is None:
        return None

    egress = ingress = None
    if egress_gateway_name:
        egress = types.ReasoningEngineSpecDeploymentSpecAgentGatewayConfigAgentToAnywhereConfig(
            agent_gateway=_validate_gateway_name(
                egress_gateway_name, name="egress gateway"
            )
        )
    if ingress_gateway_name:
        ingress = (
            types.ReasoningEngineSpecDeploymentSpecAgentGatewayConfigClientToAgentConfig(
                agent_gateway=_validate_gateway_name(
                    ingress_gateway_name, name="ingress gateway"
                )
            )
        )
    return types.ReasoningEngineSpecDeploymentSpecAgentGatewayConfig(
        agent_to_anywhere_config=egress,
        client_to_agent_config=ingress,
    )


def _validate_gateway_name(gateway: str, *, name: str) -> str:
    """Check that the value looks like an Agent Gateway resource name."""
    if not _GATEWAY_RESOURCE_RE.match(gateway):
        raise click.ClickException(
            f"Invalid value for {name}: {gateway!r}.\n"
            "  Expected a full Agent Gateway resource name:\n"
            "    projects/PROJECT/locations/LOCATION/agentGateways/GATEWAY"
        )
    return gateway


def _build_runtime_env_vars(
    *,
    env_vars: Mapping[str, str],
    secrets: dict[str, dict[str, str]],
    port: int | None = None,
    language: str,
    reserved: frozenset[str] = _AGENT_RUNTIME_RESERVED_ENV,
) -> dict[str, Any]:
    """Assemble the runtime env vars for the deployed Agent Runtime.

    Precedence (highest first): ``--secrets``, then ``env_vars`` (the project
    ``.env`` with ``--env-vars`` on top), then these overridable defaults.
    Names in ``reserved`` (by default ``GOOGLE_CLOUD_PROJECT``) are dropped:
    Agent Runtime injects them and rejects them.
    The backend defaults to Gemini platform (at ``GOOGLE_CLOUD_LOCATION=global``) unless
    the ``.env`` supplies an AI Studio API key:

    - ``AGENT_VERSION`` — the project version (parsed from correct manifest file
      ex. pyproject.toml or fallback value), read at runtime by the A2A agent card.
      Read only when the user hasn't supplied a value, so an override skips
      the read and its missing-version warning.
    - ``PORT`` — the container port, when one is supplied.
    - telemetry toggles — Cloud Trace export and prompt/response capture, off by
      default: Go uses ``false`` (opt-in; set ``true`` for the completions view),
      Python keeps ``NO_CONTENT`` (content goes to GCS via the completion hook).
    """
    runtime_env_vars: dict[str, Any] = dict(env_vars)
    runtime_env_vars.update(secrets)  # type: ignore[arg-type]
    # Agent Runtime injects these itself; including them in deployment_spec.env is
    # rejected with FAILED_PRECONDITION ("... is reserved").
    for name in reserved & runtime_env_vars.keys():
        logging.warning(
            "Ignoring reserved Agent Runtime env var %s — it is set by the platform.",
            name,
        )
        del runtime_env_vars[name]
    # Skip the version read (and its warning) when the user has already supplied one.
    if "AGENT_VERSION" not in runtime_env_vars:
        runtime_env_vars["AGENT_VERSION"] = get_project_version(
            find_project_root() or Path.cwd()
        )
    if port:
        runtime_env_vars.setdefault("PORT", str(port))
    # agent_runtime runs on Gemini platform: default to it (LLM at global) unless the
    # .env opted into AI Studio with an API key.
    if (
        "GEMINI_API_KEY" not in runtime_env_vars
        and "GOOGLE_API_KEY" not in runtime_env_vars
    ):
        # Respect either name from the user's .env: GOOGLE_GENAI_USE_ENTERPRISE wins
        # over GOOGLE_GENAI_USE_VERTEXAI, so adding it would override an explicit
        # GOOGLE_GENAI_USE_VERTEXAI=false.
        if (
            "GOOGLE_GENAI_USE_ENTERPRISE" not in runtime_env_vars
            and "GOOGLE_GENAI_USE_VERTEXAI" not in runtime_env_vars
        ):
            # Set the legacy name for every language, plus the name the language's
            # SDK reads (GOOGLE_GENAI_USE_ENTERPRISE for Python). The legacy one
            # stays for Python projects locked to google-adk < 2.3.0 /
            # google-genai < 1.74.0, which deploy with the latest CLI from CI (no
            # .env there) and ignore the new name.
            backend_env_var = get_language_config(language)["genai_backend_env_var"]
            runtime_env_vars["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
            runtime_env_vars[backend_env_var] = "true"
        runtime_env_vars.setdefault("GOOGLE_CLOUD_LOCATION", "global")
    runtime_env_vars.setdefault("GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY", "true")
    # Prompt/response content capture, off by default (opt-in).
    runtime_env_vars.setdefault(
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
        get_language_config(language)["otel_capture_message_content_default"],
    )
    # Fail closed: ADK defaults content-in-spans to true; keep it off for bare deploys.
    runtime_env_vars.setdefault("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "false")
    return runtime_env_vars


def _resolve_target_agent(
    client: AgentPlatformClient,
    display_name: str,
) -> list[Any]:
    """Resolve the target Agent Runtime agent by ID or display name.

    Target resolution:
    1. Prefer the ID recorded in metadata.
    2. If it does not exist, fall back to listing by display_name, but only if unambiguous.
    """
    target_id = read_remote_agent_runtime_id()

    matching_agents: list[Any] = []

    if target_id:
        try:
            # get() guarantees full env/resource_limits are populated
            existing = client.runtimes.get(name=target_id)
            # If the engine was found but its display name was changed out of band, warn or fail?
            # Issue requests: "Before mutation, validate its project number, location, deployment target, resource name, and actual display name. Reject stale or inconsistent metadata."
            if existing.api_resource.display_name != display_name:
                raise click.ClickException(
                    f"Agent Runtime '{target_id}' has display name "
                    f"'{existing.api_resource.display_name}', but expected '{display_name}'.\n"
                    "  If this is correct, pass the correct --service-name.\n"
                    f"  If the metadata in {METADATA_FILE} is stale, delete it and try again."
                )
            matching_agents = [existing]
        except APIError as e:
            if e.code == 404:
                raise click.ClickException(
                    f"Agent Runtime '{target_id}' not found.\n"
                    f"  The resource recorded in {METADATA_FILE} may have been deleted.\n"
                    "  Delete the metadata file to deploy a new engine, or check your permissions."
                ) from e
            raise click.ClickException(
                f"Error reading Agent Runtime '{target_id}': {e}"
            ) from e
    else:
        # Fall back to display_name scan
        existing_agents = list(client.runtimes.list())
        matching_agents = [
            agent
            for agent in existing_agents
            if agent.api_resource.display_name == display_name
        ]
        if len(matching_agents) > 1:
            raise click.ClickException(
                f"Found {len(matching_agents)} Agent Runtime deployments named '{display_name}'.\n"
                "  A display-name update is ambiguous. Delete the duplicates or "
                f"ensure the correct resource ID is recorded in {METADATA_FILE}."
            )

    return matching_agents


def _existing_kms_key(agent: types.Runtime) -> str | None:
    """The customer-managed encryption key on a deployed Agent Runtime, if any."""
    api_resource = agent.api_resource
    encryption_spec = api_resource.encryption_spec if api_resource else None
    return encryption_spec.kms_key_name if encryption_spec else None


def _check_kms_key_unchanged(
    *, requested: str, existing: str | None, display_name: str
) -> None:
    """Raise unless ``requested`` matches ``existing``; encryption is fixed at Agent Runtime creation.

    Rotation keeps the crypto key name, so a rotated key still matches.
    """
    if requested == existing:
        return
    if existing is None:
        problem = f"Agent '{display_name}' is not encrypted with a customer-managed key."
    else:
        problem = (
            f"Agent '{display_name}' is already encrypted with a different key:\n"
            f"    current:   {existing}\n"
            f"    requested: {requested}"
        )
    raise click.ClickException(
        f"{problem}\n"
        "  An Agent Runtime's encryption key is set at creation and cannot be "
        "changed.\n"
        "  To use this key, re-create the agent."
    )


def _check_deployment_source_unchanged(
    spec: types.ReasoningEngineSpec | None, *, image: str | None, display_name: str
) -> None:
    """Raise if the deploy would switch between a prebuilt image and a source build.

    The API rejects the switch in both directions once an agent has a deployment.
    """
    if spec is None:
        return
    if image and (spec.source_code_spec or spec.package_spec):
        # Includes agents the scaffolded Terraform provisions with a placeholder.
        problem = "deploys from source, so it can't switch to a prebuilt --image"
    elif not image and spec.container_spec:
        problem = "deploys a prebuilt image, so it can't switch to a source build"
    else:
        return
    raise click.ClickException(
        f"Agent '{display_name}' {problem}: Agent Runtime doesn't allow changing "
        "how an existing agent deploys.\n"
        "  Deploy it the same way as before, or pass --service-name to deploy a "
        "new agent."
    )


def _is_agent_identity_principal(effective_identity: str | None) -> bool:
    """Whether an effective identity is an Agent Identity principal."""
    return bool(
        effective_identity and _AGENT_IDENTITY_PRINCIPAL_RE.match(effective_identity)
    )


def _existing_plain_env_vars(agent: types.Runtime) -> dict[str, str]:
    """Plain env vars on a deployed Agent Runtime, as ``{name: value}``.

    An update replaces the whole ``deployment_spec.env`` block, so re-sending
    these preserves vars set outside this deploy. Secrets are skipped: the API
    only touches them when secrets are supplied.
    """
    api_resource = agent.api_resource
    spec = api_resource.spec if api_resource else None
    deployment_spec = spec.deployment_spec if spec else None
    env = deployment_spec.env if deployment_spec else None
    result: dict[str, str] = {}
    for var in env or []:
        if var.name:
            result[var.name] = var.value or ""
    return result


def _existing_labels(agent: types.Runtime) -> dict[str, str]:
    """Already existing Agent Runtime labels."""
    if not agent.api_resource:
        return {}
    return agent.api_resource.labels or {}


def _resolve_identity_type(agent_identity: bool | None) -> types.IdentityType | None:
    if agent_identity is None:
        return None
    if agent_identity:
        return types.IdentityType.AGENT_IDENTITY
    return types.IdentityType.SERVICE_ACCOUNT


def _adk_python_class_methods() -> list[dict[str, str]]:
    """Runtime contract exposed by the ADK Python Agent.

    Required to query the agent using the Gemini platform SDK after deployment.
    """
    from agentplatform.frameworks.adk import AdkApp

    # register_operations returns the same, hardcoded list for any instance of AdkApp
    # so we can just use object.__new__ to create a dummy instance.
    operations = AdkApp.register_operations(object.__new__(AdkApp))
    return [
        {"name": name, "api_mode": api_mode}
        for api_mode, names in operations.items()
        for name in names
    ]


def _adk_go_class_methods() -> list[dict[str, str]]:
    """Runtime contract exposed by the ADK Go Agent.

    Mirrors the operations that adk-go registers in its `agentengine` handler
    (https://github.com/google/adk-go/blob/main/server/agentengine/handler.go#L108).
    Keep in sync with the Go handler.
    """
    return [
        {"name": "async_create_session", "api_mode": "async"},
        {"name": "async_get_session", "api_mode": "async"},
        {"name": "async_list_sessions", "api_mode": "async"},
        {"name": "async_delete_session", "api_mode": "async"},
        {"name": "async_stream_query", "api_mode": "async_stream"},
        {"name": "streaming_agent_run_with_events", "api_mode": "async_stream"},
    ]


# Agent Runtime records a ``class_methods`` contract that clients (the Gemini
# platform SDK) read to reconstruct the callable methods on an Agent Runtime.
CLASS_METHODS_BUILDERS: dict[str, Callable[[], list[dict[str, str]]] | None] = {
    "python": _adk_python_class_methods,
    "go": _adk_go_class_methods,
    "java": None,
    "typescript": None,
    "kotlin": None,
}


def _create_api_config(
    *,
    client: AgentPlatformClient,
    config: types.AgentRuntimeConfig,
    action: str,
    agent_gateway_egress: str | None,
    agent_gateway_ingress: str | None,
) -> types.UpdateRuntimeConfigDict:
    """Create a config for create/update operations with a patched update mask."""
    api_config = _sdk_compat.build_config(
        client,
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
        agent_gateway_config=config.agent_gateway_config,
        image_spec=config.image_spec,
        container_spec=config.container_spec,
        encryption_spec=config.encryption_spec,
    )

    # build_config can only generate a config that updates both gateways at once,
    # we need to patch the update mask with more granular paths.
    update_mask = api_config.get("update_mask")
    if update_mask:
        paths = [p for p in update_mask.split(",") if p != _AG_UPDATE_MASK_PATH]
        if agent_gateway_egress is not None:
            paths.append(_AG_EGRESS_UPDATE_MASK_PATH)
        if agent_gateway_ingress is not None:
            paths.append(_AG_INGRESS_UPDATE_MASK_PATH)
        api_config["update_mask"] = ",".join(paths)

    return api_config


def format_env_value(value: Any) -> str:
    """Format an env var value for display, masking secrets."""
    if isinstance(value, dict) and "secret" in value and "version" in value:
        return f"[secret:{value['secret']}:{value['version']}]"
    return str(value)


def setup_agent_identity(
    client: Any, project: str, display_name: str, kms_key: str | None = None
) -> types.Runtime:
    """Create an agent first, so we know which principal should be granted the IAM roles."""
    click.echo(f"\n🔧 Creating agent identity for: {display_name}")
    encryption_spec = EncryptionSpec(kms_key_name=kms_key) if kms_key else None
    try:
        agent = client.runtimes.create(
            config={
                "identity_type": types.IdentityType.AGENT_IDENTITY,
                "display_name": display_name,
                "encryption_spec": encryption_spec,
            }
        )
    except APIError as exc:
        raise _request_failed_error("create", exc, kms_key) from exc
    grant_agent_identity_roles(project, agent)
    return agent


def _deploy_image_first(
    client: AgentPlatformClient,
    *,
    matching_agents: list[Any],
    container_spec: types.ReasoningEngineSpecContainerSpecDict,
    project: str,
    location: str,
    claim: OperationClaim,
) -> None:
    """Deploy only the image to an agent that has no deployment yet.

    The API rejects a deployment_spec update on such an agent unless it carries
    source, and container_spec doesn't count, so the image has to land first.
    """
    click.echo(
        "\n📦 Deploying the image before applying its configuration "
        "(this step waits even with --no-wait)..."
    )
    started_at = datetime.datetime.now(tz=datetime.UTC)
    # Records the operation so `deploy --status` can find it if this step is interrupted.
    operation_name = _start_and_record_operation(
        client=client,
        matching_agents=matching_agents,
        project=project,
        location=location,
        api_config={
            "spec": {"container_spec": container_spec},
            "update_mask": "spec.container_spec",
        },
        claim=claim,
    )
    logs_url = build_agent_engine_logs_url(operation_name, project)
    click.echo(f"   Operation: {operation_name}\n   Monitor deploy logs: {logs_url}")
    operation = _sdk_compat.wait_for_operation(client, operation_name=operation_name)
    if operation.error:
        raise click.ClickException(
            _deployment_failed_message(
                operation.error,
                operation_name=operation_name,
                project=project,
                started_at=started_at,
            )
        )


def _request_failed_error(
    action: str, exc: APIError, kms_key: str | None
) -> click.ClickException:
    """A readable error for a failed Agent Runtime request."""
    api_message = exc.message or ""
    hint = _cmek_access_hint(api_message) if kms_key and kms_key in api_message else ""
    return click.ClickException(
        f"Agent Runtime {action} request failed — "
        f"{exc.code} {exc.status}: {exc.message}{hint}"
    )


def _cmek_access_hint(api_message: str) -> str:
    """Hint naming the role and principals the CMEK key needs."""
    match = _SERVICE_AGENT_RE.search(api_message)
    principal = match.group(0) if match else "the service agent named in the error"
    sibling = _cmek_sibling_agent(principal) if match else None
    grantees = f"{principal}\n        {sibling}" if sibling else principal
    return (
        "\n\n  Gemini platform could not use this CMEK key. Check:\n"
        "    • if the key is enabled\n"
        "    • roles/cloudkms.cryptoKeyEncrypterDecrypter on the key for:\n"
        f"        {grantees}\n"
        "  Granted on the key itself.\n"
        "  A new grant can take up to 10 minutes to become effective."
    )


def _cmek_sibling_agent(principal: str) -> str | None:
    """The other CMEK service agent in ``principal``'s project, if recognized."""
    for suffix, sibling_suffix in (
        (_AIPLATFORM_AGENT_SUFFIX, _RUNTIME_AGENT_SUFFIX),
        (_RUNTIME_AGENT_SUFFIX, _AIPLATFORM_AGENT_SUFFIX),
    ):
        if principal.endswith(suffix):
            return principal.removesuffix(suffix) + sibling_suffix
    return None


def grant_agent_identity_roles(project: str, agent: types.Runtime) -> None:
    """Grant the baseline project roles to an agent's own principal."""
    api_resource = agent.api_resource
    spec = api_resource.spec if api_resource else None
    effective_identity = spec.effective_identity if spec else None
    if not _is_agent_identity_principal(effective_identity):
        logging.warning(
            "The agent's effective identity is '%s', not an Agent Identity "
            "principal, so these roles were not granted: %s",
            effective_identity or "(unset)",
            ", ".join(AGENT_IDENTITY_ROLES),
        )
        return

    grant_roles_to_principal(project, f"principal://{effective_identity}")


def _start_and_record_operation(
    *,
    client: AgentPlatformClient,
    matching_agents: list[Any],
    project: str,
    location: str,
    api_config: types.UpdateRuntimeConfigDict | None = None,
    config: types.AgentRuntimeConfig | None = None,
    agent_gateway_egress: str | None = None,
    agent_gateway_ingress: str | None = None,
    claim: OperationClaim | None = None,
) -> str:
    """Start the create/update operation and persist it so ``deploy --status``
    can recover it if the command is interrupted."""
    action = "update" if matching_agents else "create"
    if api_config is None:
        if config is None:
            raise ValueError("Either api_config or config must be provided.")
        api_config = _create_api_config(
            client=client,
            config=config,
            action=action,
            agent_gateway_egress=agent_gateway_egress,
            agent_gateway_ingress=agent_gateway_ingress,
        )

    if claim:
        claim.mark_mutation_started()

    try:
        if matching_agents:
            operation_name = _sdk_compat.start_update(
                client,
                name=matching_agents[0].api_resource.name,
                config=api_config,
            )
        else:
            operation_name = _sdk_compat.start_create(client, config=api_config)
    except APIError as exc:
        encryption_spec = config.encryption_spec if config else None
        kms_key = encryption_spec.kms_key_name if encryption_spec else None
        raise _request_failed_error(action, exc, kms_key) from exc
    except Exception as exc:
        raise click.ClickException(
            f"Agent Runtime {action} request failed — {type(exc).__name__}: {exc}"
        ) from exc

    if claim:
        claim.record_operation(operation_name)
    else:
        write_operation(
            operation_name=operation_name,
            project=project,
            location=location,
            deployment_target="agent_runtime",
        )
    return operation_name


def build_agent_engine_logs_url(operation_name: str, project: str) -> str:
    """Build a Logs Explorer URL for the engine's logs, keyed off the LRO name."""
    return _build_logs.engine_logs_url(
        project=project, engine_id=_engine_id_from_operation(operation_name)
    )


def _deployment_failed_message(
    error: Any,
    *,
    operation_name: str,
    project: str,
    started_at: datetime.datetime | None,
) -> str:
    """The error for a failed deploy operation.

    A remote build failure only reports a generic "Build failed", so append the
    tail of the build log, which holds the real cause.
    """
    message = f"Deployment failed: {error}"
    if not _build_logs.is_build_failure(error):
        return message
    details = _build_logs.build_log_details(
        project=project,
        engine_id=_engine_id_from_operation(operation_name),
        started_at=started_at,
    )
    return f"{message}\n\n{details}"


def _engine_id_from_operation(operation_name: str) -> str:
    """The ReasoningEngine ID (last path segment) from a long-running operation name."""
    return _get_resource_name_from_operation(operation_name).split("/")[-1]


def _get_resource_name_from_operation(operation_name: str) -> str:
    """Extract ReasoningEngine resource name from long-running operation name.

    GCP long-running operations on specific resources are guaranteed by API
    standards to end with "/operations/{operation_id}". Extract the full
    resource name by partitioning on "/operations/".
    """
    resource_name, _, _ = operation_name.rpartition("/operations/")
    return resource_name


def write_deployment_metadata(
    remote_agent: types.Runtime,
    cfg: ProjectConfig,
    owner_id: str | None = None,
) -> None:
    """Write deployment metadata to file, merging siblings and clearing owned claim."""
    api_resource = remote_agent.api_resource
    assert api_resource is not None, "deployed agent has no api_resource"
    metadata = {
        "remote_agent_runtime_id": api_resource.name,
        "deployment_target": "agent_runtime",
        "is_a2a": cfg.is_a2a,
        "language": cfg.language,
        "agent_directory": cfg.agent_directory,
        "app_name": cfg.app_name,
        "deployment_timestamp": datetime.datetime.now(tz=datetime.UTC).isoformat(),
    }

    complete_operation(metadata_update=metadata, owner_id=owner_id)
    logging.info(f"Agent Runtime ID written to {METADATA_FILE}")


def _sync_agent_card(
    client: AgentPlatformClient,
    remote_agent: types.Runtime,
    cfg: ProjectConfig,
    *,
    resource_name: str,
    location: str,
) -> None:
    """Copy the served A2A card to ``spec.agent_card``, which Agent Registry reads."""
    a2a_path = a2a_base_path(cfg.language, cfg.app_name)
    if a2a_path is None:
        raise RuntimeError(f"{cfg.language} has no A2A base path.")
    url = build_agent_runtime_card_url(location, resource_name, a2a_path)

    logging.info("Writing the agent card to Agent Registry...")
    try:
        card = _fetch_deployed_agent_card(url, build_remote_headers((), url)).raw
    except (requests.RequestException, ValueError) as exc:
        hint = ""
        if (
            isinstance(exc, requests.HTTPError)
            and exc.response is not None
            and exc.response.status_code in (401, 403)
        ):
            ingress_gateway = _ingress_gateway(remote_agent)
            hint = (
                f" Check that the ingress Agent Gateway {ingress_gateway} allows "
                "direct calls to the agent."
                if ingress_gateway
                else " Check that your credentials can call the agent."
            )
        logging.warning(
            "The agent is deployed, but its agent card could not be read from %s "
            "(%s), so Agent Registry may not show the current card.%s Rerun "
            "`agents-cli deploy` to retry.",
            url,
            exc,
            hint,
        )
        return

    api_resource = remote_agent.api_resource
    spec = api_resource.spec if api_resource else None
    if card == (spec.agent_card if spec else None):
        logging.info("Agent card unchanged.")
        return

    try:
        operation_name = _sdk_compat.start_update(
            client,
            name=resource_name,
            config={
                "spec": {"agent_card": card},
                "update_mask": _AGENT_CARD_UPDATE_MASK,
            },
        )
        error = _sdk_compat.wait_for_operation(
            client, operation_name=operation_name
        ).error
    except APIError as exc:
        error = exc
    if error:
        logging.warning(
            "The agent is deployed, but writing its agent card to Agent Registry "
            "failed: %s. Rerun `agents-cli deploy` to retry.",
            error,
        )
        return
    logging.info("Agent card updated.")


@backoff.on_exception(
    backoff.expo,
    requests.RequestException,
    giveup=lambda exc: not is_transient_request_error(exc),
    max_tries=None,
    max_time=_AGENT_CARD_WAIT_SECONDS,
    max_value=_AGENT_CARD_MAX_RETRY_DELAY_SECONDS,
    jitter=None,
    on_backoff=lambda details: logging.info(
        "The agent card isn't served yet; retrying in %.0fs (%.0fs of %ds elapsed)...",
        details["wait"],
        details["elapsed"],
        _AGENT_CARD_WAIT_SECONDS,
    ),
)
def _fetch_deployed_agent_card(url: str, headers: dict[str, str]) -> FetchedAgentCard:
    return fetch_agent_card(url, headers, timeout=_AGENT_CARD_REQUEST_TIMEOUT_SECONDS)


def _ingress_gateway(agent: types.Runtime) -> str | None:
    api_resource = agent.api_resource
    spec = api_resource.spec if api_resource else None
    deployment_spec = spec.deployment_spec if spec else None
    gateway_config = deployment_spec.agent_gateway_config if deployment_spec else None
    ingress = gateway_config.client_to_agent_config if gateway_config else None
    return ingress.agent_gateway if ingress else None


def print_deployment_success(
    remote_agent: types.Runtime,
    location: str,
    project: str,
    cfg: ProjectConfig,
) -> None:
    """Print deployment success message with console URL."""
    api_resource = remote_agent.api_resource
    assert api_resource is not None, "deployed agent has no api_resource"
    resource_name = api_resource.name
    assert resource_name is not None, "deployed agent has no resource name"
    resource_name_parts = resource_name.split("/")
    agent_runtime_id = resource_name_parts[-1]

    if cfg.is_a2a:
        print("\n✅ Deployment successful!")
        a2a_path = a2a_base_path(cfg.language, cfg.app_name)
        if a2a_path is None:
            logging.warning(
                "No A2A base path is defined for language '%s'; defaulting to the "
                "root path. The agent card URL may be wrong.",
                cfg.language,
            )
            a2a_path = ""
        agent_card_url = build_agent_runtime_card_url(location, resource_name, a2a_path)
        print(f"🪪 Agent Card URL: {agent_card_url}")
    else:
        print("\n✅ Deployment successful!")

    print(f"Agent Runtime ID: {resource_name}")

    spec = api_resource.spec
    identity = spec.effective_identity if spec else None
    if _is_agent_identity_principal(identity):
        print(f"Agent Identity: principal://{identity}")
    else:
        print(f"Service Account: {identity}")

    console_url = (
        f"https://console.cloud.google.com/vertex-ai/agents/agent-engines/"
        f"locations/{location}/agent-engines/{agent_runtime_id}?project={project}"
    )
    print(f"\n📊 View in Console: {console_url}\n")
