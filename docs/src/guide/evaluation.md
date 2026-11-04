# Evaluation guide

Run structured evaluations to confirm your agent calls the right tools, produces quality responses, and handles edge cases. Grading uses the [GenAI Eval SDK in Gemini platform](https://docs.cloud.google.com/gemini-enterprise-agent-platform/optimize/evaluation/agent-evaluation). Evaluation works with any framework template: `eval run`, `eval grade`, `eval compare` and `eval analyze` are framework-agnostic, and a template only changes how traces are generated. See [First-party frameworks](extensions/first-party.md).

!!! note "Upgrading from an older agents-cli?"
    If your project still has `tests/eval/evalsets/*.evalset.json` files from a previous version, see [Migrate eval datasets](../reference/eval-dataset-migration.md) for the new format.

---

<a id="run-your-first-evaluation"></a>
<a id="the-eval-fix-loop"></a>
<a id="iterate-on-failures"></a>
<a id="how-do-i-run-my-first-evaluation"></a>
<a id="how-do-i-fix-failing-eval-cases"></a>
## How do I run and fix evals?

Your project includes a default dataset at `tests/eval/datasets/basic-dataset.json` and a metrics configuration at `tests/eval/eval_config.yaml`. In a Go project (`adk_go` template), the same files live under `eval/` instead of `tests/eval/`. The examples on this page use the Python layout.

1. Write one or two core eval cases for the most important behavior.
2. Run `agents-cli eval run`. The command executes the agent over the dataset, grades the traces, and writes `results_<timestamp>.json` and `.html` files to `artifacts/grade_results/`.
3. Read the `Failures:` list above the `RESULT:` line, then open the HTML report to see why those cases failed.
4. Fix the agent's instruction, tools, or logic.
5. Re-run `agents-cli eval run`, then check the change with `agents-cli eval compare <before>.json <after>.json`.
6. Once core cases pass, add edge cases and new scenarios.

Expect 5 to 10 or more cycles before your agent consistently passes. To walk one failing case through the playground and its trace, see [Debug a failing agent](debugging.md).

```bash
# Run for a custom dataset and different metrics
agents-cli eval run --dataset tests/eval/datasets/custom-dataset.json --metrics general_quality

# Evaluate a deployed agent instead of running one locally (ADK templates)
agents-cli eval run --url https://my-agent.run.app --app-name app
```

To keep traces in a custom location, or to re-grade existing traces without re-running the agent, split the run:

```bash
agents-cli eval generate --dataset tests/eval/datasets/custom-dataset.json --output custom_traces/
agents-cli eval grade --metrics general_quality --traces custom_traces/
```

---

<a id="writing-eval-cases-and-choosing-metrics"></a>
<a id="write-eval-cases-and-choose-metrics"></a>
<a id="how-do-i-write-eval-cases-and-choose-metrics"></a>
<a id="available-metrics-reference"></a>
<a id="common-metrics-at-a-glance"></a>
<a id="quick-reference-for-common-scenarios"></a>
## How do I choose metrics?

| Agent type | Metrics to start with |
|---|---|
| Tool-calling (single-turn) | `tool_use_quality`, `final_response_quality` |
| RAG | `hallucination`, `safety`, and `grounding` when the case has a `context` field |
| Conversational (single-turn) | `general_quality`, `instruction_following`, `text_quality` |
| Multi-turn, goal-oriented | `multi_turn_task_success`, `multi_turn_trajectory_quality`, `multi_turn_tool_use_quality` |

Two metrics need extra fields on the case: `final_response_match` needs a `reference`, and `final_response_reference_free` needs `rubric_groups`.

Only the three `multi_turn_*` metrics accept multi-turn traces. The other built-ins reject them. Run `agents-cli eval metric list` for every built-in metric with a description. For case schemas and the full metric reference, see the [Gemini platform evaluation documentation](https://docs.cloud.google.com/gemini-enterprise-agent-platform/optimize/evaluation/agent-evaluation).

---

<a id="evaluation-configuration-eval_configyaml"></a>
## How do I define custom metrics?

The `eval_config.yaml` file selects the metrics to run and defines custom ones. The scaffolded file runs `custom_response_quality`, a local LLM judge defined in `tests/eval/response_quality.py`. You can pass a different YAML or JSON file with `--config`.

- The `metrics_to_run` list selects the metrics for a run. A name resolves to a `custom_metrics` entry when one matches, otherwise to the built-in metric of that name.
- The `custom_metrics` list defines metrics without running them. A metric defined here runs only when it's listed in `metrics_to_run` or passed with `--metrics`.

??? info "Example and schema rules"
    In the following example, only `response_under_500_chars` runs. The `response_quality_rubric` metric is defined but not selected.

    ```yaml
    metrics_to_run:
      - response_under_500_chars

    custom_metrics:
      - name: response_under_500_chars
        custom_function: |
          def evaluate(instance: dict) -> dict:
              response = instance.get("response") or {}
              text = "".join(
                  p.get("text", "") for p in (response.get("parts") or []) if p.get("text")
              )
              passed = len(text) <= 500
              return {
                  "score": 1.0 if passed else 0.0,
                  "explanation": f"Final response is {len(text)} chars (limit 500).",
              }
      - name: response_quality_rubric
        prompt_template: |
          Rate the agent's response 1-5 for helpfulness and accuracy.
          Prompt: {prompt}
          Final response: {response}
          Full trace (for tool-call and reasoning context): {agent_data}
          Return JSON: {"score": <1|2|3|4|5>, "explanation": "<reason>"}
        judge_model: gemini-3.8-flash
        judge_model_sampling_count: 3
    ```

    Each custom metric must conform to either the **Code Execution Metric** or **LLM-as-a-Judge Metric** (`LLMMetric`) schema:

    - **Code Execution Metric**: Runs custom Python code for evaluation, whatever language your agent uses. Must have a `name` and either a `custom_function` (inline source containing a `def evaluate(instance):` signature) or a `custom_function_file` (path to a `.py` file with that function, resolved relative to the config file). By default, the function executes locally in the CLI process. Local execution needs no GCP project or region, but your code runs with the CLI's privileges. Add `execution: remote` to run it server-side in the sandboxed `CodeExecutionMetric` in Gemini platform instead, which requires a configured GCP project and region.
    - **LLM-as-a-Judge Metric**: Grades responses with an LLM judge. Must have a `name` and a `prompt_template`. Optional fields include `judge_model` (for example, `gemini-3.8-flash`) and `judge_model_sampling_count` (between `1` and `32`).

    An entry with neither `custom_function` nor `prompt_template`, whose `name` is a built-in metric, passes parameters to that built-in instead, for example `metric_spec_parameters.rubric_group_key` to pick one of the case's `rubric_groups`.

---

<a id="gating-on-scores"></a>
## How do I gate CI on eval scores?

Set the minimum mean score for each metric, in that metric's own scale, under `thresholds`. The `eval run` and `eval grade` commands end with a verdict line such as `RESULT: FAIL (response_quality_rubric 3.20 < 4.00)` and exit 1 when a metric misses its threshold, so CI can gate on the exit code. A run also fails when any case errored, with or without thresholds.

```yaml
thresholds:
  response_under_500_chars: 1.0
  response_quality_rubric: 4
```

Override one threshold for a run with `--fail-under response_quality_rubric=4`. Repeat the flag for more metrics. The flag always names a metric because metrics score on different scales. When a threshold is set and the eval service reports no `pass_rate` (local custom metrics), `pass_rate` in the results is the share of cases at or above the threshold.

---

<a id="beyond-eval-run"></a>
<a id="extend-your-eval-setup"></a>
<a id="how-do-i-extend-my-eval-setup"></a>
<a id="agents-cli-eval-dataset-synthesize"></a>
<a id="synthesize-a-dataset"></a>
<a id="how-do-i-synthesize-an-eval-dataset"></a>
<a id="agents-cli-eval-compare"></a>
<a id="compare-two-runs"></a>
<a id="how-do-i-compare-two-eval-runs"></a>
<a id="agents-cli-eval-analyze"></a>
<a id="cluster-failure-modes"></a>
<a id="how-do-i-cluster-eval-failure-modes"></a>
<a id="agents-cli-eval-metric-list"></a>
<a id="list-built-in-metrics"></a>
<a id="how-do-i-list-built-in-metrics"></a>
<a id="agents-cli-eval-optimize"></a>
<a id="optimize-prompts"></a>
<a id="how-do-i-optimize-prompts-from-eval-results"></a>
<a id="run-evaluations-in-the-cloud"></a>
<a id="how-do-i-run-evaluations-in-the-cloud"></a>
## What other eval commands are there?

The `eval run` command covers the inner loop. The CLI marks `eval dataset`, `eval analyze`, `eval optimize`, `eval submit`, and `eval results` as experimental.

| Command | What it does | Notes |
|---|---|---|
| `eval dataset synthesize --count 10` | Inspects your local ADK agent and generates multi-turn scenarios, each with a starting message, a conversation plan, and the full trace from an LLM-backed user simulator. | ADK Python only (LangChain and Claude templates refuse it). Needs no input file. Steer with `--instruction` and `--environment-context`. Writes to `artifacts/traces/` or the `-o` path. Skip `eval generate` and run `eval grade`, with a `multi_turn_*` metric. |
| `eval compare a.json b.json` | Compares two `results_*.json` files side by side. | Use it for before and after runs in the fix loop. |
| `eval analyze --eval-result <results.json>` | Clusters failure modes into themes. | Experimental. Most useful with 10 or more failing cases. `--metric` accepts `multi_turn_task_success` or `multi_turn_tool_use_quality`. |
| `eval metric list` | Prints every built-in metric with a description. | |
| `eval optimize --dataset <file> --target-metric <metric>` | Tunes agent prompts with GEPA prompt optimization. You can put both options in `tests/eval/optimization_config.json`. | Experimental. ADK Python only. Many LLM calls, a few minutes to several hours a run. Use it after manual fixes stop helping. |
| `eval submit --dataset <file> --dest gs://<bucket>`, then `eval results --run-id <id>` | Hands the dataset and metrics to the Eval Service in Gemini platform, then downloads the scores. | Experimental. For large datasets or CI. Add `--resource-name projects/<p>/locations/<l>/reasoningEngines/<id>` to run inference server-side too. |

For eval case schemas, metrics, and user simulation, see the [Gemini platform evaluation documentation](https://docs.cloud.google.com/gemini-enterprise-agent-platform/optimize/evaluation/agent-evaluation).

To keep eval spend down, see [Control cost](cost.md#how-do-i-keep-eval-spend-down).
