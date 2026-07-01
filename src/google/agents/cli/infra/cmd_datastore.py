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

"""agents-cli infra datastore command — removed (RAG is now a clone-and-study recipe)."""

import click

from google.agents.cli.data._recipe import RAG_RECIPE_MESSAGE


@click.command(
    "datastore",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def cmd_infra_datastore(args):
    """Removed: RAG is now a clone-and-study recipe."""
    raise click.ClickException(RAG_RECIPE_MESSAGE)
