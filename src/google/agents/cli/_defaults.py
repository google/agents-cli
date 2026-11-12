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

"""Defaults shared by the CLI and the projects it scaffolds."""

from __future__ import annotations

# Reaches the templates as the ``default_model`` cookiecutter variable. Nothing
# renders the skills, docs, eval goldens or extension templates, so their copies
# of the model id are updated by hand alongside this one.
DEFAULT_MODEL = "gemini-3.8-flash"

# Written to the generated .env when no GCP project could be resolved.
GCP_PROJECT_PLACEHOLDER = "your-gcp-project-id"
