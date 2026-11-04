# Agent Runtime infrastructure

> **Assumes `/google-agents-cli-scaffold` scaffolding.** If your project isn't scaffolded yet, see `/google-agents-cli-scaffold` first.

## Deployment Architecture

Agent Runtime in Gemini platform uses **container-based deployment**: `agents-cli deploy` packages your project and Agent Runtime builds a container image from your project's `Dockerfile` (required at the project root; scaffolded projects ship one). To deploy an image you already built instead, pass `agents-cli deploy --image <artifact-registry-uri>`; no `Dockerfile` or source upload is needed. An existing agent can't switch between the two (Terraform-provisioned agents are source builds), so pick one per agent. On the first deploy of a new Agent Identity agent, the image starts once before its env vars and secrets are applied, so it must start without them.

File selection honors the project-root `.gcloudignore`, else the project-root `.gitignore` (nested `.gitignore` files are not consulted).

Each template builds the served application its own way:

- **Python** — the container runs `uvicorn app.fast_api_app:app`, the same entrypoint as Cloud
  Run and GKE. Which routes that app exposes depends on the framework; check
  `app/fast_api_app.py`. There is no top-level `AgentEngineApp`/`AdkApp` deployment
  entrypoint — the container serves HTTP directly.
- **Go** — the container runs the binary compiled from `main.go`, which composes ADK Go's
  launcher sub-commands (`web`, `api`, `a2a`, `appinfo`). Agent Runtime is supported natively,
  so there is no adapter layer.

`agents-cli deploy` labels the deployment with the `framework` recorded in
`agents-cli-manifest.yaml`, and `--framework` overrides it. Terraform has its own copy, the
`agent_framework` default in `deployment/terraform/*/variables.tf`, so editing the manifest
alone leaves the tfvar stale and the next `terraform apply` puts the old label back. Edit
both. The label picks the console playground and decides whether ADK class methods are
declared; it does not constrain the container.

> **ADK Python projects.** `fast_api_app.py` builds the FastAPI `app` via
> `get_fast_api_app(web=True, lifespan=...)`. The lifespan builds one `Runner`
> from the shared session/artifact services (`app_utils/services.py`) and mounts
> A2A routes (`attach_a2a_routes`); `attach_reasoning_engine_routes(app)` adds
> the reasoning_engine contract routes (the adapter constructs an `AdkApp`
> internally to dispatch the native `:streamQuery`/`:query` contract). So the
> container serves the ADK HTTP surface (`/run_sse`, `/apps/...`), the A2A routes
> under `/a2a/{app_name}` (JSON-RPC + agent card), and the reasoning_engine
> adapter routes `/api/reasoning_engine` + `/api/stream_reasoning_engine` (used
> by the console playground and ADK registration with Gemini).

> **ADK Go projects.** `main.go` composes launcher sub-commands instead of building a web
> app. The `agentengine` sub-launcher (`google.golang.org/adk/v2/cmd/launcher/web/agentengine`,
> constructed as `agentengine.NewLauncher(rootAgent.Name())`) is the counterpart
> to Python's reasoning_engine adapter — it is what makes the console playground
> work. The container serves the ADK HTTP surface at the root (hence
> `-path_prefix /`), the agent card at `/.well-known/agent-card.json` and A2A
> JSON-RPC at `/a2a/v1/invoke`. There is no `webui` sub-launcher on this target.

### The `/api` HTTP passthrough

Agent Runtime exposes the container's HTTP routes externally under an
`/api` prefix, so deployed agents are reachable without a public Cloud Run URL:

```
https://{location}-aiplatform.googleapis.com/reasoningEngines/v1/{resource}/api/{container_path}
```

where `{resource}` is the full `projects/.../reasoningEngines/...` name. For
example, the A2A agent card (container route `/a2a/{app_name}/.well-known/agent-card.json`)
is reachable at:

```
https://{location}-aiplatform.googleapis.com/reasoningEngines/v1/{resource}/api/a2a/{app_name}/.well-known/agent-card.json
```

`{app_name}` is `create_params.app_name` in `agents-cli-manifest.yaml`;
projects created before that key existed use the basename of
`agent_directory`, e.g. `my_agent` for `src/my_agent` (`app_name` is recorded
in `deployment_metadata.json`). This is
the exact URL `deploy` advertises on success and `run` constructs for
`--mode a2a` against an Agent Runtime URL — both
authenticate with your Google credentials. On Agent Runtime, `publish` defaults
to **ADK** registration (`:streamQuery` against the reasoning-engine resource
name) rather than this card URL; pass `--registration-type a2a` if your container
serves only A2A.

## Deploying

Deploy with `agents-cli deploy` (run `agents-cli deploy --help` for the full flag reference). CI/CD pipelines invoke the same command.

**Deployment flow:**
1. `agents-cli deploy` packages the project files (honoring `.gcloudignore`/`.gitignore`)
2. Agent Runtime builds the container image and creates/updates the Agent Runtime instance
3. Writes `deployment_metadata.json` with the engine resource ID
4. For A2A projects, copies the served agent card to the engine's `spec.agent_card`, where Agent Registry reads it

