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

import logging
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from google.agents.cli._project import ProjectConfig

# Shared machine-shape defaults for the imperative deploy paths: the deploy
# command (Cloud Run) and deploy_agent_runtime() both pull from here. The Cloud
# Run Terraform (service.tf) duplicates these values and must be kept in sync by
# hand — Terraform can't import them.
DEFAULT_CPU = "1"
DEFAULT_MEMORY = "4Gi"
DEFAULT_MIN_INSTANCES = 1
DEFAULT_MAX_INSTANCES = 10
# Max in-flight requests per container. Conservative on purpose: the worker is
# I/O-bound (CPU isn't the limit), but peak memory grows with concurrency and is
# agent-specific, so 8 keeps a RAG/large-context agent inside 4Gi. Lighter agents
# can raise --concurrency (and --memory) after load testing.
# https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/optimize-and-scale#underutilized-workers
DEFAULT_CONCURRENCY = 8


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
    """Parse key-value pairs from a comma-separated KEY=VALUE string."""
    result = {}
    if kv_string:
        for pair in kv_string.split(","):
            if "=" in pair:
                key, value = pair.split("=", 1)
                result[key.strip()] = value.strip()
            else:
                logging.warning(f"Skipping malformed key-value pair: {pair}")
    return result


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
