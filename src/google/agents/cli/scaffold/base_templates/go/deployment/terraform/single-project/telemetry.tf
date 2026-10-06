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

# GenAI telemetry for Go agents.
#
# ADK Go emits prompt/response content inline in OpenTelemetry log records
# (exported over OTLP to telemetry.googleapis.com -> Cloud Logging).

# BigQuery dataset for telemetry data.
resource "google_bigquery_dataset" "telemetry_dataset" {
  project       = var.project_id
  dataset_id    = replace("${var.project_name}_telemetry", "-", "_")
  friendly_name = "${var.project_name} Telemetry"
  location      = var.region
  description   = "Dataset for GenAI telemetry data from Cloud Logging"
  depends_on    = [google_project_service.services]
}

# ====================================================================
# Log Sink — route GenAI completion logs to BigQuery
# ====================================================================

# telemetry.googleapis.com sets the Cloud Logging log id from the OTLP event
# name, so ADK Go prompts land under the "gen_ai.user.message" log and responses
# under "gen_ai.choice". Route both to BigQuery.
resource "google_logging_project_sink" "genai_logs_to_bq" {
  name        = "${var.project_name}-genai-logs"
  project     = var.project_id
  destination = "bigquery.googleapis.com/projects/${var.project_id}/datasets/${google_bigquery_dataset.telemetry_dataset.dataset_id}"
  filter      = "log_id(\"gen_ai.user.message\") OR log_id(\"gen_ai.choice\")"

  unique_writer_identity = true

  bigquery_options {
    use_partitioned_tables = true
  }

  depends_on = [google_bigquery_dataset.telemetry_dataset]
}

# Grant the log sink service account write access to the BigQuery dataset.
resource "google_bigquery_dataset_iam_member" "genai_logs_bq_writer" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.telemetry_dataset.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = google_logging_project_sink.genai_logs_to_bq.writer_identity
}

# ====================================================================
# GenAI Log Export Tables (pre-created for the completions view)
# ====================================================================

# Pre-create the log export tables so completions_view can be created on the first
# deploy (before any logs arrive). Cloud Logging appends to these via the sink and
# adds columns as needed (BQ schema evolution). Table ids are the Cloud Logging
# log ids with non-alphanumeric characters replaced by underscores
# (gen_ai.user.message -> gen_ai_user_message, gen_ai.choice -> gen_ai_choice).

resource "google_bigquery_table" "genai_user_message" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.telemetry_dataset.dataset_id
  table_id            = "gen_ai_user_message"
  deletion_protection = false
  description         = "GenAI prompt/history messages exported from Cloud Logging"

  time_partitioning {
    type  = "DAY"
    field = "timestamp"
  }

  schema = file("${path.module}/../shared/genai_logs_schema.json")

  # Prevents table recreation and data loss:
  # 1. Cloud Logging appends columns (including nested fields) as entries arrive.
  # 2. Terraform reads them as drops and force-replaces the table, deleting
  #    exported logs.
  # Schema updates apply only to new tables; completions.sql cannot assume
  # columns added to genai_logs_schema.json exist on existing tables.
  lifecycle {
    ignore_changes = [schema]
  }

  depends_on = [google_bigquery_dataset.telemetry_dataset]
}

resource "google_bigquery_table" "genai_choice" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.telemetry_dataset.dataset_id
  table_id            = "gen_ai_choice"
  deletion_protection = false
  description         = "GenAI responses exported from Cloud Logging"

  time_partitioning {
    type  = "DAY"
    field = "timestamp"
  }

  schema = file("${path.module}/../shared/genai_logs_schema.json")

  # Prevents table recreation when Cloud Logging appends columns (see genai_user_message).
  lifecycle {
    ignore_changes = [schema]
  }

  depends_on = [google_bigquery_dataset.telemetry_dataset]
}

# ====================================================================
# Completions View (flattens inline prompt/response content)
# ====================================================================

resource "google_bigquery_table" "completions_view" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.telemetry_dataset.dataset_id
  table_id            = "completions_view"
  description         = "View of GenAI prompt/response content read inline from Cloud Logging"
  deletion_protection = false

  view {
    query = templatefile("${path.module}/../shared/completions.sql", {
      project_id        = var.project_id
      dataset_id        = google_bigquery_dataset.telemetry_dataset.dataset_id
      input_logs_table  = google_bigquery_table.genai_user_message.table_id
      output_logs_table = google_bigquery_table.genai_choice.table_id
    })
    use_legacy_sql = false
  }

  depends_on = [
    google_bigquery_table.genai_user_message,
    google_bigquery_table.genai_choice,
    google_logging_project_sink.genai_logs_to_bq
  ]
}
