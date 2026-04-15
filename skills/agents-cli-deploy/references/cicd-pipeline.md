# Production Deployment — CI/CD Pipeline

**Best for:** Production applications, teams requiring staging → production promotion.

**Prerequisites:**
1. Project must NOT be in a gitignored folder
2. User must provide staging and production GCP project IDs
3. GitHub repository name and owner

**Steps:**
1. If prototype, first add Terraform/CI-CD files using the Agents CLI (see `/agents-cli-scaffold` for full options):
   ```bash
   agents-cli enhance . --cicd-runner github_actions
   ```

2. Ensure you're logged in to GitHub CLI:
   ```bash
   gh auth login  # (skip if already authenticated)
   ```

3. Run infra prod:
   ```bash
   agents-cli infra prod \
     --staging-project YOUR_STAGING_PROJECT \
     --prod-project YOUR_PROD_PROJECT \
     --repository-name YOUR_REPO_NAME \
     --repository-owner YOUR_GITHUB_USERNAME \
     --auto-approve \
     --create-repository
   ```

4. Push code to trigger deployments

## Key `infra prod` Flags

| Flag | Description |
|------|-------------|
| `--staging-project` | GCP project ID for staging environment |
| `--prod-project` | GCP project ID for production environment |
| `--repository-name` / `--repository-owner` | GitHub repository name and owner |
| `--auto-approve` | Skip Terraform plan confirmation prompts |
| `--create-repository` | Create the GitHub repo if it doesn't exist |
| `--cicd-project` | Separate GCP project for CI/CD infrastructure. Defaults to prod project |
| `--local-state` | Store Terraform state locally instead of in GCS (see `references/terraform-patterns.md`) |

Run `agents-cli infra prod --help` for the full flag reference (Cloud Build options, dev project, region, etc.).

## Choosing a CI/CD Runner

| Runner | Pros | Cons |
|--------|------|------|
| **github_actions** (Default) | No PAT needed, uses `gh auth`, WIF-based, fully automated | Requires GitHub CLI authentication |
| **google_cloud_build** | Native GCP integration | Requires interactive browser authorization (or PAT + app installation ID for programmatic mode) |

## How Authentication Works (WIF)

Both runners use **Workload Identity Federation (WIF)** — GitHub/Cloud Build OIDC tokens are trusted by a GCP Workload Identity Pool, which grants `cicd_runner_sa` impersonation. No long-lived service account keys needed. Terraform in `setup-cicd` creates the pool, provider, and SA bindings automatically. If auth fails, re-run `terraform apply` in the CI/CD Terraform directory.

## CI/CD Pipeline Stages

The pipeline has three stages:

1. **CI (PR checks)** — Triggered on pull request. Runs unit and integration tests.
2. **Staging CD** — Triggered on merge to `main`. Builds container, deploys to staging, runs load tests.
   > **Path filter:** Staging CD uses `paths: ['app/**']` — it only triggers when files under `app/` change. The first push after `setup-cicd` won't trigger staging CD unless you modify something in `app/`. If nothing happens after pushing, this is why.
3. **Production CD** — Triggered after successful staging deploy via `workflow_run`. Might require **manual approval** before deploying to production.
   > **Approving:** Go to GitHub Actions → the production workflow run → click "Review deployments" → approve the pending `production` environment. This is GitHub's environment protection rules, not a custom mechanism.

**IMPORTANT**: `setup-cicd` creates infrastructure but doesn't deploy automatically. Terraform configures all required GitHub secrets and variables (WIF credentials, project IDs, service accounts). Push code to trigger the pipeline:

```bash
git add . && git commit -m "Initial agent implementation"
git push origin main
```

To approve production deployment:

```bash
# GitHub Actions: Approve via repository Actions tab (environment protection rules)

# Cloud Build: Find pending build and approve
gcloud builds list --project=PROD_PROJECT --region=REGION --filter="status=PENDING"
gcloud builds approve BUILD_ID --project=PROD_PROJECT
```
