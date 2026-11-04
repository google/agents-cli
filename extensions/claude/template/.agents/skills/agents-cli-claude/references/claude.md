# Claude Agent SDK with agents-cli

This project carries `agents-cli-extension.yaml` at its root, which overrides the
commands that assume ADK. Everything else (deploy, infra, eval grade) runs natively.

## On Agent Runtime

The container serves A2A and exports traces as on Cloud Run. It also serves Agent
Runtime's `:query` and `:streamQuery` methods (see Serving), so `agents-cli run --mode
adk` works and an ingress Agent Gateway can govern the agent. `publish gemini` is refused,
the Console playground cannot invoke the agent, and the Console's session and trace views
stay empty; read traces in Cloud Trace. Transcripts stay on the instance unless you set
up Cloud SQL by hand (see Sessions).

## How the agent runs

`app/app_utils/claude.py` runs each turn of `root_agent` in a Claude Code CLI
subprocess. For every turn it:

- points the CLI at Claude on Vertex AI (`CLAUDE_CODE_USE_VERTEX=1`,
  `CLOUD_ML_REGION`, default `global`, and `ANTHROPIC_VERTEX_PROJECT_ID`, default
  `GOOGLE_CLOUD_PROJECT`, else the ADC project). The region and project in the
  environment or `.env` win. The Anthropic API is not supported: the CLI gets
  `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` blanked.
- runs the turn in a working directory for its conversation,
  `<tmp>/claude-agent/<AGENT_NAME>/<session id>`, so concurrent conversations don't
  share files. It is a working directory, not a sandbox: tools you allow can still
  reach the rest of the container.
- denies any tool outside `allowed_tools` (`permission_mode="dontAsk"`) unless
  `root_agent` sets its own `permission_mode` or `can_use_tool`. Its other defaults can
  be overridden in `root_agent.env`.

On Gemini platform the CLI always starts the system prompt with "You are Claude Code,
Anthropic's official CLI for Claude.", and no option removes it. Give the agent a name
in `system_prompt` ("You are Skye, ..."), or it introduces itself as Claude Code.

## Model access

The model in `app/agent.py` (default `claude-haiku-4-5`) must be enabled for the
project in Model Garden; unversioned ids work. "not available on your vertex
deployment" means it is not enabled, a one-time Console step for the user. With no
`model=` (and no `ANTHROPIC_MODEL` or settings file naming one), a turn fails before any
model call rather than fall back to the CLI's default model.

The deployed identity needs `roles/aiplatform.user`. New projects deploy with Agent
Identity, and `agents-cli deploy` grants the role to the agent's Agent Identity principal.
A Cloud Run project whose `cloud_run.identity_default` is `service_account` deploys either
as the service account passed via `--service-account`, or as the default compute account,
which may lack the role (a 403 on `aiplatform.endpoints.predict`): with the user's
approval, run `agents-cli infra single-project --apply` and deploy with
`--service-account <name>-app@<project>.iam.gserviceaccount.com`, or grant the role.

Subagents often ask for an alias (`model: sonnet`). If that model isn't enabled, map
the alias: `env={"ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-haiku-4-5"}`.

The Claude Code CLI, and every tool, hook and stdio MCP server it runs, inherits the
server's environment, including the values passed with `--secrets`. Give an agent with
`Bash` or file tools only the secrets it needs.

## Project settings (agent_config/)

The served agent's Claude Code files live in `agent_config/`: `CLAUDE.md`,
`CLAUDE.local.md`, `.claude/` (settings, output styles, skills, subagents, hooks) and
`.mcp.json`. They are copied into every conversation's working directory and loaded as
project settings whenever `setting_sources` is unset or includes `"project"`/`"local"`.
With `setting_sources=[]` nothing is loaded; `~/.claude` is never loaded unless the agent
asks for `"user"`. The same files at the project root belong to your coding assistant:
the served agent never reads them, and they are left out of the image. Each turn
replaces the working directory's copy, so settings the agent writes there don't load on
the next turn.
A `root_agent` with its own `cwd` is left alone: nothing is copied into or deleted from
that directory, and its own settings load only if `setting_sources` asks for them.

`permissions.allow` rules in `agent_config/.claude/settings.json` (and
`settings.local.json` with `"local"`) are added to `allowed_tools`. The CLI still logs
"Ignoring N permissions.allow entries ... not been trusted"; the rules apply anyway.

Shell hooks run in the container: a hook that calls a desktop tool (`afplay`,
`osascript`) fails there and is skipped. Python `hooks` and `can_use_tool` in
`root_agent` run in the server process unchanged; `can_use_tool` decides tools that are
not in `allowed_tools`.

## Sessions

A conversation (an A2A `context_id`, or one eval case) maps to a Claude session. A UUID
`context_id` is the session id itself, so callers, transcripts and traces share one id.
Sessions are keyed by `AGENT_NAME` in `app/app_utils/settings.py`: give each agent its
own name; renaming it starts its conversations afresh.

With an authenticated A2A caller, a conversation is scoped to that caller. Without one
(the default on Cloud Run and Agent Runtime), anyone who sends a `context_id` resumes
and reads that conversation: treat it as a secret and never derive it from guessable
values.

Transcripts live on the instance's disk unless a session store is configured, so a
conversation ends when its instance restarts or a turn lands on another one. A bare
deploy scales to zero (`--min-instances 1` keeps one warm); the Agent Runtime engine
`infra single-project` creates keeps one instance always on, billed while idle.

On Cloud Run, scaffold with `--session-type cloud_sql` (or add it later with
`agents-cli scaffold enhance --session-type cloud_sql`): `infra single-project --apply`
then creates a Cloud SQL for PostgreSQL instance (billed while it exists) and wires it
into the service, and transcripts are mirrored to its `claude_session_store` table, so
conversations survive restarts and move between instances. Deployed without `infra`
first, the service has no database. Locally, transcripts stay on disk.

An agent can pass its own `session_store` in `root_agent`; it wins over Cloud SQL. If a
write to the store still fails after the SDK's retries, the turn fails, and the store
keeps the conversation without that turn: later turns, on any instance, resume as if it
didn't happen, though its tool calls did run. Rows are never deleted: schedule a
`DELETE ... WHERE mtime < <cutoff ms>` if transcripts must expire.

At most `CLAUDE_MAX_WORKSPACES` (default 100) conversation directories are kept per
instance; the least recently used beyond that are deleted. Without Cloud SQL an evicted
conversation starts fresh.

### Cloud SQL on Agent Runtime (manual setup)

Agent Runtime has no `/cloudsql` socket, so the engine reaches a private-IP instance
through a PSC interface and the Cloud SQL Python Connector:

1. Network: a VPC with a subnet of at least `/28` in the engine's region, and a network
   attachment on it created with `--connection-preference=ACCEPT_MANUAL`
   (`ACCEPT_AUTOMATIC` fails the deploy). With the user's approval, grant the Vertex AI
   service agent (`service-<project-number>@gcp-sa-aiplatform.iam.gserviceaccount.com`)
   `roles/compute.networkAdmin`.
2. Database: a Cloud SQL for PostgreSQL instance with a private IP on that VPC through
   Private Service Access (`--network=<vpc> --no-assign-ip`; the peering needs
   `roles/servicenetworking.networksAdmin`), a database, and a password user whose
   password is in Secret Manager.
3. Code: `uv add "cloud-sql-python-connector[asyncpg]"`, and in `app/agent.py`:

   ```python
   import asyncio
   import os

   from app.app_utils.session_store import PostgresSessionStore


   def _cloud_sql_store() -> PostgresSessionStore | None:
       if not os.environ.get("INSTANCE_CONNECTION_NAME"):
           return None  # local runs keep transcripts on disk
       state = {}

       async def connect(*_args, **_kwargs):
           from google.cloud.sql.connector import create_async_connector

           # A connector belongs to the event loop that created it.
           if state.get("loop") is not asyncio.get_running_loop():
               state["connector"] = await create_async_connector(
                   ip_type="PRIVATE", refresh_strategy="lazy"
               )
               state["loop"] = asyncio.get_running_loop()
           return await state["connector"].connect_async(
               os.environ["INSTANCE_CONNECTION_NAME"],
               "asyncpg",
               user=os.environ["DB_USER"],
               password=os.environ["DB_PASS"],
               db=os.environ["DB_NAME"],
           )

       return PostgresSessionStore(connect=connect)
   ```

   and pass `session_store=_cloud_sql_store()` to `root_agent`.
4. Deploy. The agent's identity exists only after the first deploy: deploy once, grant
   its principal `roles/cloudsql.client` and `roles/secretmanager.secretAccessor` on the
   secret, then deploy again with:

   ```bash
   agents-cli deploy \
     --network-attachment projects/<project>/regions/<region>/networkAttachments/<name> \
     --env-vars INSTANCE_CONNECTION_NAME=<project>:<region>:<instance>,DB_NAME=<db>,DB_USER=<user>,GOOGLE_API_ENABLE_RUNTIME_BOUND_TOKEN=false \
     --secrets DB_PASS=<secret>
   ```

   `GOOGLE_API_ENABLE_RUNTIME_BOUND_TOKEN=false` is required with Agent Identity: the
   connector's API calls fail with `401` on bound tokens. Later deploys keep the network
   attachment.

## MCP servers and tools

In-process `@tool` functions run in the server process, from the project root, so
relative paths such as `db/products.json` keep working. stdio MCP servers run as
subprocesses: `npx`-based ones need the Node line in the Dockerfile uncommented. Servers
in `agent_config/.mcp.json` load when project settings do; their tools still need to be in
`allowed_tools` (`mcp__<server>__<tool>`).

## Sizing

A tool-using turn takes about 200 MiB for the CLI subprocess plus ~70 MiB for the
server, so the default Cloud Run shape (4 GiB, concurrency 8) fits tool agents. Agents
that work with many files or run long sessions need more: deploy with `--memory 8Gi`
or `--concurrency 4`.

## Eval traces

`agents-cli eval generate` runs each case in process, for `eval grade` and its tool
metrics. A failing case is dropped and reported. `TOOL_USE_QUALITY` errors on cases where
the agent called no tool; grade those in a dataset without it.

## Serving (A2A protocol)

- Entrypoint: `uvicorn app.fast_api_app:app`. JSON-RPC at `POST /a2a/app`; Agent Card at
  `/a2a/app/.well-known/agent-card.json`.
- Keep the card's `description` and skill in `_agent_card()` (`app/app_utils/a2a.py`)
  describing what the agent does: callers read them to decide whether to call it.
- A failed turn ends the A2A task as `failed` with the error text.
- The reply is the text after the last tool call; text before a tool call ("Let me
  check that.") streams but is not part of the reply. With `output_format` set, the
  reply is the structured output as JSON, in one chunk.

On Agent Runtime, `:query` serves `async_create_session` and `:streamQuery` serves
`async_stream_query` (`app/app_utils/reasoning_engine.py`); other methods return 404. A
session id is a conversation id, scoped to the request's `user_id`, which the caller
chooses: anyone allowed to query the engine can resume a conversation whose id and
`user_id` they know. A turn without a session id starts a new conversation. A failed turn
ends with an event carrying `error_code` and `error_message`.

Query a served agent with `--url`:

```bash
agents-cli run --url https://<service-url> --mode a2a --app-name app "hello"
# Agent Runtime: the engine's resource URL; the client finds the passthrough itself
agents-cli run --url https://<location>-aiplatform.googleapis.com/v1/projects/<project>/locations/<location>/reasoningEngines/<id> --mode a2a "hello"
# Agent Runtime, through :streamQuery
agents-cli run --url https://<location>-aiplatform.googleapis.com/v1/projects/<project>/locations/<location>/reasoningEngines/<id> --mode adk "hello"
```

## Calling other agents (A2A)

`remote_agent` in `app/app_utils/remote_agents.py` turns an agent served over A2A
(Claude, ADK or LangChain) into an in-process MCP server with one tool, `ask`:

```python
from app.app_utils.remote_agents import remote_agent

