# User Simulation for Dynamic Evaluation

> File paths below reference the scaffolded layout. Adjust for your project structure if not using /agents-cli-scaffold.

## When to Use

Use user simulation when fixed prompts are impractical — the agent may ask for information in different orders or respond in unexpected ways. Instead of hardcoding every user turn, define a **conversation scenario** and let an AI model generate realistic user responses dynamically.

---

## ConversationScenario Schema

Instead of `conversation` (static turns), use `conversation_scenario` in your eval case:

```json
{
  "eval_set_id": "dynamic_tests",
  "eval_cases": [
    {
      "eval_id": "booking_flow",
      "conversation_scenario": {
        "starting_prompt": "I need to book a flight to London",
        "conversation_plan": "Provide your name (John Smith) and email (john@example.com) when asked. Confirm the booking when the agent offers a flight."
      },
      "session_input": {
        "app_name": "my_app",
        "user_id": "test_user",
        "state": {}
      }
    }
  ]
}
```

- `starting_prompt` — fixed first user message
- `conversation_plan` — guidelines for how the simulated user should behave in subsequent turns

**Important:** An eval case must have exactly one of `conversation` or `conversation_scenario`, not both.

---

## Compatible Criteria

Only these criteria work with user simulation (no ground-truth available):

| Criterion | Compatible |
|-----------|:-:|
| `hallucinations_v1` | Yes |
| `safety_v1` | Yes |
| `rubric_based_final_response_quality_v1` | Yes |
| `rubric_based_tool_use_quality_v1` | Yes |
| `per_turn_user_simulator_quality_v1` | Yes |
| `tool_trajectory_avg_score` | No |
| `response_match_score` | No |
| `final_response_match_v2` | No |

Example config for user simulation evals:
```json
{
  "criteria": {
    "hallucinations_v1": {
      "threshold": 0.5,
      "evaluate_intermediate_nl_responses": true
    },
    "safety_v1": 0.8
  }
}
```

---

## User Simulator Configuration

Override default simulator behavior in `eval_config.json`:

```json
{
  "criteria": { ... },
  "user_simulator_config": {
    "model": "gemini-3-flash-preview",
    "model_configuration": {
      "thinking_config": {
        "include_thoughts": true,
        "thinking_budget": 10240
      }
    },
    "max_allowed_invocations": 20,
    "custom_instructions": "..."
  }
}
```

- `model` — model backing the user simulator
- `model_configuration` — GenerateContentConfig controlling model behavior
- `max_allowed_invocations` — max user-agent turns before forced termination (set higher than your longest expected conversation)
- `custom_instructions` — override default simulator instructions (must include `{stop_signal}`, `{conversation_plan}`, `{conversation_history}` placeholders)

---

## Creating Eval Sets with Scenarios

```bash
# Run evaluations
agents-cli eval --evalset <path_to_evalset.json>
```

**Scenarios file format:**
```json
{
  "scenarios": [
    {
      "starting_prompt": "What can you do for me?",
      "conversation_plan": "Ask the agent to search for flights to Paris. After results, ask to book the cheapest option."
    }
  ]
}
```

**Session input file:**
```json
{
  "app_name": "my_app",
  "user_id": "test_user"
}
```

---

## Evaluating Simulator Quality

Use `per_turn_user_simulator_quality_v1` to verify the simulator follows the conversation plan:

```json
{
  "criteria": {
    "per_turn_user_simulator_quality_v1": {
      "threshold": 1.0,
      "judge_model_options": {
        "judge_model": "gemini-3-flash-preview",
        "num_samples": 5
      },
      "stop_signal": "</finished>"
    }
  }
}
```

---

## Deep Dive

For the full user simulation guide with examples:
- WebFetch: `https://raw.githubusercontent.com/google/adk-docs/main/docs/evaluate/user-sim.md`
