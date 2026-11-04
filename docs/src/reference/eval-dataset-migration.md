# Migrate eval datasets

If you started using Agents CLI before the eval commands were rebuilt around the [GenAI Eval SDK in Gemini platform](https://docs.cloud.google.com/gemini-enterprise-agent-platform/optimize/evaluation/agent-evaluation), you may have evaluation files under `tests/eval/evalsets/` using the older ADK `EvalSet` schema. The `agents-cli eval generate` command and related commands no longer read those files. This page walks through converting them to the new format.

If your project doesn't have a `tests/eval/evalsets/` directory, you don't need to do anything.

---

## Automatic migration

The `agents-cli scaffold upgrade` command detects legacy `*.evalset.json` files and converts them to the new format automatically. The conversion follows the rules below: it writes new files under `tests/eval/datasets/`, skips destinations that already exist, and leaves the legacy directory in place so you can verify before deleting. The `eval generate` command populates `agent_data.agents` from your running agent on the next run, so the migrator doesn't write a stub. The migrator does not carry over `session_input.state`; a case that relied on seeded state needs the hand edit in [Seed session state](#seed-session-state).

If you'd rather do the conversion by hand, the rest of this page walks through the schema changes.

---

## Why this changed

The new eval commands (`eval run`, `eval generate`, `eval grade`, `eval dataset synthesize`, `eval compare`, `eval analyze`, `eval metric list`, `eval optimize`, `eval submit`, `eval results`) are built on the GenAI Eval SDK's `EvaluationDataset` / `EvalCase` types. Using the platform's own schema gives Agents CLI the Gemini platform evaluation features without converting between two data formats: built-in and custom metrics, LLM-as-judge grading, dataset synthesis, regression comparison, failure-mode analysis, and prompt optimization.

---

## What changed at a glance

| | Old (ADK `EvalSet`) | New (Gemini platform `EvaluationDataset`) |
|---|---|---|
| Directory | `tests/eval/evalsets/` | `tests/eval/datasets/` |
| Filename | `*.evalset.json` | `*-dataset.json` |
| Default file | `basic.evalset.json` | `basic-dataset.json` |
| Schema source | `google.adk.evaluation` | `agentplatform._genai.types.EvaluationDataset` |

By default, `agents-cli eval generate` looks for `tests/eval/datasets/basic-dataset.json`. Use `--dataset PATH` to point at a different file.

---

## Schema changes

!!! note "Two valid input shapes"
    Each new-format eval case must provide **one** of:

    - **Shape A, single-prompt case**: a top-level `prompt` field (a single user message). Use Shape A when the case is a one-shot user query.
    - **Shape B, continued-conversation case (the "N+1" pattern)**: an `agent_data` block whose turns end with a user message. The `agents-cli eval generate` command then appends the next agent response.

    The old `EvalSet` schema's *single-turn* cases map to **Shape A**. Old *multi-turn* cases map to **Shape B**: the recorded prior turns become `agent_data.turns`, ending with the user message you want the agent to respond to. The sections below show both.

### Envelope

The outer wrapper drops `eval_set_id`, `name`, and `description`. Only `eval_cases` remains.

**Old:**
```json
{
  "eval_set_id": "basic_eval",
  "name": "Basic Agent Evaluation",
  "description": "Sample evaluation set for testing core agent functionality.",
  "eval_cases": [ ... ]
}
```

**New:**
```json
{
  "eval_cases": [ ... ]
}
```

### Single-turn case

Each case changes in three ways:

- The `eval_id` field becomes `eval_case_id`.
- The first turn's `conversation[0].user_content` moves to a top-level `prompt`. If the turn also has a `final_response`, that gold answer moves to a top-level `reference`.
- The `session_input` block is dropped. The `eval generate` command chooses `app_name` and `user_id`; you have to re-express a non-empty `state` by hand as a seed event (see [Seed session state](#seed-session-state)).

**Old:**
```json
{
  "eval_id": "greeting",
  "conversation": [
    {
      "user_content": {
        "parts": [{"text": "Hello, what can you help me with?"}]
      }
    }
  ],
  "session_input": {
    "app_name": "app",
    "user_id": "eval_user",
    "state": {}
  }
}
```

**New:**
```json
{
  "eval_case_id": "greeting",
  "prompt": {
    "role": "user",
    "parts": [{"text": "Hello, what can you help me with?"}]
  }
}
```

The prompt now has `"role": "user"`, which the Gemini platform `Content` type requires.

If the old turn had a `final_response`, wrap its `parts` in a `response` object with `"role": "model"`. Unlike `prompt`, `reference` isn't a bare `Content`:

```json
{
  "eval_case_id": "greeting",
  "prompt": {
    "role": "user",
    "parts": [{"text": "Hello, what can you help me with?"}]
  },
  "reference": {
    "response": {
      "role": "model",
      "parts": [{"text": "I can answer questions about the weather and the time."}]
    }
  }
}
```

### Multi-turn case (Shape B)

In the old schema, multi-turn conversations were a list of turns under `conversation`. In the new schema, they map to **Shape B**: prior turns live under `agent_data.turns`, and `eval generate` responds to the last user message in that history (no separate top-level `prompt`).

Each entry under `agent_data.turns[].events` is an event with an `author` (either `"user"` or an agent ID) and `content` (which carries `role: "user"` for user turns and `role: "model"` for agent turns).

**Old (two-turn conversation):**
```json
{
  "eval_id": "follow_up",
  "conversation": [
    {
      "user_content": {
        "parts": [{"text": "Book a flight to Paris."}]
      },
      "final_response": {
        "parts": [{"text": "What dates are you flying?"}]
      }
    },
    {
      "user_content": {
        "parts": [{"text": "Next Monday, returning Friday."}]
      }
    }
  ]
}
```

**New (Shape B):**
```json
{
  "eval_case_id": "follow_up",
  "agent_data": {
    "agents": {
      "flight_booker": {
        "agent_id": "flight_booker",
        "agent_type": "llm_agent",
        "description": "Books flights and answers itinerary questions.",
        "instruction": "Help the user book flights. Ask clarifying questions about dates, origin, and passenger count before calling any booking tool.",
        "tools": [
          {
            "function_declarations": [
              {"name": "search_flights", "description": "Search available flights."},
              {"name": "book_flight",    "description": "Book a flight by ID."}
            ]
          }
        ],
        "sub_agents": []
      }
    },
    "turns": [
      {
        "turn_index": 0,
        "events": [
          {
            "author": "user",
            "content": {
              "role": "user",
              "parts": [{"text": "Book a flight to Paris."}]
            }
          },
          {
            "author": "flight_booker",
            "content": {
              "role": "model",
              "parts": [{"text": "What dates are you flying?"}]
            }
          },
          {
            "author": "user",
            "content": {
              "role": "user",
              "parts": [{"text": "Next Monday, returning Friday."}]
            }
          }
        ]
      }
    ]
  }
}
```

!!! note "About `agent_data.agents`"
    The `agents` map declares the topology of the agent system under evaluation. It is keyed by agent ID, and each entry carries that agent's configuration: `agent_type`, `description`, `instruction`, `tools` (the function declarations the agent may call, in the same shape used by `google.genai.types.Tool`), and `sub_agents`. Multi-agent systems use these IDs as event authors to attribute responses and tool calls to the correct sub-agent during grading, and the `tools` block lets graders check that the agent picked the right tool with sensible arguments. You don't have to write the map by hand: when the running agent reports its metadata, `eval generate` fills `agent_data.agents` from it and replaces any map you wrote. The values shown here are examples.

The `eval generate` command runs the agent against this history and appends its reply as the next agent event, producing a populated trace ready for `eval grade`.

If your old case had `final_response` set on the **last** turn (the one being graded) to express a gold answer, that's a different concept. Put the answer on a top-level `reference` field instead of mixing it into `agent_data.turns`. Past actual responses go into the turn history; the target answer for the final user message goes into `reference`.

<a id="seeding-session-state"></a>
### Seed session state

The old `session_input.state` block has no top-level equivalent. Express it instead as a `state_delta` on an event at the front of `agent_data.turns[0].events`. The `eval generate` command seeds those events into the fresh session before running the case and applies the delta to session state.

A case that needs seeded state has to use **Shape B**, since Shape A carries no events. The seed event needs no content. Putting the delta on the final user message also works, because it is applied before that turn runs, but a separate seed event keeps the intent readable:

```json
{
  "eval_case_id": "halted_turn",
  "agent_data": {
    "turns": [
      {
        "turn_index": 0,
        "events": [
          {
            "author": "user",
            "state_delta": {"halt_reason": "repeated tool failure"}
          },
          {
            "author": "user",
            "content": {
              "role": "user",
              "parts": [{"text": "Try that again."}]
            }
          }
        ]
      }
    ]
  }
}
```

---

## Step-by-step conversion

For a single file `tests/eval/evalsets/basic.evalset.json`:

1. Create the new directory: `mkdir -p tests/eval/datasets`.
2. Copy the file: `cp tests/eval/evalsets/basic.evalset.json tests/eval/datasets/basic-dataset.json`.
3. Open `tests/eval/datasets/basic-dataset.json` in your editor.
4. Delete `eval_set_id`, `name`, and `description` from the top level.
5. For each entry under `eval_cases`, rename `eval_id` to `eval_case_id`, then pick **Shape A** or **Shape B**:
    - **Single-turn (Shape A)**: Move the only turn's `user_content` out to a top-level `prompt`, adding `"role": "user"`. Delete the `conversation` array and `session_input` block.
    - **Gold answer (either shape)**: If the graded turn has a `final_response`, move it to a top-level `reference` shaped as `{"response": {"role": "model", "parts": [...]}}`.
    - **Multi-turn (Shape B)**: Build an `agent_data.turns[0].events` list whose last entry is the user message you want the agent to respond to. Convert each prior turn's `user_content` into an event with `author: "user"` (`role: "user"`) and any recorded agent response into an event whose `author` is the responding agent's ID (`role: "model"`). Delete the `conversation` array and `session_input` block; do **not** set a top-level `prompt` for Shape B.
6. Save and verify with `agents-cli eval generate`, which finds the file automatically.

When everything works, delete the old `tests/eval/evalsets/` directory.

### Multiple files

Repeat the steps above for each `*.evalset.json`. Filenames in `tests/eval/datasets/` should follow the `*-dataset.json` convention (so `flight_booking.evalset.json` becomes `flight_booking-dataset.json`).

---

<a id="verifying"></a>
## Verify the conversion

```bash
agents-cli eval generate
```

If you used the default name (`basic-dataset.json`), `eval generate` picks it up automatically. For other filenames:

```bash
agents-cli eval generate --dataset tests/eval/datasets/your-file-dataset.json
```

A successful run produces a populated traces file you can pass to `agents-cli eval grade`.
