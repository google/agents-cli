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

"""agents-cli infra command — infrastructure provisioning."""

import click

from google.agents.cli._click import LazyGroup


@click.group("infra", cls=LazyGroup)
def infra_group():
    """Provision infrastructure for your agent project.

    \b
    Subcommands:
      single-project  Optional — custom infrastructure for a single GCP project
      cicd            Set up CI/CD pipelines and multi-environment infrastructure
    """
    pass


infra_group.add_lazy_command(
    "single-project",
    "google.agents.cli.infra.cmd_infra_single_project:cmd_infra_single_project",
    "Provision single-project infrastructure (optional).",
)
infra_group.add_lazy_command(
    "cicd",
    "google.agents.cli.infra.cmd_cicd:setup_cicd",
    "Set up CI/CD pipelines and Terraform infrastructure for your agent project.",
)
infra_group.add_lazy_command(
    "datastore",
    "google.agents.cli.infra.cmd_datastore:cmd_infra_datastore",
    "Removed: RAG is now a clone-and-study recipe.",
)
