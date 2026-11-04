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

locals {
  project_ids = {
    default = var.project_id
  }
}


# Get the project number. depends_on defers the read to apply time, after
# bootstrap has enabled Cloud Resource Manager.
data "google_project" "project" {
  project_id = var.project_id

  depends_on = [google_project_service.bootstrap]
}

# Grant Storage Object Creator role to default compute service account
resource "google_project_iam_member" "default_compute_sa_storage_object_creator" {
  project    = var.project_id
  role       = "roles/cloudbuild.builds.builder"
  member     = "serviceAccount:${data.google_project.project.number}-compute@developer.gserviceaccount.com"
  depends_on = [resource.google_project_service.services]
}
{%- if not cookiecutter.agent_identity_enabled or cookiecutter.deployment_target == 'cloud_run' %}

# Agent service account
resource "google_service_account" "app_sa" {
  account_id   = "${var.project_name}-app"
  display_name = "${var.project_name} Agent Service Account"
  project      = var.project_id
  depends_on   = [resource.google_project_service.services]
}
{%- endif %}
{%- if not cookiecutter.agent_identity_enabled %}

# Grant application SA the required permissions to run the application
resource "google_project_iam_member" "app_sa_roles" {
  for_each = {
    for pair in setproduct(keys(local.project_ids), var.app_sa_roles) :
    join(",", pair) => {
      project = local.project_ids[pair[0]]
      role    = pair[1]
    }
  }

  project    = each.value.project
  role       = each.value.role
  member     = "serviceAccount:${google_service_account.app_sa.email}"
  depends_on = [resource.google_project_service.services]
}
{%- endif %}

{% if cookiecutter.deployment_target == 'agent_runtime' %}
# Grant required permissions to the Gemini platform service agent for Agent Runtime
resource "google_project_iam_member" "vertex_ai_sa_permissions" {
  for_each = {
    for pair in setproduct(keys(local.project_ids), var.app_sa_roles) :
    join(",", pair) => pair[1]
  }

  project = var.project_id
  role    = each.value
  member  = google_project_service_identity.vertex_sa.member
  depends_on = [resource.google_project_service.services]
}
{%- if cookiecutter.agent_identity_enabled %}

# An Agent Identity principal only exists once its engine does.
resource "google_project_iam_member" "agent_identity_roles" {
  for_each = toset(var.agent_identity_roles)

  project = var.project_id
  role    = each.value
  member  = "principal://${google_vertex_ai_reasoning_engine.app.spec[0].effective_identity}"
}

resource "google_storage_bucket_iam_member" "agent_identity_logs_bucket" {
  bucket = google_storage_bucket.logs_data_bucket.name
  role   = "roles/storage.objectAdmin"
  member = "principal://${google_vertex_ai_reasoning_engine.app.spec[0].effective_identity}"
}
{%- endif %}
{% endif %}
{%- if cookiecutter.deployment_target == 'cloud_run' and cookiecutter.agent_identity_enabled %}

# Cloud Run names a service's Agent Identity after the service, so the principal
# is known before the service exists, and IAM accepts grants to it then.
# Principal format: https://docs.cloud.google.com/run/docs/ai/authenticate-agents#authenticate-apis-agents-tools
data "google_project_ancestry" "project" {
  project = var.project_id

  depends_on = [google_project_service.bootstrap]
}

locals {
  # Projects outside an organization get a project-scoped trust domain.
  agent_identity_trust_domain = (
    data.google_project_ancestry.project.org_id != ""
    ? "org-${data.google_project_ancestry.project.org_id}"
    : "project-${data.google_project.project.number}"
  )
  agent_identity_principal = join("", [
    "principal://agents.global.${local.agent_identity_trust_domain}.system.id.goog",
    "/resources/run/projects/${data.google_project.project.number}",
    "/locations/${var.region}/services/${var.project_name}",
  ])
}

resource "google_project_iam_member" "agent_identity_roles" {
  for_each = toset(var.agent_identity_roles)

  project = var.project_id
  role    = each.value
  member  = local.agent_identity_principal
}

resource "google_storage_bucket_iam_member" "agent_identity_logs_bucket" {
  bucket = google_storage_bucket.logs_data_bucket.name
  role   = "roles/storage.objectAdmin"
  member = local.agent_identity_principal
}
{%- endif %}
