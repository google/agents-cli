---
name: google-agents-cli-observability
description: >
  This skill should be used when the user wants to "set up tracing",
  "monitor my agent", "configure logging", "add observability",
  "debug production traffic", or needs guidance on monitoring deployed
  agents, including ADK (Agent Development Kit) agents.
  Covers Cloud Trace, prompt-response logging, BigQuery Agent Analytics,
  third-party integrations (AgentOps, Phoenix, MLflow, etc.), and troubleshooting.
  Part of the agents-cli skills suite.
  Do NOT use for deployment setup (use google-agents-cli-deploy) or
  API code patterns (use google-agents-cli-adk-code).
metadata:
  author: Google
  license: Apache-2.0
  version: 1.9.0
  requires:
    bins:
      - agents-cli
    install: "uv tool install google-agents-cli"
---

# Observability Guide

> **Cloud Trace** works out of the box — no infrastructure needed. **Prompt-response logging** and **BigQuery Agent Analytics** require Terraform-provisioned infrastructure (service account, GCS bucket, BigQuery dataset). Run `agents-cli infra single-project --apply --project PROJECT_ID` to provision these resources. Go projects get the BigQuery telemetry stack too; the GCS completion upload behind prompt-response logging and the BigQuery Agent Analytics plugin are Python only. See `references/cloud-trace-and-logging.md` for details, env vars, and verification commands. If your project isn't scaffolded yet, see `/google-agents-cli-scaffold` first.

### Deployment order

| Do you want Terraform-managed observability? | Action |
|---|---|
| **No** | Run `agents-cli deploy` directly (works for all targets). |
| **Yes** | Run `agents-cli infra single-project --apply` first, then `agents-cli deploy`. |

**If you already ran `agents-cli deploy` imperatively:**
Do not apply Terraform afterward. For Agent Runtime, Terraform owns the whole engine (service account, deployment spec, env vars), so a later apply can't be reconciled without taking over the resource. 
Either delete the deployment and start over, or keep the SDK-deployed instance — skip `infra single-project` and set the observability env vars by re-running `agents-cli deploy --update-env-vars "KEY=VALUE,..."`; deploy matches the existing Reasoning Engine by display name and updates it in place, preserving env vars set outside the deploy. You must also grant its service account the telemetry IAM roles the Terraform module would otherwise provision: `roles/storage.admin` (write completions to the logs bucket), `roles/logging.logWriter`, `roles/cloudtrace.agent`, plus `roles/bigquery.dataOwner` + `roles/bigquery.jobUser` when scaffolded with `--bq-analytics`. The full set lives in `deployment/terraform/single-project/iam.tf` (from `app_sa_roles`) and `telemetry.tf`. Terraform-managed env vars aren't available in this mode.

### Reference Files

| File | Contents |
|------|----------|
| `references/cloud-trace-and-logging.md` | Scaffolded project details — Terraform-provisioned resources, environment variables, verification commands, enabling/disabling locally |
| `references/bigquery-agent-analytics.md` | BQ Agent Analytics plugin — enabling, key features, GCS offloading, tool provenance |
| `references/adk-docs.md` | **ADK:** adk.dev pages to fetch for detail beyond this skill |
| `references/feedback-mechanism.md` | Adding a user-feedback endpoint — request model, structured logging, log sink → BigQuery |

---

## Observability Tiers

Choose the right level of observability based on your needs:

| Tier | What It Does | Scope | Default State | Best For |
|------|-------------|-------|---------------|----------|
| **Cloud Trace** | Distributed tracing — execution flow, latency, errors via OpenTelemetry spans | All templates, all environments | Always enabled | Debugging latency, understanding agent execution flow |
| **Prompt-Response Logging** | GenAI interactions exported to GCS, BigQuery, and Cloud Logging | Scaffolded ADK Python projects | Disabled locally, enabled when deployed | Auditing LLM interactions, compliance |
| **BigQuery Agent Analytics** | Structured agent events (LLM calls, tool use, outcomes) to BigQuery | ADK Python agents with the plugin enabled | Opt-in (`--bq-analytics` at scaffold time) | Conversational analytics, custom dashboards, LLM-as-judge evals |
| **Third-Party Integrations** | External observability platforms (AgentOps, Phoenix, MLflow, etc.) | Any OpenTelemetry-instrumented agent | Opt-in, per-provider setup | Team collaboration, specialized visualization, prompt management |

**Ask the user** which tier(s) they need — they can be combined. Cloud Trace is always on; the others are additive.

---

## Cloud Trace

Scaffolded agents use OpenTelemetry to emit distributed traces. Every agent invocation produces spans that track the full execution flow.

### Span Hierarchy

> **ADK projects.** These are ADK's span names; other frameworks emit their own (`generate_content` comes from the shared google-genai instrumentor either way).

```
invoke_workflow (top-level run)
  └── invoke_agent (one per agent in the chain)
        ├── call_llm (model request)
        │     └── generate_content (underlying GenAI model call)
        └── execute_tool (tool execution)
```

### Setup by Deployment Type

| Deployment | Setup |
|-----------|-------|
| **Agent Runtime** | Automatic — exporters wired at startup, gated on `GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY` (set by deploy); exports to Cloud Trace/Logging + Agent Engine console |
| **Cloud Run / GKE (scaffolded)** | Automatic — exporters wired at startup, exports to Cloud Trace/Logging |
| **Cloud Run / GKE (manual)** | Configure OpenTelemetry exporter in your app |
| **Local dev** | Works with `agents-cli playground`; traces visible in Cloud Console |

