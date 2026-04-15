---
name: agents-cli-observability
description: >
  This skill should be used when the user wants to "set up tracing",
  "configure logging", "add observability", "debug production traffic",
  or needs guidance on monitoring deployed ADK agents.
  Covers Cloud Trace, prompt-response logging, BigQuery Agent Analytics,
  third-party integrations (AgentOps, Phoenix, MLflow, etc.), and troubleshooting.
  Do NOT use for deployment setup (use agents-cli-deploy) or
  API code patterns (use agents-cli-adk-code).
metadata:
  author: Google
  license: Apache-2.0
  version: 0.0.3
  requires:
    bins:
      - agents-cli
    install: "uv tool install agents-cli"
---

# ADK Observability Guide

> Cloud Trace and prompt-response logging are pre-configured by Terraform. See `references/cloud-trace-and-logging.md` for infrastructure details, env vars, and verification commands. If your project isn't scaffolded yet, see `/agents-cli-scaffold` first.

### Reference Files

| File | Contents |
|------|----------|
| `references/cloud-trace-and-logging.md` | Scaffolded project details — Terraform-provisioned resources, environment variables, verification commands, enabling/disabling locally |
| `references/bigquery-agent-analytics.md` | BQ Agent Analytics plugin — enabling, key features, GCS offloading, tool provenance |

---

## Observability Tiers

Choose the right level of observability based on your needs:

| Tier | What It Does | Scope | Default State | Best For |
|------|-------------|-------|---------------|----------|
| **Cloud Trace** | Distributed tracing — execution flow, latency, errors via OpenTelemetry spans | All templates, all environments | Always enabled | Debugging latency, understanding agent execution flow |
| **Prompt-Response Logging** | GenAI interactions exported to GCS, BigQuery, and Cloud Logging | ADK agents only | Disabled locally, enabled when deployed | Auditing LLM interactions, compliance |
| **BigQuery Agent Analytics** | Structured agent events (LLM calls, tool use, outcomes) to BigQuery | ADK agents with plugin enabled | Opt-in (`--bq-analytics` at scaffold time) | Conversational analytics, custom dashboards, LLM-as-judge evals |
| **Third-Party Integrations** | External observability platforms (AgentOps, Phoenix, MLflow, etc.) | Any ADK agent | Opt-in, per-provider setup | Team collaboration, specialized visualization, prompt management |

**Ask the user** which tier(s) they need — they can be combined. Cloud Trace is always on; the others are additive.

---

## Cloud Trace

ADK uses OpenTelemetry to emit distributed traces. Every agent invocation produces spans that track the full execution flow.

### Span Hierarchy

```
invocation
  └── agent_run (one per agent in the chain)
        ├── call_llm (model request/response)
        └── execute_tool (tool execution)
```

### Setup by Deployment Type

| Deployment | Setup |
|-----------|-------|
| **Agent Engine** | Automatic — traces are exported to Cloud Trace by default |
| **Cloud Run (scaffolded)** | Automatic — `otel_to_cloud=True` in the FastAPI app |
| **GKE (scaffolded)** | Automatic — `otel_to_cloud=True` in the FastAPI app |
| **Cloud Run / GKE (manual)** | Configure OpenTelemetry exporter in your app |
| **Local dev** | Works with `agents-cli dev`; traces visible in Cloud Console |

View traces: **Cloud Console → Trace → Trace explorer**

For detailed setup instructions (Agent Engine CLI/SDK, Cloud Run, custom deployments), fetch `https://google.github.io/adk-docs/integrations/cloud-trace/index.md`.

---

## Prompt-Response Logging

Captures GenAI interactions (model name, tokens, timing) and exports to GCS (JSONL), BigQuery (external tables), and Cloud Logging (dedicated bucket). Privacy-preserving by default — only metadata is logged unless explicitly configured otherwise.

Key env var: `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` — set to `NO_CONTENT` (metadata only, default in deployed envs), `true` (full content), or `false` (disabled). Logging is disabled locally unless `LOGS_BUCKET_NAME` is set.