billing = remote_agent(
    "billing",
    "https://billing-xyz.a.run.app/a2a/app",  # where its agent card is served
    description="Answers billing questions.",
)
root_agent = ClaudeAgentOptions(
    ...,
    mcp_servers={"billing": billing},
    allowed_tools=["mcp__billing__ask"],
)
```

- The URL is where the agent card is served: `https://<service>/a2a/app` for an agent
  from this template, the service root for an ADK agent served with `to_a2a`. For an
  Agent Runtime agent, its passthrough URL plus that path:
  `https://<location>-aiplatform.googleapis.com/reasoningEngines/v1/projects/<project>/locations/<location>/reasoningEngines/<id>/api/a2a/app`.
- By default only Google-hosted agents get a token: an OAuth access token for an Agent
  Runtime passthrough URL, a Google ID token for a `https://*.run.app` URL, nothing for
  other hosts. What the caller needs, with the user's approval:
  - Callee on Agent Runtime: `roles/aiplatform.user` in the callee's project (the
    `infra` service account and Agent Identity already have it).
  - Callee on Cloud Run: `roles/run.invoker` on the callee, granted to the caller's
    service account or, from Agent Runtime, to the Agent Identity principal
    `agents-cli deploy` prints. From Agent Runtime also pass `bind_id_token=False`:
    Cloud Run rejects a bound token with a 401.
