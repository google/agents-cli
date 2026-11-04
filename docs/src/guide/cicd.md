# CI/CD and production

Set up a CI/CD pipeline that tests on pull request, deploys to staging on merge, and then promotes the same commit to production behind an approval gate.

![Prototype to Production](../assets/prototype_to_prod.png)

---

## How the pipeline works

1. **CI pipeline** (triggered on pull request):
    - Runs unit and integration tests.

2. **Staging CD pipeline** (triggered on merge to `main`):
    - Builds the container image. On Cloud Run and GKE, the pipeline pushes it to Artifact Registry. On Agent Runtime, Agent Runtime builds it from your `Dockerfile`.
    - Deploys to the staging environment.
    - Runs automated load testing.

3. **Production deployment** (triggered after successful staging):
    - Waits for manual approval. Cloud Build always requires approval. On GitHub Actions, the job targets the `production` environment, and it waits only after you add a **Required reviewers** protection rule to that environment in your repository settings.
    - Builds and deploys the same commit that passed staging.

---

## Set up the pipeline

Before you start, sign in to the GitHub CLI with `gh auth login`, and make sure the project isn't in a gitignored folder. The `infra cicd` command supports GitHub repositories only, and it needs both a staging and a production project. For a single environment, use `agents-cli infra single-project` and `agents-cli deploy` instead.

The `agents-cli infra cicd` command runs `terraform plan` by default. Preview the changes first:

```bash
agents-cli infra cicd \
  --staging-project my-staging-project \
  --prod-project my-prod-project \
  --repository-name my-agent
```

Plan mode still does the setup that an accurate plan needs. It writes `env.tfvars` and `backend.tf`, creates the `<cicd-project>-terraform-state` bucket if it is missing (skipped with `--local-state`), and checks GitHub CLI auth and scopes. For Cloud Build, it also stores your GitHub token in Secret Manager or creates the GitHub connection.

When the plan looks right, re-run the same command with `--apply`:

```bash
agents-cli infra cicd \
  --staging-project my-staging-project \
  --prod-project my-prod-project \
  --repository-name my-agent \
  --apply
```

With `--apply`, the command sets up:

- **Infrastructure provisioning** via Terraform for staging and production
- **CI/CD configuration** with your chosen runner (Cloud Build or GitHub Actions)
- **Repository connection** to GitHub

The command doesn't deploy anything. Push to `main` to start the first pipeline run. Staging CD runs only when relevant paths change (Python: the agent directory, `tests/**`, `deployment/**`, `uv.lock`; Go: the agent directory, `e2e/**`, `deployment/**`, `go.mod`, `go.sum`), so a push that touches none of them doesn't trigger it.

### CI/CD runner detection

Without `--cicd-runner`, the command picks the runner from the files in `deployment/terraform/cicd/`:

| Runner | How it's detected |
|--------|-------------------|
| GitHub Actions | `wif.tf` and `github.tf` both exist. Uses Workload Identity Federation for keyless auth. |
| Google Cloud Build | Any other case. Sets up a Cloud Build connection to GitHub. |

### Options

| Flag | Description |
|------|-------------|
| `--staging-project` | GCP project ID for staging (required unless `--interactive`) |
| `--prod-project` | GCP project ID for production (required unless `--interactive`) |
| `--cicd-project` | Separate project for CI/CD resources (defaults to prod) |
| `--dev-project` | Dev project (optional, provisions dev infra too) |
| `--region` | GCP region (auto-detected from Terraform vars if not set, otherwise `us-east1`) |
| `--repository-name` | GitHub repository name (required unless `--interactive`) |
| `--repository-owner` | GitHub repository owner (defaults to the current GitHub user) |
| `--host-connection-name` | Cloud Build host connection name (defaults to `git-<repository-name>`) |
| `--github-pat` | GitHub personal access token. Cloud Build needs it, with `--github-app-installation-id`, when you don't pass `--interactive`. Defaults to `$GH_TOKEN` or `$GITHUB_TOKEN`. With `--interactive`, Cloud Build connects to GitHub through a browser OAuth flow instead. |
| `--github-app-installation-id` | GitHub App installation ID for programmatic auth. Cloud Build needs it without `--interactive`. |
| `--local-state` | Use local Terraform state instead of the remote GCS backend |
| `--create` | Create a new GitHub repository (omit to use an existing one) |
| `--cicd-runner` | `google_cloud_build` or `github_actions` (auto-detected if not set) |
| `--apply` | Apply the changes. Without it, only a plan is shown. |
| `--force-bucket`, `--force` | Allow a Terraform state bucket that exists in a different GCP project. Off by default to guard against bucket squatting. |
| `-i`, `--interactive` | Prompt for missing values |
| `--debug` | Enable debug logging |

---

## Terraform variables

The pipeline uses Terraform variables defined in `deployment/terraform/cicd/variables.tf`:

| Variable | Description |
|----------|-------------|
| `project_name` | Base name for resource naming |
| `prod_project_id` | Google Cloud project ID for production |
| `staging_project_id` | Google Cloud project ID for staging |
| `cicd_runner_project_id` | Google Cloud project ID where CI/CD pipelines execute |
| `region` | Google Cloud region (default: `us-east1`) |
| `repository_name` | GitHub repository name |
| `repository_owner` | GitHub username or organization |
| `app_sa_roles` | Roles for the application service account |
| `cicd_roles` | Roles for CI/CD runner service account |

---

Once your agent is deployed, register it with the Gemini app using `agents-cli publish gemini`. Run with `--help` for all options.
