# Cloud Run Infrastructure

> **Assumes `/google-agents-cli-scaffold` scaffolding.** If your project isn't scaffolded yet, see `/google-agents-cli-scaffold` first.

## Scaling & Resource Defaults

Agents CLI scaffolds Cloud Run infrastructure in `deployment/terraform/single-project/service.tf` (and the `cicd/` variant). Check that file for current resource limits, scaling configuration, concurrency, and session affinity settings.

Key settings to be aware of: `cpu_idle` (CPU allocation strategy), `min_instance_count` (cold start avoidance), `max_instance_request_concurrency` (concurrency per instance), and `session_affinity` (sticky routing).

For how to size cpu/memory/workers/concurrency together (and avoid OOM), see **Sizing a deployment** in the `/google-agents-cli-deploy` skill.

## Dockerfile

Without `--image`, `agents-cli deploy` runs a source deploy (`gcloud run deploy --source .`): it uploads the project to a `run-sources-*` bucket, Cloud Build builds the `Dockerfile` into the `cloud-run-source-deploy` Artifact Registry repository, and the console shows the deployment type as Source. Pass `--image` with a prebuilt image for a Container deploy.

Scaffolded projects include a `Dockerfile`:
* Python uses a single-stage build with `uv` for dependency management.
* Go uses a multi-stage build — a `golang` builder image compiles the binary, which is copied into a slim runtime image.

Check the project root `Dockerfile` for the exact configuration.

## Served Endpoints

A scaffolded Python project serves `uvicorn app.fast_api_app:app` on port 8080; a Go project serves the binary built from `main.go`. Which routes that app exposes depends on the framework, so check `app/fast_api_app.py` or `main.go`.

> **ADK projects.** The app serves the ADK HTTP surface (`/run_sse`, `/apps/...`) plus A2A (JSON-RPC + agent card — A2A is built into every ADK agent). A2A differs between languages — Python and TypeScript serve both under `/a2a/{app_name}`; Go serves the agent card at the root (`/.well-known/agent-card.json`) and JSON-RPC under `/a2a/` (`/a2a/v1/invoke`, plus `/a2a/invoke` for the v0 protocol); Java and Kotlin serve the agent card at the root and JSON-RPC at `/a2a/remote/v1/message:send`, which takes only the non-streaming `message/send` method.

## Session Types

> **ADK projects.** The session wiring below (`shared://session`, `app_utils/services.py`) is ADK scaffold behavior. The Cloud SQL infrastructure it uses is framework-agnostic.

| Type | Configuration | Use Case |
|------|--------------|----------|
| **In-memory** | Default (`shared://session` resolved by `app_utils/services.py` (in-memory)) | Local dev only; lost on instance restart |
| **Cloud SQL** | `--session-type cloud_sql` at scaffold time | Production persistent sessions (Postgres 15, IAM auth) |
| **Agent Runtime in Gemini platform** | Managed sessions (`agentengine://{resource_name}`) | When using Agent Runtime as session backend |

The concrete session URI for `cloud_sql` / `agent_platform_sessions` is now built inside `app_utils/services.py`, not `fast_api_app.py`.

Cloud SQL session infrastructure (instance, database, Cloud SQL Unix socket volume mount) is configured in `deployment/terraform/single-project/service.tf`.

> **Manual Deployment Warning:** When using Cloud SQL without Terraform (e.g., direct `gcloud run deploy` with `--add-cloudsql-instances`), you MUST manually grant `roles/cloudsql.client` to the runtime service account, otherwise the connection will fail with authorization errors.

## Network & Ingress

Default ingress is `all`. `agents-cli deploy --ingress internal` (VPC only) or `--ingress internal-and-cloud-load-balancing` (VPC plus Cloud Load Balancing) restricts it; projects whose org policy `constraints/run.allowedIngress` rejects `all` need one of these. The `run.app` URL, and `gcloud run services proxy` which goes through it, then answer 404 from outside the VPC.

The generated `service.tf` sets `ingress = "INGRESS_TRAFFIC_ALL"`, so change it too when you deploy with `--ingress`, or `terraform apply` resets it.

IAP (Identity-Aware Proxy) can be enabled by running `agents-cli deploy --iap` (Cloud Run only), which adds Google identity authentication without code changes. IAP is configured by the deploy flag, not by a generated Terraform variable.

VPC connectors are not configured by default. Add them in custom Terraform if needed for private resource access (see `references/terraform-patterns.md`).
