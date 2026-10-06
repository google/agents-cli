---
name: google-agents-cli-scaffold
description: >
  This skill should be used when the user wants to "create an agent project",
  "start a new ADK project", "build me a new agent", "add CI/CD to my project",
  "add deployment", "enhance my project", or "upgrade my project".
  Part of the agents-cli skills suite.
  Covers `agents-cli scaffold create`, `scaffold enhance`, and `scaffold upgrade` commands,
  template options, deployment targets, and the prototype-first workflow.
  Do NOT use for writing agent code (ADK projects: use google-agents-cli-adk-code) or
  deployment operations (use google-agents-cli-deploy).
metadata:
  author: Google
  license: Apache-2.0
  version: 1.9.0
  requires:
    bins:
      - agents-cli
    install: "uv tool install google-agents-cli"
---

# Project Scaffolding Guide

> **Requires:** `agents-cli` (`uv tool install google-agents-cli`) — [install uv](https://docs.astral.sh/uv/getting-started/installation/index.md) first if needed.

Use the `agents-cli` CLI to create new agent projects or enhance existing ones with deployment, CI/CD, and infrastructure scaffolding.

---

## Prerequisite: Clarify Requirements (MANDATORY for new projects)

**Before scaffolding a new project, load `/google-agents-cli-workflow` and complete Phase 0** — clarify the user's requirements before running any `scaffold create` command. Ask what the agent should do, what tools/APIs it needs, and whether they want a prototype or full deployment.

---

## Step 1: Choose Architecture

**Mapping user choices to CLI flags:**

| Choice | CLI flag |
|--------|----------|
| Retrieval/RAG, sandboxed execution, cross-session memory, OAuth consent, guardrails, scheduled runs | **No flag** — these come from clone-and-study recipes. **ADK Python:** see the topic index in `/google-agents-cli-adk-code` → `references/samples.md`; on other frameworks, see the sample index the framework template ships |
| A2A protocol | built into the scaffolded app — scaffold normally (**ADK Python:** `--agent adk`, the default; **ADK Go:** `--agent adk_go`) |
| Prototype (no deployment) | `--prototype` |
| Deployment target | `--deployment-target <agent_runtime\|cloud_run\|gke>` |
| CI/CD runner | `--cicd-runner <github_actions\|google_cloud_build>` |
| Session storage | `--session-type <in_memory\|cloud_sql\|agent_platform_sessions>` |

### Product name mapping

Older names → CLI values (`vertexai` SDK package name unchanged):

- Agent Engine / Vertex AI Agent Engine → `--deployment-target agent_runtime`
- Agent Engine sessions / Agent Platform Sessions → `--session-type agent_platform_sessions`
- Vertex AI Search / Vertex AI Vector Search / RAG → clone-and-study recipe, not a flag

> **Removed flags.** `--datastore`, the `agentic_rag` template, and `agents-cli infra datastore` /
> `agents-cli data-ingestion` no longer exist. If you reach for one, you want a recipe instead.

---

## Step 2: Create or Enhance the Project

### Create a New Project

```bash
agents-cli scaffold create <project-name> \
  --agent <template> \
  --deployment-target <target> \
  --region <region> \
  --prototype
```

**Constraints:**
- Project name must be **26 characters or less**, lowercase letters, numbers, and hyphens only.
- Do NOT `mkdir` the project directory before running `create` — the CLI creates it automatically. If you mkdir first, `create` will fail or behave unexpectedly.
- Auto-detect the guidance filename based on the IDE you are running in and pass `--agent-guidance-filename` accordingly (`GEMINI.md` for Antigravity CLI, `CLAUDE.md` for Claude Code, `AGENTS.md` for OpenAI Codex/other).
- When enhancing an existing project, check where the agent code lives. If it's not in `app/`, pass `--agent-directory <dir>` (e.g. `--agent-directory agent`). Getting this wrong causes enhance to miss or misplace files.

### Reference Files

| File | Contents |
|------|----------|
| `references/flags.md` | Full flag reference for `create` and `enhance` commands |

### Enhance an Existing Project

```bash
agents-cli scaffold enhance . --deployment-target <target>
agents-cli scaffold enhance . --cicd-runner <runner>
```

Run this from inside the project directory (or pass the path instead of `.`).

### Upgrade a Project

Upgrade an existing project to a newer agents-cli version, intelligently applying updates while preserving your customizations:

```bash
agents-cli scaffold upgrade                # Upgrade current directory
agents-cli scaffold upgrade <project-path> # Upgrade specific project
agents-cli scaffold upgrade --dry-run      # Preview changes without applying
agents-cli scaffold upgrade --auto-approve  # Auto-apply non-conflicting changes
agents-cli scaffold upgrade --force        # Overwrite scaffolding files when smart-merge fails
agents-cli scaffold upgrade --force --dry-run  # List what --force would overwrite
```

`--force` is a fallback for when smart-merge fails. It overwrites scaffolding files with the new template and keeps agent code, config files, `README.md`, the agent guidance file and eval datasets and config. It never removes dependencies or files the new template dropped (for example, a removed CI workflow), so remove unused ones by hand. Afterwards, check your imports and run your tests. Use `--dry-run` first to see which files change.

### Execution Modes

The CLI defaults to **strict programmatic mode** — all required params must be supplied as CLI flags or a `UsageError` is raised. No approval flags needed. Pass all required params explicitly.

### Common Workflows

**Always ask the user before running these commands.** Present the options (CI/CD runner, deployment target, etc.) and confirm before executing.

```bash
# Add deployment to an existing prototype (strict programmatic)
agents-cli scaffold enhance . --deployment-target agent_runtime

# Add CI/CD pipeline (ask: GitHub Actions or Cloud Build?)
agents-cli scaffold enhance . --cicd-runner github_actions
```

---

## Template Options

| Template | Language | Deployment | Description |
|----------|----------|------------|-------------|
| `adk` | Python | Agent Runtime, Cloud Run, GKE | Standard ADK agent (default); A2A protocol built in |
| `adk_go` | Go | Agent Runtime, Cloud Run, GKE | Standard ADK Go agent; A2A protocol built in |

> **`adk` and `adk_go` are the only built-in templates.** `adk` is the default, so a Go project
> needs `--agent adk_go` explicitly.
> Other frameworks ship as template repos you scaffold
> from directly: `--agent google/agents-cli/extensions/langchain/template@v1.9.0`, with nothing installed. The first-party LangChain
> template is `extensions/langchain/template/` in the agents-cli repo; see
> `/google-agents-cli-workflow` → `references/extension.md` to publish your own. Capabilities
> beyond the template — retrieval, sandboxed execution, memory, OAuth, guardrails — are
> clone-and-study recipes, not templates. **ADK Python:** see the topic index in
> `/google-agents-cli-adk-code` → `references/samples.md`.

> **Live and voice agents:** no template. Scaffold `adk`, then follow the conversion checklist
> in `/google-agents-cli-adk-code` (`references/adk-python-live.md`, "Converting a scaffolded
> project to Live"). The model swap is one of five edits.

---

## Deployment Options

| Target | Description |
|--------|-------------|
| `agent_runtime` | Managed by Google (Vertex AI Agent Runtime). Container-based — Agent Engine builds the project Dockerfile. Sessions handled automatically. |
| `cloud_run` | Container-based deployment. More control; you build and deploy the Dockerfile. |
| `gke` | Container-based on GKE Autopilot. Full Kubernetes control. |
| `none` | No deployment scaffolding. Code only (still includes a Dockerfile). |

### "Prototype First" Pattern (Recommended)

Start with `--prototype` to skip CI/CD and Terraform. Focus on getting the agent working first, then add deployment later with `scaffold enhance`:

```bash
# Step 1: Create a prototype
agents-cli scaffold create my-agent --agent adk --prototype

# Step 2: Iterate on the agent code...

# Step 3: Add deployment when ready
agents-cli scaffold enhance . --deployment-target agent_runtime
```

### Agent Runtime and session_type

When using `agent_runtime` as the deployment target, Agent Runtime manages sessions internally. If your code sets a `session_type`, clear it — Agent Runtime overrides it.

---

## Step 3: Load Dev Workflow

After scaffolding, immediately load `/google-agents-cli-workflow` — it contains the development workflow, coding guidelines, and operational rules you must follow when implementing the agent.

What you edit differs by language; `/google-agents-cli-adk-code` has the annotated tree for each,
in `references/adk-python.md` and `references/adk-go.md`.

- **ADK Python (`--agent adk`)** — customize `app/agent.py` and `app/tools.py`.
- **ADK Go (`--agent adk_go`)** — customize `app/agent.go`.

`.env` is yours. Preserve everything else the template generated — it wires up serving, sessions
and the built-in A2A surface.

**Adapting a recipe:** copy its `app/`, `infra/terraform/`, and any ingestion or provisioning into
your scaffolded project, then run provisioning from the recipe's own `Makefile` (e.g.
`make setup-infra`). Start from its `AGENTS.md`.

**Verifying your agent works:** Use `agents-cli run "test prompt"` for quick smoke tests, then `agents-cli eval run` for systematic validation. Do NOT write pytest tests that assert on LLM response content, that belongs in eval.

---

## Scaffold as Reference

When you need specific files (Terraform, CI/CD workflows, Dockerfile) but don't want to scaffold the current project directly, create a temporary reference project in `/tmp/`:

```bash
agents-cli scaffold create ref-project --output-dir /tmp \
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

- **NEVER skip requirements clarification** — load `/google-agents-cli-workflow` Phase 0 and clarify the user's intent before running `scaffold create`
- **NEVER change the model** in existing code unless explicitly asked
- **NEVER `mkdir` before `create`** — the CLI creates the directory; pre-creating it causes enhance mode instead of create mode
- **NEVER create a Git repo or push to remote without asking** — confirm repo name, public vs private, and whether the user wants it created at all
- **Always ask before choosing CI/CD runner** — present GitHub Actions and Cloud Build as options, don't default silently
- **Agent Runtime clears session_type** — if deploying to `agent_runtime`, remove any `session_type` setting from your code
- **Start with `--prototype`** for quick iteration — add deployment later with `enhance`
- **Project names** must be ≤26 characters, lowercase, letters/numbers/hyphens only
- **NEVER write A2A code from scratch** — A2A is built into the scaffolded app (the `adk` and `adk_go` templates and framework templates alike); each language's A2A surface (import paths, `AgentCard` schema etc.) is non-trivial and changes across versions. Scaffold normally; never hand-write the A2A surface. (The only sanctioned A2A hand-edit is *removing* the generated wiring for a **Live** agent, which can't be served over A2A — see `/google-agents-cli-adk-code`, `references/adk-python-live.md`.)

---

# Examples

Using scaffold as reference:
User says: "I need a Dockerfile for my non-standard project"
Actions:
1. Create temp project: `agents-cli scaffold create ref --output-dir /tmp --agent adk --deployment-target cloud_run`
2. Copy relevant files (Dockerfile, etc.) from /tmp/ref
3. Delete temp project
Result: Infrastructure files adapted to the actual project

---

A2A project:
User says: "Build me a Python agent that exposes A2A and deploys to Cloud Run"
Actions:
1. Follow the standard flow (understand requirements, choose architecture, scaffold)
2. `agents-cli scaffold create my-a2a-agent --agent adk --deployment-target cloud_run --prototype`
Result: Valid A2A imports and Dockerfile — no manual A2A code written.

---

Go project:
User says: "Build me a Go agent that deploys to Cloud Run"
Actions:
1. Follow the standard flow (understand requirements, choose architecture, scaffold)
2. `agents-cli scaffold create my-go-agent --agent adk_go --deployment-target cloud_run --prototype`
Result: Go project with `app/agent.go`, the launcher in `main.go`, and the A2A card at the root.

---

## Troubleshooting

### `agents-cli` command not found

See `/google-agents-cli-workflow` → **Setup** section.

---

## Related Skills

- `/google-agents-cli-workflow` — Development workflow, coding guidelines, and the build-evaluate-deploy lifecycle
- `/google-agents-cli-adk-code` — ADK API quick reference for writing agent code, including the graph Workflow API
- `/google-agents-cli-deploy` — Deployment targets, CI/CD pipelines, and production workflows
- `/google-agents-cli-eval` — Evaluation methodology, dataset schema, and the eval-fix loop
