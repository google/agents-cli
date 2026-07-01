# BigQuery Agent Analytics Plugin

*For teams who want SQL-based analytics on agent behavior, token usage, and conversation patterns.*

## Overview

The BigQuery Agent Analytics Plugin offers enhanced observability by logging detailed agent events directly to BigQuery. This enables rich, SQL-based analysis of agent behavior, interactions, and performance over time.

This is an **opt-in** feature, available for **ADK-based agents** only.

---

## When to Use

Enable this plugin when you need to:

*   **Use BigQuery's advanced LLM capabilities** for semantic analysis of your agents — semantically group conversations, rank them, identify errors, or evaluate using LLM-as-judge via `AI.Search`, `AI.Score`, and `AI.Generate_text`.
*   **Utilize BigQuery's conversational analytics** to analyze your agents using another conversational agent, eliminating the need to write complex SQL queries manually.
*   **Create custom dashboards and reports** on agent performance, tool usage, and token consumption.
*   **Retain a structured, queryable history** of agent events for auditing, fine-tuning, or joining with other business data.

Compared to the always-on [Cloud Trace telemetry](cloud-trace.md), this plugin provides more granular data in a structured table format, designed for offline analysis.

---

## Prerequisites

*   Agent project generated with an **ADK-based** template (e.g., `adk`).
*   `google-adk` version `>=1.21.0` (added automatically when you enable the plugin).
*   A Google Cloud project with BigQuery API and BigQuery Storage API enabled (typically handled by Terraform).

---

## Enabling the Plugin

Use the `--bq-analytics` flag during project creation:

```bash
agents-cli create my-agent \
  -a adk \
  -d cloud_run \
  --bq-analytics
```

This flag includes the plugin initialization code in `app/agent.py` and configures environment variables in Terraform.

---

## Configuration

The plugin is configured in your `app/agent.py` file:

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
    location=os.environ.get("GOOGLE_CLOUD_LOCATION", "US"),
)

app = App(
    name="my-agent",
    root_agent=root_agent,
    plugins=[bq_analytics_plugin],
)
```

**Key `BigQueryLoggerConfig` options:**

*   **`enabled`**: Toggles the plugin.
*   **`gcs_bucket_name`** (optional): GCS bucket for offloading large/binary content. Required only for multimodal data.
*   **`connection_id`** (optional): BigQuery Connection ID for GCS access. Required only for multimodal data.
*   **`log_multi_modal_content`**: Whether to handle and offload content parts to GCS.
*   **`max_content_length`**: Threshold for offloading text to GCS.
*   **`table_id`**: BigQuery table name (defaults to `agent_events`).
*   **`event_allowlist`** / **`event_denylist`**: Filter which event types are logged.
*   **`batch_size`**: Number of rows to batch before writing.

---

## Infrastructure

When deployed with Terraform (`agents-cli infra single-project`):

*   **Dataset:** A BigQuery dataset named `{project_name}_telemetry` is created.
*   **GCS Bucket** (optional): `{project_id}-{project_name}-logs` for content offloading.
*   **BigQuery Connection** (optional): `{project_name}-genai-telemetry` for GCS access from BigQuery.
*   **Table:** The `agent_events` table is **auto-created** by the plugin on the first event.

---

## Example Queries

Replace `YOUR_PROJECT_ID` and `YOUR_AGENT_NAME` accordingly.

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

**LLM token usage:**

```sql
SELECT
  agent,
  JSON_VALUE(attributes, '$.model') AS model,
  SUM(CAST(JSON_VALUE(attributes, '$.usage_metadata.prompt') AS INT64)) AS total_prompt_tokens,
  SUM(CAST(JSON_VALUE(attributes, '$.usage_metadata.completion') AS INT64)) AS total_completion_tokens
FROM `YOUR_PROJECT_ID.YOUR_AGENT_NAME_telemetry.agent_events`
WHERE event_type = 'LLM_RESPONSE'
  AND JSON_VALUE(attributes, '$.usage_metadata.prompt') IS NOT NULL
GROUP BY agent, model;
```
