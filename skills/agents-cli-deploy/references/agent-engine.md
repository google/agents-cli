# Agent Engine Infrastructure

> **Assumes `/agents-cli-scaffold` scaffolding.** If your project isn't scaffolded yet, see `/agents-cli-scaffold` first.

## Deployment Architecture

Agent Engine uses **source-based deployment** — no Docker container or Dockerfile. Your agent code is packaged as a base64-encoded tarball and deployed directly to the managed Vertex AI service.

**App class:** Your agent extends `AdkApp` (from `vertexai.agent_engines.templates.adk`). Check `agent_engine_app.py` for the exact implementation. Key methods:

- `set_up()` — Initialization (Vertex AI client, telemetry)
- `register_operations()` — Declare operations exposed to Agent Engine
- `register_feedback()` — Collect and log user feedback
- `async_stream_query()` — Streaming response method

## deploy.py CLI

Scaffolded projects deploy via `uv run -m app.app_utils.deploy`. Run `uv run -m app.app_utils.deploy --help` for the full flag reference.

**Deployment flow:**
1. `uv export` generates `.requirements.txt` from lockfile
2. `deploy.py` packages source, creates/updates the Agent Engine instance
3. Writes `deployment_metadata.json` with the engine resource ID

## Terraform Resource

Agent Engine uses `google_vertex_ai_reasoning_engine` in `deployment/terraform/service.tf`. Check that file for current scaling, concurrency, and resource limit settings.

Key difference from Cloud Run: the `lifecycle.ignore_changes` on `source_code_spec` is critical — source code is updated by CI/CD, not Terraform.

## deployment_metadata.json

Written by `deploy.py` after successful deployment:

```json
{
  "remote_agent_engine_id": "projects/PROJECT/locations/LOCATION/reasoningEngines/ENGINE_ID",
  "deployment_target": "agent_engine",
  "is_a2a": false,
  "deployment_timestamp": "2025-02-25T10:30:00.000Z"
}
```

Used by: subsequent deploys (update vs create), testing notebook, `agents-cli run --remote`. Cloud Run does not use this file.

If deployment times out but the engine was created, manually populate this file with the engine resource ID.

## CI/CD Differences from Cloud Run

| Aspect | Agent Engine | Cloud Run |
|--------|-------------|-----------|
| **Build** | `uv export` → requirements file | Docker build → container image |
| **Deploy command** | `uv run -m app.app_utils.deploy` | `gcloud run deploy --image ...` |
| **Artifact** | Base64 source tarball | Container image in Artifact Registry |
| **Python version** | Fixed at 3.12 (Terraform) | Configurable in Dockerfile |
| **Load testing** | Via `locust` against Agent Engine endpoint | Direct HTTP to Cloud Run URL |

## Playground & Remote Testing

```bash
# Local mode (uses local agent instance)
agents-cli dev

# Query your deployed Agent Engine remotely
agents-cli run --remote "Hello, what can you do?"

# Use a custom metadata file path (Agent Engine only)
agents-cli run --remote --metadata-file path/to/deployment_metadata.json "Hello"
```

`--remote` supports **Agent Engine** (reads `deployment_metadata.json`) and **Cloud Run** (discovers service URL via gcloud). GKE is not yet supported.

To query Agent Engine programmatically:

```python
import vertexai

client = vertexai.Client(location="us-central1")
agent = client.agent_engines.get(name="projects/PROJECT/locations/LOCATION/reasoningEngines/ENGINE_ID")

async for event in agent.async_stream_query(message="Hello!", user_id="test"):
    print(event)
```

## Session & Artifact Services

| Service | Configuration | Notes |
|---------|--------------|-------|
| **Sessions** | `InMemorySessionService` (default) | Stateless; state per connection |
| **Sessions** | `VertexAiSessionService` | Native managed sessions (persistent) |
| **Artifacts** | `GcsArtifactService` | Uses `LOGS_BUCKET_NAME` env var |
| **Artifacts** | `InMemoryArtifactService` | Fallback when no bucket configured |

Environment variables set during deployment are configured in `deploy.py` and `deployment/terraform/service.tf`. Check those files for current values.
