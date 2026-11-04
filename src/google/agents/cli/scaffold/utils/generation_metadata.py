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

import logging

from google.agents.cli._project import ProjectConfig, parse_version
from google.agents.cli.scaffold.utils import remote_template

# Tracks when certain `scaffold create` flags were introduced, so that
# they're not passed to older release that don't recognize them.
_LAST_RELEASE_WITHOUT_FLAG = {
    "agent_runtime_identity_default": parse_version("1.9.0"),
    "cloud_run_identity_default": parse_version("1.12.0"),
}

_DEFAULT_REGION = ProjectConfig.region


def _flags_unknown_to(target_version: str) -> set[str]:
    """The create_params keys whose flag ``target_version`` doesn't recognize.

    Empty, and logged at debug level, when ``target_version`` isn't a dotted
    numeric version.
    """
    try:
        target = parse_version(target_version)
    except ValueError:
        logging.debug(
            "acli_version '%s' is not a dotted numeric version. Assuming all "
            "`scaffold create` flags are recognized.",
            target_version,
        )
        return set()
    return {key for key, last in _LAST_RELEASE_WITHOUT_FLAG.items() if target <= last}


def metadata_to_cli_args(
    metadata: ProjectConfig,
    *,
    for_enhance: bool = False,
    target_version: str | None = None,
) -> list[str]:
    """Convert ProjectConfig to CLI arguments for re-creating or enhancing a project.

    Maps agents-cli-manifest.yaml metadata back to CLI arguments.
    Used by upgrade/enhance commands to re-template old/new versions.

    Args:
        metadata: The project configuration to convert.
        for_enhance: Returns the args for ``enhance`` rather than ``create``.
        target_version: The agents-cli release that will consume these args.
            None means "target the current version".
    """
    args: list[str] = []

    if metadata.base_template:
        is_spec = remote_template.is_template_spec(metadata.base_template)
        if not for_enhance:
            args.extend(["--agent", metadata.base_template])
        elif is_spec:
            # A spec goes in `enhance`'s positional argument, which is what it
            # fetches. --base-template only names a template inside the wheel, so
            # a spec there resolves to `agents/<org>/<repo>@<tag>` and is not found.
            args.append(metadata.base_template)
        else:
            args.extend(["--base-template", metadata.base_template])

    if metadata.agent_directory and metadata.agent_directory != "app":
        args.extend(["--agent-directory", metadata.agent_directory])

    # Without it a re-render writes the default region into the files it adds.
    if metadata.region and metadata.region != _DEFAULT_REGION:
        args.extend(["--region", str(metadata.region)])

    # Both are recorded by the template rather than chosen by the user: is_a2a
    # is derived from tags and framework comes from templateconfig, and `create`
    # has no flag for either. A re-render reads them from the template again.
    skip_keys = {"is_a2a"}
    if for_enhance:
        # The root agent is named at create time, so `enhance` has no flag for
        # it -- and needs none: it re-reads the name from the manifest it is
        # standing in. Passing it would abort with "No such option".
        skip_keys.add("root_agent_name")

    if target_version is not None:
        skip_keys |= _flags_unknown_to(target_version)

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
