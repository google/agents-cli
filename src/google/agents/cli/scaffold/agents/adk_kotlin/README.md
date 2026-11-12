# {{cookiecutter.project_name}}

A Kotlin agent built with Google's Agent Development Kit (ADK).

## Project Structure

```
{{cookiecutter.project_name}}/
├── build.gradle.kts         # Gradle build file
├── settings.gradle.kts      # Gradle project settings
├── src/main/kotlin/{{cookiecutter.java_package_path}}/
│   ├── Agent.kt             # Agent implementation
│   └── Main.kt              # ADK API server (development UI with --dev)
├── src/test/kotlin/{{cookiecutter.java_package_path}}/
│   └── AgentTest.kt         # Unit tests
├── deployment/
│   └── terraform/           # Infrastructure as Code
├── Dockerfile               # Container build
├── GEMINI.md                # AI-assisted development guide
└── Makefile                 # Common commands
```

> **Tip:** Use [Antigravity CLI](https://antigravity.google/) for AI-assisted development - project context is pre-configured in `GEMINI.md`.

## Requirements

- Java 17 or later
- Gradle 9.5 or later
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
   Open http://localhost:8080 in your browser.

## Commands

| Command | Description |
|---------|-------------|
| `make install` | Download Gradle dependencies |
| `make playground` | Launch local development environment with web UI |
| `make local-backend` | Start the ADK API server on port 8080 |
| `make test` | Run tests |
| `make lint` | Run code quality checks using ktlint |
| `make build` | Build the application |
| `make clean` | Clean build artifacts |
{%- if cookiecutter.deployment_target == 'cloud_run' %}
| `make deploy` | Deploy to Cloud Run |
{%- endif %}
| `make setup-dev-env` | Set up Terraform infrastructure |

## Deployment
{%- if cookiecutter.deployment_target == 'cloud_run' %}

### Quick Deploy

```bash
make deploy
```
{%- endif %}

### CI/CD Pipeline
{%- if cookiecutter.cicd_runner == 'google_cloud_build' %}

This project includes CI/CD configuration using **Cloud Build** (`.cloudbuild/` directory).
{%- elif cookiecutter.cicd_runner == 'github_actions' %}

This project includes CI/CD configuration using **GitHub Actions** (`.github/workflows/` directory).
{%- endif %}

See the CI/CD pipeline documentation for detailed deployment instructions.

## Keeping Up-to-Date

To upgrade this project to the latest agents-cli version:

```bash
uvx google-agents-cli scaffold upgrade
```

This intelligently merges updates while preserving your customizations. Use `--dry-run` to preview changes first.

## Learn More

- [ADK for Kotlin Documentation](https://adk.dev/get-started/kotlin/)
- [Gemini platform documentation](https://cloud.google.com/vertex-ai/docs)
