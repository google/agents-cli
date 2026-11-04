# Evaluation Metrics Reference

> File paths below reference the scaffolded layout (`tests/eval/eval_config.yaml` or `.json`). Adjust for your project structure if not using `google-agents-cli-scaffold`.

> Python projects scaffold the eval datasets in the `tests/eval/` directory; Go projects scaffold in the `eval/` directory. Examples below use the Python convention.

## Managed (Built-in) Metrics Reference

Run `agents-cli eval metric list` for the live set. **Single-turn only** below means the metric 400s on a trace with 2+ turns (`Single-turn metric '<name>_v1' received agent_eval_data with N turns`). The single-turn adaptive-rubric metrics grade a case's own `rubric_groups` instead of generating their own when it supplies them (see *Managed Metric Parameters*).

### Agent metrics (adaptive rubrics)

| Metric ID | Evaluates | Trace |
|-----------|-----------|-------|
| `multi_turn_task_success` | User goal/intent fulfillment across the conversation. Ignores supplied `rubric_groups`. | any |
| `multi_turn_trajectory_quality` | Step sequencing, efficiency, error recovery. | any |
| `multi_turn_tool_use_quality` | Technical and semantic correctness of tool calls. | any |
| `final_response_quality` | Final response plus intermediate tool usage. | single-turn only |
| `final_response_reference_free` | Final response quality with no reference answer. Needs `rubric_groups` on the case (500s without). | single-turn only |
| `tool_use_quality` | Tool selection, parameter accuracy, step order. Needs `function_call` events in the trace. | single-turn only |

> `multi_turn_general_quality` and `multi_turn_text_quality` need a `conversation_history` field that `eval generate` does not produce, and 400 on agent traces. Use `multi_turn_task_success` or `multi_turn_trajectory_quality`.

### General quality metrics (adaptive rubrics, single-turn only)

| Metric ID | Evaluates |
|-----------|-----------|
| `general_quality` | Overall quality with auto-generated criteria. Best starting point for non-agent eval. |
| `text_quality` | Fluency, coherence, grammar. |
| `instruction_following` | Adherence to the constraints in the prompt. |

### Static rubric metrics (fixed criteria, single-turn only)

| Metric ID | Evaluates |
|-----------|-----------|
| `hallucination` | Segments the response into atomic claims and checks each against tool output. |
| `final_response_match` | Judge-scored semantic match against a golden answer, not string equality. Needs `reference` on the case. |
| `grounding` | Labels each sentence of the response supported or contradictory against context. Needs `context` (a string or `Content`) on the case. |
| `safety` | Policy compliance (PII, hate speech, dangerous content, harassment, sexual). |

---

## Custom Metrics

Custom metrics are declared in `eval_config.yaml` (or `.json`) under `custom_metrics`. See SKILL.md's *Evaluation Configuration Schema* section for how `metrics_to_run` selects from the pool. The schema below defines the per-entry fields.

Code-based metrics default to **local in-process execution** (no GCP project or region required); opt into the `CodeExecutionMetric` sandbox in Gemini platform with `execution: "remote"`. The metric functions are written in Python, regardless of the project's language.

> **Scaffolded default metric.** The scaffolded `eval_config.yaml` ships `custom_response_quality` as a local LLM-judge in `tests/eval/response_quality.py` (referenced via `custom_function_file`, run in-process via `google-genai`). It grades on either backend — `genai.Client()` uses `GEMINI_API_KEY` (AI Studio) or ADC (Gemini platform) — and reads each case's `reference` (ground truth) when present. To grade with the managed Eval Service in Gemini platform instead, replace it with a built-in metric or an `LLMMetric` (`prompt_template`).

### Example

```yaml
metrics_to_run:
  - multi_turn_trajectory_quality
  - project_response_rubric
  - agent_turn_count

custom_metrics:
  - name: project_response_rubric
    prompt_template: |
      Rate the agent's response 1-5 for helpfulness and accuracy.
      Prompt: {prompt}
      Final response: {response}
      Full trace (for tool-call and reasoning context): {agent_data}
      Return JSON: {"score": <1|2|3|4|5>, "explanation": "<reason>"}
    judge_model_sampling_count: 3

  - name: agent_turn_count
    custom_function: |
      def evaluate(instance):
          turns = (instance.get("agent_data") or {}).get("turns", [])
          return {'score': len(turns)}

  - name: tool_call_count
    execution: remote
    custom_function: |
      def evaluate(instance):
          n = 0
          for turn in (instance.get("agent_data") or {}).get("turns", []):
              for event in turn.get("events", []):
                  for part in (event.get("content") or {}).get("parts", []):
                      if "function_call" in part:
                          n += 1
          return {'score': n}
```

Metrics receive the eval case's `{prompt}`, `{response}`, and `{agent_data}` (and `{reference}` / `{context}` when the case populates them) — see SKILL.md's *Evaluation Configuration Schema → Agent trace field model* for details.

