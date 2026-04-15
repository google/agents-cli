# Cloud Trace & Prompt-Response Logging (Scaffolded Projects)

> **Assumes `/agents-cli-scaffold` scaffolding.** Observability infrastructure is provisioned by Terraform in scaffolded projects.

## Cloud Trace

Always-on distributed tracing via `otel_to_cloud=True` in the FastAPI app. Tracks requests through LLM calls and tool executions with latency analysis and error visibility.

View traces: **Cloud Console → Trace → Trace explorer**

No configuration required. Works in local dev (`agents-cli dev`) and all deployed environments.

## Prompt-Response Logging Infrastructure

All provisioned automatically by `deployment/terraform/telemetry.tf`:

- **Cloud Logging bucket** — 10-year retention, analytics enabled, dedicated to GenAI telemetry
- **Log sinks** — Route GenAI inference logs and feedback logs to the telemetry bucket
- **Linked dataset** — Cloud Logging bucket linked to BigQuery for SQL access
- **GCS logs bucket** — Stores completions as NDJSON
- **BigQuery dataset** — External tables over GCS data, linked dataset from Cloud Logging
- **BigQuery connection** — Service account for GCS access from BigQuery

Check `deployment/terraform/telemetry.tf` for exact configuration. IAM bindings are in `iam.tf`.

## Environment Variables

Set automatically by Terraform on the deployed service:

| Variable | Purpose |
|----------|---------|
| `LOGS_BUCKET_NAME` | GCS bucket for completions and logs. Required to enable prompt-response logging |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | Controls logging state and content capture |
| `BQ_ANALYTICS_DATASET_ID` | BigQuery dataset for telemetry |
| `BQ_ANALYTICS_CONNECTION_ID` | BigQuery connection for GCS access |
| `GENAI_TELEMETRY_PATH` | Optional: override upload path within bucket (default: `completions`) |

## Enabling / Disabling

### Enable Locally

Set these before running `agents-cli dev`:

```bash
export LOGS_BUCKET_NAME="your-bucket-name"
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT="NO_CONTENT"
```

### Disable in Deployed Environments

Set `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false` in `deployment/terraform/service.tf` and re-apply Terraform.

## BigQuery Dataset Naming Convention

BigQuery dataset names **cannot contain hyphens**. Terraform automatically converts hyphens to underscores when creating dataset names from your project name:

- Project name `my-agent` → BQ dataset `my_agent_telemetry`

Two datasets are created:
- **`{name}_telemetry`** — External tables over GCS completions data (NDJSON)
- **`{name}_genai_telemetry_logs`** — Linked dataset from Cloud Logging bucket (inference + feedback logs)

To discover the actual dataset names in your project:
```bash
bq ls --project_id=${PROJECT_ID}
```

## Verifying Telemetry

After deploying, verify prompt-response logging is working:

```bash
PROJECT_ID="your-dev-project-id"
PROJECT_NAME="your-app-name"  # The starter pack project name (not the GCP project ID)

# Check GCS data
gsutil ls gs://${PROJECT_ID}-${PROJECT_NAME}-logs/completions/

# Check Cloud Logging bucket
gcloud logging buckets describe ${PROJECT_NAME}-genai-telemetry \
  --location=us-central1 --project=${PROJECT_ID}

# Query BigQuery
bq query --use_legacy_sql=false \
  "SELECT * FROM \`${PROJECT_ID}.${PROJECT_NAME}_telemetry.completions\` LIMIT 10"
```

If data is not appearing: check `LOGS_BUCKET_NAME` is set, verify SA has `storage.objectCreator` on the bucket, check application logs for telemetry setup warnings.
