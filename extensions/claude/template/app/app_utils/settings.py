# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Project settings."""

# The agent's name in traces and analytics. Keep in sync with
# create_params.root_agent_name in agents-cli-manifest.yaml.
AGENT_NAME: str = "{{cookiecutter.root_agent_name}}"

# The agents-cli version that created this project. Model calls send it in the
# x-goog-api-client header so Google can count traffic from agents-cli projects;
# set DO_NOT_TRACK=1 or AGENTS_CLI_TELEMETRY=0 to stop sending it.
AGENTS_CLI_VERSION: str = "{{cookiecutter.package_version}}"
