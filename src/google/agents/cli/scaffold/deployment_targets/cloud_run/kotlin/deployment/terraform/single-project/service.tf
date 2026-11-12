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


resource "google_cloud_run_v2_service" "app" {
  provider = google-beta
{%- if cookiecutter.agent_identity_enabled %}
  functional_type = "FUNCTIONAL_TYPE_AGENT"
{%- endif %}
  name                = var.project_name
  location            = var.region
  project             = var.project_id
  deletion_protection = false
  ingress             = "INGRESS_TRAFFIC_ALL"
  labels = {
    "created-by" = "agents-cli"
    "language"   = "kotlin"
  }

  template {
    containers {
      image = "us-docker.pkg.dev/cloudrun/container/hello"
      resources {
        limits = {
          cpu    = "1"
          memory = "4Gi"
        }
      }

      env {
        name  = "APP_URL"
        value = "https://${var.project_name}-${data.google_project.project.number}.${var.region}.run.app"
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }

      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = "global"
      }

      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "true"
      }

      env {
        name  = "LOGS_BUCKET_NAME"
        value = google_storage_bucket.logs_data_bucket.name
      }

      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
        value = "NO_CONTENT"
      }
    }
{%- if cookiecutter.agent_identity_enabled %}

    workload_identity_config {
      identity_type = "IDENTITY_TYPE_AGENT_IDENTITY"
    }
{%- endif %}

    service_account                  = google_service_account.app_sa.email
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
    resource.google_project_service.services,
  ]
}
