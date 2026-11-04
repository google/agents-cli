# Debug a failing agent

To debug a failing agent, reproduce the failure with one prompt, save it as an eval case, grade it to see which metric fails and why, read the trace to find the step that went wrong, then rerun and compare against the previous results. Agents CLI has a command for each step.

---

## How do I reproduce the failure?

Send the failing prompt to the agent with `agents-cli run`. Add `-v` (ADK and Claude templates) to print the full JSON event payloads, which show tool calls and intermediate steps:

```bash
agents-cli run "What's the weather like in Paris?" -v
```

For a failure that needs several turns, keep the local server alive with `--start-server` (ADK templates) and pass the same `--session-id` (ADK and Claude templates) on each run. A local session lives in the server's memory. Stop the server with `--stop-server` when you finish.

To reproduce against a deployed agent, pass `--url` with `--mode`. The `--mode a2a` option works for every template. The `adk` and `adk_live` modes need an ADK-style server:

```bash
agents-cli run "What's the weather like in Paris?" --url https://my-agent.run.app --mode a2a --app-name app
```

In a LangChain project, prefix the command with `AGENTS_CLI_DISABLE_OVERRIDES=1`. See [First-party frameworks](extensions/first-party.md).

To try prompts interactively, run `agents-cli playground`. In ADK templates it opens the ADK web UI at `localhost:8080`. The LangChain template runs the LangGraph dev server, and the Claude template has no playground.

---

## How do I turn the failure into an eval case?

Add the prompt to your dataset, `tests/eval/datasets/basic-dataset.json` (`eval/datasets/` in a Go project):

```json
{
  "eval_case_id": "weather_paris",
  "prompt": {
    "role": "user",
    "parts": [{"text": "What's the weather like in Paris?"}]
  }
}
```

A case that needs earlier turns carries them in `agent_data` and ends with a user message. See [Evaluation guide](evaluation.md#writing-eval-cases-and-choosing-metrics) for the schema and metrics, and add `reference` when you grade with `final_response_match`.

Run the dataset:

```bash
agents-cli eval run
```

---

## How do I see which cases fail and why?

The `eval run` command prints scores for each case, lists the failing metrics and cases under `Failures:` above its final `RESULT:` line, and writes `results_<timestamp>.json` and `.html` files to `artifacts/grade_results/`. Open the `.html` file for per-case scores and the judge's rationale for each metric.

The failing metric points to the fix:

- `multi_turn_tool_use_quality` is low: fix tool descriptions, parameter docstrings, or the agent's instructions for choosing tools.
- `hallucination` is low: tighten the instructions to stay grounded in tool output, and check that the tool returned the data the agent claimed.
- `multi_turn_task_success` is low: look for missing tool calls, premature termination, or the wrong tool.

When 10 or more cases fail, cluster them into failure modes instead of reading each one:

```bash
agents-cli eval analyze --eval-result artifacts/grade_results/results_<timestamp>.json
```

The `--metric` flag accepts `multi_turn_task_success` and `multi_turn_tool_use_quality`. The CLI marks this command as experimental. To grade existing traces again without running the agent, use `agents-cli eval grade --traces artifacts/traces/`. See [Iterate on failures](evaluation.md#the-eval-fix-loop) for the full loop.

---

## How do I see what the agent did step by step?

In the playground of an ADK template, the ADK web UI has a **Trace** tab. It groups traces by user message and shows the event, the model request, the model response, and a graph of the tool calls. See [Debugging with the Trace View](https://adk.dev/evaluate/#debugging-with-the-trace-view) in the ADK docs.

For a deployed agent, Cloud Trace is on by default. Open **Trace > Trace explorer** in the Google Cloud console. ADK spans nest as `invoke_agent`, `call_llm`, and `execute_tool` (other frameworks emit their own span names), so you can check whether the agent called the tool you expected, how long it took, and where an error occurred. Spans carry no prompt or response text unless you turn on content capture. See [Cloud Trace](observability/cloud-trace.md#how-do-i-view-traces).

To send traces from a local run to Cloud Trace, add `--otel-to-cloud` (ADK templates):

```bash
agents-cli playground --otel-to-cloud
```

---

## How do I confirm the fix and catch regressions?

Change one thing, then rerun the whole dataset, not only the case you fixed. Compare the new results file with the previous one:

```bash
agents-cli eval run
agents-cli eval compare artifacts/grade_results/results_<before>.json artifacts/grade_results/results_<after>.json
```

The `eval compare` command prints the keys that changed between the two files, with a delta for numeric values. Check that the target metric improved and no other case dropped.

The `eval run` command ends with a `RESULT:` line and exits 1 when a metric misses its threshold or a case errors. Set `thresholds` in the eval config so the exit code matches your bar ([details](evaluation.md#gating-on-scores)). The scaffolded CI/CD pipeline runs unit, integration, and load tests but doesn't run `eval run`, so run it before you open a pull request. See [CI/CD and production](cicd.md).

When a production failure slips through, add it to your dataset as a new case, or ask your coding agent to.

---

## How does my coding agent do this for me?

Give your coding agent the failing prompt and ask for the whole loop:

```
My agent gives a wrong answer for "What's the weather like in Paris?".
Reproduce it, find the cause in the trace, fix it, add an eval case,
and show me before and after scores for every case.
```

The `google-agents-cli-workflow` skill gives it a debugging sequence: reproduce, localize with `agents-cli run -v`, fix one thing, verify, and add an eval case. The `google-agents-cli-eval` skill covers grading and comparing, and `google-agents-cli-observability` covers traces. See [The lifecycle](lifecycle.md) and [Skills](../reference/skills.md).
