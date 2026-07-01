# {{cookiecutter.project_name}}

{{cookiecutter.agent_description}}
Agent generated with `agents-cli` version `{{ cookiecutter.package_version }}`

## Project Structure

```
{{cookiecutter.project_name}}/
├── {{cookiecutter.agent_directory}}/         # Core agent code
│   ├── agent.py               # Main agent logic
│   ├── fast_api_app.py        # FastAPI Backend server
│   └── app_utils/             # App utilities and helpers
{%- if cookiecutter.cicd_runner == 'google_cloud_build' %}
├── .cloudbuild/               # CI/CD pipeline configurations for Google Cloud Build
{%- elif cookiecutter.cicd_runner == 'github_actions' %}
├── .github/                   # CI/CD pipeline configurations for GitHub Actions
{%- endif %}
{%- if cookiecutter.cicd_runner != 'skip' %}
├── deployment/                # Infrastructure and deployment scripts
{%- if cookiecutter.deployment_target == 'gke' %}
│   ├── k8s/                   # Kubernetes manifests for GKE deployment
{%- endif %}
{%- endif %}
├── tests/                     # Unit, integration, and load tests
├── GEMINI.md                  # AI-assisted development guide
└── pyproject.toml             # Project dependencies
```

> 💡 **Tip:** Use [Antigravity CLI](https://antigravity.google/) for AI-assisted development - project context is pre-configured in `GEMINI.md`.

## Requirements

Before you begin, ensure you have:
- **uv**: Python package manager (used for all dependency management in this project) - [Install](https://docs.astral.sh/uv/getting-started/installation/) ([add packages](https://docs.astral.sh/uv/concepts/dependencies/) with `uv add <package>`)
- **agents-cli**: Agents CLI - Install with `uv tool install google-agents-cli`
- **Google Cloud SDK**: For GCP services - [Install](https://cloud.google.com/sdk/docs/install)
{%- if cookiecutter.cicd_runner != 'skip' %}
- **Terraform**: For infrastructure deployment - [Install](https://developer.hashicorp.com/terraform/downloads)
{%- endif %}


## Quick Start

Install `agents-cli` and its skills if not already installed:

```bash
uvx google-agents-cli setup
```

Install required packages:

```bash
agents-cli install
```

Test the agent with a local web server:

```bash
agents-cli playground
```

You can also use features from the [ADK](https://adk.dev/) CLI with `uv run adk`.

## Commands

| Command              | Description                                                                                 |
| -------------------- | ------------------------------------------------------------------------------------------- |
| `agents-cli install` | Install dependencies using uv                                                         |
| `agents-cli playground` | Launch local development environment                                                  |
| `agents-cli lint`    | Run code quality checks                                                               |
{%- if cookiecutter.settings.get("commands", {}).get("extra", {}) %}
{%- for cmd_name, cmd_value in cookiecutter.settings.get("commands", {}).get("extra", {}).items() %}
| `agents-cli run {{ cmd_name }}`       | {% if cmd_value is mapping %}{% if cmd_value.description %}{{ cmd_value.description }}{% else %}{% if cookiecutter.deployment_target in cmd_value %}{{ cmd_value[cookiecutter.deployment_target] }}{% else %}{{ cmd_value.command if cmd_value.command is string else "" }}{% endif %}{% endif %}{% else %}{{ cmd_value }}{% endif %} |
{%- endfor %}
{%- endif %}
| `agents-cli eval`    | Evaluate agent behavior (generate, grade, analyze, and more — see `agents-cli eval --help`) |
| `uv run pytest tests/unit tests/integration` | Run unit and integration tests                                                        |
{%- if cookiecutter.deployment_target in ('cloud_run', 'gke') %}
| `agents-cli deploy`  | Deploy agent to {{ 'GKE' if cookiecutter.deployment_target == 'gke' else 'Cloud Run' }}                                                                   |
{%- elif cookiecutter.deployment_target == 'agent_runtime' %}
| `agents-cli deploy`  | Deploy agent to Agent Runtime                                                                |
| `agents-cli publish gemini-enterprise` | Register deployed agent to Gemini Enterprise                    |
{%- endif -%}
| [A2A Inspector](https://github.com/a2aproject/a2a-inspector) | Launch A2A Protocol Inspector                                                        |
{%- if cookiecutter.cicd_runner != 'skip' %}
| `agents-cli infra single-project` | Set up single-project infrastructure using Terraform                              |
{%- endif %}

## 🛠️ Project Management

| Command | What It Does |
|---------|--------------|
{%- if cookiecutter.cicd_runner == 'skip' %}
| `agents-cli scaffold enhance` | Add CI/CD pipelines and Terraform infrastructure |
{%- endif %}
| `agents-cli infra cicd` | One-command setup of entire CI/CD pipeline + infrastructure |
| `agents-cli scaffold upgrade` | Auto-upgrade to latest version while preserving customizations |

---

## Development

Edit your agent logic in `{{cookiecutter.agent_directory}}/agent.py` and test with `agents-cli playground` - it auto-reloads on save.

## Deployment

```bash
gcloud config set project <your-project-id>
agents-cli deploy
```
{%- if cookiecutter.cicd_runner == 'skip' %}

To add CI/CD and Terraform, run `agents-cli scaffold enhance`.
{%- endif %}
To set up your production infrastructure, run `agents-cli infra cicd`.

## Observability

Built-in telemetry exports to Cloud Trace, BigQuery, and Cloud Logging.

## A2A Inspector

This agent supports the [A2A Protocol](https://a2a-protocol.org/). Use the [A2A Inspector](https://github.com/a2aproject/a2a-inspector) to test interoperability.
See the [A2A Inspector docs](https://github.com/a2aproject/a2a-inspector) for details.