## Terraform Resource

Agent Runtime uses `google_vertex_ai_reasoning_engine` in `deployment/terraform/single-project/service.tf` (and the `cicd/service.tf` variant for CI/CD-managed deployments). Check those files for current scaling, concurrency, and resource limit settings.

Key difference from Cloud Run: the `lifecycle.ignore_changes` (covering `container_spec`, `source_code_spec`, `deployment_spec`, and `agent_card`) is critical — the image, source, and A2A agent card are updated by `agents-cli deploy` / CI/CD, not Terraform. The resource uses the `google-beta` provider because only it has `spec.agent_card`.

If your `service.tf` doesn't list `spec[0].agent_card` in `ignore_changes` (projects scaffolded with older agents-cli versions), a `terraform apply` that changes the engine's `spec` clears the agent card, and Agent Registry stops listing the agent as A2A. Run `agents-cli scaffold upgrade` to pick up the updated `service.tf`, or rerun `agents-cli deploy` after such an apply to write the card back.

## deployment_metadata.json

Written by `agents-cli deploy` after a successful deployment:

```json
{
  "remote_agent_runtime_id": "projects/PROJECT/locations/LOCATION/reasoningEngines/ENGINE_ID",
  "deployment_target": "agent_runtime",
  "is_a2a": true,
  "agent_directory": "app",
  "deployment_timestamp": "2025-02-25T10:30:00.000+00:00"
}
```

Used by: subsequent deploys (update vs create), `agents-cli run --url`, and `agents-cli publish` (reads the runtime ID for the default ADK registration on Agent Runtime, and constructs the A2A card URL only when A2A registration is explicitly chosen). Cloud Run does not use this file.

If deployment times out but the engine was created, manually populate this file with the engine resource ID.

## CI/CD Differences from Cloud Run

| Aspect | Agent Runtime | Cloud Run |
|--------|-------------|-----------|
| **Build** | Dockerfile → image (built by Agent Runtime) | Dockerfile → image (`gcloud builds`) |
| **Deploy command** | `agents-cli deploy` | `gcloud run deploy --image ...` |
| **Artifact** | Container image | Container image in Artifact Registry |
| **Language toolchain version** | Configurable in Dockerfile | Configurable in Dockerfile |
| **Load testing** | Scaffolded load test against the Agent Runtime endpoint | Scaffolded load test, direct HTTP to the Cloud Run URL |

## Playground & Remote Testing

```bash
# Local mode (uses local agent instance)
agents-cli playground

# Query your deployed Agent Runtime remotely (ADK projects; use --mode a2a otherwise)
agents-cli run --url https://LOCATION-aiplatform.googleapis.com/v1/projects/PROJECT/locations/LOCATION/reasoningEngines/ID --mode adk "Hello, what can you do?"
```

`--mode` is required with `--url`: use `adk` for the ADK streaming API (`:streamQuery`) or `a2a` for the A2A protocol. Add `-v` for full JSON event payloads. Auth is auto-detected via Google Cloud credentials.

To query Agent Runtime programmatically:

```python
import agentplatform

client = agentplatform.Client(location="us-east1")
agent = client.runtimes.get(name="projects/PROJECT/locations/LOCATION/reasoningEngines/ENGINE_ID")

async for event in agent.async_stream_query(message="Hello!", user_id="test"):
    print(event)
```

## Session & Artifact Services

> **ADK projects.** The two paragraphs below are the ADK scaffold's session and
> artifact wiring. The environment-variable sources at the end of this section
> apply to any framework.

Agent Runtime always uses in-memory sessions at scaffold time; at runtime `app_utils/services.py` upgrades to `VertexAiSessionService` when Agent Runtime injects `GOOGLE_CLOUD_AGENT_ENGINE_ID`. `get_fast_api_app` receives the `shared://session` URI, resolved by `services.py`.

Artifacts use `GcsArtifactService` when `LOGS_BUCKET_NAME` is set, otherwise `InMemoryArtifactService`.

Environment variables set during deployment come from `agents-cli deploy` (the CLI's `deploy/agent_runtime.py`) for SDK deploys, and from `deployment/terraform/single-project/service.tf` (or the `cicd/` variant) for Terraform-managed deploys. Check those for current values.

### Memory Bank

To enable cross-session memory on Agent Runtime, configure `memory_bank_config` via `context_spec`. See the ADK [`cross-session-memory` recipe](https://github.com/google/adk-recipes/tree/main/core/python/cross-session-memory) for the full pattern.

## Networking (PSC Interface)

Agent Runtime cannot reach your VPC by default. To enable private connectivity, create a [network attachment](https://cloud.google.com/vpc/docs/create-manage-network-attachments) and deploy with `--network-attachment`. Add `--dns-peering-domain`, `--dns-peering-project`, and `--dns-peering-network` if you need private DNS resolution. PSC config is immutable after deployment — delete and redeploy to change it. See the [GCP docs](https://cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/private-service-connect-interface) for prerequisites and setup.
