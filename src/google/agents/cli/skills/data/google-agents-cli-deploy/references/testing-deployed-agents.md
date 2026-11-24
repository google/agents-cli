# Testing Your Deployed Agent

## Quick Test (Recommended)

The fastest way to test any deployed agent is the `run --url` command — it handles authentication, session creation, and streaming automatically:

```bash
# A2A protocol
agents-cli run --url https://my-agent-abc123.run.app --mode a2a "Hello, what can you do?"

# ADK streaming API (ADK projects only)
agents-cli run --url https://my-agent-abc123.run.app --mode adk "Hello, what can you do?"

# Agent Runtime in Gemini platform (auto-detected from URL — works with either mode)
agents-cli run --url https://LOCATION-aiplatform.googleapis.com/v1/projects/PROJECT/locations/LOCATION/reasoningEngines/ID --mode adk "Hello!"

# Custom auth header (overrides auto-detected credentials)
agents-cli run --url https://my-agent.run.app --mode a2a -H "Authorization: Bearer my-token" "Hello!"
```

The `--mode` flag is required with `--url`: use `a2a` for the A2A protocol, or `adk` for the ADK streaming API (`/run_sse`, or `:streamQuery` for Agent Runtime) if the deployed container serves it. Agent Runtime URLs are detected automatically. Add `-v` for full JSON event payloads.

> Agent Runtime exposes the whole container under an `/api/...` HTTP passthrough (`https://{location}-aiplatform.googleapis.com/reasoningEngines/v1/{resource}/api/<path>`), so the container's own routes — `/run_sse`, `/a2a/{app_name}`, etc. — are also reachable there. (This is separate from the reasoning_engine adapter, which serves only `/api/reasoning_engine` + `/api/stream_reasoning_engine` for the native `:streamQuery` contract.)

Auth is auto-detected via Google Cloud credentials. Use `--header` / `-H` to override.

For more control (scripting, direct curl), see the target-specific sections below.

---

## Agent Runtime deployment

Beyond the `run --url` quick test above, you can query the deployment directly.

**Option 1: Python Script**
```python
import json
import agentplatform

with open("deployment_metadata.json") as f:
    engine_id = json.load(f)["remote_agent_runtime_id"]

client = agentplatform.Client(location="us-east1")
agent = client.runtimes.get(name=engine_id)

async for event in agent.async_stream_query(message="Hello!", user_id="test"):
    print(event)
```

**Option 2: Playground**
```bash
agents-cli playground
```

## Cloud Run Deployment

> **Auth required by default.** Cloud Run deploys with `--no-allow-unauthenticated`, so all requests need an `Authorization: Bearer` header with an identity token. Getting a 403? You're likely missing this header. To allow public access, redeploy with `--allow-unauthenticated`.

### Quickest: `run --url` (direct or via local proxy)

`agents-cli run` mints the identity token for you, so you don't have to construct auth headers. Two ways to point it at the service:

```bash
# Direct: use the Service URL from your deploy output
agents-cli run --url https://SERVICE_NAME-PROJECT_NUMBER.REGION.run.app --mode a2a "Hello!"

# Or proxy locally (the flow gcloud suggests after deploy), then use the proxy URL:
gcloud run services proxy SERVICE_NAME --region REGION --project PROJECT
# in another shell (the proxy holds the terminal, listening on 127.0.0.1:8080):
agents-cli run --url http://127.0.0.1:8080 --mode a2a "Hello!"
```

Pass the **base** service (or proxy) URL — not a `/a2a` suffix; the CLI finds the agent card itself. Swap `--mode a2a` for `--mode adk` to use the ADK HTTP API instead.

> **ADK projects.** The session + `/run_sse` calls below are the ADK HTTP surface. On other frameworks the health check and auth header are the same, but call your app's own routes (e.g. the A2A endpoint via `agents-cli run --mode a2a`).

```bash
SERVICE_URL="https://SERVICE_NAME-PROJECT_NUMBER.REGION.run.app"
AUTH="Authorization: Bearer $(gcloud auth print-identity-token)"

# Test health endpoint
curl -H "$AUTH" "$SERVICE_URL/"

# Step 1: Create a session (required before sending messages)
curl -X POST "$SERVICE_URL/apps/app/users/test-user/sessions" \
  -H "Content-Type: application/json" \
  -H "$AUTH" \
  -d '{}'
# → returns JSON with "id" — use this as SESSION_ID below

# Step 2: Send a message via SSE streaming
curl -X POST "$SERVICE_URL/run_sse" \
  -H "Content-Type: application/json" \
  -H "$AUTH" \
  -d '{
    "appName": "app",
    "userId": "test-user",
    "sessionId": "SESSION_ID",
    "newMessage": {"role": "user", "parts": [{"text": "Hello!"}]}
  }'
```

> **Common mistake:** Using `{"message": "Hello!", "userId": "...", "sessionId": "..."}` returns `422 Field required`. The ADK HTTP server expects the `newMessage` / `parts` schema shown above, and the session must already exist.
>
> **Use the camelCase field names** (`appName`, `userId`, `sessionId`, `newMessage`): Go, Java and TypeScript agents read only those, and Python and Kotlin also accept the snake_case spellings.

## GKE Deployment

GKE LoadBalancer services are **internal by default**. See `references/gke.md` for curl examples and endpoint details.

## Load Tests

See `tests/load_test/README.md` (Python) or `e2e/load_test/README.md` (Go) for configuration, default settings, and CI/CD integration details. Load tests run automatically during the staging CD pipeline stage.
