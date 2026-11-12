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

"""agents-cli infra single-project — provision single-project terraform."""

import logging
import os
from pathlib import Path

import click

from google.agents.cli._agent_platform import AgentPlatformClient
from google.agents.cli._gcp_project import get_gcp_project_number, resolve_gcp_project
from google.agents.cli._project import (
    ProjectConfig,
    chdir_project_root,
    read_project_config,
)
from google.agents.cli._runner import run_resolved
from google.agents.cli._tools import (
    ToolNotExecutableError,
    ToolNotFoundError,
    require_tool,
)
from google.agents.cli.deploy._operation import (
    METADATA_FILE,
    read_remote_agent_runtime_id,
)
from google.agents.cli.deploy._utils import resolve_service_name
from google.agents.cli.infra._terraform import require_single_project_tf_dir

_REASONING_ENGINE_TF_ADDRESS = "google_vertex_ai_reasoning_engine.app"
# Default of `variable "region"` in the scaffolded single-project root
# (scaffold/base_templates/_shared/deployment/terraform/single-project/variables.tf).
_TF_DEFAULT_REGION = "us-east1"


@click.command("single-project")
@click.option("--project", default=None, help="GCP project ID.")
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    default=False,
    help="Apply changes. Without this flag, only a plan is shown.",
)
def cmd_infra_single_project(project, apply_changes):
    """Provision single-project infrastructure (optional).

    Not required for basic deployments — `agents-cli deploy` works out of the
    box using smart defaults (default Compute Engine service account,
    on-the-fly resource provisioning). Run this when you need custom setup
    such as a dedicated service account, pre-provisioned secrets, or specific
    IAM bindings.

    \b
    By default, runs terraform init + terraform plan to preview changes.
    Use --apply to apply the changes.
    """
    chdir_project_root()

    require_tool("terraform")

    from google.agents.cli.infra._cicd_utils import run_terraform

    tf_dir = require_single_project_tf_dir()
    cfg = read_project_config()

    extra_vars = {"project_id": project} if project else None
    # The import guard reads Terraform state, which needs an initialised
    # backend, so it runs after `terraform init` and before plan/apply.
    run_terraform(
        tf_dir=tf_dir,
        apply=apply_changes,
        extra_vars=extra_vars,
        after_init=lambda: _check_unimported_agent_runtime(
            tf_dir=tf_dir, cfg=cfg, project=project
        ),
    )

    if not apply_changes:
        followup = "agents-cli infra single-project --apply"
        if project:
            followup += f" --project {project}"
        click.echo(f"\nTo apply these changes, run:\n  {followup}")
        return

    click.echo("\nTo see what was provisioned, run:\n  agents-cli infra show")


def _check_unimported_agent_runtime(
    tf_dir: Path, cfg: ProjectConfig, project: str | None
) -> None:
    """Stop if an Agent Runtime engine already exists but is missing from Terraform state."""
    if cfg.deployment_target != "agent_runtime":
        return

    # None means the state couldn't be read (already warned), so don't guess.
    if _is_in_terraform_state(tf_dir, _REASONING_ENGINE_TF_ADDRESS) is not False:
        return

    target_project = (
        project or os.environ.get("TF_VAR_project_id") or resolve_gcp_project()
    )
    # Search where `agents-cli deploy` would have created the engine (its region
    # comes from the manifest), not where Terraform would: an engine in another
    # region is still a duplicate. The import instructions below use the
    # engine's own region, taken from its resource name.
    found = _find_existing_engine(cfg, target_project)
    if not found:
        return

    project_id, region, engine_id = found
    resource_id = f"projects/{project_id}/locations/{region}/reasoningEngines/{engine_id}"
    apply_command = f"agents-cli infra single-project --apply --project {project_id}"
    # `agents-cli infra single-project` only passes project_id to Terraform, so
    # a non-default region has to come from the environment or the next run
    # plans a replacement of the imported engine (region forces re-creation).
    region_note = ""
    if region != _TF_DEFAULT_REGION and region != os.environ.get("TF_VAR_region"):
        region_note = (
            f"  Terraform's region defaults to {_TF_DEFAULT_REGION}. Set "
            f"TF_VAR_region={region}\n"
            "  in your environment before the next step, or it will plan a\n"
            "  replacement of the imported engine.\n\n"
        )
    raise click.ClickException(
        f"Existing Agent Runtime '{resource_id}' is not in Terraform state.\n"
        "  Running Terraform without importing it would create a second Agent Runtime\n"
        "  instance. Import the existing engine first:\n\n"
        f"    cd {tf_dir.as_posix()}\n"
        "    terraform init\n\n"
        "    terraform import \\\n"
        f"      -var project_id={project_id} \\\n"
        f"      -var region={region} \\\n"
        f"      {_REASONING_ENGINE_TF_ADDRESS} \\\n"
        f"      {resource_id}\n\n"
        "    # Plan updates the existing engine instead of creating a second one.\n"
        f"    terraform plan -var project_id={project_id} -var region={region}\n\n"
        f"{region_note}"
        "  Then apply through agents-cli:\n"
        f"    {apply_command}\n\n"
        "  After it applies, wire any Terraform-provisioned environment variables\n"
        "  (such as observability settings from `agents-cli infra show`) onto the\n"
        "  existing engine:\n"
        '    agents-cli deploy --update-env-vars "KEY=VALUE,..."'
    )


