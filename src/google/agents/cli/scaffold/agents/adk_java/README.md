# {{cookiecutter.project_name}}

A Java agent built with Google's Agent Development Kit (ADK).

## Project Structure

```
{{cookiecutter.project_name}}/
├── build.gradle.kts         # Gradle build file
├── settings.gradle.kts      # Gradle project settings
├── src/
│   ├── main/
│   │   ├── java/{{cookiecutter.java_package_path}}/
│   │   │   ├── Main.java    # Application entry point
│   │   │   ├── Agent.java   # Agent implementation
│   │   │   └── A2AController.java # A2A agent card and endpoint
│   │   └── resources/
│   │       └── application.properties
│   └── test/java/{{cookiecutter.java_package_path}}/
│       └── unit/            # Unit tests
│       └── e2e/             # End-to-end tests
│           ├── integration/ # Server integration tests
│           └── load_test/   # Load tests
├── deployment/
│   └── terraform/           # Infrastructure as Code
{%- if cookiecutter.deployment_target == 'gke' %}
│   ├── k8s/                 # Kubernetes manifests for GKE deployment
{%- endif %}
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
   Open http://localhost:8080/dev-ui/ in your browser.

## Commands

| Command | Description |
|---------|-------------|
| `make install` | Download Gradle dependencies |
| `make playground` | Launch local development environment with web UI |
| `make test` | Run unit and e2e integration tests |
| `make build` | Build the application |
| `make clean` | Clean build artifacts |
| `make lint` | Run code quality checks |
| `make local-backend` | Start server on port 8080 |
| `make deploy` | Deploy to Cloud Run |
| `make load-test` | Run load tests (requires running server) |
| `make inspector` | Launch A2A Protocol Inspector |
| `make setup-dev-env` | Set up Terraform infrastructure |
| `make publish-gemini` | Register agent with Gemini (alias of `make publish-gemini-enterprise`) |

## Deployment

### Quick Deploy

```bash
make deploy
```

### CI/CD Pipeline
{%- if cookiecutter.cicd_runner == 'google_cloud_build' %}

This project includes CI/CD configuration using **Cloud Build** (`.cloudbuild/` directory).
{%- elif cookiecutter.cicd_runner == 'github_actions' %}

This project includes CI/CD configuration using **GitHub Actions** (`.github/workflows/` directory).
{%- endif %}

See the CI/CD pipeline documentation for detailed deployment instructions.

## Testing

```bash
# Run unit and e2e integration tests
make test

# Run load tests locally (start server first with `make local-backend`)
make load-test

# Run load tests against remote deployment
make load-test URL=https://your-service.run.app

# Run load tests with custom parameters
make load-test DURATION=60 USERS=20 RAMP=5
```

Use `make inspector` to launch the A2A Protocol Inspector for interactive testing.

## Keeping Up-to-Date

To upgrade this project to the latest agents-cli version:

```bash
uvx google-agents-cli scaffold upgrade
```

This intelligently merges updates while preserving your customizations. Use `--dry-run` to preview changes first.

## Learn More

- [ADK for Java Documentation](https://google.github.io/adk-docs/)
- [Gemini platform documentation](https://cloud.google.com/vertex-ai/docs)
