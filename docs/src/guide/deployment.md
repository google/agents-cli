# Deployment

Deploy your agent to a development environment or production with a CI/CD pipeline.

![Prototype to Production](../assets/prototype_to_prod.png)

---

## Infrastructure vs deployment

**Infrastructure** (`agents-cli infra`) provisions the cloud resources your agent needs: service accounts, IAM bindings, APIs, telemetry buckets, and Terraform state. It doesn't run your agent.

**Deployment** (`agents-cli deploy`) takes your agent code and puts it on the provisioned infrastructure. It builds a container, pushes it to a registry, and starts the service.

A dev deployment doesn't need provisioned infrastructure: `agents-cli deploy` works on its own. Run `agents-cli infra single-project --apply` first only when you want Terraform to manage resources such as prompt-response logging and BigQuery Agent Analytics.

---

## Deploy to a dev environment

To get a running deployment in two steps:

**1. Set your dev project:**

```bash
gcloud config set project YOUR_DEV_PROJECT_ID
```

**2. Deploy the agent:**

```bash
agents-cli deploy --project YOUR_DEV_PROJECT_ID
```

Without `--project`, the deploy reads the project from `gcloud config` and stops to ask you to confirm it. Pass `--no-confirm-project` to skip the check, or `-i` to answer a prompt.

The command reads your `deployment_target` from `agents-cli-manifest.yaml` (under `create_params`) and dispatches to the right flow:

| `deployment_target`  | What happens                                  |
|----------------------|-----------------------------------------------|
| `agent_runtime`      | Deployment to Agent Runtime in Gemini platform (fully managed) |
| `cloud_run`          | `gcloud run deploy` (container on Cloud Run) |
| `gke`                | Terraform + `gcloud builds submit` + `kubectl set image` |

To override the manifest, or to deploy without one (for example, from CI), pass `-d` or `--deployment-target`.

The deployment target is set when you create your project:

```bash
agents-cli create my-agent -a adk_python -d cloud_run    # or agent_runtime, gke
```

To change the deployment target for an existing project, use `scaffold enhance`:

```bash
agents-cli scaffold enhance -d cloud_run
```

Run `agents-cli scaffold enhance --help` to see all available options.

!!! tip "Terraform-managed observability"
    For prompt-response logging and BigQuery Agent Analytics, run `agents-cli infra single-project --apply` before your first deploy, never after, because Terraform can't take over a resource that `agents-cli deploy` already created. On Cloud Run, and on Agent Runtime projects that run as a service account, pass the Terraform-created account with `--service-account SA_EMAIL`. See the [Observability guide](observability/index.md).

**Verify it works:**

```bash
agents-cli deploy --list    # List deployments
agents-cli deploy --status  # Check a pending --no-wait deployment
```

---

## Deployment targets

### Agent Runtime

*Selected with `agents-cli create my-agent -a <template> -d agent_runtime`, or `create_params.deployment_target: agent_runtime` in `agents-cli-manifest.yaml`.*

Agent Runtime is fully managed. You provide a `Dockerfile` (scaffolded for you), and Agent Runtime builds and runs the container. There is no cluster or service to operate. The LangChain and Claude templates are experimental on Agent Runtime and have limits, such as `publish gemini` being refused. See [First-party frameworks](extensions/first-party.md). Deploy with:

```bash
agents-cli deploy --project my-gcp-project --region us-east1
```

You can pass Docker build args or a container port:

```bash
agents-cli deploy --project my-gcp-project --build-args KEY=VALUE --port 8080
```

Or deploy a prebuilt Artifact Registry image instead of building from the Dockerfile. The Agent Runtime service agent (`service-PROJECT_NUMBER@gcp-sa-aiplatform-re.iam.gserviceaccount.com`) needs `roles/artifactregistry.reader` on the repository:

```bash
agents-cli deploy --project my-gcp-project --image us-docker.pkg.dev/my-project/my-repo/my-agent:v1
```

