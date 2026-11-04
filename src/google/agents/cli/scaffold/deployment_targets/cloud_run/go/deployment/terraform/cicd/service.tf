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


{%- if cookiecutter.session_type == "cloud_sql" %}

# Generate a random password for the database user
resource "random_password" "db_password" {
  for_each = local.deploy_project_ids

  length           = 16
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

# Cloud SQL Instance
resource "google_sql_database_instance" "session_db" {
  for_each = local.deploy_project_ids

  project             = local.deploy_project_ids[each.key]
  name                = "${var.project_name}-db-${each.key}"
  database_version    = "POSTGRES_15"
  region              = var.region
  deletion_protection = false # For easier teardown in starter packs

  settings {
    tier = "db-custom-1-3840"

    backup_configuration {
      enabled    = true
      start_time = "03:00"
    }

    # Enable IAM authentication
    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }
  }

  depends_on = [google_project_service.deploy_project_services]
}

# Cloud SQL Database
resource "google_sql_database" "database" {
  for_each = local.deploy_project_ids

  project  = local.deploy_project_ids[each.key]
  name     = "${var.project_name}" # Use project name for DB to avoid conflict with default 'postgres'
  instance = google_sql_database_instance.session_db[each.key].name
}

# Cloud SQL User
resource "google_sql_user" "db_user" {
  for_each = local.deploy_project_ids

  project  = local.deploy_project_ids[each.key]
  name     = "${var.project_name}" # Use project name for user to avoid conflict with default 'postgres'
  instance = google_sql_database_instance.session_db[each.key].name
  password = random_password.db_password[each.key].result
}

# Store the password in Secret Manager
resource "google_secret_manager_secret" "db_password" {
  for_each = local.deploy_project_ids

  project   = local.deploy_project_ids[each.key]
  secret_id = "${var.project_name}-db-password"

  replication {
    auto {}
  }

  depends_on = [google_project_service.deploy_project_services]
}

resource "google_secret_manager_secret_version" "db_password" {
  for_each = local.deploy_project_ids

  secret      = google_secret_manager_secret.db_password[each.key].id
  secret_data = random_password.db_password[each.key].result
}
{%- if cookiecutter.agent_identity_enabled %}

# Lets app_sa read the DB password secret.
resource "google_secret_manager_secret_iam_member" "app_sa_db_password" {
  for_each = local.deploy_project_ids

  project   = each.value
  secret_id = google_secret_manager_secret.db_password[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app_sa[each.key].email}"
}
{%- endif %}

{%- endif %}

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
        name  = "APP_URL"
        value = "https://${var.project_name}-${data.google_project.project[each.key].number}.${var.region}.run.app"
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

      # Prompt/response content capture, off by default. Set "true" to log content
      # to OTLP log events for the Go completions view (parsed as a boolean).
      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
        value = "false"
      }

{%- if cookiecutter.session_type == "cloud_sql" %}
      # Mount the Cloud SQL unix socket
      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      env {
        name  = "INSTANCE_CONNECTION_NAME"
        value = google_sql_database_instance.session_db[each.key].connection_name
      }

      env {
        name = "DB_PASS"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.db_password[each.key].secret_id
            version = "latest"
          }
        }
      }

      env {
        name  = "DB_NAME"
        value = "${var.project_name}"
      }

      env {
        name  = "DB_USER"
        value = "${var.project_name}"
      }
{%- endif %}
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

{%- if cookiecutter.session_type == "cloud_sql" %}
    # Cloud SQL volume
    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.session_db[each.key].connection_name]
      }
    }
{%- endif %}
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
{%- if cookiecutter.session_type == "cloud_sql" %}
    google_sql_user.db_user,
    google_secret_manager_secret_version.db_password,
{%- if cookiecutter.agent_identity_enabled %}
    google_secret_manager_secret_iam_member.app_sa_db_password,
{%- endif %}
{%- endif %}
  ]
}