Wired at app startup — **ADK Python:** `get_fast_api_app(otel_to_cloud=True)` in `app/fast_api_app.py`; **ADK Go:** `setupObservability()` in `observability.go`; other templates call their own. `references/cloud-trace-and-logging.md` has the details.

View traces: **Cloud Console → Trace → Trace explorer**

**ADK:** for detailed setup instructions (Agent Runtime CLI/SDK, Cloud Run, custom deployments), fetch `https://adk.dev/integrations/cloud-trace/index.md`.

---

## Prompt-Response Logging

Captures GenAI interactions and exports to GCS (JSONL) and BigQuery (via log sinks + external tables). Content is governed by **two independent tiers**; the net Terraform-deploy default is **full content in GCS/BigQuery, none in traces**:

| Tier | Captures | Controlled by | Default (Terraform deploy) |
|------|----------|---------------|----------------------------|
| **GCS/BigQuery completions** | Full prompts/responses (the prompt-response logging feature) | `OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK=upload` + `LOGS_BUCKET_NAME` | **On** — full content |
| **Trace spans / Cloud Logging events** | Span/event content | `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` (plus `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false`, **ADK Python only**) | **Off** — `NO_CONTENT` |

The tiers are independent: GCS/BigQuery uploads capture full content whenever their upload vars are set and do **not** honor `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`, which governs the traces/events tier only.

**ADK Python** reads it as the experimental-semconv enum:

- `NO_CONTENT` — no content in spans/events (scaffolded default)
- `EVENT_ONLY` — content in Cloud Logging events
- `SPAN_ONLY` / `SPAN_AND_EVENT` — content in trace spans
- `true` / `false` — **invalid**; fall back to `NO_CONTENT`

**ADK Go** reads the same variable as a **boolean**: `"1"` or `"true"` capture content, every other
value — including the enum members above — elides it.

For the full mechanics (semconv opt-in, declarative Terraform config, env-var table, enabling/disabling, verification commands), see `references/cloud-trace-and-logging.md`. For ADK logging docs (log levels, configuration, debugging), fetch `https://adk.dev/observability/logging/index.md`.

---

## BigQuery Agent Analytics Plugin

> **ADK projects.** Optional ADK plugin that logs structured agent events to BigQuery. Enable with `--bq-analytics` at scaffold time. See `references/bigquery-agent-analytics.md` for details.

---

## Third-Party Integrations

Many third-party observability platforms can ingest agent telemetry (via OpenTelemetry or custom instrumentation). The table below covers common ones; the full list is larger (see the pointer below it).

| Platform | Key Differentiator | Setup Complexity | Self-Hosted Option |
|----------|-------------------|-----------------|-------------------|
| **AgentOps** | Session replays, 2-line setup, replaces native telemetry | Minimal | No (SaaS) |
| **Arize AX** | Commercial platform, production monitoring, evaluation dashboards | Low | No (SaaS) |
| **Phoenix** | Open-source, custom evaluators, experiment testing | Low | Yes |
| **MLflow** | OTel traces to MLflow Tracking Server, span tree visualization | Medium (needs SQL backend) | Yes |
| **Monocle** | 1-call setup, VS Code Gantt chart visualizer | Minimal | Yes (local files) |
| **Weave** | W&B platform, team collaboration, timeline views | Low | No (SaaS) |
| **Freeplay** | Prompt management + evals + observability in one platform | Low | No (SaaS) |

**Ask the user** which platform they prefer — present the trade-offs and let them choose. **ADK:** fetch a platform's setup page at `https://adk.dev/integrations/<slug>/index.md` (slugs for the table above: `agentops`, `arize-ax`, `phoenix`, `mlflow-tracing`, `monocle`, `weave`, `freeplay`); ADK has more observability integrations (Datadog, Galileo, LangWatch, Latitude, Future AGI, Respan, Zespan, …) — browse the complete, current list at `https://adk.dev/integrations/` (observability topic). On other frameworks the OpenTelemetry-based platforms still work, but follow the platform's own setup docs.

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| No traces in Cloud Trace | Verify telemetry setup runs at startup and the SA has the `cloudtrace.agent` role. **ADK Python:** `fast_api_app.py` uses `get_fast_api_app(otel_to_cloud=True)`; **ADK Go:** `observability.go` build the exporter manually. Agent Runtime additionally gates this on `GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY`. |
| Prompt-response data not appearing | Check `LOGS_BUCKET_NAME` is set; verify SA has `storage.objectCreator` on the bucket; check app logs for telemetry setup warnings |
| Content in traces/events (unwanted) | **ADK Python:** `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT` keeps content out of spans/events. **ADK Go:** any value other than `1`/`true` does, so unset it or set `false`. NOTE: GCS/BigQuery completions still capture full content — to stop that, remove `LOGS_BUCKET_NAME`/`OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK` (drop the upload block in `service.tf`) |
| BigQuery Analytics not logging | **ADK Python:** verify the plugin is configured in `app/agent.py`; check `BQ_ANALYTICS_DATASET_ID` env var is set |
| Third-party integration not capturing spans | Check provider-specific env vars (API keys, endpoints); some providers (AgentOps) replace native telemetry |
| Traces missing tool spans | **ADK:** tool execution spans appear under `execute_tool` (other frameworks use their own span names) — check trace explorer filters |
| High telemetry costs | Turn content capture off (`NO_CONTENT` in Python, `false` in Go); reduce BigQuery retention; disable unused tiers |

---

## Related Skills

- `/google-agents-cli-deploy` — Deployment targets, CI/CD pipelines, and production workflows
- `/google-agents-cli-workflow` — Development workflow, coding guidelines, and operational rules
- `/google-agents-cli-adk-code` — ADK API quick reference for writing agent code, Python and Go
