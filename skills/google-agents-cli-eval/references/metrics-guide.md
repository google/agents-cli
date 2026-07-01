# Evaluation Metrics Reference

> File paths below reference the scaffolded layout (`tests/eval/eval_config.yaml` or `.json`). Adjust for your project structure if not using `google-agents-cli-scaffold`.

## Managed (Built-in) Metrics Reference

Run `agents-cli eval metric list` for the live set. The tables below summarize the predefined managed metrics in the Agent Platform Evaluation SDK, grouped by category.

### Agent metrics (multi-turn / agent-aware, adaptive rubrics)

| Metric Name | Metric ID | Description |
|-------------|-----------|-------------|
| **Agent Multi-turn Task Success** | `multi_turn_task_success` | Validates user goal/intent fulfillment across the full multi-turn conversation. |
| **Agent Multi-turn Tool Use** | `multi_turn_tool_use_quality` | Evaluates technical and semantic correctness of tool calls across multi-turn conversation. |
| **Agent Multi-turn Trajectory** | `multi_turn_trajectory_quality` | Evaluates sequential logic, efficiency, and error-recovery robustness across turns. |
| **Agent Final Response Quality** | `final_response_quality` | Comprehensive evaluation of final response and intermediate tool usage correctness. |
| **Agent Final Response Reference-Free** | `final_response_reference_free` | Evaluates agent response quality without a reference answer (requires custom rubrics). |
| **Agent Tool Use Quality** | `tool_use_quality` | Evaluates tool selection, parameter accuracy, and step sequence correctness (single-turn). |
| **Multi-turn General Quality** | `multi_turn_general_quality` | Evaluates overall response quality within a multi-turn dialogue. |
| **Multi-turn Text Quality** | `multi_turn_text_quality` | Evaluates linguistic text quality within a multi-turn dialogue. |

### General quality metrics (single-turn, adaptive rubrics)

| Metric Name | Metric ID | Description |
|-------------|-----------|-------------|
| **General Quality** | `general_quality` | Overall response quality with auto-generated content-based criteria. Recommended starting point for non-agent eval. |
| **Text Quality** | `text_quality` | Linguistic aspects: fluency, coherence, grammar. |
| **Instruction Following** | `instruction_following` | How well the response adheres to specific constraints and instructions. |

### Static rubric metrics (fixed criteria)

| Metric Name | Metric ID | Description |
|-------------|-----------|-------------|
| **Agent Hallucination** | `hallucination` | Segments response into atomic claims; verifies grounding in intermediate tool outputs. |
| **Agent Final Response Match** | `final_response_match` | Compares agent response to a provided golden reference answer. |
| **Grounding** | `grounding` | Checks factuality and consistency against provided context. |
| **Safety** | `safety` | Compliance against policies (PII, hate speech, dangerous content, harassment, sexual). |

---

## Custom Metrics

Custom metrics are declared in `eval_config.yaml` (or `.json`) under `custom_metrics`. See SKILL.md's *Evaluation Configuration Schema* section for how `metrics_to_run` selects from the pool. The schema below defines the per-entry fields.

Code-based metrics default to **local in-process execution** (no GCP project or region required); opt into the Vertex AI sandbox with `execution: "remote"`.

> **Scaffolded default metric.** The scaffolded `eval_config.yaml` ships `custom_response_quality` as a local LLM-judge in `tests/eval/metrics.py` (referenced via `custom_function_file`, run in-process via `google-genai`). It grades on either backend — `genai.Client()` uses `GEMINI_API_KEY` (AI Studio) or ADC (Vertex) — and reads each case's `reference` (ground truth) when present. To grade with the managed Vertex eval service instead, replace it with a built-in metric or an `LLMMetric` (`prompt_template`).

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

### Schema reference

Each entry in `custom_metrics` must conform to one of two Agent Platform evaluation metric schemas. The presence of `custom_function` or `custom_function_file` selects `CodeExecutionMetric`; otherwise it's `LLMMetric`.

#### Code Execution Metric (`CodeExecutionMetric`)

Evaluates responses using custom Python code.

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Unique identifier for the metric. |
| `custom_function` | one of | Python source containing `def evaluate(instance):`. Receives an evaluation instance, returns a numeric score or a `{'score', 'explanation'}` dict. |
| `custom_function_file` | one of | Path to a `.py` file containing `def evaluate(instance):`, **resolved relative to the eval config file's directory** (absolute paths honored). Keeps the metric a real, lintable/testable module instead of an inline blob. Mutually exclusive with `custom_function`. Works with both `execution` modes (for `remote`, the file's source is uploaded). |
| `execution` | no | Where the function runs. `"local"` (default) — executed in the CLI process; no GCP project or region required; **runs with the CLI's privileges**, so only use trusted code. `"remote"` — uploaded and executed inside Vertex AI's `CodeExecutionMetric` sandbox; requires a configured GCP project + region. |

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

Grade with `agents-cli eval grade --config tests/eval/eval_config.yaml`.

#### LLM-as-a-Judge Metric (`LLMMetric`)

Evaluates responses using an LLM judge driven by a prompt template.

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Unique identifier for the metric. |
| `prompt_template` | yes | Prompt template used by the judge model. With agents-cli's file-based `EvaluationDataset` use `{prompt}`, `{response}`, and `{agent_data}` (the full trajectory). `{reference}` and `{context}` resolve only when the eval case has those fields populated. |
| `rubric_group_name` | no | Name of the rubric group containing rubrics this metric uses. **Must match a key under `rubric_groups` in your dataset's `EvalCase` entries** (see `dataset_schema.md`). When set, the judge prompt is augmented with the rubrics from the matching group; when omitted, the metric runs without per-case rubrics. |
| `judge_model` | no | Judge model (e.g., `gemini-flash-latest`). |
| `judge_model_sampling_count` | no | Number of judge samples to compute the score (1–32). |
| `judge_model_system_instruction` | no | System instruction for the judge model. |
| `judge_model_generation_config` | no | Generation config for the judge LLM (e.g., `temperature`). |