def _is_in_terraform_state(tf_dir: Path, address: str) -> bool | None:
    """Return whether ``address`` is tracked in ``tf_dir``'s Terraform state.

    Returns ``None``, after warning, when the state can't be read for a reason
    other than it being empty (for example, a backend or permission error), so
    callers can tell "not in state" apart from "unknown".
    """
    result = run_resolved(
        ["terraform", "state", "list"],
        cwd=tf_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        # Terraform writes diagnostics to stderr, but a wrapper may not; read
        # both so the "no state" check and the warning below see everything.
        output = "\n".join(
            s for s in (result.stderr, result.stdout) if s and s.strip()
        ).strip()
        # A root with nothing applied yet has no state file, which Terraform
        # reports as an error ("No state file was found!", errStateNotFound in
        # https://github.com/hashicorp/terraform/blob/main/internal/command/state_command.go).
        # That's an empty state, not a failure.
        if "No state file" in output:
            return False
        logging.warning(
            "Could not read Terraform state in %s (%s). Skipping the Agent Runtime "
            "import check.",
            tf_dir,
            output,
        )
        return None
    return address in (line.strip() for line in (result.stdout or "").splitlines())


def _find_existing_engine(
    cfg: ProjectConfig, target_project: str
) -> tuple[str, str, str] | None:
    """Return ``(project_id, region, engine_id)`` of a deployed engine, if any.

    Reads ``remote_agent_runtime_id`` from ``deployment_metadata.json`` first,
    and falls back to ``runtimes.list()`` by display name in the manifest's
    region only when the metadata file is missing.
    """
    if Path(METADATA_FILE).exists():
        remote_id = read_remote_agent_runtime_id() or ""
        parsed = _parse_reasoning_engine_name(remote_id)
        if not parsed:
            return None
        meta_project, meta_region, engine_id = parsed
        if target_project and not _projects_match(meta_project, target_project):
            return None
        project_id = target_project or meta_project
        # Deleted (False) or unverifiable (None, already warned): either way
        # there's nothing safe to tell the user to import, so don't block.
        if not _engine_exists(remote_id, project_id, meta_region):
            return None
        return (project_id, meta_region, engine_id)

    if not target_project:
        return None

    # `agents-cli deploy` names the engine after the project (see
    # resolve_service_name); the Terraform root uses the same name with `_`
    # replaced by `-` (`project_name` in single-project/variables.tf). Match both.
    service_name = resolve_service_name(cfg, None)
    display_names = {service_name, service_name.replace("_", "-")}
    try:
        client = AgentPlatformClient(project=target_project, location=cfg.region)
        agents = list(client.runtimes.list())
    except Exception as exc:
        # Best-effort check, so any failure (most often a 403 on a fresh project
        # whose Vertex AI API Terraform is about to enable, or missing
        # credentials) must not stop provisioning; warn so the user knows the
        # duplicate-engine guard didn't run.
        logging.warning(
            "Could not list Agent Runtime instances in %s (%s). Skipping the "
            "Agent Runtime import check.",
            target_project,
            exc,
        )
        return None

    matches = [
        (res.name, parsed)
        for agent in agents
        if (res := agent.api_resource)
        and res.display_name in display_names
        and (parsed := _parse_reasoning_engine_name(res.name or ""))
    ]
    if not matches:
        return None
    if len(matches) > 1:
        # Guessing would import the wrong engine, so make the user choose, as
        # `agents-cli deploy` does when a display-name lookup is ambiguous.
        listed = "\n".join(f"    {name}" for name, _ in matches)
        raise click.ClickException(
            f"Found {len(matches)} Agent Runtime instances named "
            f"'{service_name}' in {target_project}:\n{listed}\n"
            "  Can't tell which one Terraform should manage. Delete the duplicates, "
            "or run\n  `agents-cli deploy` against the one to keep so its ID is "
            f"recorded in {METADATA_FILE},\n  then re-run this command."
        )
    _name, parsed = matches[0]
    _project, region, engine_id = parsed
    return (target_project, region, engine_id)


def _parse_reasoning_engine_name(resource_name: str) -> tuple[str, str, str] | None:
    """Parse ``projects/{project}/locations/{region}/reasoningEngines/{id}``."""
    match resource_name.split("/"):
        case [
            "projects",
            project,
            "locations",
            region,
            "reasoningEngines",
            engine_id,
        ] if project and region and engine_id:
            return (project, region, engine_id)
        case _:
            return None


def _projects_match(meta_project: str, target_project: str) -> bool:
    """Whether ``meta_project`` (often a project number) refers to ``target_project``.

    Returns ``False``, after warning, when the project-number lookup can't run,
    so the best-effort import check is skipped rather than aborting the command.
    """
    if meta_project == target_project:
        return True
    # Two different IDs, or two different numbers, are different projects. Only
    # a number on one side and an ID on the other needs a lookup.
    if meta_project.isdigit() == target_project.isdigit():
        return False
    number, project_id = (
        (meta_project, target_project)
        if meta_project.isdigit()
        else (target_project, meta_project)
    )
    # The lookup shells out to gcloud, which this command doesn't otherwise
    # need (only terraform is required), so it may be missing or blocked.
    try:
        project_number = get_gcp_project_number(project_id)
    except (ToolNotFoundError, ToolNotExecutableError) as exc:
        logging.warning(
            "Could not compare project '%s' with '%s' (%s). Skipping the "
            "Agent Runtime import check.",
            meta_project,
            target_project,
            exc,
        )
        return False
    # None means `gcloud projects describe` ran but failed (no credentials, no
    # permission, network). Say so rather than silently treating it as "no match".
    if project_number is None:
        logging.warning(
            "Could not determine the project number of '%s'. Skipping the "
            "Agent Runtime import check.",
            project_id,
        )
        return False
    return project_number == number


def _engine_exists(resource_name: str, project: str, region: str) -> bool | None:
    """Whether the engine recorded in ``deployment_metadata.json`` still exists.

    Deleting an engine leaves the metadata file behind. Blocking on it would
    suggest a ``terraform import`` that can't succeed, so a deleted engine
    counts as "no engine" and the user is told how to clear the stale file.

    Returns ``None``, after warning, when the check itself fails (for example,
    a permission error or missing credentials), like ``_is_in_terraform_state``.
    """
    # SDK import kept inside the function so the CLI starts fast.
    from google.genai.errors import APIError

    try:
        AgentPlatformClient(project=project, location=region).runtimes.get(
            name=resource_name
        )
    except Exception as exc:
        if isinstance(exc, APIError) and exc.code == 404:
            logging.warning(
                "Agent Runtime '%s' recorded in %s no longer exists; proceeding with "
                "Terraform. Delete %s so `agents-cli deploy` creates a new engine.",
                resource_name,
                METADATA_FILE,
                METADATA_FILE,
            )
            return False
        # Best-effort check, like the list path in _find_existing_engine. Not
        # only APIError: the SDK loads credentials lazily on the first request,
        # so with no ADC `get` raises DefaultCredentialsError.
        logging.warning(
            "Could not verify Agent Runtime '%s' (%s). Skipping the Agent Runtime "
            "import check.",
            resource_name,
            exc,
        )
        return None
    return True
