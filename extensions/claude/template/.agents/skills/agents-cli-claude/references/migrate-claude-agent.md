# Migrating an existing Claude Agent SDK agent

Use when the user has a working Claude Agent SDK (Python) agent and wants it served on
GCP. Assume the Claude models it uses are enabled in Model Garden.

Work through the phases in order. Each ends with a check: when a check fails, fix it or
stop and tell the user, before moving on.

## 1. Preflight

- The agent imports `claude_agent_sdk` (Python). A TypeScript agent is not supported:
  stop and say so.
- The agent runs on AWS today (Bedrock settings or model IDs, AgentCore, Lambda, `boto3`
  tools): read `migrate-from-aws.md` now. Its rows join the Assess table, and its edits
  come after the table is shown, before step 4.
- ADC is set up: `gcloud auth application-default login`.
- The deployed agent needs `roles/aiplatform.user`; without it every turn fails with
  `403 Permission 'aiplatform.endpoints.predict' denied`. With Agent Identity,
  `agents-cli deploy` grants it to the agent's principal and prints that principal.
  Otherwise a bare `agents-cli deploy` runs as the project's default compute account,
  which may lack it: plan step 7 with `agents-cli infra single-project --apply` and
  `--service-account`.

Check: `gcloud projects get-iam-policy <project> --flatten="bindings[].members"
--filter="bindings.role:roles/aiplatform.user" --format="value(bindings.members)"`.

## 2. Assess

Read the entrypoint (`main.py`, scripts, notebooks) and fill in this table:

