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

"""Shared utilities for deploy commands."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from rich.table import Table

    from google.agents.cli._project import ProjectConfig

# Shared machine-shape defaults for the imperative deploy paths: the deploy
# command (Cloud Run) and deploy_agent_runtime() both pull from here. The
# generated Terraform (service.tf) duplicates these values and must be kept in
# sync by hand, since Terraform can't import them.
DEFAULT_CPU = "1"
DEFAULT_MEMORY = "4Gi"
# Scale to zero by default: `agents-cli deploy` is the iteration path, and idle
# dev/demo agents holding a warm instance burn regional capacity for nothing.
# Production deployments go through Terraform, whose service.tf pins
# min_instances = 1 to avoid cold starts; users can also pass --min-instances.
DEFAULT_MIN_INSTANCES = 0
DEFAULT_MAX_INSTANCES = 10
# Max in-flight requests per container. Conservative on purpose: the worker is
# I/O-bound (CPU isn't the limit), but peak memory grows with concurrency and is
# agent-specific, so 8 keeps a RAG/large-context agent inside 4Gi. Lighter agents
# can raise --concurrency (and --memory) after load testing.
# https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/optimize-and-scale#underutilized-workers
DEFAULT_CONCURRENCY = 8


@dataclass(frozen=True)
class MachineShape:
    """Machine sizing flags; ``None`` means the flag was not passed.

    Unset values stay ``None`` so each target decides how to treat them: Agent
    Runtime omits them from the FieldMask (preserving live values on update),
    Cloud Run fills defaults only when creating the service.
    """

    cpu: str | None = None
    memory: str | None = None
    min_instances: int | None = None
    max_instances: int | None = None
    concurrency: int | None = None

    def with_defaults(self) -> MachineShape:
        """Fill each unset value with its ``DEFAULT_*`` constant."""
        return MachineShape(
            cpu=DEFAULT_CPU if self.cpu is None else self.cpu,
            memory=DEFAULT_MEMORY if self.memory is None else self.memory,
            min_instances=DEFAULT_MIN_INSTANCES
            if self.min_instances is None
            else self.min_instances,
            max_instances=DEFAULT_MAX_INSTANCES
            if self.max_instances is None
            else self.max_instances,
            concurrency=DEFAULT_CONCURRENCY
            if self.concurrency is None
            else self.concurrency,
        )


def redact_command(args: list[str]) -> str:
    """``shlex.join(args)`` with every env-var VALUE masked for display.

    Any ``KEY=VALUE`` segment is shown as ``KEY=***``, so values propagated from
    the project ``.env`` (which may include secrets) never reach the terminal or
    CI logs — only the key names are visible. Handles both a single comma-joined
    ``--update-env-vars`` argument (``K1=V1,K2=V2``) and individual ``KEY=VALUE``
    args. Only the printed form is masked; the caller executes the real command
    unredacted.
    """

    def _redact(arg: str) -> str:
        if "=" not in arg:
            return arg
        segments = []
        for seg in arg.split(","):
            key, sep, _value = seg.partition("=")
            segments.append(f"{key}=***" if sep else seg)
        return ",".join(segments)

    return shlex.join(_redact(str(a)) for a in args)


def resolve_service_name(cfg: ProjectConfig, override: str | None) -> str:
    """Deployed service name.

    Precedence: the explicit ``override`` (``--service-name`` flag) wins, then the
    project name, then a generic fallback when deploying without a manifest.
    """
    return override or cfg.project_name or "agent"


def parse_key_value_pairs(kv_string: str | None) -> dict[str, str]:
    """Parse a comma-separated ``KEY=VALUE`` string into a dict.

    Splits on commas, strips surrounding whitespace from each key and value,
    and lets the last value win on duplicate keys. Returns ``{}`` when the input
    is ``None`` or empty.

    Raises:
        ValueError: If a segment is malformed (has no ``=``).
    """
    result = {}
    if kv_string:
        for pair in kv_string.split(","):
            if "=" in pair:
                key, value = pair.split("=", 1)
                result[key.strip()] = value.strip()
            else:
                raise ValueError(f"Malformed key-value pair: {pair}")
    return result


def parse_kv_flag(flag: str, value: str | None) -> dict[str, str]:
    """``parse_key_value_pairs`` for a CLI flag, failing with a ClickException."""
    try:
        return parse_key_value_pairs(value)
    except ValueError as e:
        raise click.ClickException(
            f"Error parsing {flag} flag value '{value}': {e}"
        ) from e


def parse_secrets(secrets_string: str | None) -> dict[str, dict[str, str]]:
    """Parse secrets from ENV_VAR=SECRET_ID or ENV_VAR=SECRET_ID:VERSION format."""
    try:
        raw = parse_key_value_pairs(secrets_string)
    except ValueError as e:
        raise click.ClickException(f"Error parsing secrets: {e}") from e

    result: dict[str, dict[str, str]] = {}
    for key, spec in raw.items():
        if ":" not in spec:
            secret_id, version = spec, "latest"
        else:
            secret_id, _, version = spec.rpartition(":")
        result[key] = {"secret": secret_id, "version": version}
    return result


def print_table(table: Table) -> None:
    """Print a rich table after a blank line."""
    from google.agents.cli._output import Console

    console = Console()
    console.print()
    console.print(table)


def read_project_dotenv(project_root: str | Path) -> dict[str, str]:
    """Read the project-root ``.env`` into a dict, or ``{}`` when absent.

    Used by the deploy paths to propagate local config to the deployed service.
    Values are copied as-is ("copy the .env" model); callers layer explicit
    ``--update-env-vars`` on top so the CLI flag always wins.
    """
    import io

    from dotenv import dotenv_values

    env_path = Path(project_root) / ".env"
    if not env_path.is_file():
        return {}
    # Read the bytes ourselves and hand dotenv a stream: dotenv_values(path) does
    # its own file open that pyfakefs (use_dynamic_patch=False) doesn't patch.
    with open(env_path, encoding="utf-8") as f:
        content = f.read()
    return {
        k: v
        for k, v in dotenv_values(stream=io.StringIO(content)).items()
        if v is not None
    }


def validate_deployment_region(
    region: str | None, deployment_target: str | None = None
) -> None:
    """Validate that the deployment region is a single regional location, not a multi-region."""
    if not region:
        return

    normalized = region.strip().lower()
    if not re.match(r"^[a-z]+-[a-z]+\d+$", normalized):
        target_str = f" for {deployment_target}" if deployment_target else ""
        raise click.ClickException(
            f"Region '{region}' is not a valid single regional location and is not supported{target_str} deployments.\n"
            "  Cloud infrastructure (Cloud Run, GKE, Agent Runtime) must be deployed to a single regional location (not a multi-region or zone).\n"
            "  Please specify a valid Google Cloud Platform region (e.g., 'us-central1', 'europe-west4', 'asia-northeast1') via --region or in agents-cli-manifest.yaml.\n"
            "  See https://cloud.google.com/about/locations for valid regions.\n"
            "  (Note: To configure Gemini model location separately, set GOOGLE_CLOUD_LOCATION in your .env or --update-env-vars)."
        )
