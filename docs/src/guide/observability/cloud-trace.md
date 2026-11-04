# Cloud Trace

*For developers who have deployed an agent and want to verify tracing works and inspect telemetry data.*

![Observability monitoring flow](../../assets/observability.png)

Cloud Trace is enabled by default in all `agents-cli` projects. This guide shows how to verify it works and query your telemetry data.

---

<a id="verify-tracing-in-your-deployment"></a>
## How do I verify tracing in my deployment?

After deploying to your development environment, confirm telemetry data is flowing:

<a id="1-deploy-and-generate-test-traffic"></a>
<a id="deploy-and-generate-test-traffic"></a>
### How do I generate test traffic?

```bash
gcloud config set project YOUR_DEV_PROJECT_ID
agents-cli deploy --project YOUR_DEV_PROJECT_ID
```

Send a few test requests to your agent.

<a id="2-view-traces"></a>
<a id="view-traces"></a>
### How do I view traces?

Open the Google Cloud Console and navigate to **Trace > Trace explorer**. You should see traces for each request, with spans showing LLM calls and tool executions. On Agent Runtime, traces also appear in the Agent Runtime page of the console.

To use a trace to find why the agent gave a wrong answer, see [Debug a failing agent](../debugging.md#how-do-i-see-what-the-agent-did-step-by-step).

<a id="3-verify-prompt-response-logging-optional"></a>
<a id="verify-prompt-response-logging"></a>
### How do I verify prompt-response logging?

This step is optional. Terraform (`agents-cli infra single-project` or `agents-cli infra cicd`) provisions prompt-response logging to GCS and BigQuery. It creates the logs bucket and dataset and sets `LOGS_BUCKET_NAME`, so logging is on automatically in those deployments. A bare `agents-cli deploy` does not create these resources, so the checks below apply only to Terraform-provisioned deployments. The GCS completion upload is available for ADK Python and Claude Agent SDK agents.

```bash
PROJECT_ID="your-dev-project-id"
PROJECT_NAME="your-project-name"  # The agents-cli project name, not the GCP project ID

# Check for telemetry files in GCS
gsutil ls gs://${PROJECT_ID}-${PROJECT_NAME}-logs/completions/

# Query telemetry in BigQuery (dataset names use underscores instead of hyphens)
bq query --use_legacy_sql=false \
  "SELECT * FROM \`${PROJECT_ID}.${PROJECT_NAME//-/_}_telemetry.completions\` LIMIT 10"
```

If data isn't appearing:

1. Check that the service account has the `storage.objectCreator` role.
2. Verify `LOGS_BUCKET_NAME` is set in your deployment environment variables.
3. Check application logs in Cloud Logging for telemetry setup warnings.

---

<a id="enable-prompt-response-logging-locally"></a>
## How do I enable prompt-response logging locally?

By default, `agents-cli playground` runs without prompt-response logging. Telemetry is
declarative (there's no runtime `setup_telemetry()`). To turn on completions logging locally, set
the same vars Terraform sets for deployed agents. The completion upload works for ADK Python and Claude Agent SDK agents:

```bash
export LOGS_BUCKET_NAME="your-dev-project-id-your-project-name-logs"   # bare bucket name, no gs://
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT="NO_CONTENT"  # or EVENT_ONLY for full content in logs
export OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK="upload"
export OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH="gs://your-dev-project-id-your-project-name-logs/completions"
export OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT="jsonl"
export OTEL_SEMCONV_STABILITY_OPT_IN="gen_ai_latest_experimental"
agents-cli playground
```

---

<a id="disable-prompt-response-logging-in-deployments"></a>
## How do I disable prompt-response logging in deployments?

Content in trace spans and Cloud Logging events is already off by default, so you don't need to change `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`. To stop the upload of full prompts and responses to GCS and BigQuery, remove these `env` blocks from `deployment/terraform/single-project/service.tf` (or `deployment/terraform/cicd/service.tf` for staging and production):

- `OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK`
- `OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH`
- `LOGS_BUCKET_NAME`

The scaffolded ADK Python app also stores artifacts in the `LOGS_BUCKET_NAME` bucket, so removing that variable moves artifacts to in-memory storage.

Then apply:

```bash
cd deployment/terraform/single-project
terraform apply -var-file=vars/env.tfvars
```

These steps work on Cloud Run and GKE. On Agent Runtime, Terraform ignores changes to the agent's environment variables after it creates the agent, and `agents-cli deploy` keeps the existing variables and has no flag to remove one. Remove the `env` blocks before you first run `agents-cli infra single-project --apply` or `agents-cli infra cicd --apply`, which creates the agent. For an Agent Runtime agent that already exists, agents-cli can't remove these variables. The Agent Runtime CI/CD pipelines also pass `LOGS_BUCKET_NAME` on every deploy, so remove it from the `--update-env-vars` line in `.cloudbuild/` or `.github/workflows/` too.

---

## Configuration reference

| Variable | Values | Purpose |
|---|---|---|
| `LOGS_BUCKET_NAME` | Cloud Storage bucket **name** (no `gs://`) | Required for prompt-response logging. If not set, logging is disabled. |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | `NO_CONTENT`, `EVENT_ONLY`, `SPAN_ONLY`, `SPAN_AND_EVENT` | Controls content in **traces/events only** (not the GCS/BigQuery completions, which always capture full content when a bucket is set). Experimental semconv (the scaffold sets `OTEL_SEMCONV_STABILITY_OPT_IN`): `NO_CONTENT` = none in spans/events (default); `EVENT_ONLY` = content in Cloud Logging events; `SPAN_*` = content in trace spans. In ADK Python, **`true`/`false` are invalid** and fall back to `NO_CONTENT`. ADK Go reads the variable as a boolean: `true` or `1` captures content, and any other value keeps it out. |
| `OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK` | `upload` | Enables uploading completion records |
| `OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH` | `gs://<bucket>/completions` | Destination for completion records |
| `OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT` | `jsonl` | Upload format |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | `gen_ai_latest_experimental` | Required for the GenAI completion/upload semconv |
| `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS` | `false`, `true` | ADK Python only. Keeps prompt/response content out of trace spans when `false`. ADK defaults to `true`; Terraform and `agents-cli deploy` set `false`. |

