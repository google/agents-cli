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


{%- if cookiecutter.session_type == "cloud_sql" %}

# Generate a random password for the database user
resource "random_password" "db_password" {
  length           = 16
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

# Cloud SQL Instance
resource "google_sql_database_instance" "session_db" {
  project          = var.project_id
  name             = "${var.project_name}-db"
  database_version = "POSTGRES_15"
  region           = var.region
  deletion_protection = false

  settings {
    tier = "db-custom-1-3840"

    backup_configuration {
      enabled = false
    }
    
    # Enable IAM authentication
    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }
  }

  depends_on = [resource.google_project_service.services]
}

# Cloud SQL Database
resource "google_sql_database" "database" {
  project  = var.project_id
  name     = "${var.project_name}" # Use project name for DB to avoid conflict with default 'postgres'
  instance = google_sql_database_instance.session_db.name
}

# Cloud SQL User
resource "google_sql_user" "db_user" {
  project  = var.project_id
  name     = "${var.project_name}" # Use project name for user to avoid conflict with default 'postgres'
  instance = google_sql_database_instance.session_db.name
  password = google_secret_manager_secret_version.db_password.secret_data
}

# Store the password in Secret Manager
resource "google_secret_manager_secret" "db_password" {
  project   = var.project_id
  secret_id = "${var.project_name}-db-password"

  replication {
    auto {}
  }

  depends_on = [resource.google_project_service.services]
}

resource "google_secret_manager_secret_version" "db_password" {
  secret      = google_secret_manager_secret.db_password.id
  secret_data = random_password.db_password.result
}

{%- endif %}


resource "google_cloud_run_v2_service" "app" {
  name                = var.project_name
  location            = var.region
  project             = var.project_id
  deletion_protection = false
  ingress             = "INGRESS_TRAFFIC_ALL"
  labels = {
    "created-by"                  = "adk"
{%- if cookiecutter.agent_garden %}
    "deployed-with"               = "agent-garden"
{%- if cookiecutter.agent_sample_id %}
    "vertex-agent-sample-id"      = "{{cookiecutter.agent_sample_id}}"
    "vertex-agent-sample-publisher" = "{{cookiecutter.agent_sample_publisher}}"
{%- endif %}
{%- endif %}
  }

  template {
    containers {
      image = "us-docker.pkg.dev/cloudrun/container/hello"

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
        value = "True"
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "4Gi"
        }
      }

{%- if cookiecutter.session_type == "cloud_sql" %}
      # Mount the volume
      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      # Environment variables
      env {
        name  = "INSTANCE_CONNECTION_NAME"
        value = google_sql_database_instance.session_db.connection_name
      }

      env {
        name = "DB_PASS"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.db_password.secret_id
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

      env {
        name  = "LOGS_BUCKET_NAME"
        value = google_storage_bucket.logs_data_bucket.name
      }

      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
        value = "NO_CONTENT"
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

    service_account = google_service_account.app_sa.email
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
        instances = [google_sql_database_instance.session_db.connection_name]
      }
    }
{%- endif %}
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  # This lifecycle block prevents Terraform from overwriting the container image when it's
  # updated by Cloud Run deployments outside of Terraform (e.g., via CI/CD pipelines)
  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
    ]
  }

  # Make dependencies conditional to avoid errors.
  depends_on = [
    resource.google_project_service.services,
{%- if cookiecutter.session_type == "cloud_sql" %}
    google_sql_user.db_user,
    google_secret_manager_secret_version.db_password,
{%- endif %}
  ]
}
