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

"""Shared redirect message for the removed RAG datastore/ingestion commands."""

# RAG moved out of the built-in templates into clone-and-study samples. Datastore
# provisioning and data ingestion now live in each sample's own Makefile, so these
# CLI commands are tombstoned with an actionable pointer to the samples.
RAG_RECIPE_MESSAGE = (
    "RAG is now a clone-and-study recipe — this command has been removed.\n\n"
    "Datastore provisioning and data ingestion live in each sample's own Makefile\n"
    "(e.g. `make setup-infra`, `make data-ingestion`). Start from one of:\n"
    "  - rag-vector-search  Vertex AI Vector Search + KFP ingestion pipeline\n"
    "  - rag-agent-search   Agent Platform Search + managed GCS data connector\n"
    "under core/ in https://github.com/google/adk-samples\n\n"
    "See the agents-cli workflow skill (Phase 1: Study Reference Samples) for the\n"
    "clone-and-study steps."
)
