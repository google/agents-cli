# Project Structure

*For developers who want to understand the layout of a generated agent project.*

When you run `agents-cli create my-agent --prototype --yes`, you get a ready-to-run project. This page explains what each file does.

---

## Directory Layout

```
my-agent/
├── app/                          # Your agent code
│   ├── __init__.py               # Registers the app (exports `app`)
│   ├── agent.py                  # Agent definition — instructions, model, tools
│   └── app_utils/                # Utilities (telemetry, converters)
│       ├── __init__.py
│       ├── telemetry.py          # OpenTelemetry setup for Cloud Trace
│       ├── typing.py             # Request/response Pydantic models
│       └── gcs.py                # GCS utility functions
│
├── tests/
│   ├── eval/                     # Evaluation test cases
│   │   ├── datasets/
│   │   │   └── basic-dataset.json    # Default eval cases
│   │   └── eval_config.yaml          # Evaluation metrics configuration
│   ├── integration/
│   │   └── test_agent.py         # Integration test (runs agent end-to-end)
│   └── unit/
│       └── test_dummy.py         # Placeholder for unit tests
│
├── pyproject.toml                # Project config and dependencies
├── agents-cli-manifest.yaml      # Configuration for agents-cli
├── GEMINI.md                     # Guidance file for coding agents
├── Makefile                      # Shortcut commands (make dev, make eval, etc.)
├── .env                          # Environment variables (project ID, location)
└── uv.lock                       # Locked dependency versions
```

---

## Key Files

### `app/agent.py`

This is where your agent lives. The default template looks like this:

```python title="app/agent.py"
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types


def get_weather(query: str) -> str:
    """Simulates a web search. Use it get information on weather."""
    if "sf" in query.lower() or "san francisco" in query.lower():
        return "It's 60 degrees and foggy."
    return "It's 90 degrees and sunny."


def get_current_time(query: str) -> str:
    """Simulates getting the current time for a city."""
    # ... implementation
    return f"The current time is ..."


root_agent = Agent(
    name="root_agent",
    model=Gemini(
        model="gemini-flash-latest",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="You are a helpful AI assistant.",
    tools=[get_weather, get_current_time],
)

app = App(
    root_agent=root_agent,
    name="app",  # Must match the agent directory name
)
```

The four key parts:

1. **Tool functions** — plain Python functions with docstrings. The docstring tells the LLM when to use the tool.
2. **`Agent`** — combines a model, instruction (system prompt), and tools.
3. **`App`** — wraps the agent for serving. The `name` must match the directory name (`app`).
4. **Model** — defaults to `gemini-flash-latest`. Change it in the `Gemini()` constructor.

### `pyproject.toml`

Contains Python project metadata and dependencies:

```toml title="pyproject.toml"
[project]
name = "my-agent"
version = "0.0.1"
requires-python = ">=3.11"
dependencies = [
    "google-adk[gcp]>=2.0.0,<3.0.0",
    # ... other dependencies
]
```

### `agents-cli-manifest.yaml`

Contains agents-cli project metadata and configuration:

```yaml title="agents-cli-manifest.yaml"
name: my-agent
agent_directory: app
create_params:
  deployment_target: none
  session_type: in_memory
```

- **`agent_directory`** — tells `agents-cli` commands where your agent code is.
- **`create_params`** — records how the project was created. Used by `agents-cli scaffold upgrade` to preserve your configuration.

### `tests/eval/datasets/basic-dataset.json`

Default evaluation cases. Each case defines a user message and the session context for running it. See the [Evaluation Guide](evaluation.md) for the full schema.

### `GEMINI.md`

A guidance file that coding agents (Antigravity CLI, Claude Code, etc.) read automatically. It contains project-specific instructions — ADK patterns, coding conventions, and workflow guidance. You don't need to read or edit this file unless you want to customize how coding agents work with your project.

### `.env`

Environment variables for local development:

```bash title=".env"
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-east1
```

These are read by the agent at runtime. Set them to match your Google Cloud project, or leave them empty if using a Gemini API key.

---

## With Deployment Infrastructure

When you create a project with a deployment target (or add one with `agents-cli scaffold enhance`), additional directories appear:

```
my-agent/
├── deployment/
│   └── terraform/
│       ├── dev/              # Dev environment Terraform
│       ├── staging/          # Staging Terraform
│       ├── prod/             # Production Terraform
│       └── variables.tf      # Shared variables
│
├── .github/                  # GitHub Actions CI/CD (if selected)
│   └── workflows/
│       ├── pr_checks.yaml
│       ├── staging.yaml
│       └── deploy-to-prod.yaml
│
└── .cloudbuild/              # Cloud Build CI/CD (if selected)
    ├── pr_checks.yaml
    ├── staging.yaml
    └── deploy-to-prod.yaml
```

### Adding Infrastructure Later

Start with a prototype and add infrastructure when you need it:

```bash
# Add Cloud Run deployment
agents-cli scaffold enhance --deployment-target cloud_run

# Preview changes without applying
agents-cli scaffold enhance --deployment-target cloud_run --dry-run
```
