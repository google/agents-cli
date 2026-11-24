# {{cookiecutter.project_name}}

A base ReAct agent built with Google's Agent Development Kit (ADK)
Agent generated with Agents CLI version `{{cookiecutter.package_version}}`

## Project Structure

This project is organized as follows:

```
{{cookiecutter.project_name}}/
├── {{cookiecutter.agent_directory}}/                 # Core application code
│   └── agent.ts         # Main agent logic with tools
├── .cloudbuild/         # CI/CD pipeline configurations for Google Cloud Build
├── eval/                # Eval dataset and config (`agents-cli eval run`)
├── deployment/          # Infrastructure and deployment scripts
{%- if cookiecutter.deployment_target == 'gke' %}
│   ├── k8s/             # Kubernetes manifests for GKE deployment
{%- endif %}
├── tests/               # Unit, integration, and load tests
│   ├── unit/            # Unit tests
│   ├── integration/     # Integration tests
│   └── load_test/       # Load tests
├── Makefile             # Makefile for common commands
├── GEMINI.md            # AI-assisted development guide
├── package.json         # Project dependencies and configuration
├── tsconfig.json        # TypeScript configuration
└── vitest.config.ts     # Vitest test configuration
```

> 💡 **Tip:** Use [Antigravity CLI](https://antigravity.google/) for AI-assisted development - project context is pre-configured in `GEMINI.md`.

## Requirements

Before you begin, ensure you have:
- **Node.js 22.17+**: JavaScript runtime (used for all dependencies in this project) - [Install](https://nodejs.org/)
- **Python 3.10+**: Build tool required by `node-gyp` for native dependencies (`better-sqlite3`) - [Install](https://www.python.org/)
- **npm**: Node package manager (comes with Node.js) - add packages with `npm install <package>`
- **Google Cloud SDK**: For GCP services - [Install](https://cloud.google.com/sdk/docs/install)
- **Terraform**: For infrastructure deployment - [Install](https://developer.hashicorp.com/terraform/downloads)
- **make**: Build automation tool - [Install](https://www.gnu.org/software/make/) (pre-installed on most Unix-based systems)


## Quick Start (Local Testing)

1. Create your environment file from the example:

```bash
cp .env.example .env
```

2. Edit `.env` with your configuration:

```bash
# For Gemini platform (recommended):
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=global

# Or for Gemini API:
# GEMINI_API_KEY=your-api-key
```

3. Install and launch:

```bash
make install && make playground
```
> **📊 Observability Note:** ADK TypeScript agents don't export traces or prompt-response logs to Google Cloud yet. See [Monitoring and Observability](#monitoring-and-observability) for details.

## Commands

| Command              | Description                                                                                 |
| -------------------- | ------------------------------------------------------------------------------------------- |
| `make install`       | Install all required dependencies using npm                                                 |
| `make playground`    | Launch local development environment with backend and frontend - leveraging ADK devtools   |
{%- if cookiecutter.deployment_target == 'cloud_run' %}
| `make deploy`        | Deploy agent to Cloud Run (use `IAP=true` to enable Identity-Aware Proxy, `PORT=8080` to specify container port) |
{%- elif cookiecutter.deployment_target == 'gke' %}
| `make deploy`        | Deploy agent to GKE                                                                         |
{%- endif %}
| `make local-backend` | Launch local development server                                                             |
| `make test`          | Run unit and integration tests using vitest                                                 |
| `make lint`          | Run code quality checks using eslint                                                        |
| `make build`         | Build TypeScript to JavaScript                                                              |
| `make typecheck`     | Run TypeScript type checking                                                                |
| `make clean`         | Remove build artifacts (dist, node_modules)                                                 |
{%- if cookiecutter.deployment_target != 'none' %}
| `make setup-dev-env` | Set up development environment resources using Terraform                                    |
{%- endif %}

For full command options and usage, refer to the [Makefile](Makefile).


## Usage

This template follows a "bring your own agent" approach - you focus on your business logic, and the template handles everything else (UI, infrastructure, deployment, monitoring).
1. **Prototype:** Build your Generative AI Agent using the intro notebooks in `notebooks/` for guidance. Use the Eval Service in Gemini platform to assess performance.
2. **Integrate:** Import your agent into the app by editing `{{cookiecutter.agent_directory}}/agent.ts`. Add tools using `FunctionTool` with Zod schemas for parameter validation.
3. **Test:** Explore your agent functionality using the local playground with `make playground`. The playground automatically reloads your agent on code changes.
4. **Deploy:** Set up and initiate the CI/CD pipelines, customizing tests as necessary. Refer to the [deployment section](#deployment) for comprehensive instructions. For streamlined infrastructure deployment, simply run `uvx google-agents-cli infra cicd`. Currently supports GitHub with both Google Cloud Build and GitHub Actions as CI/CD runners.
5. **Monitor:** Follow your deployed agent's logs in Cloud Logging to iterate on your application.

The project includes a `GEMINI.md` file that provides context for AI tools like Antigravity CLI when asking questions about your template.


## Deployment

> **Note:** For a streamlined one-command deployment of the entire CI/CD pipeline and infrastructure using Terraform, you can use the `agents-cli infra cicd` CLI command. Currently supports GitHub with both Google Cloud Build and GitHub Actions as CI/CD runners.

### Dev Environment
{%- if cookiecutter.deployment_target in ('cloud_run', 'gke') %}

You can test deployment towards a Dev Environment using the following command:

```bash
gcloud config set project <your-dev-project-id>
make deploy
```
{%- else %}

This project has no deployment target. Add one with `agents-cli scaffold enhance --deployment-target <target>`.
{%- endif %}


The repository includes a Terraform configuration for the setup of the Dev Google Cloud project.

### Production Deployment

The repository includes a Terraform configuration for the setup of a production Google Cloud project.

## Monitoring and Observability

A deployed agent's output goes to **Cloud Logging**.

Not available for ADK TypeScript agents yet:

- **Traces in Cloud Trace.** `agents-cli run` and `agents-cli playground` ignore `--otel-to-cloud`.
- **Prompt-response logging** to Google Cloud Storage, BigQuery and Cloud Logging.
