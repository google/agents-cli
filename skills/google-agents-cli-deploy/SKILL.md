---
name: google-agents-cli-deploy
description: >
  This skill should be used when the user wants to "deploy an agent",
  "deploy my ADK agent", "set up CI/CD", "configure secrets",
  "troubleshoot a deployment", or needs guidance on Agent Engine,
  Cloud Run, or GKE deployment targets.
  Covers deployment workflows, service accounts, rollback, and production infrastructure.
  Part of the Google ADK (Agent Development Kit) skills suite.
  Do NOT use for API code patterns (use google-agents-cli-adk-code), evaluation
  (use google-agents-cli-eval), or project scaffolding (use google-agents-cli-scaffold).
metadata:
  author: Google
  license: Apache-2.0
  version: 0.0.5
  requires:
    bins:
      - agents-cli
    install: "uv tool install google-agents-cli"
---

# ADK Deployment Guide

> **Requires:** `agents-cli` (`uv tool install google-agents-cli`) — [install uv](https://docs.astral.sh/uv/getting-started/installation/index.md) first if needed.

> Prefer using the `agents-cli` commands throughout this guide — they wrap Terraform, Docker, and deployment into a tested pipeline. If your project isn't scaffolded yet, see `/google-agents-cli-scaffold` to add deployment support first.

### Reference Files

For deeper details, consult these reference files in `references/`:

- **`cloud-run.md`** — Scaling defaults, Dockerfile, session types, networking
- **`agent-engine.md`** — deploy.py CLI, AdkApp pattern, Terraform resource, deployment metadata, CI/CD differences
- **`gke.md`** — GKE Autopilot cluster, Kubernetes manifests, Workload Identity, session types, networking
- **`terraform-patterns.md`** — Custom infrastructure, IAM, state management, importing resources
- **`batch-inference.md`** — BigQuery Remote Function trigger; for Pub/Sub / Eventarc see `/google-agents-cli-adk-code`
- **`cicd-pipeline.md`** — Full CI/CD pipeline setup, `infra cicd` flags, runner comparison, WIF auth, pipeline stages
- **`testing-deployed-agents.md`** — Testing instructions per deployment target, curl examples, load tests

> **Observability:** See the `/google-agents-cli-observability` skill for Cloud Trace, prompt-response logging, BigQuery Analytics, and third-party integrations.

---

## Deployment Target Decision Matrix

Choose the right deployment target based on your requirements:

| Criteria | Agent Engine | Cloud Run | GKE |
|----------|-------------|-----------|-----|
| **Languages** | Python | Python | Python (+ others via custom containers) |
| **Scaling** | Managed auto-scaling (configurable min/max, concurrency) | Fully configurable (min/max instances, concurrency, CPU allocation) | Full Kubernetes scaling (HPA, VPA, node auto-provisioning) |
| **Networking** | VPC-SC and PSC supported | Full VPC support, direct VPC egress, IAP, ingress rules | Full Kubernetes networking|
| **Session state** | Native `VertexAiSessionService` (persistent, managed) | In-memory (dev), Cloud SQL, or Agent Engine session backend | In-memory (dev), Cloud SQL, or Agent Engine session backend |
| **Batch/event processing** | Not supported | Native trigger endpoints (Pub/Sub, Eventarc); see `/google-agents-cli-adk-code` | Custom (Kubernetes Jobs, Pub/Sub) |
| **Cost model** | vCPU-iours + memory-iours (not billed when idle) | Per-instance-second + min instance costs | Node pool costs (always-on or auto-provisioned) |
| **Setup complexity** | Lower (managed, purpose-built for agents) | Medium (Dockerfile, Terraform, networking) | Higher (Kubernetes expertise required) |
| **Best for** | Managed infrastructure, minimal ops | Custom infra, event-driven workloads | Full Kubernetes control |

**Ask the user** which deployment target fits their needs. Each is a valid production choice with different trade-offs.

> **Ambient / scheduled / event-driven agents:** Agent Engine does not support Pub/Sub, Eventarc, or Cloud Scheduler triggers. Use **Cloud Run** (recommended) or **GKE** for these workloads. See `/google-agents-cli-adk-code` Section 12 for the `trigger_sources` pattern.

> **OAuth / user consent agents:** Use **Agent Engine** with Gemini Enterprise for agents that need OAuth 2.0 user consent (e.g., accessing Google Drive, Calendar, or other user-scoped APIs). Cloud Run does not currently support managed OAuth flows. See the `adk-ae-oauth` sample in `/google-agents-cli-workflow` Phase 2.

---

## Deploying to Dev

### Deploy Workflow

**Task tracking:** Deployment involves multiple sequential steps (infra setup, CI/CD configuration, deploy, verification). Use a task list to track progress through these steps — skipping one often causes failures in later steps that are hard to trace back.

1. If prototype (no deployment target), first enhance: `agents-cli scaffold enhance . --deployment-target <target>`
2. **Notify the human**: "Eval scores meet thresholds and tests pass. Ready to deploy to dev?"
3. **Wait for explicit approval**
4. Once approved: `agents-cli deploy`

> **Agent Engine timeout recovery:** Agent Engine deploys can take 5-10 minutes and may exceed command timeouts. If the deploy command is cancelled or times out, the deployment continues server-side. Run `agents-cli deploy --status` to check progress — poll every 60 seconds until it reports completion or failure.

**IMPORTANT**: Never run `agents-cli deploy` without explicit human approval.

> **Do NOT run `agents-cli infra single-project` before deploying.** It is not a prerequisite — `agents-cli deploy` works on its own. Run it separately if the user needs observability features (prompt-response logging, BigQuery analytics) — see `/google-agents-cli-observability`.

### Single-Project Infrastructure Setup (Optional — Advanced)

`agents-cli infra single-project` runs `terraform apply` in `deployment/terraform/single-project/`. Use this to **provision single-project GCP infrastructure without CI/CD** (service accounts, IAM bindings, telemetry resources, Artifact Registry). Also useful to test things in a single project before going to production. It is NOT required for deploying.

```bash
# Optional — provision infrastructure in a single GCP project
agents-cli infra single-project
```

> **Note:** `agents-cli deploy` doesn't automatically use the Terraform-created `app_sa`. Pass the service account via `agents-cli deploy --service-account SA_EMAIL` or `uv run -m app.app_utils.deploy --service-account SA_EMAIL` for Agent Engine targets.

### Deploy Flag Reference

| Flag | Description | Targets |
|------|-------------|---------|
| `--project` | GCP project ID | All |
| `--region` | GCP region | All |
| `--service-account` | Service account email for the deployed agent | All |
| `--secrets` | Comma-separated `ENV=SECRET` or `ENV=SECRET:VERSION` pairs | Agent Engine |
| `--update-env-vars` | Comma-separated `KEY=VALUE` environment variables | Agent Engine, Cloud Run |
| `--agent-identity` | Enable [agent identity](https://cloud.google.com/vertex-ai/generative-ai/docs/agent-engine/agent-identity) (Preview) | Agent Engine |
| `--memory` | Memory limit (default: `4Gi`) | Cloud Run |
| `--port` | Container port | Cloud Run |
| `--iap` | Enable Identity-Aware Proxy | Cloud Run |
| `--image` | Container image URI (skips source build) | Cloud Run, GKE |
| `--no-wait` | Start deployment and return immediately | Agent Engine, Cloud Run |
| `--status` | Check the status of a pending `--no-wait` deployment | Agent Engine, Cloud Run |
| `--list` | List existing deployments and exit | All |
| `--dry-run` / `-n` | Print what would be executed without running it | All |
| `--no-confirm-project` | Skip project confirmation prompt | All |

Run `agents-cli deploy --help` for the full flag reference. Cloud Run also accepts extra `gcloud` flags after `--` (e.g., `-- --timeout=600`).

> **Project Confirmation:** If the project is resolved automatically (not passed via `--project`), the command will prompt for confirmation in interactive mode. Since agents typically run in non-interactive mode, you MUST pass `--no-confirm-project` to proceed if you are relying on automatic project resolution.

---

## Production Deployment — CI/CD Pipeline

For the full CI/CD pipeline setup guide — prerequisites, `infra cicd` flags, runner comparison, WIF authentication, pipeline stages, and production approval — see `references/cicd-pipeline.md`.

---

## Cloud Run Specifics

For detailed infrastructure configuration (scaling defaults, Dockerfile, FastAPI endpoints, session types, networking), see `references/cloud-run.md`. For ADK docs on Cloud Run deployment, fetch `https://adk.dev/deploy/cloud-run/index.md`.

For event-driven / ambient agent deployment on Cloud Run, see the [`ambient-expense-agent`](https://github.com/google/adk-samples/tree/main/python/agents/ambient-expense-agent) sample and `/google-agents-cli-adk-code` for the `trigger_sources` pattern.

---

## Agent Engine Specifics

Agent Engine is a managed Vertex AI service for deploying Python ADK agents. Uses source-based deployment (no Dockerfile) via `deploy.py` and the `AdkApp` class.

> **No `gcloud` CLI exists for Agent Engine.** Deploy via `agents-cli deploy` or `deploy.py`. Query via the Python `vertexai.Client` SDK.

Deployments can take 5-10 minutes. Use `--no-wait` to start a deployment and return immediately, then check on it later with `--status`:

```bash
# Start deployment without blocking
agents-cli deploy --no-wait

# Check on progress later
agents-cli deploy --status
```

When `--status` detects the operation has completed, it writes `deployment_metadata.json` and prints the same success output as a normal deploy.

For detailed infrastructure configuration (deploy.py flags, AdkApp pattern, Terraform resource, deployment metadata, session/artifact services, CI/CD differences), see `references/agent-engine.md`. For ADK docs on Agent Engine deployment, fetch `https://adk.dev/deploy/agent-engine/index.md`.

---

## GKE Specifics

For detailed infrastructure configuration (Kubernetes manifests, Terraform resources, Workload Identity, session types, networking), see `references/gke.md`. For ADK docs on GKE deployment, fetch `https://adk.dev/deploy/gke/index.md`.

---

## Service Account Architecture

Scaffolded projects use two service accounts:

- **`app_sa`** (per environment) — Runtime identity for the deployed agent. Roles defined in `deployment/terraform/iam.tf`.
- **`cicd_runner_sa`** (CI/CD project) — CI/CD pipeline identity (GitHub Actions / Cloud Build). Lives in the CI/CD project (defaults to prod project), needs permissions in **both** staging and prod projects.

Check `deployment/terraform/iam.tf` for exact role bindings. Cross-project permissions (Cloud Run service agents, artifact registry access) are also configured there.

**Common 403 errors:**
- "Permission denied on Cloud Run" → `cicd_runner_sa` missing deployment role in the target project
- "Cannot act as service account" → Missing `iam.serviceAccountUser` binding on `app_sa`
- "Secret access denied" → `app_sa` missing `secretmanager.secretAccessor`
- "Artifact Registry read denied" → Cloud Run service agent missing read access in CI/CD project

---

## Required Permissions for CI/CD Setup

- **`roles/secretmanager.admin`** granted to the Cloud Build service account (`service-<PROJECT_NUMBER>@gcp-sa-cloudbuild.iam.gserviceaccount.com`) in the CI/CD project. This allows Cloud Build to access the GitHub token stored in Secret Manager.

---

## Required APIs

The following Google Cloud APIs must be enabled in your project for the skills and deployment to work:

- **`cloudbuild.googleapis.com`** — Required for building container images and running CI/CD pipelines.
- **`secretmanager.googleapis.com`** — Required for managing secrets and API keys.
- **`run.googleapis.com`** — Required for deploying to Cloud Run.

Ensure these are enabled before running deployment or CI/CD setup commands:
```bash
gcloud services enable cloudbuild.googleapis.com secretmanager.googleapis.com run.googleapis.com --project=YOUR_PROJECT_ID
```

---

## Secret Manager (for API Credentials)

Instead of passing sensitive keys as environment variables, use GCP Secret Manager.

```bash
# Create a secret
echo -n "YOUR_API_KEY" | gcloud secrets create MY_SECRET_NAME --data-file=-

# Update an existing secret
echo -n "NEW_API_KEY" | gcloud secrets versions add MY_SECRET_NAME --data-file=-
```

**Grant access:** For Cloud Run, grant `secretmanager.secretAccessor` to `app_sa`. For Agent Engine, grant it to the platform-managed SA (`service-PROJECT_NUMBER@gcp-sa-aiplatform-re.iam.gserviceaccount.com`). For GKE, grant `secretmanager.secretAccessor` to `app_sa`. Access secrets via Kubernetes Secrets or directly via the Secret Manager API with Workload Identity.

**Pass secrets at deploy time (Agent Engine):**
```bash
agents-cli deploy --secrets "API_KEY=my-api-key,DB_PASS=db-password:2"
```

Format: `ENV_VAR=SECRET_ID` or `ENV_VAR=SECRET_ID:VERSION` (defaults to latest). Access in code via `os.environ.get("API_KEY")`.

---

## Observability

See the **agents-cli-observability** skill for observability configuration (Cloud Trace, prompt-response logging, BigQuery Analytics, third-party integrations).

---

## Testing Your Deployed Agent

The quickest way to test a deployed agent is `agents-cli run --url <service-url> --mode <a2a|adk> "your prompt"` — it handles auth, sessions, and streaming automatically (supports Agent Engine and Cloud Run).

For advanced testing (custom headers, session reuse, scripting, load tests), see `references/testing-deployed-agents.md`.

---

## Deploying with a UI (IAP)

IAP (Identity-Aware Proxy) secures a Cloud Run service so only authorized Google accounts can access it. Support for IAP deployment via `agents-cli deploy` is planned for a future release.

For Agent Engine with a custom frontend, use a **decoupled deployment** — deploy the frontend separately to Cloud Run or Cloud Storage, connecting to the Agent Engine backend API.

For more information on IAP with Cloud Run, see the [Cloud Console IAP settings](https://cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run#manage_user_or_group_access).

---

## Rollback & Recovery

The primary rollback mechanism is **git-based**: fix the issue, commit, and push to `main`. The CI/CD pipeline will automatically build and deploy the new version through staging → production.

For immediate Cloud Run rollback without a new commit, use revision traffic shifting:
```bash
gcloud run revisions list --service=SERVICE_NAME --region=REGION
gcloud run services update-traffic SERVICE_NAME \
  --to-revisions=REVISION_NAME=100 --region=REGION
```

Agent Engine doesn't support revision-based rollback — fix and redeploy via `agents-cli deploy`.

For GKE rollback, use `kubectl rollout undo`:
```bash
kubectl rollout undo deployment/DEPLOYMENT_NAME -n NAMESPACE
kubectl rollout status deployment/DEPLOYMENT_NAME -n NAMESPACE
```

---

## Custom Infrastructure (Terraform)

**CRITICAL**: When your agent requires custom infrastructure (Cloud SQL, Pub/Sub, Eventarc, BigQuery, etc.), you MUST define it in Terraform — never create resources manually via `gcloud` commands. Exception: quick experimentation is fine with `gcloud` or console, but production infrastructure must be in Terraform.

For custom infrastructure patterns, consult `references/terraform-patterns.md` for:
- Where to put custom Terraform files (single-project vs CI/CD)
- Resource examples (Pub/Sub, BigQuery, Eventarc triggers)
- IAM bindings for custom resources
- Terraform state management (remote vs local, importing resources)
- Common infrastructure patterns

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Terraform state locked | `terraform force-unlock -force LOCK_ID` in deployment/terraform/ |
| GitHub Actions auth failed | Re-run `terraform apply` in CI/CD terraform dir; verify WIF pool/provider |
| Cloud Build authorization pending | Use `github_actions` runner instead |
| Resource already exists | `terraform import` (see `references/terraform-patterns.md`) |
| Agent Engine deploy timeout / hangs | Deployments take 5-10 min; check if engine was created (see Agent Engine Specifics) |
| Secret not available | Verify `secretAccessor` granted to `app_sa` (not the default compute SA) |
| 403 on deploy | Check `deployment/terraform/iam.tf` — `cicd_runner_sa` needs deployment + SA impersonation roles in the target project |
| 403 when testing Cloud Run | Default is `--no-allow-unauthenticated`; include `Authorization: Bearer $(gcloud auth print-identity-token)` header |
| Cold starts too slow | Set `min_instance_count > 0` in Cloud Run Terraform config |
| Cloud Run 503 errors | Check resource limits (memory/CPU), increase `max_instance_count`, or check container crash logs |
| 403 right after granting IAM role | IAM propagation is not instant — wait a couple of minutes before retrying. Don't keep re-granting the same role |
| Resource seems missing but Terraform created it | Run `terraform state list` to check what Terraform actually manages. Resources created via `null_resource` + `local-exec` (e.g., BQ linked datasets) won't appear in `gcloud` CLI output |
| Deployment failed or agent not responding | Check Cloud Logging: `gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=SERVICE" --project=PROJECT --limit=50 --format="table(timestamp,severity,textPayload)"` for Cloud Run, or `gcloud logging read "resource.type=aiplatform.googleapis.com/ReasoningEngine" --project=PROJECT --limit=50` for Agent Engine |
| Agent returns errors after deploy | Open Cloud Logging in Console → filter by service name (Cloud Run) or reasoning engine resource (Agent Engine) → look for Python tracebacks or permission errors in recent log entries |

---

## Platform Registration

For registering deployed agents with Gemini Enterprise, see `/google-agents-cli-publish`.

---

## Related Skills

- `/google-agents-cli-workflow` — Development workflow, coding guidelines, and operational rules
- `/google-agents-cli-adk-code` — ADK Python API quick reference for writing agent code
- `/google-agents-cli-eval` — Evaluation methodology, evalset schema, and the eval-fix loop
- `/google-agents-cli-scaffold` — Project creation and enhancement with `agents-cli scaffold create` / `scaffold enhance`
- `/google-agents-cli-observability` — Cloud Trace, logging, BigQuery Analytics, and third-party integrations
- `/google-agents-cli-publish` — Gemini Enterprise registration
