# Control cost

Four things drive what an agent costs: model tokens, compute for the deployed service, judge calls during evaluation, and telemetry storage. Measure first, then change one setting at a time and check with your evals that quality holds. Snippets and measurement details are ADK Python.

## How do I measure token usage?

- **Cloud Trace**: ADK exports a `generate_content {model.name}` span for each model call. It carries `gen_ai.request.model`, `gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens`. ADK Go emits the same span. The Claude template emits an `invoke_agent` span per turn instead. See [ADK traces](https://adk.dev/observability/traces/) and [Cloud Trace](observability/cloud-trace.md).
- **BigQuery Agent Analytics**: the `v_llm_response` view exposes `usage_prompt_tokens`, `usage_completion_tokens`, `usage_cached_tokens`, `usage_thinking_tokens` and `context_cache_hit_rate`. The query in [BigQuery Agent Analytics](observability/bq-agent-analytics.md) sums tokens per agent and model.

## How do I pick a model?

Let your evals decide. Run the same dataset against a few current Gemini models and keep the one with the best balance of eval score and cost for the task:

1. Change `model=` in `app/agent.py`. New agents start on `gemini-3.8-flash`.
2. Run `agents-cli eval run`, then `agents-cli eval compare` against the previous results.
3. Weigh the scores against the token usage from the previous section.

Each agent in a multi-agent app can use its own model, so a simple routing or summarizing agent can run on a Flash-Lite model while the main agent stays on Flash or Pro. See [Google models](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/google-models) for the current lineup and the [Evaluation guide](evaluation.md) for the eval loop.

## How do I cut input tokens?

Cache long, stable context. `ContextCacheConfig` applies to all agents in the `App` and is experimental. Caching starts on the second turn, so single-turn sessions are never cached. Cached context incurs storage charges ([context cache overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/context-cache/context-cache-overview)). ADK documents caching for Python, Java and Kotlin, so it doesn't apply to Go or TypeScript agents ([ADK context caching](https://adk.dev/context/caching/)).

```python
from google.adk.apps import App
from google.adk.agents.context_cache_config import ContextCacheConfig

app = App(
    name="app",
    root_agent=root_agent,
    context_cache_config=ContextCacheConfig(
        min_tokens=4096,     # Gemini 3 and 4 minimum
        ttl_seconds=1800,    # cache lifetime (default 1800)
        cache_intervals=10,  # reuse a cache at most N times
    ),
)
```

To check that caching works, look for `gen_ai.usage.cache_read.input_tokens` on the span or `context_cache_hit_rate` in `v_llm_response`.

Compact long sessions. Token-based compaction summarizes older events once the prompt reaches `token_threshold` and keeps the last `event_retention_size` events raw. ADK Java uses an interval and overlap pair instead ([ADK compaction](https://adk.dev/context/compaction/)). Each compaction is a summarizer LLM call. Pass `summarizer=LlmEventSummarizer(llm=Gemini(model=...))` to use a cheaper model.

```python
from google.adk.apps.app import App, EventsCompactionConfig

app = App(
    name="app",
    root_agent=root_agent,
    events_compaction_config=EventsCompactionConfig(
        token_threshold=32000,
        event_retention_size=5,
    ),
)
```

## How do I cap output tokens?

Set `max_output_tokens` in the agent's generate config:

```python
from google.genai import types as genai_types
from google.adk.agents import Agent

agent = Agent(
    name="my_agent",
    model="gemini-3.8-flash",
    instruction="...",
    generate_content_config=genai_types.GenerateContentConfig(max_output_tokens=1024),
)
```

Leave room for thinking tokens, or a thinking model can return an empty response with `finish_reason=MAX_TOKENS` ([python-genai#782](https://github.com/googleapis/python-genai/issues/782)).

## How do I scale to zero?

On create, `agents-cli deploy` defaults to `--min-instances 0` on Agent Runtime and Cloud Run. On an existing service it keeps the current value. A Terraform-provisioned service stays at `min_instances = 1` until you change it in Terraform or pass `--min-instances 0`. The Agent Runtime Terraform also sets 4 vCPU and 8Gi, against CLI defaults of 1 and 4Gi, so edit `deployment/terraform/` to size down. Use `--min-instances 1` when a cold start is not acceptable, for example for live and voice agents.

| Target | Cost model | Pricing |
|---|---|---|
| Agent Runtime | vCPU-hours and memory-hours. Idle time between turns isn't billed. A warm minimum instance may be. | [Gemini platform pricing](https://cloud.google.com/products/gemini-enterprise-agent-platform/pricing) |
| Cloud Run | CPU and memory while handling requests (default) or for the instance lifetime. Minimum instances bill at the idle rate. | [Cloud Run pricing](https://cloud.google.com/run/pricing) |
| GKE | Node costs plus a cluster management fee. The generated Terraform keeps at least 2 replicas. | [GKE pricing](https://cloud.google.com/kubernetes-engine/pricing) |

The `--min-instances` flag applies to Agent Runtime and Cloud Run only. See [Deployment](deployment.md).

## How do I keep eval spend down?

A run spends agent tokens on every case as well as judge tokens.

- Run a small dataset with `--dataset` while you iterate, and the full set before you merge. Use `--metrics a,b` to run a subset of metrics.
- On custom LLM-judge metrics (`prompt_template`), set `judge_model` to a cheaper model and lower `judge_model_sampling_count` (1 to 32). Use 3 to 5 only for noisy judges, such as multimodal ones. For the scaffolded judge, change the `model=` line in `tests/eval/response_quality.py`.
- A rule-based `custom_function` (counts, regex, schema checks) runs in the CLI process and makes no model call. The scaffolded `custom_response_quality` is an LLM judge, so it does.
- Run `agents-cli eval optimize` only when you need prompt optimization. It's ADK Python only, makes many LLM calls and can take a long time.

## How do I cut telemetry cost?

- Keep message content out of trace spans and log events. `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` does that when it's `NO_CONTENT` (the Python default) or, in Go, unset or `false`.
- Terraform-provisioned deployments upload full prompts and responses to GCS and BigQuery. To stop that, remove the `OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK`, `OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH` and `LOGS_BUCKET_NAME` env blocks. Removing `LOGS_BUCKET_NAME` moves ADK artifacts to in-memory storage. On Agent Runtime, do this before the first `infra ... --apply`. See [Cloud Trace](observability/cloud-trace.md#how-do-i-disable-prompt-response-logging-in-deployments) and [Security and trust](security.md).
- Reduce BigQuery retention, and leave out the BigQuery Agent Analytics plugin (`create --bq-analytics`) if you don't need it.