- `auth=True` sends the ID token to another host (custom domain, load balancer, IAP).
  With user credentials that token works for any Cloud Run service you can call, so
  run as a service account to call a host you don't trust. `auth=False` sends none;
  `headers={...}` adds an API key or your own `Authorization`.
- A failed remote task, transport error or timeout (`timeout=`, default 300 s) reaches
  the model as a tool error, not a failed turn. Follow-ups in the same conversation
  continue the remote conversation.

## Telemetry

`app/app_utils/telemetry/` exports to Cloud Trace, Cloud Logging and Cloud Monitoring
through the Telemetry API, using the standard GenAI span, log and metric names. Each
turn is an `invoke_agent` span, each tool call an `execute_tool` span, and the CLI's own
spans (`claude_code.llm_request` per model call, with tokens) nest under the turn.
Spans and metrics carry `gen_ai.agent.name`, which is `AGENT_NAME`; to rename the
agent, change it and `create_params.root_agent_name` in `agents-cli-manifest.yaml`
together.

Settings (also read from `.env`):

- `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` records prompts, replies
  and tool arguments and results in spans; they are redacted otherwise.
- `OTEL_TRACES_SAMPLER=parentbased_traceidratio` and `OTEL_TRACES_SAMPLER_ARG=0.1`
  sample traces; every request is traced by default.