| Finding | Consequence |
|---|---|
| Options built in `main()` and run with `query()` or `ClaudeSDKClient` | Supported: lift them into `root_agent` (step 4) |
| Options depend on the request or user (per-tenant tokens, dynamic prompt) | Not supported as-is: `root_agent` is one static `ClaudeAgentOptions`. Tell the user |
| HTTP routes the code serves (FastAPI, Flask: agent calls, streaming, webhooks, metrics, data lookups, health checks) | Recreate every one with the same method, path, request and response (step 5), whoever calls it: an app, a service, or another agent calling it as a tool. List each route and its callers. The A2A endpoint is added beside them, not instead |
| A route that runs its own `ClaudeAgentOptions` (a summarizer next to the main agent) | Supported: the recreated route runs those options (step 5); `root_agent` is the agent A2A serves |
| Code between turns (interrupts, orchestration of several agents in Python) | Not supported as-is. Tell the user |
| An `mcp_servers` entry with a `command` (`npx`, `uvx`) | That runtime must be in the image (step 5) |
| A browser MCP server (Playwright) | Needs Chromium in the image, which the template does not provide. Tell the user |
| `agents=` or `.claude/agents/*.md` | Needs the `Agent` tool (step 5); `.claude/agents/` files move to `agent_config/` (step 4) |
| `.claude/`, `CLAUDE.md` or `.mcp.json` at the project root | The served agent loads these only from `agent_config/`. Ask the user which parts the agent itself uses (subagents, skills, its instructions, MCP servers) and move those (step 4); development settings (`permissions.allow` rules, hooks, coding notes) stay at the root |
| `cwd=` pointing at data that `Bash` or `Read` use | Tell the user: keeping `cwd` shares one directory across conversations; dropping it leaves those tools an empty workspace |
| Hooks or a `statusLine` the agent needs, in `.claude/settings.json`, that run local commands (`afplay`, `uv run`, a script) | Moved to `agent_config/`, they run in the container: drop the ones the image can't run (step 5). Tell the user |
| A model, or a subagent's `model:`, that isn't enabled in Model Garden | Ask the user to enable it or name a replacement. Don't pick one yourself |
| Calls to other agents (HTTP clients, other services' agents) | If they serve A2A, use the `remote_agent` helper in place of the tool that calls them (step 5). Tell the user |
| Multi-turn conversations | On `cloud_run`, scaffold with `--session-type cloud_sql` to keep them across restarts and instances; on `agent_runtime`, Cloud SQL needs manual setup (step 3) |
| `requires-python` that excludes 3.12 (e.g. `>=3.13`) | The image builds on `python:3.12-slim` and `uv sync` fails there: change it (step 5) |

Check: show the user the filled table and the plan before editing anything.

## 3. Scaffold

Ask the user for the deployment target (`cloud_run` or `agent_runtime`; the template does
not support `gke`) and whether
they want CI/CD, then run from the project root:

```bash
agents-cli scaffold enhance google/agents-cli/extensions/claude/template@v1.14.0 \
  --deployment-target cloud_run
```

Optional: `--name <name>` (default: the directory name), which also names the deployed
service or agent; `--region <region>` (default `us-east1`; `infra` deploys there, so pass
the same one to `deploy`); `--bq-analytics` to log each turn to BigQuery Agent
Analytics; and `--memory-bank` to give the agent memory of each user in Memory Bank.

On `agent_runtime`, `publish gemini`, the Console playground and the Console's
session views do not work, and Cloud SQL sessions need manual network and database setup
(the project's `.agents/skills/agents-cli-claude/references/claude.md`, Sessions): tell
the user before choosing it.

Agent Runtime's managed sessions don't apply to Claude agents. Multi-turn works on every target while a conversation's turns reach the same live
instance, and ends when that instance restarts or a turn lands on another one. On
`cloud_run`, add `--session-type cloud_sql` when the agent holds multi-turn
conversations: Terraform then provisions a Cloud SQL Postgres database the transcripts
are mirrored to, so a conversation survives restarts and moves between instances. Cloud
SQL costs money while it runs.

`enhance` writes the template as `create` would: the A2A server, runtime, a sample
`app/agent.py`, `pyproject.toml`, `Dockerfile`, deployment files, tests and command
overrides. Each user file it replaces (typically `pyproject.toml`, `README.md`, `.env`,
`.gitignore`, `Dockerfile`) moves to `.agents-cli-original/` at the same path and is listed
in the output; every other file stays where it is. Until step 4, the project runs the
template's sample agent. Running `enhance` again re-renders the template without moving
anything.

Check: every file `enhance` listed is under `.agents-cli-original/` (`ls -a`: the folder
is gitignored), and `git status` shows no other user file modified or deleted.

## 4. Port the agent

Replace the template's sample `app/agent.py` with one exporting `root_agent`, the options
object the existing code builds:

```python
# app/agent.py
from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, tool


# In a real repo, import the existing tool instead of defining it here.
@tool("search_products", "Search the catalog.", {"query": str})
async def search_products(args):
    return {"content": [{"type": "text", "text": f"Results for {args['query']}"}]}


root_agent = ClaudeAgentOptions(
    model="claude-haiku-4-5",
    system_prompt="You help customers find products.",
    mcp_servers={
        "products": create_sdk_mcp_server(name="products", tools=[search_products])
    },
    tools=[],
    allowed_tools=["mcp__products__search_products"],
)
```

- Move the fields from the original `ClaudeAgentOptions(...)` unchanged: `model`,
  `system_prompt`, `tools`, `allowed_tools`, `mcp_servers`, `agents`, `hooks`,
  `can_use_tool`, `permission_mode`, `setting_sources`, `env`.
- When the original leaves `tools` unset, set it to the built-in tools `allowed_tools`
  names (bare names, without a `(...)` rule), plus `"Agent"` with subagents, or `[]`:
  unset gives the agent every built-in Claude Code tool.
- When `app/agent.py`, or a module it imports, reads `.env` values at import
  (`os.environ["WEATHER_URL"]`), call `load_dotenv()` before those imports (mark the
  imports after it `# noqa: E402`): `tests/unit` imports it before anything else loads
  `.env`.
- Import existing tool functions and servers. Do not rewrite them. In-process tools
  (`@tool`, `create_sdk_mcp_server`) run from the project root, so their relative paths
  still work; only Claude Code's built-in tools (`Read`, `Bash`) run in a
  per-conversation workspace.
- **Keep the user's scripts and modules.** Delete only the deployment files below, after
  the user agrees.
- Never change the model.
- Do not add `cwd`, `verbatim_prompts` or other server settings: the scaffolded runtime
  sets them.

Then carry over what the template replaced, from `.agents-cli-original/`:

- Dependencies: `uv add` each runtime dependency of the original project that the
  template's `pyproject.toml` lacks, with its original spec, from the moved
  `pyproject.toml` (`[project]`, or `[tool.poetry.dependencies]` without `python`, with
  `^`/`*` translated), `requirements.txt` or `setup.py`. Don't carry over specs for
  `claude-agent-sdk`, `fastapi`, `uvicorn` or `a2a-sdk`: the template's code needs its own
  ranges, and an old pin such as `claude-agent-sdk==0.1.30` resolves without an error and
  breaks at runtime. `uv add -r requirements.txt` rewrites those specs too: note them in
  `pyproject.toml` first, and `uv add` each one the file named back afterwards.
- `.env`: add the values the agent reads to the template's `.env`, and keep its
  `GOOGLE_CLOUD_PROJECT`, `GOOGLE_GENAI_USE_ENTERPRISE` and `GOOGLE_CLOUD_LOCATION` lines,
  or `agents-cli eval` can't grade ("No API key was provided"). Set `GOOGLE_CLOUD_PROJECT`
  to the deploy project. `deploy` copies `.env` to the service, so don't carry over
  `ANTHROPIC_VERTEX_PROJECT_ID`, which sends model calls to another project, or
  `ANTHROPIC_API_KEY`, which the agent doesn't use. Check your shell too (`env | grep -E
  '^(ANTHROPIC_|CLAUDE_CODE_USE_)'`): shell values win over `.env` locally.
- `README.md`: move the user's back over the template's. `.gitignore`: append the user's
  lines to the template's.
- `Dockerfile`: keep the template's, which serves the agent; add any system packages the
  user's installed.
- Anything else listed: port what still applies.
- Claude Code files the agent uses (the Assess row on `.claude/`): copy them from the
  project root into `agent_config/`, which is the only place the served agent loads
  them from, and leave the root files for the developer's coding assistant.

Files outside the folder that only the old deployment reads (its infrastructure, its
platform's entrypoint, CI that deploys there, e.g. `fly.toml`, `render.yaml`): list them
and ask the user before deleting. `enhance`'s backup in `~/.agents-cli/backups/` and git
history keep them. On AWS, follow `migrate-from-aws.md` step 3.

Then delete `.agents-cli-original/`; `enhance`'s backup keeps a copy, `.env` included.

Check: `uv run python -c "from app.agent import root_agent; print(type(root_agent).__name__)"`
prints `ClaudeAgentOptions`, `agents-cli install` succeeds, `.agents-cli-original/` is
gone, and `git status` shows no deleted user file besides the ones the user agreed to.

## 5. Tweaks

Apply the rows step 2 found, and the `packages` row when step 4 finds it:

| Finding | Edit |
|---|---|
| stdio MCP server started with `npx` | Uncomment the Node.js install line in `Dockerfile` |
| Hooks that run local commands | Remove those entries from `agent_config/.claude/settings.json`; keep the rest of the file |
| `requires-python` excludes 3.12 | Set `FROM python:3.12-slim` in `Dockerfile` to a version it allows, e.g. `python:3.13-slim` |
| The moved `pyproject.toml` listed packages besides `app` in `[tool.hatch.build.targets.wheel] packages` | Add them to that list in `pyproject.toml`, and `COPY ./<package> ./<package>` for each before `RUN uv sync --frozen` in `Dockerfile`, or the image build can't find them |
| Subagents | Add `"Agent"` to `tools` (when it is a list) and to `allowed_tools`; older SDKs called it `"Task"`, so replace that |
| Calls to another A2A agent | `remote_agent("<name>", "<url>", description="<the original tool's description>")` from `app/app_utils/remote_agents.py` in `mcp_servers` (the description tells the model when to call it), `mcp__<name>__ask` in `allowed_tools`; the caller needs `roles/run.invoker` on a Cloud Run target, `roles/aiplatform.user` for an Agent Runtime target (URL: its `.../reasoningEngines/<id>/api/a2a/app` passthrough); a caller on Agent Runtime calling a Cloud Run target also passes `bind_id_token=False`; a token goes by default only to Agent Runtime and `*.run.app` URLs, so a Cloud Run target behind a custom domain passes `auth=True` |

Always describe the agent in `_agent_card()` in `app/app_utils/a2a.py`: callers read the
card to decide what the agent does, and the template's is generic. Set the card's
`description` and the skill's `description` (optionally its `name` and `tags`) from the
original agent's purpose and system prompt. Keep `name` and the URLs: the name is
`A2A_NAME`, also the endpoint path `/a2a/app` that `agents-cli run --url` and other
agents call.

Check: with `uv run uvicorn app.fast_api_app:app --port 8080` running, `curl -s
localhost:8080/a2a/app/.well-known/agent-card.json` shows the new descriptions.

### Keep the existing endpoints

Recreate every route the Assess table lists in a new `app/routes.py` (an `APIRouter`),
so its callers keep working unchanged: same method, path, request and response models,
status codes and headers. Then, in `app/fast_api_app.py`, add
`from app.routes import router` to its imports and `app.include_router(router)` after
`app = FastAPI(...)`: its only change. `scaffold upgrade` never touches `app/routes.py`,
which the template doesn't ship.

How to recreate a route depends on what it does:

| The original route | Recreate it by |
|---|---|
| Doesn't run the agent (data lookups, feedback, metrics export, health and readiness checks, auth or signature checks) | Copying its handler as it is, importing the user's modules |
| Runs a turn and returns a result | Replacing the SDK client calls with `reply = await run_turn(prompt, options, conversation_id)` from `app/app_utils/claude.py`, then building the original response from `reply` (below) |
| Streams a turn (SSE, chunked) | Iterating `stream_turn(prompt, options, conversation_id)` from `app/app_utils/claude.py` and sending `text_delta(message)` from `app/app_utils/replies.py` in the original event format; when the route also needs the turn's result, start from `reply = TurnReply()` and fold in each message with `reply = record(reply, message)`, both from `replies.py` |
| Runs a turn after it responds (a webhook that acknowledges, then posts the answer) | The same `run_turn`, in the same background task |

What `reply` (a `TurnReply`) holds, for responses, metrics and logs: `reply.text`, the
final answer; `reply.tool_calls`, one dict per call with `name`, `args` and `result`;
`reply.result`, the turn's `ResultMessage`, with `usage` (`input_tokens`,
`output_tokens`) and `total_cost_usd`.

- `options` is `root_agent`, or the route's own `ClaudeAgentOptions` when it ran other
  options: imported from the user's module, or copied when the handler built them inline.
- The conversation id resumes a conversation: pass the caller's, or a new one when the
  original route created it, or a new one per call when the route holds none. Drop
  per-process client maps (a dict of `ClaudeSDKClient`s): the runtime keeps the
  transcript under that id.
- `run_turn` raises `AgentError` (also from `app/app_utils/claude.py`) when a turn ends in
  an error. Catch it where the original returned an error in its response body or with
  another status code.
- State the original server kept besides clients (a history dict a route returns,
  in-process counters, files it writes such as `feedback.jsonl`): keep its code. Tell the
  user each one lives on one instance and is lost on restart, and that a route reading it
  can miss what another instance served; moving it to a shared store is their call.
- Secrets a route reads (a webhook signing secret): export them in your shell for local
  runs, not in `.env`, which `deploy` copies into the service's environment. Tell the user
  to pass them at deploy with `agents-cli deploy --secrets ENV=SECRET`.
- Keep a health route callers or probes use (`GET /healthz`) even though the template
  serves `/health`.
- The original server module is no longer served: ask the user before deleting it, as
  with the deployment files (step 4).

Check: with `uv run uvicorn app.fast_api_app:app --port 8080` running, send each route a
request taken from its callers (README, client code, tests), including a failing one
where the original handled errors, and compare the response's status, headers and fields
with the original server's.

Leave the rest of `app/app_utils/` alone: it is the runtime that serves the agent.

## 6. Verify

```bash
agents-cli install
agents-cli lint
uv run pytest tests/unit
agents-cli run "<a prompt from the user's README or scripts>"
uv run pytest tests/integration
```

Fix lint in the files you wrote or edited with `uv run ruff check --fix app && uv run
ruff format app`. If `agents-cli lint` flags the user's original files (scripts,
notebooks, parked files), add them to `[tool.ruff] extend-exclude`, `[tool.ty.src]
exclude` (e.g. `"bookshop/**"`) and, for spelling, `[tool.codespell] skip` instead of
restyling them. If it flags the template's files because the user's own `[tool.ruff]`
settings differ (e.g. `line-length`), format only those files once: `uv run ruff format
app/app_utils app/fast_api_app.py scripts tests`.

Check: run two or three of the user's own prompts. The replies must come from the user's
agent (its persona, its tools), not the template's sample weather agent, and match what
the original script answers.

## 7. Deploy

Ask the user before running `infra single-project --apply` or `deploy`: both create billed
resources and IAM bindings.

```bash
agents-cli infra single-project --apply --project <gcp-project>
agents-cli deploy --project <gcp-project> --region <region> \
  --service-account <project-name>-app@<gcp-project>.iam.gserviceaccount.com
agents-cli run --url <service-url> --mode a2a --app-name app "<the same prompt>"
```

On projects deploying to `agent_runtime` with Agent Identity, skip the service account.
Still run `infra single-project --apply` first when the project has `--bq-analytics`: it
creates the Agent Runtime agent (named after the project), the dataset and the agent's
BigQuery roles, and `deploy` then updates that same agent. Query it with
`--url https://<region>-aiplatform.googleapis.com/v1/<Agent Runtime ID>`, the resource
name `deploy` prints.

`agents-cli deploy` copies `.env` into the agent's environment. CI/CD deploys have no
`.env`: add the values the agent needs (e.g. remote agent URLs) to the pipeline's
`--update-env-vars`.

The recreated routes keep their paths, but each caller still needs the new base URL
and a credential. On `cloud_run`, the service URL, `roles/run.invoker` and an ID token
for the service. On `agent_runtime`, the passthrough
`https://<region>-aiplatform.googleapis.com/reasoningEngines/v1/<Agent Runtime ID>/api`
(the resource name `deploy` prints) followed by the route's path (e.g. `.../api/chat`),
`roles/aiplatform.user` and an OAuth access token. A caller that can't send a Google token (a Slack or Stripe webhook, a
Prometheus scrape) needs, on `cloud_run`, a service that allows unauthenticated access,
or a proxy in front of it; on `agent_runtime`, which takes no unauthenticated calls, a
proxy. Give the user that list, route by route, for each caller's owner.

Check: the deployed agent gives the same answers as step 6, through A2A and through each
recreated route.

## Troubleshooting

| Issue | Solution |
|---|---|
| `403 Permission 'aiplatform.endpoints.predict' denied` | Deploy with a service account that has `roles/aiplatform.user` (step 1) |
| `infra single-project` fails on `logging.sinks.create` or `storage.buckets.setIamPolicy` (a project Editor) | Only the prompt-response logging export needs those. If the service account, dataset and engine or service exist, deploy anyway; a project owner can rerun `infra` later |
| `infra single-project` fails with `constraints/sql.restrictPublicIp` (`--session-type cloud_sql`) | The organization forbids public-IP Cloud SQL. In `deployment/terraform/single-project/service.tf`, give the instance `ip_configuration { ipv4_enabled = false, private_network = <VPC> }` and the Cloud Run service `vpc_access { network_interfaces { network = <VPC>, subnetwork = <subnet> }, egress = "PRIVATE_RANGES_ONLY" }`, then rerun `infra`. The VPC needs Private Service Access, which needs `roles/servicenetworking.networksAdmin` to set up |
| `MCP server '<name>' runs 'npx', which is not installed here` | Uncomment the Node.js line in `Dockerfile` |
| Agent answers without using its tools | Check `allowed_tools` names: `mcp__<mcp_servers key>__<tool>` |
| Subagent never called | Add `"Agent"` to `tools` and `allowed_tools` |
| `agents-cli install` finds no solution for `claude-agent-sdk` | A spec carried over from the original project is older than the template needs (`>=0.2.158`). Restore the template's (step 4) |
| Cloud Build fails at `uv sync --frozen` with no lock file | `.gitignore` excludes `uv.lock`, and `.gcloudignore` includes `.gitignore`. Remove `uv.lock` from `.gitignore` |
| Cloud Build fails to download packages | `uv.lock` points at a private index. Re-lock with `uv lock --default-index https://pypi.org/simple` |
