---
name: agents-cli-scaffold
description: >
  This skill should be used when the user wants to "create an agent project",
  "build me a new agent", "add CI/CD to my project", "add deployment",
  "enhance my project", "extract an agent", or "upgrade my project".
  Covers `agents-cli init`, `enhance`, `extract`, and `upgrade` commands,
  template options, deployment targets, and the prototype-first workflow.
  Do NOT use for writing agent code (use agents-cli-adk-code) or
  deployment operations (use agents-cli-deploy).
metadata:
  author: Google
  license: Apache-2.0
  version: 0.0.3
  requires:
    bins:
      - agents-cli
    install: "uv tool install agents-cli"
---

# ADK Project Scaffolding Guide

> **Requires:** `agents-cli` (`uv tool install agents-cli`) — [install uv](https://docs.astral.sh/uv/getting-started/installation/index.md) first if needed.

Use the `agents-cli` CLI to create new ADK agent projects or enhance existing ones with deployment, CI/CD, and infrastructure scaffolding.

---

## Step 1: Choose Architecture

Assumes you have a clear understanding of the agent's purpose and constraints — including deployment preference, datastore needs, A2A, CI/CD, and session storage. If not, see **Phase 0** of `/agents-cli-workflow` first; it has the full list of questions to ask the user.

**Mapping user choices to CLI flags:**

| Choice | CLI flag |
|--------|----------|
| RAG with vector search | `--agent agentic_rag --datastore vertex_ai_vector_search` |
| RAG with document search | `--agent agentic_rag --datastore vertex_ai_search` |
| A2A protocol | `--agent adk_a2a` |
| Prototype (no deployment) | `--prototype` |
| Deployment target | `--deployment-target <agent_engine\|cloud_run\|gke>` |
| CI/CD runner | `--cicd-runner <github_actions\|cloud_build>` |
| Session storage | `--session-type <in_memory\|cloud_sql\|agent_engine>` |

---

## Step 2: Create or Enhance the Project

### Create a New Project

```bash
agents-cli init <project-name> \
  --agent <template> \
  --deployment-target <target> \
  --region <region> \
  --prototype
```

**Constraints:**
- Project name must be **26 characters or less**, lowercase letters, numbers, and hyphens only.
- Do NOT `mkdir` the project directory before running `init` — the CLI creates it automatically. If you mkdir first, `init` will fail or behave unexpectedly.
- Auto-detect the guidance filename based on the IDE you are running in and pass `--agent-guidance-filename` accordingly (`GEMINI.md` for Gemini CLI, `CLAUDE.md` for Claude Code, `AGENTS.md` for OpenAI Codex/other).
- When enhancing an existing project, check where the agent code lives. If it's not in `app/`, pass `--agent-directory <dir>` (e.g. `--agent-directory agent`). Getting this wrong causes enhance to miss or misplace files.

### Reference Files

| File | Contents |
|------|----------|
| `references/flags.md` | Full flag reference for `init` and `enhance` commands |

### Enhance an Existing Project

```bash
agents-cli enhance . --deployment-target <target>
agents-cli enhance . --cicd-runner <runner>
```

Run this from inside the project directory (or pass the path instead of `.`).

### Extract a Shareable Agent

Extract a minimal, shareable version of your agent — strips deployment infrastructure while preserving core agent logic:

```bash
agents-cli extract <output-path>             # Extract from current directory
agents-cli extract <output-path> --source .   # Explicit source directory
agents-cli extract <output-path> --dry-run    # Preview what would be extracted
agents-cli extract <output-path> --force      # Overwrite output if it exists
```

### Upgrade a Project

Upgrade an existing project to a newer agents-cli version, intelligently applying updates while preserving your customizations:

```bash
agents-cli upgrade                # Upgrade current directory
agents-cli upgrade <project-path> # Upgrade specific project
agents-cli upgrade --dry-run      # Preview changes without applying
agents-cli upgrade --auto-approve  # Auto-apply non-conflicting changes
```

### Execution Modes

The CLI defaults to **strict programmatic mode** — all required params must be supplied as CLI flags or a `UsageError` is raised. No approval flags needed. Pass all required params explicitly.

### Common Workflows

**Always ask the user before running these commands.** Present the options (CI/CD runner, deployment target, etc.) and confirm before executing.

```bash
# Add deployment to an existing prototype (strict programmatic)
agents-cli enhance . --deployment-target agent_engine

# Add CI/CD pipeline (ask: GitHub Actions or Cloud Build?)
agents-cli enhance . --cicd-runner github_actions
```

---

## Template Options

| Template | Deployment | Description |
|----------|------------|-------------|
| `adk` | Agent Engine, Cloud Run, GKE | Standard ADK agent (default) |
| `adk_a2a` | Agent Engine, Cloud Run, GKE | Agent-to-agent coordination (A2A protocol) |
| `agentic_rag` | Agent Engine, Cloud Run, GKE | RAG with data ingestion pipeline |

---

## Deployment Options

| Target | Description |
|--------|-------------|
| `agent_engine` | Managed by Google (Vertex AI Agent Engine). Sessions handled automatically. |
| `cloud_run` | Container-based deployment. More control, requires Dockerfile. |
| `gke` | Container-based on GKE Autopilot. Full Kubernetes control. |
| `none` | No deployment scaffolding. Code only. |

### "Prototype First" Pattern (Recommended)

Start with `--prototype` to skip CI/CD and Terraform. Focus on getting the agent working first, then add deployment later with `enhance`:

```bash
# Step 1: Create a prototype
agents-cli init my-agent --agent adk --prototype

# Step 2: Iterate on the agent code...

# Step 3: Add deployment when ready
agents-cli enhance . --deployment-target agent_engine
```

### Agent Engine and session_type

When using `agent_engine` as the deployment target, Agent Engine manages sessions internally. If your code sets a `session_type`, clear it — Agent Engine overrides it.

---

## Step 3: Load Dev Workflow

After scaffolding, save `DESIGN_SPEC.md` to the project root if it isn't there already.

**Then immediately load `/agents-cli-workflow`** — it contains the development workflow, coding guidelines, and operational rules you must follow when implementing the agent.

**Key files to customize:** `app/agent.py` (instruction, tools, model), `app/tools.py` (custom tool functions), `.env` (project ID, location, API keys).
**Files to preserve:** `pyproject.toml` `[tool.agents-cli]` section (CLI reads this), deployment configs under `deployment/`, `Makefile`, `app/__init__.py` (the `App(name=...)` must match the directory name — default `app`).

**RAG projects (`agentic_rag`) — provision datastore first:**
Before running `agents-cli dev` or testing your RAG agent, you must provision the datastore and ingest data:
```bash
agents-cli setup-datastore   # Provision datastore infrastructure
agents-cli data-ingestion    # Ingest data into the datastore
```
Use `setup-datastore` — **not** `infra dev`. Both provision the datastore, but `setup-datastore` is faster because it skips unrelated Terraform. Without this step, the agent won't have data to search over.

**Verifying your agent works:** Use `agents-cli run "test prompt"` for quick smoke tests, then `agents-cli eval` for systematic validation. Do NOT write pytest tests that assert on LLM response content — that belongs in eval.

---

## Scaffold as Reference

When you need specific files (Terraform, CI/CD workflows, Dockerfile) but don't want to scaffold the current project directly, create a temporary reference project in `/tmp/`:

```bash
agents-cli init /tmp/ref-project \
  --agent adk \
  --deployment-target cloud_run
```

Inspect the generated files, adapt what you need, and copy into the actual project. Delete the reference project when done.

This is useful for:
- Non-standard project structures that `enhance` can't handle
- Cherry-picking specific infrastructure files
- Understanding what the CLI generates before committing to it

---

## Critical Rules

- **NEVER change the model** in existing code unless explicitly asked
- **NEVER `mkdir` before `init`** — the CLI creates the directory; pre-creating it causes enhance mode instead of create mode
- **NEVER create a Git repo or push to remote without asking** — confirm repo name, public vs private, and whether the user wants it created at all
- **Always ask before choosing CI/CD runner** — present GitHub Actions and Cloud Build as options, don't default silently
- **Agent Engine clears session_type** — if deploying to `agent_engine`, remove any `session_type` setting from your code
- **Start with `--prototype`** for quick iteration — add deployment later with `enhance`
- **Project names** must be ≤26 characters, lowercase, letters/numbers/hyphens only
- **NEVER write A2A code from scratch** — the A2A Python API surface (import paths, `AgentCard` schema, `to_a2a()` signature) is non-trivial and changes across versions. Always use `--agent adk_a2a` to scaffold A2A projects.

---

# Examples

Using scaffold as reference:
User says: "I need a Dockerfile for my non-standard project"
Actions:
1. Create temp project: `agents-cli init /tmp/ref --agent adk --deployment-target cloud_run`
2. Copy relevant files (Dockerfile, etc.) from /tmp/ref
3. Delete temp project
Result: Infrastructure files adapted to the actual project

---

A2A project:
User says: "Build me a Python agent that exposes A2A and deploys to Cloud Run"
Actions:
1. Follow the standard flow (understand requirements, choose architecture, scaffold)
2. `agents-cli init my-a2a-agent --agent adk_a2a --deployment-target cloud_run --prototype`
Result: Valid A2A imports and Dockerfile — no manual A2A code written.

---

## Troubleshooting

### `agents-cli` command not found

See `/agents-cli-workflow` → **Setup** section.

---

## Related Skills

- `/agents-cli-workflow` — Development workflow, coding guidelines, and the build-evaluate-deploy lifecycle
- `/agents-cli-adk-code` — ADK Python API quick reference for writing agent code
- `/agents-cli-deploy` — Deployment targets, CI/CD pipelines, and production workflows
- `/agents-cli-eval` — Evaluation methodology, evalset schema, and the eval-fix loop
