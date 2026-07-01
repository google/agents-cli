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

"""agents-cli eval command group."""

import click

from google.agents.cli._click import LazyGroup


@click.group("eval", cls=LazyGroup)
def eval_group():
    """Evaluate agents and compare results.

    \b
    Core:
      run       Chain generate + grade in one command
      generate  Run agent inference over eval cases
      grade     Grade generated traces
      compare   Compare two eval result JSON files
      metric    Discover and manage evaluation metrics

    \b
    Advanced (experimental):
      dataset   Synthesize multi-turn eval traces (user simulation)
      analyze   Analyze failure clusters from results
      optimize  Optimize agent prompts using the GEPA framework
      submit    Submit an E2E cloud-side evaluation run on Vertex AI Eval Service
      results   Fetch results from a completed cloud evaluation run
    """


eval_group.add_lazy_command(
    "generate",
    "google.agents.cli.eval.cmd_generate:cmd_generate",
    "Generate agent traces by running inference over eval cases.",
)
eval_group.add_lazy_command(
    "grade",
    "google.agents.cli.eval.cmd_grade:cmd_grade",
    "Score populated agent traces against one or more metrics.",
)
eval_group.add_lazy_command(
    "run",
    "google.agents.cli.eval.cmd_run:cmd_run",
    "Chain `eval generate` and `eval grade` in one command.",
)
eval_group.add_lazy_command(
    "dataset",
    "google.agents.cli.eval.cmd_dataset:dataset_group",
    "[Experimental] Manage evaluation traces.",
)
eval_group.add_lazy_command(
    "metric",
    "google.agents.cli.eval.cmd_metric:metric_group",
    "Discover and manage evaluation metrics.",
)
eval_group.add_lazy_command(
    "compare",
    "google.agents.cli.eval.cmd_compare:cmd_compare",
    "Compare two eval result JSON files.",
)
eval_group.add_lazy_command(
    "analyze",
    "google.agents.cli.eval.cmd_analyze:cmd_analyze",
    "[Experimental] Analyze failure clusters from an evaluation run result JSON file.",
)
eval_group.add_lazy_command(
    "optimize",
    "google.agents.cli.eval.cmd_optimize:cmd_optimize",
    "[Experimental] Optimize agent prompts using the GEPA framework.",
)
eval_group.add_lazy_command(
    "submit",
    "google.agents.cli.eval.cmd_submit:cmd_submit",
    "[Experimental] Submit an E2E cloud-side evaluation run on Vertex AI Eval Service.",
)
eval_group.add_lazy_command(
    "results",
    "google.agents.cli.eval.cmd_submit:cmd_results",
    "[Experimental] Fetch results from a completed cloud evaluation run.",
)
