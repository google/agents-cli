# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The server's spans: OpenInference instrumentation, and export to Cloud Trace."""

from __future__ import annotations

import logging
import os
from typing import Any

from openinference.instrumentation import REDACTED_VALUE, TraceConfig
from openinference.semconv.trace import SpanAttributes
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult
from opentelemetry.trace import NoOpTracer
from opentelemetry.util.genai.types import TextPart
from opentelemetry.util.genai.utils import gen_ai_json_dumps

from app.app_utils.telemetry import config
from app.app_utils.telemetry.subagent_spans import current_subagent_spans
from app.app_utils.turn_events import system_prompt_text


def install_instrumentation() -> None:
    try:
        from openinference.instrumentation.claude_agent_sdk import (
            ClaudeAgentSDKInstrumentor,
        )

        ClaudeAgentSDKInstrumentor().instrument(config=_trace_config())
    except Exception as e:  # pragma: no cover - optional dependency
        logging.warning("Claude Agent SDK instrumentation unavailable: %s", e)


def setup_cloud_tracing(
    credentials: Any, resource: Any, project_id: str
) -> TracerProvider:
    """A tracer provider that names the agent's spans and exports them to Cloud Trace."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.resources import OTELResourceDetector, Resource
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON

    # Cloud Run and Agent Runtime front ends usually mark traceparent not
    # sampled, so a parent-based sampler would drop every span.
    sampler = None if os.environ.get("OTEL_TRACES_SAMPLER") else ALWAYS_ON
    # Spans only: logs and metrics need the detected resource to map to their
    # Cloud Logging and Cloud Monitoring resources.
    span_resource = resource.merge(
        Resource(config.agent_runtime_attributes(project_id))
    ).merge(OTELResourceDetector().detect())
    tracer_provider = _TracerProvider(resource=span_resource, sampler=sampler)
    tracer_provider.add_span_processor(AgentNameProcessor(config.agent_name()))
    session, upstream = config.telemetry_session(credentials)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(session=session, endpoint=f"{upstream}/v1/traces")
        )
    )
    return tracer_provider


class AgentNameProcessor(SpanProcessor):
    """Adds gen_ai.agent.name to the agent's and its tools' spans."""

    def __init__(self, agent_name: str) -> None:
        self._agent_name = agent_name

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        attributes = span.attributes or {}
        if SpanAttributes.OPENINFERENCE_SPAN_KIND not in attributes:
            return
        subagent_spans = current_subagent_spans.get()
        name = subagent_spans.span_started(span) if subagent_spans is not None else None
        if name is not None:
            span.set_attribute("gen_ai.agent.name", name)
        elif SpanAttributes.AGENT_NAME not in attributes:
            span.set_attribute("gen_ai.agent.name", self._agent_name)


# NOTE: The OpenTelemetry GenAI conventions are adding memory operations (a
# search_memory span with gen_ai.memory.records). Once a released version defines
# them, record recalled memories that way.
def system_prompt_attributes(system_prompt: Any) -> dict[str, str]:
    """The turn span's record of the system prompt the model got, as ADK's.

    Empty unless content capture is on: the prompt can hold the user's memories.
    """
    text = system_prompt_text(system_prompt)
    if not text or not config.capture_content():
        return {}
    return {"gen_ai.system_instructions": gen_ai_json_dumps([TextPart(content=text)])}


def _trace_config() -> TraceConfig:
    os.environ.setdefault("OPENINFERENCE_ENABLE_GENAI_SEMCONV", "true")
    if not config.capture_content():
        os.environ.setdefault("OPENINFERENCE_HIDE_INPUTS", "true")
        os.environ.setdefault("OPENINFERENCE_HIDE_OUTPUTS", "true")
    return _TraceConfig()


class _TraceConfig(TraceConfig):
    """Also hides tool.parameters when inputs are hidden; OpenInference doesn't."""

    def mask(self, key, value, *, externalize: bool = True):
        if self.hide_inputs and key == SpanAttributes.TOOL_PARAMETERS:
            return REDACTED_VALUE
        return super().mask(key, value, externalize=externalize)


# MCP spans duplicate OpenInference's tool spans; BigQuery spans come from
# BigQuery Agent Analytics' own writes.
_UNRECORDED_SCOPES = ("mcp-python-sdk", "google.cloud.bigquery")


class _TracerProvider(TracerProvider):
    """Gives _UNRECORDED_SCOPES no-op tracers, and records unsampled spans.

    No-op tracers rather than export filtering, so child spans keep a parent
    that exists.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.sampler = _RecordUnsampled(self.sampler)

    def get_tracer(self, instrumenting_module_name: str, *args: Any, **kwargs: Any):
        if instrumenting_module_name.startswith(_UNRECORDED_SCOPES):
            return NoOpTracer()
        return super().get_tracer(instrumenting_module_name, *args, **kwargs)


class _RecordUnsampled(Sampler):
    """Records dropped spans without sampling them.

    OpenInference fails a turn whose span isn't recording.
    """

    def __init__(self, sampler: Sampler) -> None:
        self._sampler = sampler

    def should_sample(self, *args: Any, **kwargs: Any) -> SamplingResult:
        result = self._sampler.should_sample(*args, **kwargs)
        if result.decision is not Decision.DROP:
            return result
        return SamplingResult(
            Decision.RECORD_ONLY, result.attributes, result.trace_state
        )

    def get_description(self) -> str:
        return self._sampler.get_description()
