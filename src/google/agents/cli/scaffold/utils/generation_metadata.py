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


from google.agents.cli._project import ProjectConfig


def metadata_to_cli_args(
    metadata: ProjectConfig,
    *,
    for_enhance: bool = False,
) -> list[str]:
    """Convert ProjectConfig to CLI arguments for re-creating or enhancing a project.

    Maps agents-cli-manifest.yaml metadata back to CLI arguments.
    Used by upgrade/enhance commands to re-template old/new versions.
    """
    args: list[str] = []

    if metadata.base_template:
        arg_name = "--base-template" if for_enhance else "--agent"
        args.extend([arg_name, metadata.base_template])

    if metadata.agent_directory and metadata.agent_directory != "app":
        args.extend(["--agent-directory", metadata.agent_directory])

    # Skip is_a2a - not currently a valid option on `create`, despite being grouped that way in config
    skip_keys = {"is_a2a"}
    for key, value in metadata.create_params.items():
        if key in skip_keys:
            continue
        # "none" is a valid value for deployment_target (prototype mode)
        if key != "deployment_target" and str(value).lower() in ("none", "skip"):
            continue
        if value is None or value is False or value == "":
            continue

        arg_name = f"--{key.replace('_', '-')}"
        if value is True:
            args.append(arg_name)
        else:
            args.extend([arg_name, str(value)])

    return args
