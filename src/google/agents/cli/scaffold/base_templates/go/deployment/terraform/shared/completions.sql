-- Copyright 2026 Google LLC
--
-- Licensed under the Apache License, Version 2.0 (the "License");
-- you may not use this file except in compliance with the License.
-- You may obtain a copy of the License at
--
--     https://www.apache.org/licenses/LICENSE-2.0
--
-- Unless required by applicable law or agreed to in writing, software
-- distributed under the License is distributed on an "AS IS" BASIS,
-- WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
-- See the License for the specific language governing permissions and
-- limitations under the License.

-- SQL view for Cloud Logging prompt/response data.
-- This query extracts both input and output messages referenced in logs.
-- Note: Input files contain full conversation history, so messages may appear multiple times.
--
-- Log data is exported directly to BigQuery via log sinks.
-- The GenAI log tables (input_logs_table / output_logs_table) are pre-created by
-- Terraform and populated by Cloud Logging via the sink.
-- Labels and OTel resource attributes are flattened into individual columns (dots replaced with underscores).

-- Prompt + history messages (one OTLP record per message in the request),
-- flattened to one row per part.
WITH input_parts AS (
  SELECT
    timestamp,
    insertId AS insert_id,
    trace,
    spanId AS span_id,
    'input' AS message_type,
    jsonPayload.content.role AS role,
    part_idx,
    part.text AS text,
    part.functionCall.name AS function_call_name,
    part.functionResponse.name AS function_response_name,
    part.inlineData.mimeType AS inline_mime_type,
    part.fileData.mimeType AS file_mime_type,
    part.fileData.fileUri AS file_uri,
    -- Tool call args / tool response, serialized to a JSON string. TO_JSON_STRING
    -- handles the arbitrary (and per-table schema-evolved) shape without breaking
    -- the UNION; deeply nested values are already trimmed at OTLP ingest (depth 5).
    JSON_QUERY(TO_JSON_STRING(part.functionCall), '$.args') AS tool_args,
    JSON_QUERY(TO_JSON_STRING(part.functionResponse), '$.response') AS tool_response,
    CAST(NULL AS STRING) AS finish_reasons,
    -- telemetry.googleapis.com exports OTel attributes under otel, not labels. Existing
    -- tables may lack service_version, so JSON extraction yields NULL. The otel record must
    -- exist: declared in schema for new tables, added by Cloud Logging on the first entry.
    JSON_VALUE(TO_JSON_STRING(otel.resource.attributes), '$.service_version') AS service_version
  FROM `${project_id}.${dataset_id}.${input_logs_table}`
  CROSS JOIN UNNEST(jsonPayload.content.parts) AS part WITH OFFSET AS part_idx
  WHERE jsonPayload.content IS NOT NULL
),

-- Model responses (one OTLP record per inference), flattened to one row per part.
output_parts AS (
  SELECT
    timestamp,
    insertId AS insert_id,
    trace,
    spanId AS span_id,
    'output' AS message_type,
    jsonPayload.content.role AS role,
    part_idx,
    part.text AS text,
    part.functionCall.name AS function_call_name,
    part.functionResponse.name AS function_response_name,
    part.inlineData.mimeType AS inline_mime_type,
    part.fileData.mimeType AS file_mime_type,
    part.fileData.fileUri AS file_uri,
    JSON_QUERY(TO_JSON_STRING(part.functionCall), '$.args') AS tool_args,
    JSON_QUERY(TO_JSON_STRING(part.functionResponse), '$.response') AS tool_response,
    jsonPayload.finish_reason AS finish_reasons,
    JSON_VALUE(TO_JSON_STRING(otel.resource.attributes), '$.service_version') AS service_version
  FROM `${project_id}.${dataset_id}.${output_logs_table}`
  CROSS JOIN UNNEST(jsonPayload.content.parts) AS part WITH OFFSET AS part_idx
  WHERE jsonPayload.content IS NOT NULL
),

-- Union of scalar columns only (never the divergent content STRUCTs).
all_parts AS (
  SELECT * FROM input_parts
  UNION ALL
  SELECT * FROM output_parts
),

-- Classify each part and normalise the tool/content columns.
typed AS (
  SELECT
    timestamp,
    insert_id,
    trace,
    span_id,
    message_type,
    role,
    part_idx,
    CASE
      WHEN text IS NOT NULL THEN 'text'
      WHEN function_call_name IS NOT NULL THEN 'function_call'
      WHEN function_response_name IS NOT NULL THEN 'function_response'
      WHEN inline_mime_type IS NOT NULL THEN 'inline_data'
      WHEN file_uri IS NOT NULL OR file_mime_type IS NOT NULL THEN 'file_data'
      ELSE NULL
    END AS part_type,
    text AS content,
    COALESCE(function_call_name, function_response_name) AS tool_name,
    COALESCE(file_mime_type, inline_mime_type) AS mime_type,
    file_uri AS uri,
    tool_args,
    tool_response,
    finish_reasons,
    service_version
  FROM all_parts
),

-- Stage 1: de-dupe exact repeats within a trace+span (e.g. retried log writes).
dedup_within_trace AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY trace, span_id, message_type, role, part_idx, part_type, content, tool_name
      ORDER BY timestamp DESC
    ) AS row_num
  FROM typed
),

-- Stage 2: de-dupe conversation history repeated across traces.
-- ADK Go re-logs the full request history on every turn, so an early user
-- message reappears in later turns. Keep the earliest input copy of each message;
-- outputs are unique per trace, so keep them all. There is no conversation id in
-- the logs, so messages are keyed on their own content.
dedup_across_traces AS (
  SELECT
    *,
    CASE
      WHEN message_type = 'input' THEN ROW_NUMBER() OVER (
        PARTITION BY message_type, role, part_idx, part_type, content, tool_name
        ORDER BY timestamp ASC
      )
      ELSE 1
    END AS cross_trace_row_num
  FROM dedup_within_trace
  WHERE row_num = 1
)

SELECT
  -- Core identifiers and timestamps
  timestamp,
  insert_id,
  trace,
  span_id,

  -- Span-only in ADK Go (query Cloud Trace) or GCS-pipeline only; NULL for parity.
  CAST(NULL AS STRING) AS conversation_id,
  CAST(NULL AS STRING) AS api_call_id,

  -- Message metadata
  message_type,
  role,
  CAST(NULL AS INT64) AS message_idx,
  part_idx,

  -- Message content
  content,

  -- Tool/function calling. tool_args/tool_response are JSON strings holding just
  -- the args / response payload (NULL for non-tool parts); values nested beyond
  -- OTLP depth 5 are trimmed on ingest.
  part_type,
  tool_name,
  tool_args,
  tool_response,

  -- Usage metadata (span-only in ADK Go; NULL for parity).
  CAST(NULL AS STRING) AS usage_input_tokens,
  CAST(NULL AS STRING) AS usage_output_tokens,
  CAST(NULL AS STRING) AS agent_name,
  finish_reasons,

  -- Deployment revision from OTel resource attribute service.version; NULL when unset by the deployment.
  service_version,

  -- Additional metadata
  uri,
  mime_type,
  CAST(NULL AS STRING) AS data_md5_hex,
  CAST(NULL AS STRING) AS messages_ref_uri
FROM dedup_across_traces
WHERE cross_trace_row_num = 1
  -- Exclude model messages echoed back as input context (already captured as output)
  AND NOT (message_type = 'input' AND role = 'model')
ORDER BY trace ASC, timestamp ASC, message_type ASC, part_idx ASC
