---
name: google-agents-cli-workflow
description: >
  This skill should be used when the user wants to "develop an agent",
  "build an agent using ADK", "run the agent locally", "debug agent code",
  "test an agent", "deploy an agent", "publish an agent", "monitor an agent",
  or needs the ADK (Agent Development Kit) development lifecycle and coding
  guidelines. Entrypoint for building ADK agents.
  Always active — provides the full workflow (scaffold, build, evaluate,
  deploy, publish, observe), code preservation rules, model selection
  guidance, and troubleshooting steps for ADK or any agent development.
metadata:
  author: Google
  license: Apache-2.0
  version: 1.9.0
  requires:
    bins:
      - agents-cli
    install: "uv tool install google-agents-cli"
---

# Agent Development Workflow & Guidelines

**agents-cli** is a CLI and skills toolkit for building, evaluating, and deploying agents on Google Cloud. It works with any coding agent — Antigravity CLI, Claude Code, Codex, or others — and with the agent framework of your choice (the [Agent Development Kit (ADK)](https://adk.dev/) by default). Install with `uvx google-agents-cli setup`.

> **Before writing agent code, make sure a scaffolded project exists (see Phase 2).** Skipping scaffolding loses eval boilerplate, CI/CD config, and project conventions.


> Requires: google-agents-cli ~= 1.9.0
> If version is behind, run: uv tool install "google-agents-cli~=1.9.0"

> Check version: agents-cli info
> [Install uv](https://docs.astral.sh/uv/getting-started/installation/index.md) first if needed.

## Session Continuity & Skill Cross-References

Re-read the relevant skill **before** each phase — not after you've already started and hit a problem. Context compaction may have dropped earlier skill content. If skills are not available, run `uvx google-agents-cli setup` to install them.

| Phase | Skill | When to load |
|-------|-------|--------------|
| 0 — Understand | — | No skill needed — read `.agents-cli-spec.md` if present, else clarify goals with the user |
| 1 — Study recipes | `/google-agents-cli-adk-code` | **Load it during design**, before scaffolding. Python: the `references/samples.md` topic index maps a need to the recipe that implements it. Go: the upstream [examples/](https://github.com/google/adk-go/tree/main/examples) are the equivalent. Yes, load this early. |
| 2 — Scaffold | `/google-agents-cli-scaffold` | Before creating or enhancing a project |
| 3 — Build | `/google-agents-cli-adk-code` | Before writing agent code — API patterns, tools, callbacks, state |
| 4 — Evaluate | `/google-agents-cli-eval` | Before running any eval — dataset schema, metrics, eval-fix loop |
| 5 — Deploy | `/google-agents-cli-deploy` | Before deploying — target selection, troubleshooting 403/timeouts |
| 6 — Publish | `/google-agents-cli-publish` | After deploying, if registering with Gemini Enterprise (optional) |
| 7 — Observe | `/google-agents-cli-observability` | After deploying — traces, logging, monitoring setup |

---

## Setup

If `agents-cli` is not installed:
```bash
uv tool install google-agents-cli
```

### `uv` command not found

Install `uv` following the [official installation guide](https://docs.astral.sh/uv/getting-started/installation/index.md).

### Product name mapping

Users name products inconsistently (Vertex AI → Agent Platform, Agent Engine → Agent Runtime, etc.). Map user terms to CLI values using `references/terminology.md`.

---

## Phase 0: Understand

Before writing or scaffolding anything, understand what you're building — through a **design dialogue**, not a checklist. Load `references/brainstorming.md` and follow it: ask **one question at a time**, propose 2–3 architecture approaches for non-trivial agents, and validate the design before any scaffolding.

If `.agents-cli-spec.md` exists in the current directory, read it — it is your primary source of truth. Otherwise:

Do NOT proceed to planning, scaffolding, or coding until the user approves the spec. Do not assume, research, or fill in the blanks yourself — the user's intent drives everything.

**Scale the ceremony to complexity:** a trivial agent (single tool, fixed persona) needs only a couple of questions, a 2–3 sentence spec, and one approval; a complex agent (multi-agent, RAG, external APIs/auth, safety-critical) gets the full treatment in `references/brainstorming.md`.

**Topics to cover** (one question at a time, adapting to the user — see the playbook):

1. **What problem will the agent solve?** — Core purpose and capabilities
2. **External APIs or data sources needed?** — Tools, integrations, auth requirements
3. **Safety constraints?** — What the agent must NOT do, guardrails
4. **Deployment preference?** — Prototype first (recommended) or full deployment? If deploying: Agent Runtime, Cloud Run, or GKE?

**Ask based on context:**

- If the agent needs a **capability the scaffold doesn't ship** — retrieval over your data, sandboxed code execution, memory across sessions, OAuth consent, safety guardrails, event-driven triggers — that capability comes from a **clone-and-study recipe**, not a scaffold flag. Look the need up in the topic index in `/google-agents-cli-adk-code` → `references/samples.md` and study the matching recipe in Phase 1.
- If the agent is a **live or voice agent** (Live API, spoken conversation, barge-in, telephony) → load `/google-agents-cli-adk-code` (`references/adk-python-live.md`) **before writing the spec**. Live rules out A2A, Gemini Enterprise, and the default model.
- If agent should be **available to other agents** → **A2A protocol** is built into every Python agent scaffolded by agents-cli; no separate choice needed — just scaffold normally.
- If **full deployment** chosen → **CI/CD runner?** GitHub Actions (default) or Google Cloud Build?
- If agent should **remember user preferences or facts across sessions** → long-term memory across conversations. Load `/google-agents-cli-adk-code` — it has both the recipe (in `references/samples.md`) and the ADK memory API details.
- If **Cloud Run** or **GKE** chosen → **Session storage?** In-memory (default), Cloud SQL (persistent), or Agent Platform Sessions (managed).
- If **deployment with CI/CD** chosen → **Git repository?** Does one already exist, or should one be created? If creating, public or private?

Once the design is agreed, write the spec to `.agents-cli-spec.md` using the template in `references/spec-template.md`, self-review it, then get the user's approval. See `/google-agents-cli-scaffold` for how these choices map to CLI flags.

Once you have a clear understanding, proceed to **Phase 1**.

## Phase 1: Study Reference Recipes

> **Trigger.** If the request involves any of: searching your
> own documents · running shell or Python code on a user's behalf · a sandboxed or isolated per-user
> environment · loading skills the agent picks up at runtime · work that spans days, resumes, or runs
> unattended · remembering across conversations · approving a risky action before it executes ·
> blocking harmful content or moderating what agents say · acting with a user's own API keys or
> credentials · OAuth consent to reach a user's own data · delegating to sub-agents with isolated
> context · speaking A2A to other agents · reacting to events or a schedule · researching a topic
> online and reporting it with citations · generating images or video — then a recipe already
> implements it. Look it up **before you commit to an implementation.**
>
> This list covers the same capabilities as the topic index in `/google-agents-cli-adk-code` → `references/samples.md`. If you extend one, extend the other.

**Load `/google-agents-cli-adk-code` now**.Load it even though nothing is scaffolded and you are not writing code yet; "wrong phase for the code skill" is the rationalisation that makes agents skip this step, and that skill's *Prerequisites for writing code* does not apply to you.

Look up each capability the design calls for. **ADK Python:** the **topic index** in
`references/samples.md` maps needs (retrieval, sandboxed execution, memory, approval gates,
guardrails, per-user credentials, scheduling) to the recipe that teaches them and shows how to
clone one. **ADK Go:** there is no references catalog — read `references/adk-go.md` for the API and
the upstream [`examples/`](https://github.com/google/adk-go/tree/main/examples).

Multiple recipes can match — clone and study all that are relevant, starting with each one's `AGENTS.md`.

If no recipe matches, proceed to Phase 2. But first — are you sure? Re-read the user's request and re-check the topic index for your language. Skipping a matching recipe means rebuilding patterns that already exist, usually worse.

> **IMPORTANT — Exit criteria:** After studying a recipe, ask yourself: can I apply anything from it to help me deliver the design? Note what you'll reuse before moving on. Do NOT proceed until you've answered this.

> **This catalog is useful at any phase** — revisit it when you hit deployment, publishing, or infrastructure questions. A recipe's Terraform or registration pattern may be exactly what you need later.

## Phase 2: Scaffold (if needed)

First check whether a project already exists: run `agents-cli info` from the project root. If one was already created or enhanced by agents-cli, skip this phase.

Otherwise, scaffold **before writing any code**:
- **No project yet** → `agents-cli scaffold create <name>`
- **Existing code to import** → `agents-cli scaffold enhance .` (adds the agents-cli structure)

Use `/google-agents-cli-scaffold` for the full workflow — it covers architecture choices (deployment target, agent type, session storage) and project creation or enhancement.

## Phase 3: Build and Implement

Implement the agent logic:

1. Write/modify code in the agent directory (check `GEMINI.md` / `CLAUDE.md` for directory name)
2. **Quick smoke test**: Use `agents-cli run "your prompt"` to verify the agent works after changes — this is the fastest way to check behavior without leaving the terminal
3. Iterate on the implementation based on user feedback

If the user asks for interactive testing, suggest `agents-cli playground` — it opens a web-based playground for manual conversation with the agent.

For ADK API patterns and code examples, use `/google-agents-cli-adk-code`.

> **Smoke-test only here — do not write behavioral unit tests.** LLM output is non-deterministic; behavioral checks belong in eval (Phase 4), not in `pytest` or `go test`. Use `agents-cli run "prompt"` for quick checks.

### Provision recipe infrastructure (if you adapted one)

A recipe that needs backing infrastructure (a datastore, an index, a sandbox, a queue) ships its own
provisioning: follow its `Makefile` (e.g. `make setup-infra`, `make data-ingestion`) and its
`AGENTS.md` / `README.md`, adapting the recipe's `infra/terraform/` and `.env` into your project.
`agents-cli` has no command for this.

## Phase 4: Evaluate

**This is the most important phase.** Evaluation validates agent behavior end-to-end.

**MANDATORY:** Activate `/google-agents-cli-eval` before running evaluation.
It contains the dataset schema, config format, and critical gotchas. Do NOT skip this.

**Do NOT skip this phase.** After building the agent, you MUST proceed to evaluation.

**Unit tests vs `agents-cli eval` — know the difference:**
- **Unit tests** (`uv run pytest` for Python, `go test ./...` for Go) — Tests *code correctness*: imports work, functions return expected types, API contracts hold. Does NOT test whether the agent behaves well.
- **`agents-cli eval`** — Tests *agent behavior*: response quality, tool usage, persona consistency, safety compliance. This is what validates your agent actually works.
- **`agents-cli run "prompt"`** — Quick one-off smoke test during development. If testing multiple prompts use the `--start-server` option to persist the local server, which reduces overhead for repeated calls and allows resuming local sessions via `--session-id`. Use this for fast iteration, not unit tests.

**NEVER write unit tests that check LLM response content** (e.g., asserting pirate keywords appear, checking if the agent mentions allergies). LLM outputs are non-deterministic. Use eval with LLM-as-judge criteria instead.

1. **Start small**: Begin with 1-2 sample eval cases, not a full suite
2. Run evaluations: `agents-cli eval run` (chains `generate` + `grade`). For debugging or custom trace locations, use the two-step form: `agents-cli eval generate` then `agents-cli eval grade`.
3. Discuss results with the user
4. Fix issues and iterate on the core cases first
5. Only after core cases pass, add edge cases and new scenarios
6. Repeat until every case meets the bar you agreed with the user (`eval run` exits 0 whatever the scores are, so read them)

**Expect 5-10+ iterations here.**

## Phase 5: Deploy

Once the user agrees the eval scores are good enough:

1. Check if the project has a deployment target configured — run `agents-cli info` to see current config
2. If the project is a prototype (no deployment target), add deployment support first:
   ```bash
   agents-cli scaffold enhance . --deployment-target <target>
   ```
   See `/google-agents-cli-deploy` for the deployment target decision matrix (Agent Runtime vs Cloud Run vs GKE).
3. Deploy when ready: `agents-cli deploy`

**IMPORTANT**: Never deploy without explicit human approval.

## Phase 6: Publish (optional)

Not all agents require this — currently supporting Gemini Enterprise. See `/google-agents-cli-publish` for registration modes, flags, and troubleshooting.

## Phase 7: Observe

After deploying, use observability tools to monitor agent behavior in production. See `/google-agents-cli-observability` for Cloud Trace, prompt-response logging, BigQuery Analytics, and third-party integrations.

---

# Operational Guidelines for Coding Agents

## Common Shortcuts to Resist

Agents routinely skip steps with plausible-sounding excuses. Recognize these and push back:

| Shortcut | Why it fails |
|----------|-------------|
| "The user's request is clear enough, no need to clarify" | You're guessing at requirements. Phase 0 exists to confirm intent before scaffolding — even one question can prevent a full rework. |
| "The agent responded correctly in `agents-cli run`, so eval isn't needed" | One prompt is not a test suite. Eval catches regressions, edge cases, and tool trajectory issues that a single run never will. |
| "I'll use a newer/better model" | The scaffolded model was chosen deliberately. Changing it without being asked violates code preservation (Principle 1) and often breaks things — wrong location, deprecated version, or 404. Your training data is likely out of date — rely on the skills and the model listing command, not your knowledge of model names. |
| "I already know how to build this — sandbox, memory, approval gates" | You are about to reinvent a recipe, worse. These capabilities are already implemented and battle-tested in `/google-agents-cli-adk-code` → `references/samples.md`; check the topic index and open the matching recipe's `AGENTS.md` before designing anything custom. |
| "I can skip the scaffold and set up manually" | Manual setup misses eval boilerplate, CI/CD config, and project configuration manifest conventions. Use `agents-cli create` even for quick experiments. |

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
      model="gemini-3.8-flash",  # PRESERVED
      instruction="You are a recipe suggester."  # OK, the direct target
  )
  ```

## Principle 2: Execution Best Practices

- **Model Selection — CRITICAL:**
  - **NEVER change the model unless explicitly asked.**
  - When creating NEW agents (not modifying existing), use the latest Gemini model. List what is available in the location your agent actually uses (`GOOGLE_CLOUD_LOCATION`; `global` by default) — not every model is served from every location:
    ```bash
    uv run --with google-genai python -c "
    from google import genai
    # Use your GOOGLE_CLOUD_LOCATION ('global', or a region such as 'us-east1')
    client = genai.Client(vertexai=True, location='global')
    for m in client.models.list(): print(m.name)
    "
    ```
  - Do NOT use older models unless explicitly requested. For model docs, fetch `https://adk.dev/agents/models/google-gemini/index.md`. See also [stable model versions](https://cloud.google.com/vertex-ai/generative-ai/docs/learn/model-versions) and [model endpoint locations](https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/locations#google-models).

- **Running Python Commands:**
  - Always use `uv` to execute Python commands (e.g., `uv run python script.py`)
  - Run `uv sync` before executing scripts

- **Breaking Infinite Loops:**
  - **Stop immediately** if you see the same error 3+ times in a row
  - **RED FLAGS**: Lock IDs incrementing, names appending v5→v6→v7, "I'll try one more time" repeatedly
  - **State conflicts** (Error 409): Use `terraform import` instead of retrying creation
  - **When stuck**: Run underlying commands directly (e.g., `terraform` CLI)

- **Troubleshooting:**
  - Check `/google-agents-cli-adk-code` first — it covers most common patterns
  - Use WebFetch on URLs from the ADK docs index (`curl https://adk.dev/llms.txt`) for deep dives
  - When encountering persistent errors, a targeted web search often finds solutions faster
  - **CLI command failures:** run `agents-cli <command> --help` — the output ends with a `Source:` line pointing to the exact source file implementing that command. Read it to understand the logic and diagnose failures. Use `agents-cli info` to get the full CLI install path if you need to browse across multiple files.

### Systematic Debugging

When something breaks, follow this sequence — don't skip steps or shotgun fixes:

1. **Reproduce** — Run the exact command that failed. Save the full error output. If you can't reproduce it, you can't fix it.
2. **Localize** — Narrow the cause: is it the agent code, a tool, the config, or the environment? Use `agents-cli run "prompt"` to isolate agent behavior from deployment issues. Add `-v` (`--verbose`) to print the full JSON event payloads — useful for inspecting tool calls, intermediate steps, and silent failures.
3. **Fix one thing** — Change one variable at a time. If you change the instruction AND the tool AND the config simultaneously, you won't know what fixed it (or what broke something else).
4. **Verify** — Rerun the exact reproduction command. Don't assume the fix worked.
5. **Guard** — If it was a non-obvious bug, add an eval case to catch regressions.

**Stop-the-line rule:** If a change breaks something that was working, stop feature work and fix the regression first. Don't push forward hoping to circle back — regressions compound.

- **Environment Variables:**
  - `.env` files and env var assignments (e.g., `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`) are typically required for the agent to function — never remove or modify them unless the user explicitly asks
  - If a `.env` file exists in the project root, treat it as essential configuration
  - For secrets and API keys, prefer GCP Secret Manager over plain `.env` entries — see `/google-agents-cli-deploy` for secret management guidance

---

## Using a Temporary Scaffold as Reference

When you need specific infrastructure files (Terraform, CI/CD, Dockerfile) but don't want to modify the current project, use `/google-agents-cli-scaffold` to create a temporary project in `/tmp/` and copy over what you need.

---

## Reference Files

| File | Contents |
|------|----------|
| `references/internals.md` | Underlying tools and commands that `agents-cli` wraps, per language (Python: adk, uv, ruff; Go: adk-go, go, golangci-lint) |
| `references/spec-template.md` | `.agents-cli-spec.md` template and optional sections |
| `references/brainstorming.md` | Phase 0 design-dialogue playbook (one-at-a-time Q&A, approaches, gates) |
| `references/terminology.md` | Product-name → CLI-value mapping |
| `references/commands.md` | Per-phase `agents-cli` command index |
| `references/extension.md` | Author an ad-hoc extension or adopt an existing one (override or add commands) |

## Development Commands

Run `agents-cli --help` or `agents-cli <command> --help` for the authoritative flag list. A per-phase command index lives in `references/commands.md`; per-phase usage is in the phase sections above.

To **override** a built-in command (e.g. wrap `deploy` with compliance checks) or **add a new one**, agents-cli has an extension system. To author an ad-hoc extension for this repo, or adopt an existing one, load `references/extension.md`.

---

## Skills Version

> **Troubleshooting hint:** If skills seem outdated or incomplete, reinstall:
> ```
> agents-cli setup --skip-auth
> ```
> Only do this when you suspect stale skills are causing problems.

