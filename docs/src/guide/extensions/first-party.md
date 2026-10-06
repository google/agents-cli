# First-party frameworks

A framework is delivered as a **template you scaffold from**, not as an extension you install. The template carries its own `agents-cli-extension.yaml`, so the project gets the framework's command overrides with nothing installed machine-wide.

| Framework | What it does |
|--------|--------------|
| [LangChain](#langchain) | Run a LangChain agent through the `agents-cli` lifecycle. |

---

## LangChain

The LangChain template runs a [LangChain](https://python.langchain.com) agent through the `agents-cli` lifecycle. It overrides only the framework-coupled commands and leaves everything else — `deploy`, `infra`, `eval grade` — running natively.

!!! warning "Experimental: deploy to Cloud Run or GKE"

    On Agent Runtime the container serves and traces normally, but the integrations that
    expect an ADK app do not work, because this project serves A2A and no `reasoning_engine`
    routes: `publish gemini-enterprise` is refused (registration invokes `:streamQuery`), the
    Console playground cannot invoke the agent, and the Console's session and trace views stay
    empty. Traces still reach Cloud Trace.

### Create a project

```bash
agents-cli create my-agent \
    --agent google/agents-cli/extensions/langchain/template@v1.9.0 \
    -d cloud_run
cd my-agent && agents-cli install
```

The scaffolded project contains `agents-cli-extension.yaml` at its root. That file is auto-loaded at project scope, so the overrides below are active in this project and nowhere else. Commit it.

### What it overrides

| Command | Behavior in a LangChain project |
|---------|---------------------------------|
| `playground` | `uv run langgraph dev` — the local LangGraph dev server (also serves A2A). |
| `publish gemini-enterprise` | Runs the built-in, except on Agent Runtime, where it refuses with the reason. |
| `run` | Invokes the compiled graph in-process. |
| `eval generate` | Produces the standard `EvaluationDataset` shape, so `eval grade` is unchanged. |
| `eval dataset synthesize`, `eval optimize` | Refused with an explanation: both drive the agent through ADK. |

Everything else stays native — **do not override** `deploy` (the target-appropriate native deploy, e.g. `gcloud run deploy --source .` on Cloud Run), `eval grade` / `compare` / `analyze` (framework-agnostic), `infra`, or `publish`.

### The scaffolded agent

The default `app/agent.py` is a Gemini ReAct agent built with `langchain.agents.create_agent` and a sample `get_weather` tool. It calls Gemini via Vertex AI using Application Default Credentials; set `GOOGLE_API_KEY` or `GEMINI_API_KEY` in `.env` to use AI Studio instead (the scaffolded `.env.example` names the latter).

Because the project rides the framework-neutral `empty_py` substrate, the generated code contains **no ADK dependency**, and the template ships its own coding-agent skill.

### Serving over A2A

The deployed agent is served over the [Agent2Agent (A2A) protocol](https://a2a-protocol.org) — the same contract the rest of the toolchain expects — so it works unchanged:

- **Entrypoint:** `uvicorn app.fast_api_app:app` (the scaffold Dockerfile `CMD`).
- **Endpoints:** JSON-RPC at `POST /a2a/app`; Agent Card at `/a2a/app/.well-known/agent-card.json`.
- **Streaming:** the executor streams LLM token chunks as incremental A2A task artifacts (`capabilities.streaming=True`), so a real chat model streams token-by-token. Graphs whose nodes don't stream tokens fall back to a single final artifact.

Query a deployed (or locally served) agent over A2A. Because the project overrides `run` with in-process graph invocation, set `AGENTS_CLI_DISABLE_OVERRIDES=1` to reach the built-in A2A client:

```bash
AGENTS_CLI_DISABLE_OVERRIDES=1 agents-cli run --url https://<service-url> --mode a2a --app-name app "hello"
```

### Full journey

```bash
agents-cli create my-agent --agent google/agents-cli/extensions/langchain/template@v1.9.0 -d cloud_run
cd my-agent && agents-cli install
agents-cli run "what's the weather in San Francisco?"       # in-process graph
agents-cli eval generate --dataset tests/eval/datasets/basic-dataset.json -o tests/eval/output/
agents-cli eval grade --traces tests/eval/output/<dataset>.json --config tests/eval/eval_config.yaml
agents-cli deploy                                            # native Cloud Run deploy
```

### Notes

- **Credentials:** `run` and `eval` call Gemini, so they need credentials — `GOOGLE_CLOUD_PROJECT` plus ADC, or `GOOGLE_API_KEY` / `GEMINI_API_KEY` for AI Studio.
- **Run the built-in instead of an override:** prefix with `AGENTS_CLI_DISABLE_OVERRIDES=1` (e.g. `AGENTS_CLI_DISABLE_OVERRIDES=1 agents-cli run "hi"`).
- **Deploy contract:** `app/fast_api_app.py` must keep exposing `app`. If you restructure the agent, keep that import working.
- **Compatibility:** the template's manifest declares no `requires` range, so its overrides apply on any CLI version. See [Authoring → Compatibility](authoring.md#compatibility) for declaring one in your own.
