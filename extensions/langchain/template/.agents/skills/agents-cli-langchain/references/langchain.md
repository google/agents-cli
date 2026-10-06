# LangChain with agents-cli

This project was scaffolded from the LangChain template and carries
`agents-cli-extension.yaml` at its root, which overrides the framework-coupled
commands. Everything else (deploy, infra, eval grade) runs natively.

## What counts as "LangChain" here

The extension's only contract is that `app/agent.py` exports `root_agent` as a
compiled LangGraph graph with a `messages` state. Everything downstream (`run`,
`eval generate`, the A2A executor) uses just `root_agent.invoke({"messages": [...]})`
and `root_agent.astream(stream_mode="messages")`.

That covers the whole ecosystem, with no extension changes:

| You write | Import | Notes |
|---|---|---|
| **LangChain agent** | `from langchain.agents import create_agent` | The scaffolded default. LangChain 1.x compiles it to a graph. |
| **LangGraph graph** | `from langgraph.graph import StateGraph` | Hand-built graph; `.compile()` it and assign to `root_agent`. |
| **Deep Agent** | `from deepagents import create_deep_agent` | Also returns a `CompiledStateGraph` with `messages`; add `deepagents` to your deps. |

Legacy pre-1.0 LangChain (LCEL chains, `AgentExecutor`) is **not** supported:
those objects aren't compiled graphs, so `run` and A2A streaming won't work.
Wrap them in a `StateGraph` node if you need to bring one along.

## Adding deployment or CI/CD later

`create` writes the deployment target and CI/CD files, so pass them up front
when you can:

```bash
agents-cli create my-lc-agent --agent google/agents-cli/extensions/langchain/template@v1.9.0 -d cloud_run --cicd-runner github_actions
```

For a project that skipped them, `agents-cli scaffold enhance -d cloud_run
--cicd-runner github_actions` re-renders from the template recorded in
`agents-cli-manifest.yaml` as `base_template`. It fetches that template, so it
needs network access, and it does not touch `app/`.

## Serving (A2A protocol)

The deployed agent is served over the Agent2Agent (A2A) protocol — the same
A2A contract the rest of the toolchain expects — so it works unchanged:

- Entrypoint: `uvicorn app.fast_api_app:app` (the scaffold Dockerfile CMD).
- A2A JSON-RPC endpoint: `POST /a2a/app`; Agent Card at
  `/a2a/app/.well-known/agent-card.json`.
- `app/fast_api_app.py` wraps the compiled graph in an `AgentExecutor` and
  mounts it with `add_a2a_routes_to_fastapi` (a2a-sdk 1.x).
- **Streaming**: the executor streams LLM token chunks via
  `root_agent.astream(stream_mode="messages")` as incremental A2A task artifacts
  (`capabilities.streaming=True`), so a real chat model streams token-by-token
  to A2A clients. Graphs whose nodes don't stream tokens fall back to returning
  the final reply as a single artifact.

The default `app/agent.py` is a Gemini ReAct agent (`langchain.agents.create_agent`)
with a sample `get_weather` tool. It uses Vertex AI via Application Default
Credentials, or AI Studio when `GOOGLE_API_KEY` or `GEMINI_API_KEY` is set in the
environment or `.env` (the scaffolded `.env.example` names the latter).

Query a deployed (or locally served) agent over A2A. The extension overrides `run`
with in-process invocation, so bypass it to reach the built-in A2A client:

```bash
AGENTS_CLI_DISABLE_OVERRIDES=1 agents-cli run --url https://<service-url> --mode a2a --app-name app "hello"
```

## What stays native (do NOT override)

- `deploy` — native `agents-cli deploy`; dispatches to the project's configured deployment target.
- `eval grade`, `eval compare`, `eval analyze` — framework-agnostic.
- `infra`.
- `publish gemini-enterprise` — overridden only to refuse on Agent Runtime, where
  registration invokes the agent through ADK's `:streamQuery`. On cloud_run and gke
  it runs the built-in, which registers over A2A. To register a LangChain agent,
  deploy it to Cloud Run or GKE and publish from there.

## Journey

```bash
agents-cli create my-lc-agent --agent google/agents-cli/extensions/langchain/template@v1.9.0 -d cloud_run
cd my-lc-agent && agents-cli install
agents-cli run "hello"                     # in-process graph invocation
agents-cli eval generate --dataset tests/eval/datasets/basic-dataset.json -o tests/eval/output/
agents-cli eval grade --traces tests/eval/output/<dataset>.json --config tests/eval/eval_config.yaml
agents-cli deploy                          # native deploy (by deployment target)
```

Nothing is installed machine-wide: the overrides ride in the project, so a
teammate who clones it gets them with no setup step.

## Telemetry

`app/app_utils/telemetry.py` runs at startup from `app/fast_api_app.py` and reads
the same environment the ADK templates do, so the terraform in
`deployment/terraform/` configures both the same way:

| Variable | Effect |
|---|---|
| `LOGS_BUCKET_NAME` | Turns on prompt-response logging, uploaded under `gs://<bucket>/completions` |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | `NO_CONTENT` records the exchange without message bodies; unset or `false` disables capture |
| `OTEL_SERVICE_NAME` | Service name on every span |
| `GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY` | Set by `agents-cli deploy` on Agent Runtime; `false` turns Cloud export off |

Cloud Trace works with no configuration once credentials resolve. Span names
differ from ADK's (`generate_content` comes from the shared google-genai
instrumentor either way; graph, agent and tool spans come from the LangChain
instrumentor rather than ADK's `invoke_agent`/`call_llm`).

## Pitfalls

- To run the built-in instead of the override: `AGENTS_CLI_DISABLE_OVERRIDES=1 agents-cli <cmd>`.
- The default `app/agent.py` calls Gemini via Vertex AI (ADC), so `run`/`eval`
  need credentials (`GOOGLE_CLOUD_PROJECT` + ADC, or `GOOGLE_API_KEY` /
  `GEMINI_API_KEY` for AI Studio).
- The deploy contract depends on `app/fast_api_app.py` exposing `app`; keep that
  import working if you restructure the agent.
- Swapping in a different framework (Deep Agents, a hand-built graph) only means
  rewriting `app/agent.py` and adding the dependency — leave `root_agent` and
  `app/fast_api_app.py` alone.
