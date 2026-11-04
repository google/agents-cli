# BigQuery Agent Analytics plugin

*For teams who want SQL-based analytics on agent behavior, token usage, and conversation patterns.*

## Overview

The BigQuery Agent Analytics plugin logs detailed agent events directly to BigQuery, so you can analyze agent behavior, interactions, and performance over time with SQL.

The plugin is opt-in and available for ADK Python agents and the Claude Agent SDK template. The rest of this page covers the ADK plugin. For Claude, see [First-party frameworks](../extensions/first-party.md#claude-agent-sdk).

---

<a id="when-to-use"></a>
## When should I use the plugin?

Enable this plugin when you need to:

*   **Use BigQuery's LLM capabilities** for semantic analysis of your agents. Group conversations by meaning, rank them, find errors, or run LLM-as-judge evaluation with `AI.SEARCH`, `AI.SCORE`, and `AI.GENERATE_TEXT`.
*   **Analyze your agents with BigQuery's conversational analytics**, which lets another conversational agent do the analysis so you don't write complex SQL by hand.
*   **Create custom dashboards and reports** on agent performance, tool usage, and token consumption.
*   **Retain a structured, queryable history** of agent events for auditing, fine-tuning, or joining with other business data.

Compared to the always-on [Cloud Trace telemetry](cloud-trace.md), this plugin records more granular data in structured tables for offline analysis.

---

## Prerequisites

*   Agent project generated with the `adk_python` template.
*   `google-adk` with the `bigquery-analytics` extra. The `adk_python` template already depends on `google-adk[gcp,otel-gcp,bigquery-analytics]>=2.12.0`.
*   A Google Cloud project with BigQuery API and BigQuery Storage API enabled (typically handled by Terraform).

---

<a id="enabling-the-plugin"></a>
<a id="enable-the-plugin"></a>
## How do I enable the plugin?

Use the `--bq-analytics` flag during project creation:

```bash
agents-cli create my-agent \
  -a adk_python \
  -d cloud_run \
  --bq-analytics
```

This flag adds the plugin initialization code to `app/agent.py` and sets the plugin's environment variables in Terraform. To add the plugin to an existing project, add it to `app/agent.py` by hand (see the [ADK BigQuery Agent Analytics docs](https://adk.dev/integrations/bigquery-agent-analytics/)).

---

## Configuration

You configure the plugin in your `app/agent.py` file. The generated code passes only `gcs_bucket_name` and `connection_id`. The following example sets more of the available options:

```python
from google.adk.plugins.bigquery_agent_analytics_plugin import (
    BigQueryAgentAnalyticsPlugin,
    BigQueryLoggerConfig,
)

bq_config = BigQueryLoggerConfig(
    enabled=True,
    gcs_bucket_name=os.environ.get("BQ_ANALYTICS_GCS_BUCKET"),
    connection_id=os.environ.get("BQ_ANALYTICS_CONNECTION_ID"),
    log_multi_modal_content=True,
    max_content_length=500 * 1024,
    table_id="agent_events",
)

bq_analytics_plugin = BigQueryAgentAnalyticsPlugin(
    project_id=os.environ.get("GOOGLE_CLOUD_PROJECT"),
    dataset_id=os.environ.get("BQ_ANALYTICS_DATASET_ID", "adk_agent_analytics"),
    table_id=bq_config.table_id,
    config=bq_config,
    location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-east1"),
)

app = App(
    name="app",  # must match the agent directory
    root_agent=root_agent,
    plugins=[bq_analytics_plugin],
)
```

**Key `BigQueryLoggerConfig` options:**

*   **`enabled`**: Toggles the plugin.
*   **`gcs_bucket_name`** (optional): Cloud Storage bucket for offloading large/binary content. Required only for multimodal data.
*   **`connection_id`** (optional): BigQuery Connection ID for GCS access. Required only for multimodal data.
*   **`log_multi_modal_content`**: Whether to handle and offload content parts to GCS.
*   **`max_content_length`**: Threshold for offloading text to GCS.
*   **`table_id`**: BigQuery table name (defaults to `agent_events`).
*   **`event_allowlist`** / **`event_denylist`**: Filter which event types are logged.
*   **`batch_size`**: Number of rows to batch before writing.

---

## Infrastructure

When deployed with Terraform (`agents-cli infra single-project`):

*   **Dataset**: Terraform creates a BigQuery dataset named `{project_name}_telemetry`, with hyphens in the project name replaced by underscores.
*   **Cloud Storage bucket** (optional): `{project_id}-{project_name}-logs` for content offloading.
*   **BigQuery connection** (optional): `{project_name}-genai-telemetry` for GCS access from BigQuery.
*   **Table**: The plugin creates the `agent_events` table on the first event.

---

<a id="example-queries"></a>
## What queries can I run on the analytics data?

Replace `YOUR_PROJECT_ID` with your Google Cloud project ID and `YOUR_AGENT_NAME` with your project name, using underscores in place of hyphens.

**Recent events:**

```sql
SELECT *
FROM `YOUR_PROJECT_ID.YOUR_AGENT_NAME_telemetry.agent_events`
ORDER BY timestamp DESC
LIMIT 100;
```

**Tool calls and errors:**

```sql
SELECT
  timestamp,
  JSON_VALUE(content, '$.tool') AS tool_name,
  JSON_VALUE(content, '$.args') AS tool_args,
  status,
  error_message
FROM `YOUR_PROJECT_ID.YOUR_AGENT_NAME_telemetry.agent_events`
WHERE event_type IN ('TOOL_COMPLETED', 'TOOL_ERROR')
ORDER BY timestamp DESC;
```

**LLM token usage** (the auto-created `v_llm_response` view also exposes cached and thinking tokens):

```sql
SELECT
  agent,
  model_version AS model,
  SUM(usage_prompt_tokens) AS total_prompt_tokens,
  SUM(usage_completion_tokens) AS total_completion_tokens
FROM `YOUR_PROJECT_ID.YOUR_AGENT_NAME_telemetry.v_llm_response`
GROUP BY agent, model;
```
