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

"""agents-cli eval metric commands."""

import re

import click
from rich.console import Console
from rich.table import Table
from vertexai._genai._evals_constant import SUPPORTED_PREDEFINED_METRICS

from google.agents.cli._click import LazyGroup


@click.group("metric", cls=LazyGroup)
def metric_group():
    """Discover and manage evaluation metrics."""
    pass


@click.command("list")
def list_metrics():
    """List available out-of-the-box (OOTB) evaluation metrics."""
    metric_names_set = set()
    for name in SUPPORTED_PREDEFINED_METRICS:
        base_name = re.sub(r"_v\d+$", "", name)
        metric_names_set.add(base_name.upper())

    metric_names = sorted(metric_names_set)

    console = Console()
    table = Table(
        title="Available Built-in Evaluation Metrics",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Metric Name", style="cyan", no_wrap=True)

    for name in metric_names:
        table.add_row(name)

    console.print()
    console.print(table)
    console.print()


metric_group.add_lazy_command(
    "list",
    "google.agents.cli.eval.cmd_metric:list_metrics",
    "List available out-of-the-box (OOTB) evaluation metrics.",
)
