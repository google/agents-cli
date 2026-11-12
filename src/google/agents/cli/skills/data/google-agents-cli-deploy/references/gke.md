# GKE Infrastructure

> **Assumes `agents-cli` scaffolding.** If your project isn't scaffolded yet, see `/google-agents-cli-scaffold` first.

## Deployment Architecture

GKE uses **container-based deployment** to a managed GKE Autopilot cluster. Your agent is packaged as a Docker container (same Dockerfile as Cloud Run), pushed to Artifact Registry, and deployed via Terraform-managed Kubernetes resources.

## Dockerfile

Scaffolded projects include a `Dockerfile` at the project root — same as Cloud Run. Python uses a single-stage build with `uv` for dependency management; Go uses a multi-stage build that compiles the binary in a `golang` image and copies it into a slim runtime image. Check the project root `Dockerfile` for the exact configuration.

## Kubernetes Resources (Terraform-Managed)

All Kubernetes resources are managed by Terraform in `deployment/terraform/cicd/service.tf` (staging/prod) and `deployment/terraform/single-project/service.tf` (single-project). CI/CD pipelines run `agents-cli deploy --image`, which skips Terraform and updates the image and env vars with `kubectl`, so env vars it sets stay until the next `terraform apply`. A deploy without `--image` runs a targeted `terraform apply` first, which resets the env vars to Terraform's; keep lasting ones in `.env` or Terraform.

| Resource | Purpose |
|----------|---------|
| **`kubernetes_deployment_v1`** | Pod spec, container config, resource requests/limits, startup/readiness/liveness probes, env vars, optional Cloud SQL proxy sidecar |
| **`kubernetes_service_v1`** | LoadBalancer service exposing port 8080 |
| **`kubernetes_horizontal_pod_autoscaler_v2`** | HorizontalPodAutoscaler (2-10 replicas, 70% CPU target) |
| **`kubernetes_pod_disruption_budget_v1`** | PodDisruptionBudget (minAvailable: 1) |
| **`kubernetes_service_account_v1`** | Kubernetes ServiceAccount for Workload Identity |
| **`kubernetes_namespace_v1`** | Namespace for the application |
| **`kubernetes_secret_v1`** | DB password secret (Cloud SQL only) |

## Terraform Infrastructure

GKE infrastructure is provisioned in `deployment/terraform/single-project/service.tf`. Check that file for current configuration.

Key differences from Cloud Run: Terraform provisions a full networking stack (VPC, subnet, Cloud NAT for private node internet access) and a GKE Autopilot cluster with private nodes. Cloud SQL (optional, when `session_type == "cloud_sql"`) uses a proxy sidecar in the pod rather than Cloud Run's Unix socket volume mount.

## Workload Identity

GKE uses Workload Identity to map Kubernetes service accounts to GCP service accounts. The Kubernetes SA is annotated with the GCP `app_sa` email and bound via an `iam.workloadIdentityUser` IAM binding in Terraform.

This lets pods authenticate as `app_sa` without service account keys — same security model as Cloud Run's service identity, but configured through Kubernetes.

## Session Types

> **ADK projects.** The session wiring below (`shared://session`, `app_utils/services.py`) is ADK scaffold behavior. The Cloud SQL infrastructure it uses is framework-agnostic.

| Type | Configuration | Use Case |
|------|--------------|----------|
| **In-memory** | Default (`shared://session` resolved by `app_utils/services.py` (in-memory)) | Local dev only; lost on pod restart |
| **Cloud SQL** | `--session-type cloud_sql` at scaffold time | Production persistent sessions (Cloud SQL proxy sidecar in pod) |
| **Agent Runtime in Gemini platform** | Managed sessions (`agentengine://{resource_name}`) | When using Agent Runtime as session backend |

The concrete session URI for `cloud_sql` / `agent_platform_sessions` is now built inside `app_utils/services.py`, not `fast_api_app.py`.

Cloud SQL in GKE uses a **proxy sidecar container** in the pod (unlike Cloud Run which uses a Unix socket volume mount). The sidecar is configured in the `kubernetes_deployment_v1` Terraform resource.

## Served Endpoints

A scaffolded Python project serves `uvicorn app.fast_api_app:app` on port 8080; a Go project serves the binary built from `main.go`. Which routes that app exposes depends on the framework, so check `app/fast_api_app.py` or `main.go`.

> **ADK projects.** The app serves the ADK HTTP surface (`/run_sse`, `/apps/...`) plus A2A (JSON-RPC + agent card — A2A is built into every ADK agent). A2A differs between languages — Python serves both under `/a2a/{app_name}`; Go serves the agent card at the root (`/.well-known/agent-card.json`) and JSON-RPC under `/a2a/` (`/a2a/v1/invoke`, plus `/a2a/invoke` for the v0 protocol).

## Testing Your Deployed Agent

> **ADK projects.** The session + `/run_sse` calls below are the ADK HTTP surface. On other frameworks, port-forward the same way and call your app's own routes (e.g. the A2A endpoint).

GKE LoadBalancer services are **internal by default** — they are not accessible from outside the VPC. Use `kubectl port-forward` to access the service locally:

```bash
# Start port-forward (runs in background)
kubectl port-forward svc/SERVICE_NAME 8080:8080 -n NAMESPACE &

# Test health endpoint
curl "http://127.0.0.1:8080/"

# Create a session
curl -X POST "http://127.0.0.1:8080/apps/app/users/test-user/sessions" \
  -H "Content-Type: application/json" \
  -d '{}'

# Send a message via SSE streaming
curl -X POST "http://127.0.0.1:8080/run_sse" \
  -H "Content-Type: application/json" \
  -d '{
    "app_name": "app",
    "user_id": "test-user",
    "session_id": "SESSION_ID",
    "new_message": {"role": "user", "parts": [{"text": "Hello!"}]}
  }'

# Live agents: same forward, WebSocket surface
agents-cli run "Hello!" --mode adk_live --url http://127.0.0.1:8080
```

## Network & Ingress

GKE LoadBalancer services are **internal by default** (the `cloud.google.com/load-balancer-type: "Internal"` annotation is set in Terraform). The internal IP is used for pod-to-pod A2A communication within the cluster. Use `kubectl port-forward` for local access.