- `OTEL_SERVICE_NAME` sets the service name (`infra` sets it to the project name).
- `GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=false` turns Cloud export off.
- Setting `CLAUDE_CODE_ENABLE_TELEMETRY`, `OTEL_TRACES_EXPORTER` or
  `OTEL_EXPORTER_OTLP_(TRACES_)ENDPOINT` leaves the CLI's telemetry to you. Never use
  the `console` exporter: it writes into the SDK's message stream.

The project is `GOOGLE_CLOUD_PROJECT`, else the ADC project. The identity needs
`roles/cloudtrace.agent`, `roles/logging.logWriter` and
`roles/monitoring.metricWriter`.

Metrics, as `prometheus.googleapis.com/<name>/histogram`: `gen_ai.invoke_agent.duration`,
`gen_ai.execute_tool.duration`, `gen_ai.invoke_agent.inference_calls`,
`gen_ai.invoke_agent.tool_calls` and `gen_ai.client.token.usage`.

### Prompt-response logging

The `infra single-project` Terraform sets
`OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK=upload` and
`OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH` on the deployed agent, and
`app/app_utils/completions.py` uploads each model call's inputs, outputs and system
prompt as JSONL there, in full whatever the capture setting says. The `completions`
table in the `<project>_telemetry` dataset reads them. A model call's inputs are the turn
so far, without earlier turns. On Agent Runtime the agent's identity needs write access
to the bucket, which a project Editor's `infra` run can't grant (uploads then fail with
a 403).

