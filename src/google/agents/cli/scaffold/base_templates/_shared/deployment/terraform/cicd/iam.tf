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

# Data source to get project numbers.
# depends_on defers the read to apply time, after bootstrap has enabled Cloud
# Resource Manager.
data "google_project" "projects" {
  for_each   = local.deploy_project_ids
  project_id = each.value

  depends_on = [google_project_service.bootstrap]
}

# 1. Assign roles for the CICD project
resource "google_project_iam_member" "cicd_project_roles" {
  for_each = toset(var.cicd_roles)

  project    = var.cicd_runner_project_id
  role       = each.value
  member     = "serviceAccount:${resource.google_service_account.cicd_runner_sa.email}"
  depends_on = [resource.google_project_service.cicd_services, resource.google_project_service.deploy_project_services]

}

# 2. Assign roles for the other two projects (prod and staging)
resource "google_project_iam_member" "other_projects_roles" {
  for_each = {
    for pair in setproduct(keys(local.deploy_project_ids), var.cicd_sa_deployment_required_roles) :
    "${pair[0]}-${pair[1]}" => {
      project_id = local.deploy_project_ids[pair[0]]
      role       = pair[1]
    }
  }

  project    = each.value.project_id
  role       = each.value.role
  member     = "serviceAccount:${resource.google_service_account.cicd_runner_sa.email}"
  depends_on = [resource.google_project_service.cicd_services, resource.google_project_service.deploy_project_services]
}
{%- if not cookiecutter.agent_identity_enabled %}
# 3. Grant application SA the required permissions to run the application
resource "google_project_iam_member" "app_sa_roles" {
  for_each = {
    for pair in setproduct(keys(local.deploy_project_ids), var.app_sa_roles) :
    join(",", pair) => {
      project = local.deploy_project_ids[pair[0]]
      role    = pair[1]
    }
  }

  project    = each.value.project
  role       = each.value.role
  member     = "serviceAccount:${google_service_account.app_sa[split(",", each.key)[0]].email}"
  depends_on = [resource.google_project_service.cicd_services, resource.google_project_service.deploy_project_services]
}
{%- elif cookiecutter.deployment_target == 'agent_runtime' %}
# 3. Grant the Agent Identity principal the permissions required to run the application.
resource "google_project_iam_member" "agent_identity_roles" {
  for_each = {
    for pair in setproduct(keys(local.deploy_project_ids), var.agent_identity_roles) :
    join(",", pair) => {
      engine_key = pair[0]
      project    = local.deploy_project_ids[pair[0]]
      role       = pair[1]
    }
  }

  project = each.value.project
  role    = each.value.role
  member  = "principal://${google_vertex_ai_reasoning_engine.app[each.value.engine_key].spec[0].effective_identity}"
}

resource "google_storage_bucket_iam_member" "agent_identity_logs_bucket" {
  for_each = local.deploy_project_ids

  bucket = google_storage_bucket.logs_data_bucket[each.value].name
  role   = "roles/storage.objectAdmin"
  member = "principal://${google_vertex_ai_reasoning_engine.app[each.key].spec[0].effective_identity}"
}
{%- else %}
# 3. Grant the Agent Identity principal the permissions required to run the application.
# Cloud Run names a service's Agent Identity after the service, so each principal
# is known before its service exists, and IAM accepts grants to it then.
# Principal format: https://docs.cloud.google.com/run/docs/ai/authenticate-agents#authenticate-apis-agents-tools
data "google_project_ancestry" "deploy_projects" {
  for_each = local.deploy_project_ids
  project  = each.value

  depends_on = [google_project_service.bootstrap]
}

locals {
  agent_identity_principals = {
    for key, project_id in local.deploy_project_ids : key => join("", [
      "principal://agents.global.",
      # Projects outside an organization get a project-scoped trust domain.
      data.google_project_ancestry.deploy_projects[key].org_id != ""
      ? "org-${data.google_project_ancestry.deploy_projects[key].org_id}"
      : "project-${data.google_project.projects[key].number}",
      ".system.id.goog/resources/run/projects/${data.google_project.projects[key].number}",
      "/locations/${var.region}/services/${var.project_name}",
    ])
  }
}

resource "google_project_iam_member" "agent_identity_roles" {
  for_each = {
    for pair in setproduct(keys(local.deploy_project_ids), var.agent_identity_roles) :
    join(",", pair) => {
      project_key = pair[0]
      project     = local.deploy_project_ids[pair[0]]
      role        = pair[1]
    }
  }

  project = each.value.project
  role    = each.value.role
  member  = local.agent_identity_principals[each.value.project_key]
}

resource "google_storage_bucket_iam_member" "agent_identity_logs_bucket" {
  for_each = local.deploy_project_ids

  bucket = google_storage_bucket.logs_data_bucket[each.value].name
  role   = "roles/storage.objectAdmin"
  member = local.agent_identity_principals[each.key]
}
{%- endif %}

{% if cookiecutter.deployment_target == 'cloud_run' %}
# 4. Allow Cloud Run service SA to pull containers stored in the CICD project.
# Granted on this app's own repository rather than the whole CICD project.
resource "google_artifact_registry_repository_iam_member" "cicd_run_invoker_artifact_registry_reader" {
  for_each = local.deploy_project_ids

  project    = google_artifact_registry_repository.repo-artifacts-genai.project
  location   = google_artifact_registry_repository.repo-artifacts-genai.location
  repository = google_artifact_registry_repository.repo-artifacts-genai.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:service-${data.google_project.projects[each.key].number}@serverless-robot-prod.iam.gserviceaccount.com"
}

{% endif %}
{% if cookiecutter.deployment_target == 'gke' %}
# Allow GKE nodes to pull containers from CICD project Artifact Registry.
# Repository-scoped for the same reason as the Cloud Run grant above.
resource "google_artifact_registry_repository_iam_member" "cicd_gke_artifact_registry_reader" {
  for_each = local.deploy_project_ids

  project    = google_artifact_registry_repository.repo-artifacts-genai.project
  location   = google_artifact_registry_repository.repo-artifacts-genai.location
  repository = google_artifact_registry_repository.repo-artifacts-genai.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${data.google_project.projects[each.key].number}-compute@developer.gserviceaccount.com"
}

# Allow GKE Kubernetes ServiceAccount to impersonate the application GCP SA via Workload Identity
resource "google_service_account_iam_member" "workload_identity_binding" {
  for_each           = local.deploy_project_ids
  service_account_id = google_service_account.app_sa[each.key].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${each.value}.svc.id.goog[${var.project_name}/${var.project_name}]"

  depends_on = [google_container_cluster.app]
}
{% endif %}


# Special assignment: Allow the CICD SA to create tokens
resource "google_service_account_iam_member" "cicd_run_invoker_token_creator" {
  service_account_id = google_service_account.cicd_runner_sa.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${resource.google_service_account.cicd_runner_sa.email}"
  depends_on         = [resource.google_project_service.cicd_services, resource.google_project_service.deploy_project_services]
}
# Special assignment: Allow the CICD SA to impersonate itself for trigger creation
resource "google_service_account_iam_member" "cicd_run_invoker_account_user" {
  service_account_id = google_service_account.cicd_runner_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${resource.google_service_account.cicd_runner_sa.email}"
  depends_on         = [resource.google_project_service.cicd_services, resource.google_project_service.deploy_project_services]
}
