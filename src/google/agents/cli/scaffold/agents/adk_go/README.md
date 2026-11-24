# {{cookiecutter.project_name}}

A Go agent built with Google's Agent Development Kit (ADK).

## Project Structure

```
{{cookiecutter.project_name}}/
├── main.go              # Application entry point
├── {{cookiecutter.agent_directory}}/
│   └── agent.go         # Agent implementation
├── sessions/
│   └── sessions.go      # Session backend selection (in-memory / Agent Runtime / Cloud SQL)
├── e2e/
│   ├── integration/     # Integration tests
│   └── load_test/       # Load testing
├── deployment/
│   └── terraform/       # Infrastructure as Code
{%- if cookiecutter.deployment_target == 'gke' %}
│   ├── k8s/             # Kubernetes manifests for GKE deployment
{%- endif %}
├── go.mod               # Go module definition
├── Dockerfile           # Container build
├── GEMINI.md            # AI-assisted development guide
└── Makefile             # Common commands
```

> **Tip:** Use [Antigravity CLI](https://antigravity.google/) for AI-assisted development - project context is pre-configured in `GEMINI.md`.

## Requirements

- Go 1.26 or later
- Google Cloud SDK (`gcloud`)
- A Google Cloud project with Gemini platform enabled

## Quick Start

1. **Install dependencies:**
   ```bash
   make install
   ```

2. **Configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env with your Google Cloud project ID
   ```

3. **Run the playground:**
   ```bash
   make playground
   ```
   Open http://127.0.0.1:8501/ui/ in your browser.

## Commands

| Command | Description |
|---------|-------------|
| `make install` | Download Go dependencies |
| `make playground` | Launch local development environment |
| `make lint` | Run code quality checks (golangci-lint) |
| `make test` | Run all tests |
| `make local-backend` | Start API server on port 8000 |
| `make build` | Build binary |
{%- if cookiecutter.deployment_target in ('cloud_run', 'gke') %}
| `make deploy` | Deploy to {{ 'GKE' if cookiecutter.deployment_target == 'gke' else 'Cloud Run' }} |
{%- endif %}

## 🛠️ Project Management

| Command | What It Does |
|---------|--------------|
| `uvx google-agents-cli infra cicd` | One-command setup of entire CI/CD pipeline + infrastructure |
| `uvx google-agents-cli scaffold upgrade` | Auto-upgrade to latest version while preserving customizations |

---

## Development

Edit your agent logic in `{{cookiecutter.agent_directory}}/agent.go` and test with `make playground` - it auto-reloads on save.

## Deployment
{%- if cookiecutter.deployment_target in ('cloud_run', 'gke') %}

```bash
gcloud config set project <your-project-id>
make deploy
```
{%- elif cookiecutter.deployment_target == 'agent_runtime' %}

```bash
gcloud config set project <your-project-id>
agents-cli deploy
```
{%- else %}

This project has no deployment target. Add one with `agents-cli scaffold enhance --deployment-target <target>`.
{%- endif %}

## Learn More

- [ADK for Go Documentation](https://google.github.io/adk-docs/)
- [Gemini platform documentation](https://cloud.google.com/vertex-ai/docs)
