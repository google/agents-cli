# Evaluation Criteria Reference

> File paths below reference the scaffolded layout (`tests/eval/eval_config.json`). Adjust for your project structure if not using /agents-cli-scaffold.

## All Available Criteria

| Criterion | Reference-Based | Requires Rubrics | LLM-as-Judge | Supports User Simulation |
|-----------|:-:|:-:|:-:|:-:|
| `tool_trajectory_avg_score` | Yes | No | No | No |
| `response_match_score` | Yes | No | No | No |
| `final_response_match_v2` | Yes | No | Yes | No |
| `rubric_based_final_response_quality_v1` | No | Yes | Yes | Yes |
| `rubric_based_tool_use_quality_v1` | No | Yes | Yes | Yes |
| `hallucinations_v1` | No | No | Yes | Yes |
| `safety_v1` | No | No | Yes | Yes |
| `per_turn_user_simulator_quality_v1` | No | No | Yes | Yes |

Default when no config provided: `tool_trajectory_avg_score: 1.0` + `response_match_score: 0.8`

---

## Trajectory Match Types

`tool_trajectory_avg_score` supports three match modes via `match_type`:

| Match Type | Behavior |
|------------|----------|
| `EXACT` (default) | Perfect match — same tools, same order, no extras |
| `IN_ORDER` | Expected tools must appear in order, but extra tools allowed between them |
| `ANY_ORDER` | Expected tools must all appear, any order, extra tools allowed |

```json
{
  "criteria": {
    "tool_trajectory_avg_score": {
      "threshold": 1.0,
      "match_type": "IN_ORDER"
    }
  }
}
```

**When to use each:**
- `EXACT` — strict workflow validation, regression testing
- `IN_ORDER` — key actions must happen in sequence, but agent may do additional work
- `ANY_ORDER` — agent must call certain tools but order doesn't matter (e.g., multiple search queries)

---

## Rubric-Based Criteria

Two rubric-based metrics exist for different evaluation targets:

**`rubric_based_final_response_quality_v1`** — evaluates the agent's final response:
```json
{
  "criteria": {
    "rubric_based_final_response_quality_v1": {
      "threshold": 0.8,
      "rubrics": [
        {
          "rubric_id": "conciseness",
          "rubric_content": { "text_property": "The response is direct and to the point." }
        },
        {
          "rubric_id": "intent_inference",
          "rubric_content": { "text_property": "The response accurately infers the user's underlying goal." }
        }
      ]
    }
  }
}
```

**`rubric_based_tool_use_quality_v1`** — evaluates how the agent uses tools:
```json
{
  "criteria": {
    "rubric_based_tool_use_quality_v1": {
      "threshold": 1.0,
      "rubrics": [
        {
          "rubric_id": "geocoding_first",
          "rubric_content": { "text_property": "The agent calls GeoCoding before GetWeather." }
        }
      ]
    }
  }
}
```

Each rubric produces a yes (1.0) / no (0.0) verdict. The overall score is the average across all rubrics and invocations.

---

## Hallucinations

`hallucinations_v1` checks if the agent's response is grounded in context (instructions, user query, tool outputs). Two-step process:

1. **Segmenter** — splits response into individual sentences
2. **Sentence Validator** — labels each as `supported`, `unsupported`, `contradictory`, `disputed`, or `not_applicable`

Score = percentage of sentences that are `supported` or `not_applicable`.

```json
{
  "criteria": {
    "hallucinations_v1": {
      "threshold": 0.8,
      "evaluate_intermediate_nl_responses": true,
      "judge_model_options": {
        "judge_model": "gemini-3-flash-preview"
      }
    }
  }
}
```

Set `evaluate_intermediate_nl_responses: true` to also check sub-agent responses (not just final response).

---

## Safety

`safety_v1` evaluates harmlessness. Requires a Google Cloud Project (`GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` env vars). Delegates to the Vertex AI Gen AI Evaluation SDK.

```json
{
  "criteria": {
    "safety_v1": 0.8
  }
}
```

---

## Judge Model Options

LLM-as-judge criteria accept `judge_model_options`:

```json
{
  "judge_model_options": {
    "judge_model": "gemini-3-flash-preview",
    "num_samples": 5
  }
}
```

- `judge_model` — model used for judging (default: `gemini-3-flash-preview`)
- `num_samples` — how many times the judge is called per invocation (default: 5). Results are aggregated by majority vote to reduce LLM variance.

Applicable to: `final_response_match_v2`, `rubric_based_final_response_quality_v1`, `rubric_based_tool_use_quality_v1`, `hallucinations_v1`, `per_turn_user_simulator_quality_v1`

---

## Custom Metrics

Define custom Python functions as metrics in `eval_config.json`:

```json
{
  "criteria": {
    "my_custom_metric": 0.8
  },
  "custom_metrics": {
    "my_custom_metric": {
      "code_config": {
        "name": "path.to.my.metric_function"
      },
      "description": "My custom evaluation metric"
    }
  }
}
```

The function receives the eval case data and threshold, and returns a score. See `eval_config.py` for the `CustomMetricConfig` schema.

---

## Deep Dive

For complete metric documentation with detailed algorithms and output interpretation:
- WebFetch: `https://raw.githubusercontent.com/google/adk-docs/main/docs/evaluate/criteria.md`
