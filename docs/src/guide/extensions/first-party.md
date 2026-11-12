# First-party frameworks

A framework ships as a **template you scaffold from**, not as an extension you install. The template carries its own `agents-cli-extension.yaml`, so the project gets the framework's command overrides with nothing installed machine-wide.

| Framework | What it does |
|--------|--------------|
| [LangChain](#langchain) | Run a LangChain agent through the `agents-cli` lifecycle. |
| [Claude Agent SDK](#claude-agent-sdk) | Run a Claude Agent SDK agent, on Claude on Gemini platform, through the `agents-cli` lifecycle. |

---

## LangChain

The LangChain template runs a [LangChain](https://python.langchain.com) agent through the `agents-cli` lifecycle. It overrides only the framework-coupled commands and leaves everything else, such as `deploy`, `infra`, and `eval grade`, running natively.

!!! warning "Experimental: deploy to Cloud Run or GKE"

    On Agent Runtime in Gemini platform, the container serves and traces normally, but the integrations that
    expect an ADK app do not work, because this project serves A2A and no `reasoning_engine`
    routes. The `publish gemini` command is refused (registration invokes `:streamQuery`), the
    Console playground cannot invoke the agent, and the Console's session and trace views stay
    empty. Traces still reach Cloud Trace.

### Create a project

```bash
agents-cli create my-agent \
    --agent google/agents-cli/extensions/langchain/template@v1.14.0 \
    -d cloud_run
cd my-agent && agents-cli install
```

The scaffolded project contains `agents-cli-extension.yaml` at its root. That file is auto-loaded at project scope, so the overrides below are active in this project and nowhere else. Commit it.

### What it overrides

| Command | Behavior in a LangChain project |
|---------|---------------------------------|
| `playground` | `uv run langgraph dev`, the local LangGraph dev server (also serves A2A). |
| `publish gemini` | Runs the built-in, except on Agent Runtime, where it refuses with the reason. |
| `run` | Invokes the compiled graph in-process. |
| `eval generate` | Produces the standard `EvaluationDataset` shape, so `eval grade` is unchanged. |
| `eval dataset` (including `synthesize`), `eval optimize` | Refused with an explanation, because both introspect an ADK agent object to build their dataset. |

Everything else stays native: `deploy` (the target-appropriate native deploy, for example `gcloud run deploy --source .` on Cloud Run), `eval grade` / `compare` / `analyze` (framework-agnostic), and `infra`.

### The scaffolded agent

The default `app/agent.py` is a Gemini ReAct agent built with `langchain.agents.create_agent` and a sample `get_weather` tool. It calls Gemini on Gemini platform using Application Default Credentials; set `GOOGLE_API_KEY` or `GEMINI_API_KEY` in `.env` to use AI Studio instead (the scaffolded `.env.example` names the latter).

The agent is named after the project (`my-agent` becomes `my_agent`), or after `--root-agent-name` if you pass it. Telemetry reports that name as `gen_ai.agent.name`, and the manifest records it as `create_params.root_agent_name`. To rename the agent, change it in `app/agent.py` and in the manifest.

Because the project is built on the framework-neutral `empty_py` base, the generated code contains **no ADK dependency**, and the template ships its own coding-agent skill.

<a id="serving-over-a2a"></a>

### Serve over A2A

The deployed agent serves the [Agent2Agent (A2A) protocol](https://a2a-protocol.org), the same protocol the rest of the toolchain expects, so it works unchanged:

- **Entrypoint**: `uvicorn app.fast_api_app:app` (the scaffold Dockerfile `CMD`).
- **Endpoints**: JSON-RPC at `POST /a2a/app`; Agent Card at `/a2a/app/.well-known/agent-card.json`.
- **Streaming**: the executor streams LLM token chunks as incremental A2A task artifacts (`capabilities.streaming=True`), so a real chat model streams token-by-token. Graphs whose nodes don't stream tokens fall back to a single final artifact.

Query a deployed (or locally served) agent over A2A. Because the project overrides `run` with in-process graph invocation, set `AGENTS_CLI_DISABLE_OVERRIDES=1` to reach the built-in A2A client:

```bash
AGENTS_CLI_DISABLE_OVERRIDES=1 agents-cli run --url https://<service-url> --mode a2a --app-name app "hello"
```

### Full journey

```bash
agents-cli create my-agent --agent google/agents-cli/extensions/langchain/template@v1.14.0 -d cloud_run
cd my-agent && agents-cli install
agents-cli run "what's the weather in San Francisco?"       # in-process graph
agents-cli eval generate --dataset tests/eval/datasets/basic-dataset.json -o tests/eval/output/
agents-cli eval grade --traces tests/eval/output/ --config tests/eval/eval_config.yaml
agents-cli deploy --project <project-id>                    # native Cloud Run deploy
```

### Notes

- **Credentials**: `run` and `eval` call Gemini, so they need `GOOGLE_CLOUD_PROJECT` plus ADC, or `GOOGLE_API_KEY` / `GEMINI_API_KEY` for AI Studio.
- **Run the built-in instead of an override**: prefix with `AGENTS_CLI_DISABLE_OVERRIDES=1` (for example, `AGENTS_CLI_DISABLE_OVERRIDES=1 agents-cli run "hi"`).
- **Deploy contract**: `app/fast_api_app.py` must keep exposing `app`. If you restructure the agent, keep that import working.
- **Compatibility**: the template's manifest declares no `requires` range, so its overrides apply on any CLI version. See [Authoring → Compatibility](authoring.md#compatibility) for declaring one in your own.

---

## Claude Agent SDK

The Claude template runs a [Claude Agent SDK](https://pypi.org/project/claude-agent-sdk/) (Python) agent through the `agents-cli` lifecycle, with the model served by Claude on Gemini platform. Like the LangChain template, it overrides only the framework-coupled commands.

!!! warning "Experimental: deploy to Agent Runtime or Cloud Run"

    The template supports `agent_runtime` and `cloud_run`. The model named in
    `app/agent.py` (default `claude-haiku-4-5`) must be enabled for the project in Model
    Garden.

    On Agent Runtime the container serves A2A and traces normally. It also serves Agent
    Runtime's `:query` and `:streamQuery` methods, so `agents-cli run --mode adk` works and
    an ingress Agent Gateway can govern the agent. The `publish gemini` command is refused, the Console
    playground cannot invoke the agent, and the Console's session and trace views stay
    empty. Traces still reach Cloud Trace. Agent Runtime's managed sessions are ADK-only
    and Cloud SQL isn't set up there automatically, so transcripts stay on the instance
    unless you set it up by hand (see Notes).

### Scaffold an existing agent

The template's contract is `app/agent.py` exporting `root_agent`, a `ClaudeAgentOptions`. The template ships a step-by-step migration guide for your coding agent, at `.agents/skills/agents-cli-claude/references/migrate-claude-agent.md` in the scaffolded project. By hand:

```bash
cd my-claude-agent
agents-cli scaffold enhance google/agents-cli/extensions/claude/template@v1.14.0 -d cloud_run
```

The `enhance` command writes the template as `create` would: the server, a sample `app/agent.py`, `pyproject.toml`, Dockerfile, deployment files, tests and `agents-cli-extension.yaml`. Each of your files it replaces moves to `.agents-cli-original/` at the same path; your other files stay where they are. Then port your agent into `app/agent.py`, add your dependencies with `uv add`, carry over what you need from `.agents-cli-original/` and delete it. To start from scratch instead:

```bash
agents-cli create my-agent --agent google/agents-cli/extensions/claude/template@v1.14.0 -d cloud_run
```

### What it overrides

| Command | Behavior in a Claude project |
|---------|------------------------------|
| `run` | Runs `root_agent` in process through the SDK. |
| `eval generate` | Produces the standard `EvaluationDataset` shape, with tool calls as `function_call` / `function_response` parts, so `eval grade` is unchanged. |
| `playground` | Refused with an explanation: the SDK has no local dev UI. |
| `eval dataset synthesize`, `eval optimize` | Refused with an explanation: both drive the agent through ADK. |
| `publish gemini` | Runs the built-in, which registers over A2A, except on Agent Runtime, where it refuses with the reason. |

The `deploy` and `infra` commands and the `eval grade`, `eval compare` and `eval analyze` subcommands stay native.

### How the agent runs

The `app/app_utils/claude.py` file runs each turn with the SDK's `query()`, which starts the Claude Code CLI bundled in the `claude-agent-sdk` wheel; no Node.js is needed in the image for the agent itself. It points the CLI at Gemini platform (`CLAUDE_CODE_USE_VERTEX=1`, `CLOUD_ML_REGION` defaulting to `global`, and the project from `GOOGLE_CLOUD_PROJECT` or ADC). The Anthropic API is not supported.

Each conversation runs in its own working directory, so the agent never sees the app source and concurrent conversations don't share files. The agent's own `CLAUDE.md`, `.claude/` (settings, output styles, skills, subagents, hooks) and `.mcp.json` live in `agent_config/`; they are copied into that directory and loaded as project settings. The same files at the project root belong to your coding assistant: the served agent never reads them. Python `hooks` and `can_use_tool` callbacks in `root_agent` work unchanged.

Give a served agent an explicit tool list: `tools=[]` disables Claude Code's built-in tools (`Bash`, `Read`, `Write`, ...), and `allowed_tools` names the tools it may call, such as `mcp__weather__get_weather` for a `@tool` served by `create_sdk_mcp_server` (the middle part is the `mcp_servers` key). An agent with subagents (`agents=` or `agent_config/.claude/agents/`) also needs `"Agent"` in both lists. stdio MCP servers started with `npx` need Node.js: uncomment the Node line in the scaffolded Dockerfile.

The agent is named after the project (`my-agent` becomes `my_agent`), or after `--root-agent-name` if you pass it. The scaffold writes that name to `AGENT_NAME` in `app/app_utils/settings.py`, and spans (`gen_ai.agent.name`), BigQuery Agent Analytics rows and eval traces all report it. It isn't in `app/agent.py` as in the LangChain template, because in a migrated project `app/agent.py` is your file, and `ClaudeAgentOptions` has no name field. To rename the agent, change `AGENT_NAME` and `create_params.root_agent_name` in the manifest.

<a id="migrating-an-existing-agent"></a>
### Migrate an existing agent

1. Run `agents-cli scaffold enhance <template> -d cloud_run`. The files the template replaces, such as `pyproject.toml`, `README.md` and `.env`, move to `.agents-cli-original/`.
2. Expose the agent as `root_agent = ClaudeAgentOptions(...)` in `app/agent.py`, typically by moving the options a script builds in `main()` there and importing its tools. Add your dependencies with `uv add`, keeping the template's `claude-agent-sdk` range, carry over what you need from `.agents-cli-original/`, and delete it.
3. Pin the model, and map subagent aliases (`model: sonnet`) to models enabled in your project with `env={"ANTHROPIC_DEFAULT_SONNET_MODEL": "..."}`.
4. `agents-cli install`, `agents-cli run`, write eval cases, `agents-cli deploy`.

### Serve the agent over A2A

The server is the same A2A surface as the LangChain template: JSON-RPC at `POST /a2a/app`, Agent Card at `/a2a/app/.well-known/agent-card.json`, `/health`. Text streams as it is generated. Each A2A conversation maps to one Claude session. Scaffold with `--session-type cloud_sql` to store sessions in a Cloud SQL Postgres database (provisioned by `agents-cli infra single-project`), so a conversation resumes on any instance and survives restarts; the default, `in_memory`, keeps them on the instance. To let the agent call other A2A agents, add `remote_agent("<name>", "<url>")` from `app/app_utils/remote_agents.py` to `root_agent.mcp_servers`. For a Cloud Run callee it sends an ID token (the caller needs `roles/run.invoker` on it); for an Agent Runtime callee, given its passthrough URL (`https://<location>-aiplatform.googleapis.com/reasoningEngines/v1/projects/<project>/locations/<location>/reasoningEngines/<id>/api/a2a/app`), it sends an OAuth access token (the caller needs `roles/aiplatform.user`). The `run` override hands calls with `--url` or `--mode` to the built-in A2A client:

```bash
agents-cli run --url https://<service-url> --mode a2a --app-name app "hello"
# Agent Runtime: the engine's resource URL
agents-cli run --url https://<location>-aiplatform.googleapis.com/v1/projects/<project>/locations/<location>/reasoningEngines/<id> --mode a2a "hello"
```

### Full journey

```bash
cd my-claude-agent
agents-cli scaffold enhance google/agents-cli/extensions/claude/template@v1.14.0 -d cloud_run
agents-cli install
agents-cli run "what's the weather in San Francisco?"       # in-process
agents-cli eval generate --dataset tests/eval/datasets/basic-dataset.json -o tests/eval/output/
agents-cli eval grade --traces tests/eval/output/<dataset>.json --config tests/eval/eval_config.yaml
agents-cli deploy                                            # native Cloud Run deploy
```

### Notes

- **Credentials:** `run` and `eval` call Claude on Gemini platform, so they need ADC and a project with the model enabled. The deployed agent needs `roles/aiplatform.user`. New projects, on Cloud Run and Agent Runtime, run the agent with Agent Identity, and `deploy` grants it. A Cloud Run project created with `--cloud-run-identity-default service_account` (or before Agent Identity became the default) deploys either as the service account passed via `--service-account`, or as the default compute account, so run `agents-cli infra single-project --apply` and deploy with `--service-account <name>-app@<project>.iam.gserviceaccount.com`. `.env` sets `GOOGLE_CLOUD_PROJECT`, and `agents-cli deploy` forwards it to the service (Agent Runtime sets it itself).
- **Sessions:** with the default `in_memory` session type, transcripts live on the instance, so a conversation that reaches another instance, or outlives a restart, starts fresh. `--session-type cloud_sql` keeps them in Cloud SQL on Cloud Run. On Agent Runtime, `--session-type` doesn't apply: Cloud SQL works there with manual network and database setup (see the project's `.agents/skills/agents-cli-claude/references/claude.md`). Without it, the default shape scales to zero (`--min-instances 0`), so an idle conversation is lost; deploy with `--min-instances 1` to keep a single instance warm.
- **Sizing:** a tool-using turn takes about 270 MiB, so the default shape (4 GiB, concurrency 8) fits; file-heavy agents need more memory per concurrent session.
- **Telemetry:** the OpenInference Claude Agent SDK instrumentor records an `invoke_agent` span per turn (model, token usage, cost) and an `execute_tool` span per tool call in Cloud Trace, with `gen_ai.*` attributes. The Claude Code CLI's own spans (each model request, each tool step) are relayed to Cloud Trace through a localhost forwarder and nest under the turn span in the same trace. Message content and tool arguments are redacted unless `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` enables capture. As with ADK agents, every request starts its own trace, so a call to another agent through `remote_agent` shows up as two traces, the caller's and the callee's; `gen_ai.conversation.id` links each side's turns. Python log records go to Cloud Logging (the `adk-otel` log, or `reasoning_engine_stdout` on Agent Runtime, as with ADK agents) with their severity and the turn's trace, and ADK's agent metrics (`gen_ai.invoke_agent.duration`, `gen_ai.execute_tool.duration`, `gen_ai.invoke_agent.inference_calls`, `gen_ai.invoke_agent.tool_calls`) plus `gen_ai.client.token.usage` go to Cloud Monitoring. With `--bq-analytics`, each turn's events also go to BigQuery Agent Analytics: the `agent_events` table and views ADK's plugin writes, so its SDK and dashboards work for Claude agents too.
