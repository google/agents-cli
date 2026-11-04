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

# Get project information to access the project number.
# depends_on defers the read to apply time, after bootstrap has enabled Cloud
# Resource Manager.
data "google_project" "project" {
  for_each = local.deploy_project_ids

  project_id = local.deploy_project_ids[each.key]

  depends_on = [google_project_service.bootstrap]
}

resource "google_cloud_run_v2_service" "app" {
  provider = google-beta
  for_each = local.deploy_project_ids
{%- if cookiecutter.agent_identity_enabled %}
  functional_type = "FUNCTIONAL_TYPE_AGENT"
{%- endif %}

  name                = var.project_name
  location            = var.region
  project             = each.value
  deletion_protection = false
  ingress             = "INGRESS_TRAFFIC_ALL"
  labels = {
    "created-by" = "agents-cli"
  }

  template {
    containers {
      # Placeholder, will be replaced by the CI/CD pipeline
      image = "us-docker.pkg.dev/cloudrun/container/hello"
      resources {
        limits = {
          cpu    = "1"
          memory = "4Gi"
        }
        cpu_idle = false
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = each.value
      }

      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = "global"
      }

      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "True"
      }

      env {
        name  = "LOGS_BUCKET_NAME"
        value = google_storage_bucket.logs_data_bucket[each.value].name
      }
    }
{%- if cookiecutter.agent_identity_enabled %}

    workload_identity_config {
      identity_type = "IDENTITY_TYPE_AGENT_IDENTITY"
    }
{%- endif %}

    service_account                  = google_service_account.app_sa[each.key].email
    max_instance_request_concurrency = 8

    scaling {
      min_instance_count = 1
      max_instance_count = 10
    }

    session_affinity = true
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  # Deploys replace the image outside Terraform (e.g., CI/CD pipelines), and
  # `agents-cli deploy --agent-identity` sets the workload identity. Prevent
  # both from being overwritten by Terraform.
  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      template[0].workload_identity_config,
    ]
  }

  # Make dependencies conditional to avoid errors.
  depends_on = [
    google_project_service.deploy_project_services,
  ]
}