For scaffolded project details (Terraform resources, env vars, privacy modes, enabling/disabling, verification commands), see `references/cloud-trace-and-logging.md`.

For ADK logging docs (log levels, configuration, debugging), fetch `https://google.github.io/adk-docs/observability/logging/index.md`.

---

## BigQuery Agent Analytics Plugin

Optional plugin that logs structured agent events to BigQuery. Enable with `--bq-analytics` at scaffold time. See `references/bigquery-agent-analytics.md` for details.

---

## Third-Party Integrations

ADK supports several third-party observability platforms. Each uses OpenTelemetry or custom instrumentation to capture agent behavior.

| Platform | Key Differentiator | Setup Complexity | Self-Hosted Option |
|----------|-------------------|-----------------|-------------------|
| **AgentOps** | Session replays, 2-line setup, replaces native telemetry | Minimal | No (SaaS) |
| **Arize AX** | Commercial platform, production monitoring, evaluation dashboards | Low | No (SaaS) |
| **Phoenix** | Open-source, custom evaluators, experiment testing | Low | Yes |
| **MLflow** | OTel traces to MLflow Tracking Server, span tree visualization | Medium (needs SQL backend) | Yes |
| **Monocle** | 1-call setup, VS Code Gantt chart visualizer | Minimal | Yes (local files) |
| **Weave** | W&B platform, team collaboration, timeline views | Low | No (SaaS) |
| **Freeplay** | Prompt management + evals + observability in one platform | Low | No (SaaS) |

**Ask the user** which platform they prefer — present the trade-offs and let them choose. For setup details, fetch the relevant ADK docs page from the Deep Dive table below.

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| No traces in Cloud Trace | Verify `otel_to_cloud=True` in FastAPI app; check service account has `cloudtrace.agent` role |
| Prompt-response data not appearing | Check `LOGS_BUCKET_NAME` is set; verify SA has `storage.objectCreator` on the bucket; check app logs for telemetry setup warnings |
| Privacy mode misconfigured | Check `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` value — use `NO_CONTENT` for metadata-only, `false` to disable |
| BigQuery Analytics not logging | Verify plugin is configured in `app/agent.py`; check `BQ_ANALYTICS_DATASET_ID` env var is set |
| Third-party integration not capturing spans | Check provider-specific env vars (API keys, endpoints); some providers (AgentOps) replace native telemetry |
| Traces missing tool spans | Tool execution spans appear under `execute_tool` — check trace explorer filters |
| High telemetry costs | Switch to `NO_CONTENT` mode; reduce BigQuery retention; disable unused tiers |

---

## Deep Dive: ADK Docs (WebFetch URLs)

For detailed documentation beyond what this skill covers, fetch these pages:

| Topic | URL |
|-------|-----|
| Observability overview | `https://google.github.io/adk-docs/observability/index.md` |
| Agent activity logging | `https://google.github.io/adk-docs/observability/logging/index.md` |
| Cloud Trace integration | `https://google.github.io/adk-docs/integrations/cloud-trace/index.md` |
| BigQuery Agent Analytics | `https://google.github.io/adk-docs/integrations/bigquery-agent-analytics/index.md` |
| AgentOps | `https://google.github.io/adk-docs/integrations/agentops/index.md` |
| Arize AX | `https://google.github.io/adk-docs/integrations/arize-ax/index.md` |
| Phoenix (Arize) | `https://google.github.io/adk-docs/integrations/phoenix/index.md` |
| MLflow tracing | `https://google.github.io/adk-docs/integrations/mlflow/index.md` |
| Monocle | `https://google.github.io/adk-docs/integrations/monocle/index.md` |
| W&B Weave | `https://google.github.io/adk-docs/integrations/weave/index.md` |
| Freeplay | `https://google.github.io/adk-docs/integrations/freeplay/index.md` |

---

## Related Skills

- `/agents-cli-deploy` — Deployment targets, CI/CD pipelines, and production workflows
- `/agents-cli-workflow` — Development workflow, coding guidelines, and operational rules
- `/agents-cli-adk-code` — ADK Python API quick reference for writing agent code
