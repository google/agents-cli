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
  dummy_source_b64 = trimspace(file("${path.module}/../shared/dummy_source.b64"))
}

resource "google_vertex_ai_reasoning_engine" "app" {
  display_name = var.project_name
  description  = "Agent deployed via Terraform"
  region       = var.region
  project      = var.project_id

  spec {
    agent_framework = var.agent_framework
    service_account = google_service_account.app_sa.email
    identity_type   = "SERVICE_ACCOUNT"

    deployment_spec {
      min_instances         = 1
      max_instances         = 10
      container_concurrency = 9

      resource_limits = {
        cpu    = "4"
        memory = "8Gi"
      }

      env {
        name  = "LOGS_BUCKET_NAME"
        value = google_storage_bucket.logs_data_bucket.name
      }

      # GOOGLE_CLOUD_PROJECT is reserved by Agent Runtime (the platform injects
      # it) and rejected in deployment_spec.env; GOOGLE_CLOUD_LOCATION is allowed.
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = "global"
      }

      env {
        name  = "{% if cookiecutter.language == "python" %}GOOGLE_GENAI_USE_ENTERPRISE{% else %}GOOGLE_GENAI_USE_VERTEXAI{% endif %}"
        value = "True"
      }

      env {
        name  = "OTEL_SERVICE_NAME"
        value = "{{cookiecutter.project_name}}"
      }

      # Prompt/response content capture, off by default. Go: set "true" to log
      # content to OTLP log events for the completions view. Python: content goes to
      # GCS via the completion hook, so NO_CONTENT.
      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
        value = "{% if cookiecutter.language == 'go' %}false{% else %}NO_CONTENT{% endif %}"
      }

      env {
        name  = "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"
        value = "false"
      }

      env {
        name  = "OTEL_SEMCONV_STABILITY_OPT_IN"
        value = "gen_ai_latest_experimental"
      }

      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT"
        value = "jsonl"
      }

      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK"
        value = "upload"
      }

      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH"
        value = "gs://${google_storage_bucket.logs_data_bucket.name}/completions"
      }

      env {
        name  = "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"
        value = "true"
      }
{%- if cookiecutter.bq_analytics %}
      env {
        name  = "BQ_ANALYTICS_DATASET_ID"
        value = google_bigquery_dataset.telemetry_dataset.dataset_id
      }
      env {
        name  = "BQ_ANALYTICS_GCS_BUCKET"
        value = google_storage_bucket.logs_data_bucket.name
      }
      env {
        name  = "BQ_ANALYTICS_CONNECTION_ID"
        # Format: {location}.{connection_id}
        value = "${var.region}.${google_bigquery_connection.genai_telemetry_connection.connection_id}"
      }
{%- endif %}
    }

    source_code_spec {
      inline_source {
        source_archive = local.dummy_source_b64
      }
      image_spec {}
    }
  }

  # Terraform creates the resource with a placeholder source build; CI/CD
  # overwrites the same source_code_spec with the real code. The deploy writes
  # source_code_spec, so the placeholder must use it too — a container_spec
  # placeholder would be left alongside it and Agent Engine rejects the update.
  # Ignore the spec and deployment_spec so Terraform never reverts the deployed agent.
  lifecycle {
    ignore_changes = [
      spec[0].container_spec,
      spec[0].source_code_spec,
      spec[0].deployment_spec,
    ]
  }

  # Make dependencies conditional to avoid errors.
  depends_on = [google_project_service.services]
}
