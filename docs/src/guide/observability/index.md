# Observability

Every project that Agents CLI scaffolds ships with OpenTelemetry instrumentation that automatically exports traces to Cloud Trace. The instrumentation gives you:

- **Distributed tracing**: Track requests as they flow through LLM calls and tool executions.
- **Latency analysis**: Find performance bottlenecks from span durations.
- **Error visibility**: Traces capture errors, so you can see where failures occur.
- **No configuration required**: Works out of the box in all environments.

For ADK Python and Claude Agent SDK agents, **prompt-response logging** captures full model interactions (prompts, responses, tokens) and uploads them to GCS as JSONL and to a BigQuery `completions` table. It's on whenever a logs bucket is configured (`LOGS_BUCKET_NAME` plus the `OTEL_INSTRUMENTATION_GENAI_*` upload vars). Terraform-provisioned deployments configure one by default. ADK Go projects get the same BigQuery telemetry dataset, but not the GCS completion upload.

!!! note "Two independent tiers"
    Prompt-response logging (GCS/BigQuery completions) captures full content. Two separate settings control whether content also appears in Cloud Trace spans and Cloud Logging events: `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` (default `NO_CONTENT`, which keeps content out of traces and events) and, for ADK Python only, `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false`. By default, GCS and BigQuery get full content and traces get none. ADK Go reads `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` as a boolean instead: `true` or `1` captures content, and any other value keeps it out.

<a id="logging-behavior-by-environment"></a>
## How does logging behave in each environment?

| Environment | Cloud Trace spans | Prompt-response logging (GCS/BigQuery) |
|---|---|---|
| **Local** (`agents-cli playground`) | Enabled, no content | Off (no `LOGS_BUCKET_NAME`) |
| **Deployed** (Terraform-provisioned) | Enabled, no content | **On, full prompts/responses** (ADK Python, Claude) |
| **Deployed** (bare `agents-cli deploy`, no bucket) | Enabled, no content | Off (no `LOGS_BUCKET_NAME`) |

---

## Cloud Trace

Cloud Trace is the default observability method. See [Cloud Trace](cloud-trace.md) for setup and usage.

---

## BigQuery Agent Analytics

Use BigQuery Agent Analytics to query patterns across conversations, build token usage dashboards, and run LLM-as-judge scoring on production traffic. The plugin is available for ADK Python agents and the Claude Agent SDK template. Turn it on with the `--bq-analytics` flag when you create the project.

See [BigQuery Agent Analytics](bq-agent-analytics.md) for details.
