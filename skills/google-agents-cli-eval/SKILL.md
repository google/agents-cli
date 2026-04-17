---
name: google-agents-cli-eval
description: >
  This skill should be used when the user wants to "run an evaluation",
  "evaluate my ADK agent", "write an evalset", "debug eval scores",
  "compare eval results", or needs guidance on ADK (Agent Development Kit)
  evaluation methodology and the eval-fix loop.
  Covers eval metrics, evalset schema, LLM-as-judge, tool trajectory scoring,
  and common failure causes.
  Part of the Google ADK (Agent Development Kit) skills suite.
  Do NOT use for API code patterns (use google-agents-cli-adk-code), deployment
  (use google-agents-cli-deploy), or project scaffolding (use google-agents-cli-scaffold).
metadata:
  author: Google
  license: Apache-2.0
  version: 0.0.5
  requires:
    bins:
      - agents-cli
    install: "uv tool install google-agents-cli"
---

# ADK Evaluation Guide

> **Requires:** `agents-cli` (`uv tool install google-agents-cli`) — [install uv](https://docs.astral.sh/uv/getting-started/installation/index.md) first if needed.

> **Scaffolded project?** If you used `/google-agents-cli-scaffold`, you already have `agents-cli eval run`, `tests/eval/evalsets/`, and `tests/eval/eval_config.json`. Start with `agents-cli eval run` and iterate from there.

## Reference Files

| File | Contents |
|------|----------|
| `references/criteria-guide.md` | Complete metrics reference — all 8 criteria, match types, custom metrics, judge model config |
| `references/user-simulation.md` | Dynamic conversation testing — ConversationScenario, user simulator config, compatible metrics |
| `references/builtin-tools-eval.md` | google_search and model-internal tools — trajectory behavior, metric compatibility |
| `references/multimodal-eval.md` | Multimodal inputs — evalset schema, built-in metric limitations, custom evaluator pattern |

---

## The Eval-Fix Loop

Evaluation is iterative. When a score is below threshold, diagnose the cause, fix it, rerun — don't just report the failure.

### How to iterate

1. **Start small**: Begin with 1-2 eval cases, not the full suite
2. **Run eval**: `agents-cli eval run`
3. **Read the scores** — identify what failed and why
4. **Fix the code** — adjust prompts, tool logic, instructions, or the evalset
5. **Rerun eval** — verify the fix worked
6. **Repeat steps 3-5** until the case passes
7. **Only then** add more eval cases and expand coverage

**Expect 5-10+ iterations.** This is normal — each iteration makes the agent better.

**Task tracking:** When doing 5+ eval-fix iterations, use a task list to track which cases you've fixed, which are still failing, and what you've tried. This prevents re-attempting the same fix or losing track of regression across iterations.

### Shortcuts That Waste Time

Recognize these rationalizations and push back — they always cost more time than they save:

| Shortcut | Why it fails |
|----------|-------------|
| "I'll tune the eval thresholds down to make it pass" | Lowering thresholds hides real failures. If the agent can't meet the bar, fix the agent — don't move the bar. |
| "This eval case is flaky, I'll skip it" | Flaky evals reveal non-determinism in your agent. Fix with `temperature=0`, rubric-based metrics, or more specific instructions — don't delete the signal. |
| "I just need to fix the evalset, not the agent" | If you're always adjusting expected outputs, your agent has a behavior problem. Fix the instructions or tool logic first. |

### What to fix when scores fail

| Failure | What to change |
|---------|---------------|
| `tool_trajectory_avg_score` low | Fix agent instructions (tool ordering), update evalset `tool_uses`, or switch to `IN_ORDER`/`ANY_ORDER` match type |
| `response_match_score` low | Adjust agent instruction wording, or relax the expected response |
| `final_response_match_v2` low | Refine agent instructions, or adjust expected response — this is semantic, not lexical |
| `rubric_based` score low | Refine agent instructions to address the specific rubric that failed |
| `hallucinations_v1` low | Tighten agent instructions to stay grounded in tool output |
| Agent calls wrong tools | Fix tool descriptions, agent instructions, or tool_config |
| Agent calls extra tools | Use `IN_ORDER`/`ANY_ORDER` match type, add strict stop instructions, or switch to `rubric_based_tool_use_quality_v1` |

---

## Choosing the Right Criteria

| Goal | Recommended Metric |
|------|--------------------|
| Regression testing / CI/CD (fast, deterministic) | `tool_trajectory_avg_score` + `response_match_score` |
| Semantic response correctness (flexible phrasing OK) | `final_response_match_v2` |
| Response quality without reference answer | `rubric_based_final_response_quality_v1` |
| Validate tool usage reasoning | `rubric_based_tool_use_quality_v1` |
| Detect hallucinated claims | `hallucinations_v1` |
| Safety compliance | `safety_v1` |
| Dynamic multi-turn conversations | User simulation + `hallucinations_v1` / `safety_v1` (see `references/user-simulation.md`) |
| Multimodal input (image, audio, file) | `tool_trajectory_avg_score` + custom metric for response quality (see `references/multimodal-eval.md`) |

For the complete metrics reference with config examples, match types, and custom metrics, see `references/criteria-guide.md`.

---

## Running Evaluations

```bash
# Scaffolded projects — agents-cli:
agents-cli eval run --evalset tests/eval/evalsets/my_evalset.json

# With explicit config file:
agents-cli eval run --evalset tests/eval/evalsets/my_evalset.json --config tests/eval/eval_config.json

# Run all evalsets in tests/eval/evalsets/:
agents-cli eval run --all
```

**`agents-cli eval run` options:** `--evalset PATH`, `--config PATH`, `--all`

**Compare two result files:**
```bash
agents-cli eval compare baseline.json candidate.json
```

---

## Configuration Schema (`eval_config.json`)

Both camelCase and snake_case field names are accepted (Pydantic aliases). The examples below use snake_case, matching the official ADK docs.

### Full example

```json
{
  "criteria": {
    "tool_trajectory_avg_score": {
      "threshold": 1.0,
      "match_type": "IN_ORDER"
    },
    "final_response_match_v2": {
      "threshold": 0.8,
      "judge_model_options": {
        "judge_model": "gemini-flash-latest",
        "num_samples": 5
      }
    },
    "rubric_based_final_response_quality_v1": {
      "threshold": 0.8,
      "rubrics": [
        {
          "rubric_id": "professionalism",
          "rubric_content": { "text_property": "The response must be professional and helpful." }
        },
        {
          "rubric_id": "safety",
          "rubric_content": { "text_property": "The agent must NEVER book without asking for confirmation." }
        }
      ]
    }
  }
}
```

Simple threshold shorthand is also valid: `"response_match_score": 0.8`

For custom metrics, `judge_model_options` details, and `user_simulator_config`, see `references/criteria-guide.md`.

---

## EvalSet Schema (`evalset.json`)

```json
{
  "eval_set_id": "my_eval_set",
  "name": "My Eval Set",
  "description": "Tests core capabilities",
  "eval_cases": [
    {
      "eval_id": "search_test",
      "conversation": [
        {
          "invocation_id": "inv_1",
          "user_content": { "parts": [{ "text": "Find a flight to NYC" }] },
          "final_response": {
            "role": "model",
            "parts": [{ "text": "I found a flight for $500. Want to book?" }]
          },
          "intermediate_data": {
            "tool_uses": [
              { "name": "search_flights", "args": { "destination": "NYC" } }
            ],
            "intermediate_responses": [
              ["sub_agent_name", [{ "text": "Found 3 flights to NYC." }]]
            ]
          }
        }
      ],
      "session_input": { "app_name": "my_app", "user_id": "user_1", "state": {} }
    }
  ]
}
```

**Key fields:**
- `intermediate_data.tool_uses` — expected tool call trajectory (chronological order)
- `intermediate_data.intermediate_responses` — expected sub-agent responses (for multi-agent systems)
- `session_input.state` — initial session state (overrides Python-level initialization)
- `conversation_scenario` — alternative to `conversation` for user simulation (see `references/user-simulation.md`)

---

## Common Gotchas

### The Proactivity Trajectory Gap

LLMs often perform extra actions not asked for (e.g., `google_search` after `save_preferences`). This causes `tool_trajectory_avg_score` failures with `EXACT` match. Solutions:

1. **Use `IN_ORDER` or `ANY_ORDER` match type** — tolerates extra tool calls between expected ones
2. Include ALL tools the agent might call in your expected trajectory
3. Use `rubric_based_tool_use_quality_v1` instead of trajectory matching
4. Add strict stop instructions: "Stop after calling save_preferences. Do NOT search."

### Multi-turn conversations require tool_uses for ALL turns

The `tool_trajectory_avg_score` evaluates each invocation. If you don't specify expected tool calls for intermediate turns, the evaluation will fail even if the agent called the right tools.

```json
{
  "conversation": [
    {
      "invocation_id": "inv_1",
      "user_content": { "parts": [{"text": "Find me a flight from NYC to London"}] },
      "intermediate_data": {
        "tool_uses": [
          { "name": "search_flights", "args": {"origin": "NYC", "destination": "LON"} }
        ]
      }
    },
    {
      "invocation_id": "inv_2",
      "user_content": { "parts": [{"text": "Book the first option"}] },
      "final_response": { "role": "model", "parts": [{"text": "Booking confirmed!"}] },
      "intermediate_data": {
        "tool_uses": [
          { "name": "book_flight", "args": {"flight_id": "1"} }
        ]
      }
    }
  ]
}
```

### App name must match directory name

The `App` object's `name` parameter MUST match the directory containing your agent:

```python
# CORRECT - matches the "app" directory
app = App(root_agent=root_agent, name="app")

# WRONG - causes "Session not found" errors
app = App(root_agent=root_agent, name="flight_booking_assistant")
```

### The `before_agent_callback` Pattern (State Initialization)

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

### Eval-State Overrides (Type Mismatch Danger)

Be careful with `session_input.state` in your evalset. It overrides Python-level initialization:

WRONG — initializes feedback_history as a string, breaks `.append()`:
```json
"state": { "feedback_history": "" }
```

CORRECT — matches the Python type (list):
```json
"state": { "feedback_history": [] }
```

### Model thinking mode may bypass tools

Models with "thinking" enabled may skip tool calls. Use `tool_config` with `mode="ANY"` to force tool usage, or switch to a non-thinking model for predictable tool calling.

---

## Common Eval Failure Causes

| Symptom | Cause | Fix |
|---------|-------|-----|
| Missing `tool_uses` in intermediate turns | Trajectory expects match per invocation | Add expected tool calls to all turns |
| Agent mentions data not in tool output | Hallucination | Tighten agent instructions; add `hallucinations_v1` metric |
| "Session not found" error | App name mismatch | Ensure App `name` matches directory name |
| Score fluctuates between runs | Non-deterministic model | Set `temperature=0` or use rubric-based eval |
| `tool_trajectory_avg_score` always 0 | Agent uses `google_search` (model-internal) | Remove trajectory metric; see `references/builtin-tools-eval.md` |
| Trajectory fails but tools are correct | Extra tools called | Switch to `IN_ORDER`/`ANY_ORDER` match type |
| LLM judge ignores image/audio in eval | `get_text_from_content()` skips non-text parts | Use custom metric with vision-capable judge (see `references/multimodal-eval.md`) |

---

## Deep Dive: ADK Docs

For the official evaluation documentation, fetch these pages:

- **Evaluation overview**: `https://adk.dev/evaluate/index.md`
- **Criteria reference**: `https://adk.dev/evaluate/criteria/index.md`
- **User simulation**: `https://adk.dev/evaluate/user-sim/index.md`

---

## Debugging Example

User says: "tool_trajectory_avg_score is 0, what's wrong?"

1. Check if agent uses `google_search` — if so, see `references/builtin-tools-eval.md`
2. Check if using `EXACT` match and agent calls extra tools — try `IN_ORDER`
3. Compare expected `tool_uses` in evalset with actual agent behavior
4. Fix mismatch (update evalset or agent instructions)

---

## Proving Your Work

Don't assert that eval passes — show the evidence. Concrete output prevents false confidence and catches issues early.

- **After running eval:** Paste the scores table output so the user can see exactly what passed and failed.
- **After fixing a failure:** Show before/after scores for the specific case you fixed, and confirm no other cases regressed.
- **Before declaring "eval passes":** Confirm ALL cases pass, not just the one you were working on. Run `agents-cli eval run` (or `agents-cli eval run --all`) one final time.
- **Before moving to deploy:** Show the final `agents-cli eval run` output with all cases above threshold. This is the gate — no exceptions.

---

## Related Skills

- `/google-agents-cli-workflow` — Development workflow and the spec-driven build-evaluate-deploy lifecycle
- `/google-agents-cli-adk-code` — ADK Python API quick reference for writing agent code
- `/google-agents-cli-scaffold` — Project creation and enhancement with `agents-cli scaffold create` / `scaffold enhance`
- `/google-agents-cli-deploy` — Deployment targets, CI/CD pipelines, and production workflows
- `/google-agents-cli-observability` — Cloud Trace, logging, and monitoring for debugging agent behavior
