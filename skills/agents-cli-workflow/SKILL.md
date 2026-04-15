---
name: agents-cli-workflow
description: >
  This skill should be used when the user wants to "develop an agent",
  "run the agent locally", "debug agent code", "test an agent",
  "deploy an agent", "publish an agent", "monitor an agent", or needs
  the ADK development lifecycle and coding guidelines.
  Always active — provides the full workflow (scaffold, build, evaluate,
  deploy, publish, observe), code preservation rules, model selection
  guidance, and troubleshooting steps for ADK or any agent development.
metadata:
  author: Google
  license: Apache-2.0
  version: 0.0.3
  requires:
    bins:
      - agents-cli
    install: "uv tool install agents-cli"
---

# ADK Development Workflow & Guidelines

> Requires: agents-cli ~= 0.0.3
> If version is behind, run: uv tool install "agents-cli~=0.0.3"
> Check version: agents-cli info
> [Install uv](https://docs.astral.sh/uv/getting-started/installation/index.md) first if needed.

## Session Continuity & Skill Cross-References

Re-read the relevant skill **before** each phase — not after you've already started and hit a problem. Context compaction may have dropped earlier skill content.

| Phase | Skill | When to load |
|-------|-------|--------------|
| 0 — Understand | — | No skill needed — read `DESIGN_SPEC.md` or clarify goals with the user |
| 1 — Scaffold | `/agents-cli-scaffold` | Before creating or enhancing a project |
| 2 — Build | `/agents-cli-adk-code` | Before writing agent code — API patterns, tools, callbacks, state |
| 3 — Evaluate | `/agents-cli-eval` | Before running any eval — evalset schema, metrics, eval-fix loop |
| 4 — Deploy | `/agents-cli-deploy` | Before deploying — target selection, troubleshooting 403/timeouts |
| 5 — Publish | `/agents-cli-publish` | After deploying, if registering with Gemini Enterprise (optional) |
| 6 — Observe | `/agents-cli-observability` | After deploying — traces, logging, monitoring setup |

---

## Setup

If `agents-cli` is not installed:
```bash
uv tool install agents-cli
```

### `uv` command not found

Install `uv` following the [official installation guide](https://docs.astral.sh/uv/getting-started/installation/index.md).

---

## Phase 0: Understand

Before writing or scaffolding anything, understand what you're building.

If `DESIGN_SPEC.md` already exists, read it — it is your primary source of truth. Otherwise:

Do NOT proceed to planning, scaffolding, or coding. Ask the user the questions below and wait for their answers. You MUST have the user's answers before moving on. Do not assume, research, or fill in the blanks yourself. The user's intent drives everything — skipping this step leads to wasted work.

**Always ask:**

1. **What problem will the agent solve?** — Core purpose and capabilities
2. **External APIs or data sources needed?** — Tools, integrations, auth requirements
3. **Safety constraints?** — What the agent must NOT do, guardrails
4. **Deployment preference?** — Prototype first (recommended) or full deployment? If deploying: Agent Engine, Cloud Run, or GKE?

**Ask based on context:**

- If **retrieval or search over data** mentioned (RAG, semantic search, vector search, embeddings, similarity search, data ingestion) → **Datastore?** Options: `vertex_ai_vector_search` (embeddings, similarity search) or `vertex_ai_search` (document search, search engine).
- If agent should be **available to other agents** → **A2A protocol?** Enables the agent as an A2A-compatible service.
- If **full deployment** chosen → **CI/CD runner?** GitHub Actions (default) or Google Cloud Build?
- If **Cloud Run** or **GKE** chosen → **Session storage?** In-memory (default), Cloud SQL (persistent), or Agent Engine (managed).
- If **deployment with CI/CD** chosen → **Git repository?** Does one already exist, or should one be created? If creating, public or private?

Once you have the user's answers, write a `DESIGN_SPEC.md` with the user's approval. See `/agents-cli-scaffold` for how these choices map to CLI flags. At minimum include these sections — expand with more detail if the user wants a thorough spec:

```markdown
# DESIGN_SPEC.md

## Overview
Describe the agent's purpose and how it works.

## Example Use Cases
Concrete examples with expected inputs and outputs.

## Tools Required
Each tool with its purpose, API details, and authentication needs.

## Constraints & Safety Rules
Specific rules — not just generic statements.

## Success Criteria
Measurable outcomes for evaluation.
```

Optional sections for more detailed specs: **Edge Cases to Handle**, **Architecture & Sub-Agents**, **Data Sources & Auth**, **Non-Functional Requirements**.

Once you have a clear understanding, check if the project was already created or enhanced by agents-cli — run `agents-cli info` from the project root. If it shows project config, skip to **Phase 2**. Otherwise, proceed to **Phase 1**.

## Phase 1: Scaffold (if needed)

Use `/agents-cli-scaffold` to create a new project or import an existing one into the agents-cli format (adding deployment, CI/CD, infrastructure). It covers architecture choices (deployment target, agent type, session storage) and project creation or enhancement.

Skip this phase if the project was already created or enhanced by agents-cli — run `agents-cli info` from the project root to check.

## Phase 2: Build and Implement

Implement the agent logic:

1. Write/modify code in the agent directory (check `GEMINI.md` / `CLAUDE.md` for directory name)
2. Use `agents-cli dev` for interactive testing during development
3. **Quick smoke test**: Use `agents-cli run "your prompt"` to verify the agent works after changes — this is the fastest way to check behavior without leaving the terminal
4. Iterate on the implementation based on user feedback

For ADK API patterns and code examples, use `/agents-cli-adk-code`. It also includes reference implementations from production agents worth studying.

> **NEVER write pytest tests that assert on LLM output content** (e.g., checking for keywords in responses, verifying persona, validating tone). LLM outputs are non-deterministic — these tests are flaky by nature and belong in eval, not pytest. Use `agents-cli run` for quick checks and `agents-cli eval` for systematic validation.

## Phase 2.5: Provision Datastore (RAG projects only)

For `agentic_rag` projects, provision the datastore before testing: `agents-cli setup-datastore`, then `agents-cli data-ingestion`. Use `setup-datastore` — **not** `infra dev` (same datastore provisioning but faster, skips unrelated Terraform).

## Phase 3: Evaluate

**This is the most important phase.** Evaluation validates agent behavior end-to-end.

**MANDATORY:** Activate `/agents-cli-eval` before running evaluation.
It contains the evalset schema, config format, and critical gotchas. Do NOT skip this.

**Do NOT skip this phase.** After building the agent, you MUST proceed to evaluation. Do NOT write pytest tests to validate agent behavior — that is what eval is for.

**`agents-cli test` vs `agents-cli eval` — know the difference:**
- **`agents-cli test`** (pytest) — Tests *code correctness*: imports work, functions return expected types, API contracts hold. Does NOT test whether the agent behaves well.
- **`agents-cli eval`** — Tests *agent behavior*: response quality, tool usage, persona consistency, safety compliance. This is what validates your agent actually works.
- **`agents-cli run "prompt"`** — Quick one-off smoke test during development. Use this for fast iteration, not pytest.

**NEVER write pytest tests that check LLM response content** (e.g., asserting pirate keywords appear, checking if the agent mentions allergies). LLM outputs are non-deterministic. Use eval with LLM-as-judge criteria instead.

1. **Start small**: Begin with 1-2 sample eval cases, not a full suite
2. Run evaluations: `agents-cli eval`
3. Discuss results with the user
4. Fix issues and iterate on the core cases first
5. Only after core cases pass, add edge cases and new scenarios
6. Repeat until quality thresholds are met

**Expect 5-10+ iterations here.**

## Phase 4: Deploy

Once evaluation thresholds are met:

1. Check if the project has a deployment target configured — run `agents-cli info` to see current config
2. If the project is a prototype (no deployment target), add deployment support first:
   ```bash
   agents-cli enhance . --deployment-target <target>
   ```
   See `/agents-cli-deploy` for the deployment target decision matrix (Agent Engine vs Cloud Run vs GKE).
3. Deploy when ready: `agents-cli deploy`

**IMPORTANT**: Never deploy without explicit human approval.

## Phase 5: Publish (optional)

Not all agents require this — currently supporting Gemini Enterprise. See `/agents-cli-publish` for registration modes, flags, and troubleshooting.

## Phase 6: Observe

After deploying, use observability tools to monitor agent behavior in production. See `/agents-cli-observability` for Cloud Trace, prompt-response logging, BigQuery Analytics, and third-party integrations.

---

# Operational Guidelines for Coding Agents

## Principle 1: Code Preservation & Isolation

Code modifications require surgical precision — alter only the code segments directly targeted by the user's request and strictly preserve all surrounding and unrelated code.

**Mandatory Pre-Execution Verification:**

Before finalizing any code replacement, verify the following:

1. **Target Identification:** Clearly define the exact lines or expressions to change, based *solely* on the user's explicit instructions.
2. **Preservation Check:** Confirm that all code, configuration values (e.g., `model`, `version`, `api_key`), comments, and formatting *outside* the identified target remain identical.

**Example:**

- **User Request:** "Change the agent's instruction to be a recipe suggester."
- **Incorrect (VIOLATION):**
  ```python
  root_agent = Agent(
      name="recipe_suggester",
      model="gemini-1.5-flash",  # UNINTENDED - model was not requested to change
      instruction="You are a recipe suggester."
  )
  ```
- **Correct (COMPLIANT):**
  ```python
  root_agent = Agent(
      name="recipe_suggester",  # OK, related to new purpose
      model="gemini-3-flash-preview",  # PRESERVED
      instruction="You are a recipe suggester."  # OK, the direct target
  )
  ```

## Principle 2: Execution Best Practices

- **Model Selection — CRITICAL:**
  - **NEVER change the model unless explicitly asked.**
  - When creating NEW agents (not modifying existing), use the latest Gemini model. List available models to pick the newest one:
    ```bash
    # Use 'global' or any supported region (e.g. 'us-central1')
    uv run --with google-genai python -c "
    from google import genai
    client = genai.Client(vertexai=True, location='global')
    for m in client.models.list():
        if 'gemini' in m.name:
            print(m.name)
    "
    ```
  - Do NOT use older models unless explicitly requested. See [stable model versions](https://cloud.google.com/vertex-ai/generative-ai/docs/learn/model-versions) for the full list (excludes preview models).

- **Location Matters More Than Model:**
  - If a model returns a 404, it's almost always a `GOOGLE_CLOUD_LOCATION` issue (e.g., needing `global` instead of `us-central1`).
  - Changing the model name to "fix" a 404 is a violation — fix the location instead.

- **ADK Built-in Tool Imports (Precision Required):**
  ```python
  # CORRECT - imports the tool instance
  from google.adk.tools.load_web_page import load_web_page

  # WRONG - imports the module, not the tool
  from google.adk.tools import load_web_page
  ```

- **Running Python Commands:**
  - Always use `uv` to execute Python commands (e.g., `uv run python script.py`)
  - Run `uv sync` before executing scripts

- **Breaking Infinite Loops:**
  - **Stop immediately** if you see the same error 3+ times in a row
  - **RED FLAGS**: Lock IDs incrementing, names appending v5→v6→v7, "I'll try one more time" repeatedly
  - **State conflicts** (Error 409): Use `terraform import` instead of retrying creation
  - **When stuck**: Run underlying commands directly (e.g., `terraform` CLI)

- **Troubleshooting:**
  - Check `/agents-cli-adk-code` first — it covers most common patterns
  - Use WebFetch on URLs from the ADK docs index (`curl https://google.github.io/adk-docs/llms.txt`) for deep dives
  - When encountering persistent errors, a targeted web search often finds solutions faster
  - **CLI command failures:** run `agents-cli <command> --help` — the output ends with a `Source:` line pointing to the exact source file implementing that command. Read it to understand the logic and diagnose failures. Use `agents-cli info` to get the full CLI install path if you need to browse across multiple files.

- **Environment Variables:**
  - `.env` files and env var assignments (e.g., `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`) are typically required for the agent to function — never remove or modify them unless the user explicitly asks
  - If a `.env` file exists in the project root, treat it as essential configuration
  - For secrets and API keys, prefer GCP Secret Manager over plain `.env` entries — see `/agents-cli-deploy` for secret management guidance

---

## Using a Temporary Scaffold as Reference

When you need specific infrastructure files (Terraform, CI/CD, Dockerfile) but don't want to modify the current project, use `/agents-cli-scaffold` to create a temporary project in `/tmp/` and copy over what you need.

---

## Reference Files

| File | Contents |
|------|----------|
| `references/internals.md` | Underlying tools and commands that `agents-cli` wraps (adk, pytest, ruff, uvicorn) |

## Development Commands

### Setup & Skills

| Command | Purpose |
|---|---|
| `agents-cli setup` | Install skills to coding agents |
| `agents-cli setup --skip-auth` | Install skills, skip authentication step |
| `agents-cli setup --dry-run` | Preview what setup would do without executing |
| `agents-cli remove` | Remove skills from coding agents |
| `agents-cli update` | Reinstall/update skills to latest version |

### Scaffolding

| Command | Purpose |
|---|---|
| `agents-cli init <name>` | Create a new project |
| `agents-cli enhance .` | Add deployment / CI-CD to project |
| `agents-cli extract <output>` | Extract minimal shareable agent from project |
| `agents-cli upgrade` | Upgrade project to newer agents-cli version |

### Development

| Command | Purpose |
|---|---|
| `agents-cli dev` | Interactive local testing (ADK web playground) |
| `agents-cli run "prompt"` | Run agent with a single prompt (non-interactive) |
| `agents-cli dev --port PORT` | Start playground on a custom port |
| `agents-cli serve` | Start FastAPI backend server (uvicorn) |
| `agents-cli serve --host HOST --port PORT` | Start backend on a custom host/port |
| `agents-cli test` | Run unit and integration tests |
| `agents-cli test --unit-only` | Run only unit tests |
| `agents-cli test --integration-only` | Run only integration tests |
| `agents-cli lint` | Check code quality |
| `agents-cli lint --fix` | Auto-fix linting issues |
| `agents-cli lint --mypy` | Also run mypy type checking |
| `agents-cli install` | Install project dependencies (uv sync) |

### Evaluation

| Command | Purpose |
|---|---|
| `agents-cli eval` | Run evaluation against evalsets |
| `agents-cli eval --evalset F` | Run a specific evalset |
| `agents-cli eval --all` | Run all evalsets |
| `agents-cli compare BASE CAND` | Compare two eval result files |

### Deployment & Infrastructure

| Command | Purpose |
|---|---|
| `agents-cli deploy` | Deploy to dev (requires human approval) |
| `agents-cli infra dev` | Provision dev infrastructure (Terraform, optional) |
| `agents-cli infra prod` | Set up CI/CD pipeline + staging/prod infrastructure |
| `agents-cli secret set KEY` | Store a GCP secret |
| `agents-cli secret list` | List GCP secrets |
| `agents-cli secret delete KEY` | Delete a GCP secret |
| `agents-cli publish gemini-enterprise` | Register agent with Gemini Enterprise |

### Project Info

| Command | Purpose |
|---|---|
| `agents-cli info` | Show CLI install path, skills location, and project config |

Use `agents-cli info` to discover the **CLI install path** — this is where the CLI source code lives. Read files under that path to understand CLI internals, command implementations, or template logic. The command only shows project details when run inside a generated agent project (i.e., one with `[tool.agents-cli]` in `pyproject.toml`).

### Authentication

| Command | Purpose |
|---|---|
| `agents-cli login --interactive` | Authenticate with Google for ADK services (`-i` / `--interactive` is required for interactive browser-based authentication) |
| `agents-cli status` | Show authentication status |

> [!NOTE]
> When using an API key to authenticate, the `login` command does not persist them automatically, it just aids in retrieving them and providing instructions on how they can be persisted. 

---

## Skills Version

> **Troubleshooting hint:** If skills seem outdated or incomplete, reinstall:
> ```
> agents-cli setup --skip-auth
> ```
> Only do this when you suspect stale skills are causing problems.

---

## Related Skills

- `/agents-cli-scaffold` — Project creation, requirements gathering, and enhancement
- `/agents-cli-adk-code` — ADK Python API quick reference and production sample agents
- `/agents-cli-eval` — Evaluation methodology, evalset schema, and the eval-fix loop
- `/agents-cli-deploy` — Deployment targets, CI/CD pipelines, and production workflows
- `/agents-cli-publish` — Gemini Enterprise registration
- `/agents-cli-observability` — Cloud Trace, logging, BigQuery Analytics, and third-party integrations

