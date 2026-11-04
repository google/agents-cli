# Project structure

*For developers who want to understand the layout of a generated agent project.*

When you run `agents-cli create my-agent --prototype --yes`, you get a ready-to-run project. This page explains what each file does in a project made from the default `adk_python` template. For the `adk_go` template, see [Go projects](#go-projects). LangChain and Claude projects have a different layout; see [First-party frameworks](extensions/first-party.md).

---

## Directory layout

```
my-agent/
├── app/                          # Your agent code
│   ├── __init__.py               # Registers the app (exports `app`)
│   ├── agent.py                  # Agent definition: instructions, model, tools
│   ├── fast_api_app.py           # FastAPI server: telemetry setup, A2A routes
│   └── app_utils/                # Utilities
│       ├── services.py           # Shared session & artifact services
│       └── a2a.py                # A2A route wiring
│
├── tests/
│   ├── eval/                     # Evaluation test cases
│   │   ├── datasets/
│   │   │   └── basic-dataset.json    # Default eval cases
│   │   ├── eval_config.yaml          # Evaluation metrics configuration
│   │   └── response_quality.py       # Local LLM-as-judge for the default metric
│   ├── integration/
│   │   ├── test_agent.py         # Integration test (runs agent end-to-end)
│   │   └── test_server_e2e.py    # Drives a running server
│   └── unit/
│       └── test_dummy.py         # Placeholder for unit tests
│
├── pyproject.toml                # Project config and dependencies
├── agents-cli-manifest.yaml      # Configuration for agents-cli
├── GEMINI.md                     # Guidance file for coding agents
├── Dockerfile                    # Container image for deployment
├── README.md
├── .env                          # Environment variables (project ID, location)
├── .env.example
└── uv.lock                       # Locked dependency versions (written by agents-cli install)
```

---

## Key files

### `app/agent.py`

Your agent lives in `app/agent.py`. The default template looks like this:

```python title="app/agent.py"
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

MODEL = "gemini-3.8-flash"


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
    name="my_agent",  # Derived from the project name; keep in sync with agents-cli-manifest.yaml
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="You are a helpful AI assistant designed to provide accurate and useful information.",
    tools=[get_weather, get_current_time],
)

app = App(
    root_agent=root_agent,
    name="app",  # Must match the last part of agent_directory
)
```

The four key parts:

1. **Tool functions** are plain Python functions with docstrings. The docstring tells the LLM when to use the tool.
2. **`Agent`** combines a model, an instruction (system prompt), and tools. Its `name` comes from the project name and must match `root_agent_name` in `agents-cli-manifest.yaml`, because telemetry reports it as `gen_ai.agent.name`.
3. **`App`** wraps the agent for serving. The `name` must match the last part of `agent_directory`: `app` by default, or `my_agent` for `src/my_agent`.
4. **Model** defaults to `gemini-3.8-flash`. Change it with the `MODEL` constant at the top of `agent.py`.

### `pyproject.toml`

Contains Python project metadata and dependencies:

```toml title="pyproject.toml"
[project]
name = "my-agent"
version = "0.1.0"
requires-python = ">=3.11,<3.14"
dependencies = [
    "google-adk[gcp,otel-gcp,bigquery-analytics]>=2.12.0,<3.0.0",
    # ... other dependencies
]
```

### `agents-cli-manifest.yaml`

Contains agents-cli project metadata and configuration:

```yaml title="agents-cli-manifest.yaml"
name: my-agent
agent_directory: app
language: python
create_params:
  root_agent_name: my_agent
  deployment_target: none
  session_type: in_memory
```

- **`agent_directory`** tells `agents-cli` commands where your agent code is. It defaults to `app` and can be nested, such as `src/my_agent`. Set it at scaffold time with `--agent-directory`.
- **`language`** tells the dev commands (`playground`, `run`, `lint`, `install`) which toolchain to use.
- **`create_params`** records how the project was created. The `agents-cli scaffold upgrade` command uses it to preserve your configuration.

### `tests/eval/datasets/basic-dataset.json`

Default evaluation cases. Each case defines a user message and the session context for running it. See the [Evaluation guide](evaluation.md) for the full schema.

### `GEMINI.md`

A guidance file that coding agents (Antigravity CLI, Claude Code, etc.) read automatically. It contains project-specific instructions: framework patterns (ADK in the default templates), coding conventions, and workflow guidance. The file is named `GEMINI.md` by default. Pass `--agent-guidance-filename` at scaffold time to name it for your coding agent instead, such as `CLAUDE.md` or `AGENTS.md`. You don't need to read or edit the file unless you want to customize how coding agents work with your project.

### `.env`

Environment variables for local development:

```bash title=".env"
GOOGLE_GENAI_USE_ENTERPRISE=true
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=global
```

The agent reads these at runtime. Set them to match your Google Cloud project. To use a Gemini API key from Google AI Studio instead, comment out these three lines and set `GEMINI_API_KEY`. That swap works in Python projects. In a Go project, the `.env` sets `GOOGLE_GENAI_USE_VERTEXAI=true` and the generated `app/agent.go` forces the Gemini platform backend (`genai.BackendVertexAI`), so an API key works only after you change `agent.go`.

---

## With deployment infrastructure

When you create a project with a deployment target (or add one with `agents-cli scaffold enhance`), additional directories appear:

```
my-agent/
├── deployment/
│   └── terraform/
│       ├── single-project/   # One-project setup (agents-cli infra single-project)
│       ├── cicd/             # Staging, prod, and CI/CD setup (agents-cli infra cicd, if a runner is selected)
│       └── shared/           # Telemetry schemas shared by both
│
├── tests/load_test/          # Load test (if a runner is selected)
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

Agent Runtime projects also get `app/app_utils/reasoning_engine_adapter.py`.

<a id="adding-infrastructure-later"></a>

### Add infrastructure later

Start with a prototype and add infrastructure when you need it:

```bash
# Add Cloud Run deployment
agents-cli scaffold enhance --deployment-target cloud_run

# Preview changes without applying
agents-cli scaffold enhance --deployment-target cloud_run --dry-run
```

---

## Go projects

A project made with `--agent adk_go` has the same manifest, guidance file, and deployment directories, a `.env` that sets `GOOGLE_GENAI_USE_VERTEXAI` instead of `GOOGLE_GENAI_USE_ENTERPRISE`, with Go sources in place of the Python ones:

```
my-agent/
├── app/
│   ├── agent.go              # NewRootAgent: model, instruction, tools
│   └── agent_test.go
├── appinfo/                  # Serves app info for agents-cli eval
├── sessions/                 # Session service for the chosen backend
├── eval/                     # Evaluation datasets and config
├── e2e/
│   ├── integration/          # Drives a running server
│   └── load_test/
├── main.go                   # Telemetry and launcher wiring
├── observability.go          # OpenTelemetry traces and logs to Google Cloud
├── apploader.go              # Serves the root agent under the app name
├── appurl.go                 # Base URL in the A2A agent card
├── Dockerfile
├── Makefile
├── agents-cli-manifest.yaml  # language: go
├── .golangci.yml
└── go.mod
```

Customize `app/agent.go` and keep the rest. The template generates the serving, session, and A2A wiring for you.
