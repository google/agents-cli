---
name: google-agents-cli-publish
description: >
  This skill should be used when the user wants to "publish an agent",
  "publish my ADK agent", "register an agent with Gemini Enterprise",
  "publish to Gemini Enterprise", or needs guidance on the agents-cli
  publish gemini-enterprise command.
  Covers ADK vs A2A registration modes, programmatic and interactive usage,
  flag reference, auto-detection from deployment metadata, and troubleshooting.
  Part of the Google ADK (Agent Development Kit) skills suite.
  Do NOT use for deployment (use google-agents-cli-deploy).
metadata:
  author: Google
  license: Apache-2.0
  version: 0.0.5
  requires:
    bins:
      - agents-cli
    install: "uv tool install google-agents-cli"
---

# Gemini Enterprise Registration

> **Requires:** A deployed agent. For Agent Engine, `deployment_metadata.json` (created by `agents-cli deploy`) enables auto-detection. For Cloud Run or GKE, provide the agent card URL and flags directly.

## Prerequisites

1. **Agent must be deployed** — the agent must be running and reachable
2. **Gemini Enterprise app must exist** — Create one in Google Cloud Console → Gemini Enterprise → Apps before registering
3. **`deployment_metadata.json`** (Agent Engine only) — Created automatically by `agents-cli deploy`; contains the agent engine ID, deployment target, and A2A flag

## Required Permissions for A2A on Cloud Run

- **`roles/run.servicesInvoker`** granted to the Discovery Engine service account (`service-<PROJECT_NUMBER>@gcp-sa-discoveryengine.iam.gserviceaccount.com`) on the Cloud Run service.

---

## Registration Modes

### ADK Registration (default)

For standard ADK agents deployed to Agent Engine. The agent is registered directly via its reasoning engine resource name.

```bash
agents-cli publish gemini-enterprise \
  --agent-engine-id projects/123456/locations/us-east1/reasoningEngines/789 \
  --gemini-enterprise-app-id projects/123456/locations/global/collections/default_collection/engines/my-app \
  --display-name "My Agent" \
  --description "Handles customer queries" \
  --tool-description "Answers questions about products"
```

### A2A Registration

For agents using the Agent-to-Agent protocol. Requires an agent card URL — the command fetches the card and registers it.

```bash
# A2A on Cloud Run
agents-cli publish gemini-enterprise \
  --registration-type a2a \
  --agent-card-url https://my-service-abc123.us-east1.run.app/a2a/app/.well-known/agent-card.json \
  --gemini-enterprise-app-id projects/123456/locations/global/collections/default_collection/engines/my-app \
  --display-name "My A2A Agent"

# A2A on Agent Engine (card URL is auto-constructed from metadata)
agents-cli publish gemini-enterprise \
  --registration-type a2a \
  --gemini-enterprise-app-id projects/123456/locations/global/collections/default_collection/engines/my-app
```

---

## Programmatic Mode (CI/CD)

The command is non-interactive by default — pass all required values via flags or environment variables. This makes it safe for CI/CD pipelines.

### Via flags

```bash
agents-cli publish gemini-enterprise \
  --agent-engine-id "$AGENT_ENGINE_ID" \
  --gemini-enterprise-app-id "$GEMINI_ENTERPRISE_APP_ID" \
  --display-name "Production Agent" \
  --registration-type adk
```

### Via environment variables

Every flag has an env var alternative:

```bash
export AGENT_ENGINE_ID="projects/123456/locations/us-east1/reasoningEngines/789"
export GEMINI_ENTERPRISE_APP_ID="projects/123456/locations/global/collections/default_collection/engines/my-app"
export GEMINI_DISPLAY_NAME="Production Agent"
export GEMINI_DESCRIPTION="Handles customer queries"

agents-cli publish gemini-enterprise
```

---

## Interactive Mode (`--interactive`)

Pass `--interactive` (or `-i`) to be guided through any missing values with interactive prompts. The command will list available Gemini Enterprise apps, offer to auto-detect the agent engine ID from metadata, and prompt for display name and description.

```bash
agents-cli publish gemini-enterprise --interactive
```

---

## Complete Flag Reference

| Flag | Env Var | Description |
|------|---------|-------------|
| `--agent-engine-id` | `AGENT_ENGINE_ID` | Agent Engine resource name (auto-detected from `deployment_metadata.json`) |
| `--gemini-enterprise-app-id` | `ID` or `GEMINI_ENTERPRISE_APP_ID` | Gemini Enterprise app full resource name |
| `--display-name` | `GEMINI_DISPLAY_NAME` | Display name in Gemini Enterprise |
| `--description` | `GEMINI_DESCRIPTION` | Agent description |
| `--tool-description` | `GEMINI_TOOL_DESCRIPTION` | Tool description (ADK mode only, defaults to description) |
| `--registration-type` | `REGISTRATION_TYPE` | `adk` or `a2a` (auto-detected from metadata if not set) |
| `--agent-card-url` | `AGENT_CARD_URL` | Agent card URL for A2A registration |
| `--deployment-target` | `DEPLOYMENT_TARGET` | `agent_engine`, `cloud_run`, or `gke` (affects A2A auth method) |
| `--project-id` | `GOOGLE_CLOUD_PROJECT` | GCP project ID for billing |
| `--project-number` | `PROJECT_NUMBER` | GCP project number (used for Gemini Enterprise lookup) |
| `--authorization-id` | `GEMINI_AUTHORIZATION_ID` | OAuth authorization resource name |
| `--metadata-file` | — | Path to deployment metadata (default: `deployment_metadata.json`) |
| `--interactive` / `-i` | — | Enable interactive prompts |

---

## Auto-Detection from Metadata

When `deployment_metadata.json` exists, the command automatically:

- Reads the **agent engine ID** (`remote_agent_engine_id`)
- Detects the **registration type** (`is_a2a` flag)
- Constructs the **agent card URL** for A2A agents on Agent Engine
- Determines the **deployment target** for authentication

This means that for the simplest case (ADK agent on Agent Engine), you only need to provide the Gemini Enterprise app ID:

```bash
agents-cli publish gemini-enterprise \
  --gemini-enterprise-app-id projects/123456/locations/global/collections/default_collection/engines/my-app
```

---

## SDK Compatibility

Agent Engine deployments may encounter "Session not found" errors with `google-cloud-aiplatform` versions <= 1.128.0. In interactive mode (`--interactive`), the command checks the SDK version from `uv.lock` and offers to upgrade. In programmatic mode, ensure your SDK is up to date before registering.

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Session not found" after registration | SDK version issue — upgrade `google-cloud-aiplatform` (see SDK Compatibility above), redeploy, then re-register |
| `--registration-type is required` | Non-interactive mode needs `--registration-type` when no `deployment_metadata.json` exists |
| "Gemini Enterprise App ID is required" | Provide `--gemini-enterprise-app-id` or set the `ID` / `GEMINI_ENTERPRISE_APP_ID` env var |
| "Agent already registered" | The command automatically updates the existing registration — this is not an error |
| HTTP 403 on registration | Check that your account has Discovery Engine Editor permissions on the Gemini Enterprise project |
| "Could not fetch agent card" | Verify the agent is running and the URL is correct; for Cloud Run, ensure `gcloud auth login` is done |

---

## Related Skills

- `/google-agents-cli-deploy` — Deployment targets, CI/CD pipelines, and production workflows
- `/google-agents-cli-workflow` — Development workflow, coding guidelines, and operational rules
- `/google-agents-cli-scaffold` — Project creation and enhancement with `agents-cli scaffold create` / `scaffold enhance`