An existing agent can't switch between a source build and `--image`, in either direction. Agents provisioned by the scaffolded Terraform count as source builds. On the first deploy of a new agent with Agent Identity, the image starts once before its env vars and secrets are applied, so it must start without them.

Check on an async deployment:

```bash
agents-cli deploy --project my-gcp-project --no-wait  # Start and return immediately
agents-cli deploy --status      # Check progress later
```

### Cloud Run

*Selected with `agents-cli create my-agent -a <template> -d cloud_run`, or `create_params.deployment_target: cloud_run` in `agents-cli-manifest.yaml`.*

Builds a container from source and deploys as a Cloud Run service:

```bash
agents-cli deploy --project my-gcp-project --region us-east1
```

Override resource limits:

```bash
agents-cli deploy --project my-gcp-project --memory 8Gi --port 8080
```

Deploy a pre-built image instead of building from source:

```bash
agents-cli deploy --project my-gcp-project --image gcr.io/my-project/my-agent:v1
```

The service is private by default. The deploy command always passes `--no-allow-unauthenticated`, and the CLI has no flag to make the service public. Callers need an identity token, and `agents-cli run "PROMPT" --url SERVICE_URL --mode a2a` adds one for you. Use `--mode a2a` for any template and `--mode adk` for ADK templates. Add `--iap` to put Identity-Aware Proxy in front of it, or pass `--ingress internal` (or `internal-and-cloud-load-balancing`) to limit which networks can reach it.

New projects run the service as its own [Agent Identity](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/agent-identity) principal, and `--service-account` is then ignored. Opt out at scaffold time with `--cloud-run-identity-default service_account`. A project created before Agent Identity became the default opts in with `--agent-identity` on the first deploy. You can't turn it off later without deleting the service. Grant secret access to the service account (`app_sa`) even then, because Cloud Run checks that account when it deploys.

!!! tip
    If you need Cloud Run features that `agents-cli` flags don't expose, use `--dry-run` (or `-n`) to print the full `gcloud` command. Then copy it and add the arguments you need.

    The dry run first runs read-only checks on Google Cloud. They run as your gcloud account, which the deploy uses, or as your Application Default Credentials when gcloud's token does not work for direct API calls. The output names the account. If it is not the account gcloud deploys as, failed checks only warn (⚠️). It still prints the command when a check fails (❌), but then exits with code 1.

### GKE

*Selected with `agents-cli create my-agent -a <template> -d gke`, or `create_params.deployment_target: gke` in `agents-cli-manifest.yaml`.*

Deploys to a GKE cluster using Terraform and kubectl:

```bash
agents-cli deploy --project my-gcp-project
```

The deploy targets the cluster that Terraform creates, which has the same name as your agent project. To deploy to an existing cluster with a different name, add `--cluster-name CLUSTER_NAME`.

!!! tip
    Use `--dry-run` (or `-n`) to see the Terraform, Cloud Build and kubectl steps without running them. The dry run first runs read-only checks on Google Cloud: the APIs, the cluster and your IAM permissions.

    - gcloud and kubectl steps are checked as your gcloud account. Terraform steps are checked as your Application Default Credentials.
    - kubectl is checked as your Application Default Credentials instead when `CLOUDSDK_CONTAINER_USE_APPLICATION_DEFAULT_CREDENTIALS` is true.
    - If gcloud's token does not work for direct API calls (for example, it is bound to a client certificate), the gcloud steps are checked as your Application Default Credentials instead, and if those are a different account from the one gcloud deploys as, their failed checks only warn (⚠️).
    - If your Application Default Credentials do not work, the Terraform permissions are not checked, and kubectl is checked as your gcloud account. The output says so (⚠️).
    - If Terraform reads other credentials, for example from `GOOGLE_CREDENTIALS` or a provider setting, the Terraform permissions are not checked, and the output says so (⚠️).

    The dry run still prints the steps when a check fails (❌), but then exits with code 1.

---

## Next steps

- [CI/CD and production](cicd.md): set up automated pipelines for staging and production
- [Observability](observability/index.md): monitor your deployed agent