To make a custom metric gate the run, add it to the top-level `thresholds` map with a minimum mean score in the metric's own scale (e.g. `project_response_rubric: 4` for the 1-5 judge above). Only metrics with a threshold can fail on score, and a count like `agent_turn_count` usually shouldn't have one.

### Schema reference

Each entry in `custom_metrics` must conform to one of two Gemini platform evaluation metric schemas. `custom_function` or `custom_function_file` selects the code-based schema (in-process by default, `CodeExecutionMetric` with `execution: remote`); otherwise it's `LLMMetric`. An entry that carries neither, and whose `name` is a built-in metric, is a *managed metric parameterization* instead (see below).

#### Code Execution Metric (`CodeExecutionMetric`)

Evaluates responses using custom Python code.

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Unique identifier for the metric. |
| `custom_function` | one of | Python source containing `def evaluate(instance):`. Receives an evaluation instance, returns a numeric score or a `{'score', 'explanation'}` dict. |
| `custom_function_file` | one of | Path to a `.py` file containing `def evaluate(instance):`, **resolved relative to the eval config file's directory** (absolute paths honored). Keeps the metric a real, lintable/testable module instead of an inline blob. Mutually exclusive with `custom_function`. Works with both `execution` modes (for `remote`, the file's source is uploaded). |
| `execution` | no | Where the function runs. `"local"` (default) — executed in the CLI process; no GCP project or region required; **runs with the CLI's privileges**, so only use trusted code. `"remote"` — uploaded and executed inside the `CodeExecutionMetric` sandbox; requires a configured GCP project + region. |

> **Local metrics grade cases concurrently.** Build the client once per thread, never inside `evaluate()`, which re-does ADC and the TLS handshake every case. Per thread rather than per process: one client cannot serve several threads behind a client certificate. `tests/eval/response_quality.py` has the shape to copy.

> **`evaluate()` can run up to five times for one case**, since transient failures are retried. Avoid side effects that must happen exactly once.

**Minimal `custom_function_file` example** — point the metric at a sibling `.py` file instead of an inline blob:

```yaml
# tests/eval/eval_config.yaml
metrics_to_run:
  - turn_count
custom_metrics:
  - name: turn_count
    custom_function_file: metrics.py   # resolved next to this config file
```

```python
# tests/eval/metrics.py  (same directory as the config)
def evaluate(instance):
    turns = (instance.get("agent_data") or {}).get("turns", [])
    return {"score": len(turns)}
```

Run with `agents-cli eval run --config tests/eval/eval_config.yaml`.

**LLM judge in a custom function**: the way to combine your own judge prompt with per-case criteria, and how to grade multi-turn `rubric_groups`:

```python
# tests/eval/rubric_judge.py: keep execution local: the remote sandbox has no network
import json

from google import genai


def evaluate(instance):
    rubrics = [
        r["content"]["property"]["description"]
        for g in (instance.get("rubric_groups") or {}).values()
        for r in g["rubrics"]
    ]
    prompt = (
        f"Criteria: {rubrics}\nConversation: {json.dumps(instance['agent_data'])}\n"
        'Return JSON: {"score": <fraction of criteria met>, "explanation": "<what failed>"}'
    )
    out = genai.Client().models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config={"response_mime_type": "application/json"},
    )
    return json.loads(out.text)
```

#### LLM-as-a-Judge Metric (`LLMMetric`)

Evaluates responses using an LLM judge driven by a prompt template.

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Unique identifier for the metric. |
| `prompt_template` | yes | Prompt template used by the judge model. With agents-cli's file-based `EvaluationDataset` use `{prompt}`, `{response}`, and `{agent_data}` (the full trajectory). `{reference}` and `{context}` resolve only when the eval case has those fields populated. |
| `rubric_group_name` | n/a | **Rejected by agents-cli.** It makes the service demand rubric verdicts a custom prompt cannot emit (`400 No rubric verdicts found in LLM response`). Grade `rubric_groups` with a managed metric plus `metric_spec_parameters.rubric_group_key`. |
| `judge_model` | no | Judge model (e.g., `gemini-3.8-flash`). |
| `judge_model_sampling_count` | no | Number of judge samples to compute the score (1–32). |
| `judge_model_system_instruction` | no | System instruction for the judge model. |
| `judge_model_generation_config` | no | Generation config for the judge LLM (e.g., `temperature`). |

#### Managed Metric Parameters (`metric_spec_parameters`)

Parameters for a built-in metric, passed through a `custom_metrics` entry that has no `prompt_template` and no `custom_function`. Use a metric ID from the tables above. `rubric_group_key` picks which of the case's `rubric_groups` to grade against, and is required only when a case defines more than one.

```yaml
metrics_to_run:
  - final_response_quality
custom_metrics:
  - name: final_response_quality
    metric_spec_parameters:
      rubric_group_key: case_criteria
```
