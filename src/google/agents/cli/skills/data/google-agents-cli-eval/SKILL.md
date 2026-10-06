---
name: google-agents-cli-eval
description: >
  This skill should be used when the user wants to "run an evaluation",
  "evaluate my agent", "evaluate my ADK agent", "write an eval dataset",
  "analyze eval failures", "compare eval results", "optimize agent", or needs
  guidance on the Agent Platform eval methodology and the Quality Flywheel.
  Covers eval metrics, dataset schema, LLM-as-judge scoring, and common failure causes.
  Applies to any agents-cli project, whatever framework the agent is written in.
  Do NOT use for agent API code patterns (ADK: use google-agents-cli-adk-code), deployment
  (use google-agents-cli-deploy), or project scaffolding (use google-agents-cli-scaffold).
metadata:
  author: Google
  license: Apache-2.0
  version: 1.9.0
  requires:
    bins:
      - agents-cli
    install: "uv tool install google-agents-cli"
---

# Agent Evaluation Guide

> **Requires:** `agents-cli` (`uv tool install google-agents-cli`) — [install uv](https://docs.astral.sh/uv/getting-started/installation/index.md) first if needed.

> **Scaffolded project?** If you used `/google-agents-cli-scaffold`, dataset and a custom metric are already scaffolded in `tests/eval/` (Python projects) or `eval/` (Go projects). For simplicity, this skill and its references use the Python directory layout; adjust accordingly if you've scaffolded a Go agent.
> You already have `agents-cli eval run` (chains `generate` + `grade`), `tests/eval/datasets/`, and `tests/eval/eval_config.yaml`. Start with executing `eval run` and iterate from there.

## Reference Files

| File | Contents |
|------|----------|
| `references/dataset_schema.md` | Canonical EvaluationDataset schema — all field types, JSON examples for single-turn / multi-turn / multi-agent, common mistakes |
| `references/metrics-guide.md` | Complete metrics reference — all built-in metrics, match types, custom metrics, judge model config |
| `references/user-simulation.md` | Dynamic conversation testing — `eval dataset synthesize` flags, what scenarios are, compatible metrics |
| `references/builtin-tools-eval.md` | google_search and model-internal tools — trajectory behavior, metric compatibility |
| `references/advanced-commands.md` | Opt-in commands: `eval analyze`, `eval optimize`, `eval submit` / `eval results` |
| `references/multimodal-eval.md` | Multimodal inputs — eval dataset schema, built-in metric limitations, custom evaluator pattern |
| `references/live-eval.md` | Live and voice agents — `--mode adk_live`, what gets graded, user-only turn authoring, the Live-model and region traps |

---

## The Quality Flywheel

Improving agent quality is iterative. The 4 stages below describe the loop. Each stage has a Default path (you, the coding agent, do the work directly) and an Opt-in CLI command that delegates to the Agent Platform Eval Service for better quality and scale.

### 1. Prepare Data

**Default:** Use or edit the scaffolded `tests/eval/datasets/basic-dataset.json` to define single-turn eval inputs. Start with 1–2 cases.

**Opt-in (ADK projects):** `agents-cli eval dataset synthesize`: user-simulate multi-turn datasets when you lack data; its output already includes traces, so Stage 2 collapses to `agents-cli eval grade` alone. See *Eval Commands* and `references/user-simulation.md`.

### 2. Run the Eval (always run)

**Default:** `agents-cli eval run` runs the agent over the dataset and grades the traces, writing `results_<ts>.{json,html}` to `artifacts/grade_results/`.

**Decoupled form:** `eval generate` then `eval grade`, for a custom traces location, re-grading without re-running the agent, or traces from `synthesize` (`eval grade` alone).

### 3. Analyze Failures

**Default:** Open the latest `artifacts/grade_results/results_<ts>.html` (or `.json`) and identify failed metrics — see *What to fix when scores fail* below for the fix table.

**Opt-in:** `agents-cli eval analyze`, LLM-based failure clustering; prefer when you have 10+ failing cases and want categorized failure modes. See `references/advanced-commands.md`.

### 4. Optimize & Code Fix

**Default:** Edit the agent — adjust prompts, tool descriptions, instructions, or eval dataset based on the failure analysis. See *What to fix when scores fail* below for the failure → fix mapping.

**Opt-in (ADK projects):** `agents-cli eval optimize` runs ADK GEPA prompt optimization against a target metric (see `references/advanced-commands.md`). Suitable for prompt-only failures. The optimized prompt appears in the command output; capture it and apply it to the agent. For the full per-iteration trace, set `print_detailed_results: true` in your optimization config file.

> **Long-running and expensive.** GEPA optimization makes many LLM calls and can take a long time. Do not run it unless the user explicitly asks for prompt optimization. When you do run it, iterate as far as possible with manual fixes first, then run a **single** final `eval optimize` — never loop on this command.

### Running the loop

Iterate stages 2 → 3 → 4 → 2 (with `synthesize`, re-run Stage 1 each pass, then `eval grade`). After each fix, run `agents-cli eval compare <prev_results>.json <new_results>.json` to confirm the target metric improved without regressing others. Expect 5–10+ iterations per case before it passes, which is normal. Only after a case passes should you expand coverage with more eval cases.

When doing 5+ iterations, maintain a task list of which cases are fixed, which are still failing, and what fixes you've tried. Prevents re-attempting the same fix.

**Hold cases back.** Keep a slice of cases out of the loop and grade them only when you think you're done — otherwise you can't tell a fix that generalizes from one fitted to the cases you iterated against.

### Shortcuts That Waste Time

Recognize these rationalizations and push back — they always cost more time than they save:

| Shortcut | Why it fails |
|----------|-------------|
| "I'll lower the bar so it passes" | Lowering the bar hides real failures. If the agent can't meet the bar, fix the agent, don't move the bar. |
| "This eval case is flaky, I'll skip it" | Flaky evals reveal non-determinism in your agent. Fix with `temperature=0`, rubric-based metrics, or more specific instructions — don't delete the signal. |
| "I just need to fix the eval dataset, not the agent" | If you're always adjusting expected outputs, your agent has a behavior problem. Fix the instructions or tool logic first. |
| "I'll iterate until every case I have passes" | Nothing is left to detect overfitting to your own cases. See *Hold cases back* above. |

## Choosing the Right Metrics

Pick built-in metrics by what you want to measure. Only `multi_turn_task_success`, `multi_turn_trajectory_quality`, and `multi_turn_tool_use_quality` accept multi-turn traces; every other built-in 400s on one. When no built-in fits, write a custom metric (see *Evaluation Configuration Schema* below).

| Goal | Recommended built-in metrics |
|------|------------------------------|
| **Did the agent achieve the user's goal?** (catch-all for multi-turn agents) | `multi_turn_task_success` |
| **Was the agent's reasoning path logical and efficient?** | `multi_turn_trajectory_quality` |
| **Quality of tool / function calling across turns** | `multi_turn_tool_use_quality` |
| **Final response quality** (no ground-truth reference needed) | `final_response_quality` |
| **Factual grounding** (catch hallucinated claims, e.g., RAG agents) | `hallucination`, or `grounding` when the case carries a `context` field |
| **Safety policy compliance** | `safety` |
| **Match against a golden answer** | `final_response_match` (needs `reference` on the case) |
| **Different pass/fail criteria per case** | Put them on the case as `rubric_groups` and grade with a managed rubric metric. See `references/dataset_schema.md` (*Per-Case Rubrics*). |
| **Domain-specific check no built-in covers** | Write a custom `LLMMetric` (LLM-judge) or `CodeExecutionMetric` (deterministic Python). See *Evaluation Configuration Schema* below. |

Run `agents-cli eval metric list` to see all available built-ins. For full metric definitions and rubric details, see the [Agent Platform metric docs](https://cloud.google.com/gemini-enterprise-agent-platform/optimize/evaluation/manage-metrics) and `references/metrics-guide.md`.

---

## What to fix when scores fail

After `agents-cli eval run` completes, inspect the latest `artifacts/grade_results/results_<timestamp>.json` (or open the `.html` file) for per-case scores and judge rationales, the input to every fix decision below.

| Failure | What to change |
|---------|---------------|
| `multi_turn_task_success` low | The agent isn't completing the user's goal — fix orchestration, missing tool calls, premature termination, or wrong tool selection |
| `multi_turn_trajectory_quality` low | The agent reaches the goal inefficiently or takes wrong steps — refine planning prompts, tighten instruction order, or remove redundant tool calls |
| `multi_turn_tool_use_quality` low | Fix tool descriptions, parameter docstrings, or agent instructions for tool selection |
| `final_response_quality` low | Read the auto-generated rubric verdicts; refine agent instructions to address the worst-scoring criterion (often clarity, completeness, or instruction-following) |
| `hallucination` low | Tighten agent instructions to stay grounded in tool output; verify the tool actually returned the data the agent claimed |
| `safety` low | Add safety guardrails to instructions; review the violating content category in the rubric verdict |
| Agent calls wrong tools | Fix tool descriptions, agent instructions, or the model's tool-choice config (**ADK:** `tool_config`) |
| Agent calls extra tools | Add strict stop instructions, or switch to `multi_turn_tool_use_quality` |

After applying a fix, rerun `agents-cli eval run` and use `agents-cli eval compare <prev_results>.json <new_results>.json` to confirm the fix improved the target metric without regressing others.

---

## Eval Commands

`agents-cli eval <subcommand> --help` is the authoritative flag list; the examples below are the common invocations.

### `eval run` (default)

Runs the agent over the dataset and grades the traces in one command.

```bash
# Basic: dataset from tests/eval/datasets/, results to artifacts/grade_results/,
# metrics from tests/eval/eval_config.yaml
agents-cli eval run

# Advanced: pick the dataset, metrics, and output dir
agents-cli eval run --dataset tests/eval/datasets/custom.json --metrics final_response_quality,safety --output ./out/
```

### `eval generate`

Runs an agent over an evaluation dataset and writes traces to disk.

By default, runs the agent locally and records a trace per evaluation case. You can generate traces from an already-running agent by passing its HTTP endpoint and app name to `--url` and `--app-name`.

> **ADK projects.** The built-in generator serves the agent over HTTP and drives it over ADK's `/apps/...` and `/run_sse` routes — the same shape `--url` / `--app-name` expect. What it launches depends on the project's language: Python uses the project's `fast_api_app.py` if it exists, else `adk api_server`; Go runs `go run ...`. Extensions for other frameworks replace `eval generate` with their own generator, which may not serve HTTP at all; `--url` and `--app-name` are then unsupported.

```bash
# Basic — uses tests/eval/datasets/, writes to artifacts/traces/
agents-cli eval generate

# Advanced — custom dataset and output dir
agents-cli eval generate --dataset tests/eval/datasets/custom.json -o ./custom_traces/

# Against a deployed agent (or one you started manually)
agents-cli eval generate --url https://my-agent.run.app --app-name app

# Live agent — stream each case over ADK's /run_live WebSocket
agents-cli eval generate --mode adk_live
```

#### Evaluating Live agents

Live agents run over a WebSocket, not `/run_sse`: add `--mode adk_live` to `generate` or `run`. The dataset, the traces, and grading are unchanged, and the audio reply is transcribed so the transcript is what gets graded. Two things must already be true or the socket connects and *then* fails mid-session: the agent uses a Live model (the scaffold default is not one), and on Vertex its region is pinned on the model rather than left to `GOOGLE_CLOUD_LOCATION`. Both, plus dataset authoring rules: `references/live-eval.md`.

### `eval grade`

Scores traces (from `eval generate`, `eval dataset synthesize`, or hand-authored) against built-in or custom metrics. Writes timestamped `results_<YYYYMMDD_HHMMSS>.json` (consumed by `eval compare`) and `.html` (open in a browser) into the output dir, and prints a summary table to the console.

```bash
# Basic — defaults: traces from artifacts/traces/, results to artifacts/grade_results/,
# metrics from tests/eval/eval_config.yaml's metrics_to_run
agents-cli eval grade

# Advanced 1 — grade traces from a non-default location (the canonical
# pairing for `eval generate --output custom_traces/`)
agents-cli eval grade --traces custom_traces/

# Advanced 2: load metrics to run from a config file (YAML or JSON) on a specified trace file.
agents-cli eval grade --traces ./artifacts/traces/trace_1.json --config tests/eval/eval_config.yaml

# Advanced 3: dispatch rate, 15 metric computations per second by default. Lower it when the
# judge model or the eval service rate-limits you, raise it when they have headroom.
agents-cli eval grade --qps 5
```

See *Evaluation Configuration Schema* below for the config file format.

### `eval compare`

Diffs two `results_*.json` files from an eval run. Run it after a fix to confirm the target metric improved without regressing others.

```bash
agents-cli eval compare baseline.json candidate.json
```

### `eval dataset synthesize`

> **ADK projects.** It loads and runs the agent through ADK, so it is unavailable on other frameworks.

Generates user scenarios from your agent's tools and instructions, plays each against an LLM-backed user simulator, and writes graded-ready traces to `artifacts/traces/` (feed straight to `eval grade`, skip `eval generate`). Invocations, flags, and compatible metrics: `references/user-simulation.md`.

### Advanced commands

`eval analyze` (cluster failure modes), `eval optimize` (GEPA prompt tuning), and `eval submit` / `eval results` (managed cloud-side runs for CI or large datasets) are documented in `references/advanced-commands.md`.

---

## Evaluation Dataset Format

An `EvaluationDataset` is a JSON file with an `eval_cases` array. Cases come in two shapes depending on how they're used:

- **Inference input** (what you give to `eval generate`) — a single user prompt, or a multi-turn set of **user turns** (for live) / a continuation ending in a user turn (for SSE). The agent runs and produces traces. Don't pre-author agent replies for live inference.
- **Grading input** (what you give to `eval grade`) — a complete trace including the agent's responses and tool calls. Normally produced by `eval generate` or `eval dataset synthesize`; you don't write these by hand.

See `references/dataset_schema.md` for the full canonical schema, all field types, and common mistakes.

### Inference input format

Two shapes are supported.

**(a) Simple single-turn prompt** — what the scaffolded `tests/eval/datasets/basic-dataset.json` uses. The agent runs from scratch.

```json
{
  "eval_cases": [
    {
      "eval_case_id": "greeting",
      "prompt": {
        "role": "user",
        "parts": [{"text": "Hello, what can you help me with?"}]
      }
    }
  ]
}
```

**(b) Multi-turn via `agent_data`** — the shape depends on the transport:

- **Live (`--mode adk_live`):** author **user-only** turns; the agent generates every reply over one live session (authored agent turns are ignored, with a warning).
- **SSE:** continuation form — prior turns are seeded as history and only the trailing user turn is answered.

See `references/dataset_schema.md` (*Multi-Turn / Multi-Agent Dataset*) for the JSON shapes.

### Grading input format (traces)

A complete trace — agent responses plus `function_call` / `function_response` parts — normally produced by `eval generate` / `eval dataset synthesize` (you don't write these by hand). Authors are `"user"`, an agent ID from the `agents` map, or `"tool"`. See `references/dataset_schema.md` for the trace shape, multi-agent examples, and the full type reference.

---

## Evaluation Configuration Schema

`agents-cli eval run --config <path>` (and `eval grade --config <path>`) accepts a single configuration file in either **YAML** (`.yaml` / `.yml`) or **JSON** (`.json`). The file declares two parts:

- `metrics_to_run`: the **selection list** of metric names to execute on this run. A name resolves to a `custom_metrics` entry when one matches, otherwise to the built-in metric of that name.
- `custom_metrics` — a **definition pool** of custom metrics available to this project. Defining a metric here does **not** run it; it must also appear in `metrics_to_run` (or be passed via `--metrics name1,name2` on the CLI, which is equivalent to overriding `metrics_to_run` for that invocation).

**Minimal example (YAML preferred — human-readable, no JSON escaping for prompts and Python):**

```yaml
metrics_to_run:
  - multi_turn_task_success     # built-in
  - example_llm_metric          # selected from custom_metrics pool below
  - agent_turn_count            # selected from custom_metrics pool below

custom_metrics:
  - name: example_llm_metric
    prompt_template: |
      Rate the agent's response 1-5 for helpfulness and accuracy.
      Prompt: {prompt}
      Final response: {response}
      Full trace (for tool-call and reasoning context): {agent_data}
      Return JSON: {"score": <1|2|3|4|5>, "explanation": "<reason>"}

  - name: agent_turn_count
    custom_function: |
      def evaluate(instance):
          turns = (instance.get("agent_data") or {}).get("turns", [])
          return {'score': len(turns)}
```

JSON is also accepted (same field names, with `prompt_template` and `custom_function` as escaped strings) — but **always prefer YAML** for human-readable configs.

Dispatch by field: `custom_function` → Python metric; `prompt_template` → `LLMMetric` (LLM-as-judge); neither, on a built-in name → parameterizes that built-in (e.g. `metric_spec_parameters.rubric_group_key`). Field reference: `references/metrics-guide.md`.

**Agent trace field model.** For datasets produced by `agents-cli eval generate` (or `eval dataset synthesize`), each eval case exposes three standard fields to a metric:

- `{prompt}` — the user message (or first user turn).
- `{response}` — the agent's final text response, extracted from the last text-bearing event. In `custom_function` callbacks this is `instance['response']` with shape `{"role": "model", "parts": [{"text": "..."}]}`.
- `{agent_data}` — the full structured `turns`/`events` trace, useful when the judge needs to reason about tool calls or intermediate reasoning.

`reference`, `context`, and `rubric_groups` are yours to author on the case: `eval generate` carries them onto the trace but never invents them, so `{reference}` / `{context}` resolve only where you wrote them. `rubric_groups` is not a placeholder at all: managed rubric metrics read it off the case, and a `custom_function` sees `instance['rubric_groups']`. See `references/dataset_schema.md` (*Per-Case Rubrics*).

Code-based metrics default to **local in-process execution** (no GCP project or region required, but the `evaluate(instance)` function runs with the CLI's privileges). Set `execution: "remote"` on the metric to run it server-side in Vertex AI's `CodeExecutionMetric` sandbox instead — that path requires a configured GCP project + region.

---


## Common Gotchas

### Use Rubric-Based Tool Evaluation instead of Hardcoded Sequences

Evaluating agent tool usage using strict sequence matching is fragile because agents may call helper tools (like searches or geocoding) in different orders or perform extra proactive steps.

Instead, use **`multi_turn_tool_use_quality`** / **`multi_turn_trajectory_quality`**. These metrics automatically generate content-based and intent-based adaptive rubrics, assessing technical correctness and technical sequence logic semantically using an LLM judge rather than forcing a rigid match.

### App name must match directory name

> **ADK projects.**

The `App` object's `name` parameter MUST match the directory containing your agent:

```python
# CORRECT - matches the "app" directory
app = App(root_agent=root_agent, name="app")

# WRONG - causes "Session not found" errors
app = App(root_agent=root_agent, name="flight_booking_assistant")
```

### Vertex eval region

`eval run`, `eval grade`, and `eval submit` **default to the `global` endpoint**. They don't inherit the manifest `region` (the eval services support only a subset of regions), and `eval analyze` is `global`-only. Override these per run with `--region <REGION>` (e.g. data residency); the service rejects an unsupported one:

```
400 FAILED_PRECONDITION: Unsupported region for Vertex Evaluation Service: <region>
```

`eval generate` (without the `--url` flag) and `eval dataset synthesize` run your agent locally, so they honor the agent's own `.env` — notably `GOOGLE_CLOUD_LOCATION`, which selects the model endpoint **when the agent uses Vertex AI** (`GOOGLE_GENAI_USE_ENTERPRISE=true`); it's unused with a `GEMINI_API_KEY` (AI Studio). They take **no** `--region` and never override your `.env` with the manifest `region`; change the model region by editing `.env` — or, better for a single agent, pin it in code with `Gemini(model=…, client_kwargs={"location": …})`, which beats the env var and leaves it free for everything else. One caveat for `synthesize`: its scenario-generation step is a **server-side** eval call at `GOOGLE_CLOUD_LOCATION`, so keep that an eval-supported region (`global` by default) even though the agent itself could run elsewhere.

**No eval region fits your data-residency rules?** Fall back to **local custom metrics** — a `custom_metrics` entry with a `custom_function` (`execution: local`, the default) grades in-process with no GCP region required. You lose the managed built-in metrics, but your `custom_function` can still call an LLM judge in a compliant region itself — so LLM-as-judge grading stays available anywhere.

### The `before_agent_callback` Pattern (State Initialization)

> **ADK projects.**

Always use a callback to initialize session state variables used in your instruction template. This prevents `KeyError` crashes on the first turn:

```python
async def initialize_state(callback_context: CallbackContext) -> None:
    state = callback_context.state
    if "user_preferences" not in state:
        state["user_preferences"] = {}

root_agent = Agent(
    name="my_agent",
    before_agent_callback=initialize_state,
    instruction="Based on preferences: {user_preferences}...",
)
```

### Model thinking mode may bypass tools

Models with "thinking" enabled may skip tool calls. Force tool usage through the model's tool-choice config (**ADK:** `tool_config` with `mode="ANY"`), or switch to a non-thinking model for predictable tool calling.

---

## Common Eval Failure Causes

| Symptom | Cause | Fix |
|---------|-------|-----|
| Score fluctuates between runs | Non-deterministic model | Set `temperature=0` or use rubric-based eval with multiple samples |
| LLM judge ignores image/audio in eval | `get_text_from_content()` skips non-text parts | Use custom metric with vision-capable judge (see `references/multimodal-eval.md`) |

---

## Proving Your Work

Don't assert that eval passes — show the evidence. Concrete output prevents false confidence and catches issues early.

- **After running eval:** Paste the scores table output so the user can see exactly what passed and failed.
- **After fixing a failure:** Show before/after scores for the specific case you fixed, and confirm no other cases regressed.
- **Before deploy:** Rerun `agents-cli eval run` and show the scores for every case, not just the one you fixed. `eval run` exits 0 whatever the scores are, so the numbers you paste are the gate, not the exit code.

---

## Related Skills

- `/google-agents-cli-workflow` — Development workflow and the spec-driven build-evaluate-deploy lifecycle
- `/google-agents-cli-adk-code` — ADK API quick reference for writing agent code (ADK projects only)
- `/google-agents-cli-scaffold` — Project creation and enhancement with `agents-cli scaffold create` / `scaffold enhance`
- `/google-agents-cli-deploy` — Deployment targets, CI/CD pipelines, and production workflows
- `/google-agents-cli-observability` — Cloud Trace, logging, and monitoring for debugging agent behavior