### BigQuery Agent Analytics (`--bq-analytics`)

With `BQ_ANALYTICS_DATASET_ID` set (the `--bq-analytics` Terraform sets it on the
deployed agent), `app/app_utils/bq_analytics/` writes each turn's events to an
`agent_events` table with BigQuery Agent Analytics' schema, event types and `v_*` views,
so its SDK and Looker dashboards work. With the user's approval,
run `agents-cli infra single-project --apply` before the first deploy: it creates the
dataset and grants write access, which a bare `agents-cli deploy` does not.

- Subagents get their own `AGENT_*` and `LLM_*` rows; tool rows carry `tool_origin`
  (`LOCAL`, `MCP`, `SUB_AGENT`, `A2A`). `INVOCATION_*` rows carry `session_cost_usd`,
  the session's cost so far (don't sum it across turns).
- `session_id` is the A2A `context_id`; `trace_id` joins rows to Cloud Trace.
- `LLM_REQUEST` holds only the inputs new since the last call.
- Prompts, replies and tool arguments and results are logged up to 500 KB per field,
  whatever the span capture setting says. Credentials are redacted: values under keys
  such as `password`, `token`, `api_key` or `authorization`, and `key=value` pairs,
  `Authorization` headers and `Bearer`/`Basic` tokens in text.
- Writes never fail a turn; failures are logged. Uncomment `BQ_ANALYTICS_DATASET_ID` in
  `.env` to log local runs.

## Logs

Python log records, the CLI's stderr included, go to Cloud Logging with their severity
and trace: on Cloud Run to `adk-otel`, on Agent Runtime to
`aiplatform.googleapis.com/reasoning_engine_stdout`. Find a turn's records with
`logName:"adk-otel" AND trace="<trace id>"`. The platform also keeps a plain-text copy
in the container's stderr, without severity. On Agent Runtime:

```bash
gcloud logging read 'resource.type="aiplatform.googleapis.com/ReasoningEngine" AND resource.labels.reasoning_engine_id="ENGINE_ID" AND (log_id("aiplatform.googleapis.com/reasoning_engine_stdout") OR log_id("aiplatform.googleapis.com/reasoning_engine_stderr"))' --project=PROJECT --freshness=1h --limit=50 --format="table(timestamp,log_name.basename(),text_payload)"
```

`ENGINE_ID` is the last segment of the engine's resource name. On Cloud Run filter on
`resource.type="cloud_run_revision" AND resource.labels.service_name="SERVICE"`.

## Pitfalls

- To run the built-in instead of the override: `AGENTS_CLI_DISABLE_OVERRIDES=1 agents-cli <cmd>`.
- Each turn starts a CLI subprocess: allow a few seconds of startup.
- An instance runs a conversation's turns one at a time. Two instances can't coordinate
  this: with more than one instance, send a conversation's turns one at a time, or one
  of two concurrent turns is lost.
- `scaffold enhance` (e.g. `-d cloud_run --cicd-runner github_actions`) re-renders from
  the template in `agents-cli-manifest.yaml` and leaves `app/` alone.
- Deployment depends on `app/fast_api_app.py` exposing `app`; keep that import working.
