# Testing Your Deployed Agent

## Quick Test (Recommended)

The fastest way to test any deployed agent is the `run --remote` command — it handles authentication, session creation, and streaming automatically:

```bash
# Test with a single prompt
agents-cli run --remote "Hello, what can you do?"

# Agent Engine: uses deployment_metadata.json (created by deploy)
agents-cli run --remote "Search for flights to NYC"

# Custom metadata file path
agents-cli run --remote --metadata-file path/to/metadata.json "Hello!"
```

Supports **Agent Engine** and **Cloud Run** deployments. GKE is not yet supported via `--remote`.

For more control (custom headers, session reuse, scripting), see the target-specific sections below.

---

## Agent Engine Deployment

**Option 1: Testing Notebook**
```bash
jupyter notebook notebooks/adk_app_testing.ipynb
```

**Option 2: Python Script**
```python
import json
import vertexai

with open("deployment_metadata.json") as f:
    engine_id = json.load(f)["remote_agent_engine_id"]

client = vertexai.Client(location="us-central1")
agent = client.agent_engines.get(name=engine_id)

async for event in agent.async_stream_query(message="Hello!", user_id="test"):
    print(event)
```

**Option 3: Playground**
```bash
agents-cli dev
```

## Cloud Run Deployment

> **Auth required by default.** Cloud Run deploys with `--no-allow-unauthenticated`, so all requests need an `Authorization: Bearer` header with an identity token. Getting a 403? You're likely missing this header. To allow public access, redeploy with `--allow-unauthenticated`.

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
    "app_name": "app",
    "user_id": "test-user",
    "session_id": "SESSION_ID",
    "new_message": {"role": "user", "parts": [{"text": "Hello!"}]}
  }'
```

> **Common mistake:** Using `{"message": "Hello!", "user_id": "...", "session_id": "..."}` returns `422 Field required`. The ADK HTTP server expects the `new_message` / `parts` schema shown above, and the session must already exist.

## GKE Deployment

GKE LoadBalancer services are **internal by default**. See `references/gke.md` for curl examples and endpoint details.

## Load Tests

See `tests/load_test/README.md` for configuration, default settings, and CI/CD integration details. Load tests run automatically during the staging CD pipeline stage.
