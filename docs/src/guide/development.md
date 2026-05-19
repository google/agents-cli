# Development Guide

This guide covers the full development workflow — from defining what you're building to monitoring it in production. It follows the same phases your coding agent uses via the `google-agents-cli-workflow` skill.

---

## Phase 0: Understand

Before writing any code, define what you're building.

If you're working with a coding agent, it will ask you these questions automatically. If you're working manually, answer them yourself:

1. **What problem will the agent solve?** — Core purpose and capabilities
2. **External APIs or data sources needed?** — Tools, integrations, auth requirements
3. **Safety constraints?** — What the agent must NOT do
4. **Deployment preference?** — Prototype first, or full deployment (Agent Runtime, Cloud Run, GKE)?

Save your answers to `.agents-cli-spec.md` in the current directory — overview, example use cases, tools required, constraints, success criteria.

---

## Phase 1: Scaffold

Create a new project from a template:

```bash
agents-cli create my-agent
```

Choose your agent template (`adk`, `adk_a2a`, `agentic_rag`) and deployment target during creation. For fast prototyping without infrastructure decisions:

```bash
agents-cli create my-agent --prototype --yes
```

You can add deployment support later with `agents-cli scaffold enhance`.

See [Agent Templates](templates.md) for all options.

---

## Phase 2: Build & Iterate

### With a coding agent

Open your coding agent and activate the workflow skill:

```
/google-agents-cli-workflow
```

Describe what you want to build. Your coding agent uses the installed skills to write agent logic, create tools, and test changes — all following ADK best practices.

### Manually

Edit your agent logic in `app/agent.py` and test with:

- `agents-cli playground` — launches the ADK web playground at `localhost:8080` with hot reload
- `agents-cli run "your prompt"` — quick smoke test from the terminal

### Code Quality

```bash
agents-cli lint                                # Ruff checks and formatting
uv run pytest tests/unit tests/integration     # Run unit and integration tests
```

### Package Management

Add and remove dependencies with [uv](https://docs.astral.sh/uv/):

- `uv add <package>`
- `uv remove <package>`

---

## Phase 3: Evaluate

Run structured evaluations to validate agent behavior. This uses the [ADK eval CLI](https://google.github.io/adk-docs/evaluate/) under the hood.

```bash
agents-cli eval run
```

Expect **5-10+ iterations** of the eval-fix loop before your agent consistently passes. Start with 1-2 core eval cases, fix failures, then expand coverage.

See the [Evaluation Guide](evaluation.md) for metrics, evalset schemas, and the full methodology.

---

## Phase 4: Deploy

Once evaluation thresholds are met, deploy to Google Cloud.

1. **Add a deployment target** (if you started with `--prototype`):

    ```bash
    agents-cli scaffold enhance --deployment-target cloud_run
    ```

2. **Deploy**:

    ```bash
    agents-cli deploy
    ```

!!! tip
    To enable observability features (prompt-response logging, content logs), run `agents-cli infra single-project` after deploying. See the [Observability Guide](observability/index.md) for details.

For production pipelines with staging, approval gates, and CI/CD, see [Deployment](deployment.md) and [CI/CD & Production](cicd.md).

---

## Phase 5: Publish (optional)

Register your deployed agent with Gemini Enterprise:

```bash
agents-cli publish gemini-enterprise
```

Not all agents need this — only if you're distributing through Gemini Enterprise.

---

## Phase 6: Observe

Monitor your agent in production. Cloud Trace is enabled by default in all deployed agents — no configuration needed.

- **Cloud Trace** — distributed tracing, latency analysis, error visibility
- **BigQuery Agent Analytics** — opt-in advanced analytics for token usage, conversation patterns, and LLM-as-judge scoring

See the [Observability Guide](observability/index.md) for setup and usage.

---

For all commands and flags, see the [CLI Reference](../cli/index.md). For details on the skills your coding agent uses at each phase, see [Skills](../reference/skills.md).
