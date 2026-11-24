---
name: agents-cli-langchain
description: >
  Use when working in this project — adding tools, editing the agent, running,
  evaluating, serving or deploying it — or when guidance mentions ADK, LlmAgent,
  google.adk, adk web, or an ADK runner. This project is LangChain/LangGraph
  scaffolded by agents-cli, so ADK-specific instructions do not apply.
---

# LangChain project (agents-cli)

The agent is a compiled LangGraph graph exported as `root_agent` from
`app/agent.py`. There is no `google.adk` dependency and no ADK runner. Other
`google-agents-cli-*` skills assume ADK; where they describe the agent itself,
this skill wins.

**Experimental, and Agent Runtime is degraded.** Deploy to cloud_run or gke. On
agent_runtime the app serves, but `publish gemini` is refused, the Console
playground cannot invoke it, and Console sessions/traces stay empty: all three want the
ADK `reasoning_engine` routes this project does not serve. Say so before recommending it.

## What ADK guidance maps to here

| ADK guidance | This project |
|---|---|
| `LlmAgent`, `Agent`, `google.adk.tools` | `langchain.agents.create_agent`, plain Python functions as tools, or any compiled `StateGraph` |
| `adk web`, `adk run` | `agents-cli playground` (runs `langgraph dev`) |
| ADK runner behind `agents-cli run` | `agents-cli run` invokes the graph in-process and prints tool calls and responses the same way |
| `agents-cli eval dataset synthesize`, `eval optimize` | Unavailable: both drive the agent through ADK. The command says so and exits |
| Add an `LlmAgent` in `app/agent.py` | Change the graph in `app/agent.py`; keep the name `root_agent` |

## The contract

Keep these two, whatever you build inside them:

- `app/agent.py` exports `root_agent`, a compiled graph with `messages` state.
  Callers only use `root_agent.invoke({"messages": [...]})` and
  `root_agent.astream(stream_mode="messages")`.
- `app/fast_api_app.py` exposes `app`. Every deployment target runs
  `uvicorn app.fast_api_app:app`.

Adding a tool means writing a typed function with a docstring and passing it in
`tools=[...]`. Switching frameworks (LangGraph `StateGraph`,
`deepagents.create_deep_agent`) means rewriting `app/agent.py` only. Pre-1.0
LangChain (LCEL chains, `AgentExecutor`) is not supported: not compiled graphs.

## Commands

```bash
agents-cli install                  # uv sync
agents-cli playground               # langgraph dev, port 8080
agents-cli run "hello"              # invoke the graph in-process
agents-cli eval generate --dataset tests/eval/datasets/basic-dataset.json -o tests/eval/output/
agents-cli eval grade --traces tests/eval/output/<dataset>.json --config tests/eval/eval_config.yaml
agents-cli deploy                   # unchanged
agents-cli scaffold enhance -d cloud_run --cicd-runner github_actions   # add infra later
```

`playground`, `run` and `eval generate` are overridden by
`agents-cli-extension.yaml` at the project root. Prefix any command with
`AGENTS_CLI_DISABLE_OVERRIDES=1` to reach the built-in instead.

## Serving & Persistence

A2A only: JSON-RPC at `POST /a2a/app`, card at
`/a2a/app/.well-known/agent-card.json`, health at `/health`. Token streaming
comes from `astream(stream_mode="messages")`.

`app/app_utils/services.py` wires both a short-term thread checkpointer (keyed by
A2A `context_id` as `thread_id`) and a long-term `BaseStore` (reachable in tools
via `from langgraph.prebuilt import InjectedStore`) onto `root_agent` at server
startup:
- With `--session-type cloud_sql` on Cloud Run or GKE (`INSTANCE_CONNECTION_NAME` +
  `DB_PASS`, or `SESSION_DB_URI`), it uses `AsyncPostgresSaver` and
  `AsyncPostgresStore` sharing one connection pool.
- Without DB credentials, it falls back to `InMemorySaver` and `InMemoryStore`.
- Leave `root_agent` in `app/agent.py` without a custom `checkpointer` or `store`
  at module import time so `langgraph dev` (`agents-cli playground`) can attach
  its own runtime persistence.

## Common mistakes

- Renaming `root_agent` or `app`, which breaks `run`, eval and deploy.
- Reaching for `eval dataset synthesize` or `eval optimize`: they need ADK.
  Write cases into `tests/eval/datasets/` and use `eval generate` + `eval grade`.
- Expecting `/run_sse` or ADK session routes; this server serves A2A.
- Running `agents-cli run --url ...` against a deployed agent without
  `AGENTS_CLI_DISABLE_OVERRIDES=1`, which invokes the local graph instead.
- `run` and `eval generate` call Gemini on Gemini platform with ADC, so they need
  `GOOGLE_CLOUD_PROJECT` and credentials, or `GOOGLE_API_KEY` / `GEMINI_API_KEY`
  in `.env`.

## References

- `references/langchain.md` — framework contract and per-command detail.
- `references/samples.md` — agents worth copying from, by shape.
