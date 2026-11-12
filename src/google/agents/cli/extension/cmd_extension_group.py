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

"""agents-cli extension command group."""

import click

from google.agents.cli._click import LazyGroup


@click.group("extension", cls=LazyGroup)
def extension_group():
    """Manage agents-cli extensions (experimental).

    Experimental: the manifest schema and the command surface may still change
    in a breaking way. Pin the CLI version if you depend on either.

    \b
    Subcommands:
      add     Add an extension from git, a local path, or PyPI
      list    List active extensions and the commands they contribute
      update  Advance extension pins and replace their skills
      remove  Remove an installed extension
    """


extension_group.add_lazy_command(
    "add",
    "google.agents.cli.extension.cmd_extension_add:cmd_add",
    "Add an extension from git, a local path, or PyPI.",
)
extension_group.add_lazy_command(
    "list",
    "google.agents.cli.extension.cmd_extension_list:cmd_list",
    "List active extensions and the commands they contribute.",
)
extension_group.add_lazy_command(
    "update",
    "google.agents.cli.extension.cmd_extension_update:cmd_update",
    "Advance extension pins and replace their skills.",
)
extension_group.add_lazy_command(
    "remove",
    "google.agents.cli.extension.cmd_extension_remove:cmd_remove",
    "Remove an installed extension (project scope first, then user scope).",
)
