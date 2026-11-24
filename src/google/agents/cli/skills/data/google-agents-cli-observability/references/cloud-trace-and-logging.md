# Cloud Trace & Prompt-Response Logging (Scaffolded Projects)

> **Assumes `/google-agents-cli-scaffold` scaffolding.** Observability infrastructure is provisioned by Terraform in scaffolded projects.

> **ADK Java, Kotlin and TypeScript.** These templates export no traces yet and get no `telemetry.tf`,
> so this file doesn't apply to them.

> **Feature gaps in ADK Go.** Traces and logs are exported over OTLP to `telemetry.googleapis.com`.
> Three things below are **ADK Python only**: the BigQuery Agent Analytics plugin (`--bq-analytics`),
> the GCS completion upload (`OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK`) that backs prompt-response
> logging, and `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS`.
>
> `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` exists in both but means different things:
> ADK Python takes the semconv enum, ADK Go treats it as a boolean — `"1"` or `"true"` capture
> content, and every other value (including `NO_CONTENT`) elides it.

## Cloud Trace

Always-on distributed tracing, exporting spans/logs to Cloud Trace and Cloud Logging. The exporters are wired at app startup (**ADK Python:** `get_fast_api_app(otel_to_cloud=True)`; **ADK Go:** `setupObservability()` in `observability.go`, which exports traces and logs over OTLP to `telemetry.googleapis.com` regardless of the launcher's `-otel_to_cloud` flag; other templates call their own setup, e.g. `app/app_utils/telemetry.py`). For **Agent Runtime in Gemini platform** it's gated on `GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY` (set by deploy), and traces also appear on the Agent Runtime page in the Google Cloud console. Content env vars are declared statically (Terraform `service.tf` for deployed, `.env` for local). Tracks requests through LLM calls and tool executions with latency analysis and error visibility.

View traces: **Cloud Console → Trace → Trace explorer**

No configuration required. Works in local dev (`agents-cli playground`) and all deployed environments.

## Prompt-Response Logging Infrastructure

All provisioned automatically by `deployment/terraform/single-project/telemetry.tf` (and the `cicd/` variant):

- **Log sinks** — Route GenAI inference logs directly to BigQuery (partitioned tables)
- **BigQuery dataset** — Telemetry dataset with external tables over GCS data and pre-created log export table
- **Pre-created log export table** — Cloud Logging BQ export schema (labels flattened: dots become underscores). Cloud Logging names the sink table after the log id, so it varies by deployment target: `gen_ai_client_inference_operation_details` (Cloud Run / GKE) or `aiplatform_googleapis_com_reasoning_engine_stdout` (Agent Runtime, where GenAI logs arrive via stdout)
- **GCS logs bucket** — Stores completions as NDJSON
- **BigQuery connection** — Service account for GCS access from BigQuery
- **Completions view** — Joins BQ log export data with GCS-stored prompt/response data

Check `deployment/terraform/single-project/telemetry.tf` for exact configuration. IAM bindings grant log sink service accounts `roles/bigquery.dataEditor` on the telemetry dataset.

**Collecting user feedback?** The same infrastructure supports a feedback mechanism (endpoint + structured logging → log sink → BigQuery). See `references/feedback-mechanism.md`.

## Environment Variables

Set automatically by Terraform on the deployed service. The `OTEL_INSTRUMENTATION_GENAI_*` content-capture and upload variables (capture modes, `OTEL_SEMCONV_STABILITY_OPT_IN`, completion hook / upload) are documented in [`opentelemetry-util-genai`](https://github.com/open-telemetry/opentelemetry-python-genai/tree/main/util/opentelemetry-util-genai) and the [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/).

### Common (Python & Go)

| Variable | Purpose | Notes |
|----------|---------|--------|
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | Controls content capture for the traces/events tier only | Python accepts `NO_CONTENT`/`EVENT_ONLY`/`SPAN_ONLY`/`SPAN_AND_EVENT`; Go accepts `true`/`false` |

### Python only

| Variable | Purpose |
|----------|---------|
| `LOGS_BUCKET_NAME` | GCS bucket for completions and logs. Required to enable prompt-response logging |
| `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS` | **ADK only.** Keeps message content out of trace spans; Terraform sets `false` (ADK defaults to `true`) |
| `BQ_ANALYTICS_DATASET_ID` | BigQuery dataset for telemetry (ADK BigQuery Agent Analytics plugin; only when scaffolded with `--bq-analytics`) |
| `BQ_ANALYTICS_CONNECTION_ID` | BigQuery connection for GCS access (only when scaffolded with `--bq-analytics`) |
| `BQ_ANALYTICS_GCS_BUCKET` | GCS bucket for BigQuery Analytics multimodal offloading (only when scaffolded with `--bq-analytics`) |
| `OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK` | Set to `upload` to export full completions to GCS (the prompt-response logging feature) |
| `OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH` | GCS path for uploaded completions (e.g. `gs://<bucket>/completions`) |
| `OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT` | Upload format for completions (e.g. `jsonl`) |

## Enabling / Disabling

### Enable Locally

Telemetry config is env-var driven, so set the same vars Terraform sets for deployed agents before running `agents-cli playground`:

**ADK Python:**

```bash
export LOGS_BUCKET_NAME="your-bucket-name"                       # bare name, no gs://
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT="NO_CONTENT"  # or EVENT_ONLY (content in Cloud Logging events)
export OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK="upload"
export OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH="gs://your-bucket-name/completions"
export OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT="jsonl"
export OTEL_SEMCONV_STABILITY_OPT_IN="gen_ai_latest_experimental"
```

**ADK Go:** only the first variable applies, and as a boolean.

```bash
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT="true"  # "1"/"true" capture; anything else elides
```

### Disable in Deployed Environments

Content in traces/events is already off by default. Python: the default is `NO_CONTENT`, and `true`/`false` are no valid values under experimental semconv (they fall back to `NO_CONTENT`). Go: the default is off because anything other than `"1"`/`"true"` elides content. To turn off prompt-response logging to GCS/BigQuery in Python entirely, remove the upload block (`OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK`, `OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH`, `LOGS_BUCKET_NAME`) from `deployment/terraform/single-project/service.tf` (or the `cicd/` variant) and re-apply Terraform.

## BigQuery Dataset Naming Convention

BigQuery dataset names **cannot contain hyphens**. Terraform automatically converts hyphens to underscores when creating dataset names from your project name:

- Project name `my-agent` → BQ dataset `my_agent_telemetry`

One dataset is created:
- **`{name}_telemetry`** — Contains external tables over GCS completions data (NDJSON), the pre-created log export table (`gen_ai_client_inference_operation_details` on Cloud Run / GKE, `aiplatform_googleapis_com_reasoning_engine_stdout` on Agent Runtime), and the `completions_view`

To discover the actual dataset name in your project:
```bash
bq ls --project_id=${PROJECT_ID}
```

## Verifying Telemetry

After deploying, verify prompt-response logging is working:

```bash
PROJECT_ID="your-dev-project-id"
PROJECT_NAME="your-app-name"  # The agents-cli project name (not the GCP project ID)

# Check GCS data
gsutil ls gs://${PROJECT_ID}-${PROJECT_NAME}-logs/completions/

# Check BigQuery log export table (logs arrive via sink, may take a few minutes).
# Table name varies by target: gen_ai_client_inference_operation_details on
# Cloud Run / GKE, aiplatform_googleapis_com_reasoning_engine_stdout on Agent Runtime.
bq query --use_legacy_sql=false \
  "SELECT COUNT(*) FROM \`${PROJECT_ID}.${PROJECT_NAME//-/_}_telemetry.gen_ai_client_inference_operation_details\`"

# Query completions external table
bq query --use_legacy_sql=false \
  "SELECT * FROM \`${PROJECT_ID}.${PROJECT_NAME//-/_}_telemetry.completions\` LIMIT 10"

# Query the completions view (joins log export with GCS data)
bq query --use_legacy_sql=false \
  "SELECT * FROM \`${PROJECT_ID}.${PROJECT_NAME//-/_}_telemetry.completions_view\` LIMIT 10"
```

If data is not appearing: check `LOGS_BUCKET_NAME` is set, verify SA has `storage.objectCreator` on the bucket, check application logs for telemetry setup warnings. Log export to BigQuery may take a few minutes to propagate.
