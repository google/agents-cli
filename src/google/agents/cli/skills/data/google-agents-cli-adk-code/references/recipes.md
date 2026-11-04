# Reference recipes

Recipes live in [google/adk-recipes](https://github.com/google/adk-recipes). **`core/python/`** is the
curated tier — canonical ADK patterns maintained by the agents-cli team.

**Reading this page is not studying a recipe.** Every `core/` recipe ships an **`AGENTS.md`** — intent,
a ranked "study in this order" file tour, what to copy as-is versus what is recipe-specific,
and the gotchas. Clone the recipe and read its `AGENTS.md` before you implement anything it covers.
Until you have opened it you are answering from memory. Hand-writing a Docker or E2B sandbox wrapper,
a skill loader, a moderation callback or a memory store — for a capability the index lists — means
you stopped at the name.

**Study and adapt — don't scaffold from a recipe.**

```bash
[ -d /tmp/adk-recipes ] || git clone --filter=blob:none --depth 1 --sparse \
  https://github.com/google/adk-recipes /tmp/adk-recipes
cd /tmp/adk-recipes
git sparse-checkout add core/python/<recipe>
cat core/python/<recipe>/AGENTS.md
```

## Topic → recipe

Capabilities below are **not** scaffold flags — they come from studying a recipe and adapting it.

| You need | Study |
|---|---|
| Retrieval / search over your own documents (RAG) | `rag-agent-search` (managed ingestion) · `rag-vector-search` (custom chunking + embeddings) |
| Running shell commands or Python on a user's behalf; a sandboxed, isolated or per-user environment or workspace | `long-horizon-harness` |
| Agent-loadable skills — `SKILL.md` folders discovered at runtime, rebound mid-session, promoted and demoted from memory | `long-horizon-harness` |
| Long-running autonomy — works across days, resumes, unattended, compacts context | `long-horizon-harness` |
| Approval gate, escalation or human sign-off before a risky, high-value or irreversible action (human-in-the-loop) | `long-horizon-harness` (durable, mid-turn) · `ambient-expense-agent` (workflow pause) · `deep-search` (plan approval) |
| Memory across conversations | `cross-session-memory` (the primitive) · `long-horizon-harness` (self-improvement loop built on it) |
| Blocking harmful content or risky calls — moderation in one place, covering a coordinator and every sub-agent without editing them | `safety-plugins` (runner-wide plugins) · `long-horizon-harness` (per-tool guard chain + exfil detection) |
| Per-user credentials the model must never see | `long-horizon-harness` |
| OAuth user consent to act on a user's data | `oauth-user-consent-flow` |
| Sub-agent delegation with isolated context windows | `long-horizon-harness` |
| No chat interface — records or messages land on a queue and are processed automatically; event-driven, scheduled, batch or headless worker | `ambient-expense-agent` (Pub/Sub queue consumer) · `long-horizon-harness` (routines + scheduler) |
| Iterative research with cited sources | `deep-search` |
| Generating images or video — product photography, a model wearing the item (virtual try-on), 360° spins, background replacement — and MCP toolsets | `genmedia-for-commerce` |
| A2A interop, incl. Gemini app client quirks | `long-horizon-harness` |
| Monitoring a deployed agent's quality from its production traces — scheduled evaluation, recurring-failure tracking, root-cause analysis | `ambient-quality-agent` |
| Real-time voice conversation (Gemini Live) that stays responsive while slow tools or coding agents work in the background | `adk-voice-agent` |

In Phase 1, clone the recipes named above and read `/tmp/adk-recipes/core/python/<recipe>/AGENTS.md`
before you write any code. During Phase 0, naming them in the spec is enough — the clone waits for
approval. A bare how-question has no spec to wait for: clone before you answer it.

## The recipes

These eleven are the **complete** set of `core/` python recipes. If a capability isn't listed here,
there is no core recipe for it — don't guess at a plausible name (`core/python/code-execution` and
`core/python/human-in-the-loop` do not exist). Check `contrib/` or build it yourself.

- **`long-horizon-harness`** — a complete agent *harness*: per-user sandbox, runtime-discovered `SKILL.md`
  skills, cross-session memory with a self-improvement loop, layered tool guardrails, sub-agent delegation
  with durable HITL, and per-user secrets. Its `AGENTS.md` maps each interface to the real function
  that implements it, so lift one pattern without adopting the whole harness.
  - Keywords: harness, sandbox, shell execution, code execution, isolated environment, long-horizon, long-running, multi-day, autonomous, resumable, compaction, guardrails, exfil, egress, approval gate, human-in-the-loop, HITL, per-user secrets, credentials, sub-agents, delegation, self-improving, memory bank, routines, scheduler, a2a, skills, model routing
- **`rag-agent-search`** — managed document search via Search in Gemini platform (Discovery Engine) with a
  fully-managed GCS Data Connector: drop files in a bucket, no ingestion code to maintain.
  - Keywords: RAG, document search, Discovery Engine, Search in Gemini platform, Agent Platform Search, managed ingestion, GCS data connector, PDF, HTML, grounding
- **`rag-vector-search`** — RAG with Vector Search 2.0 in Gemini platform and a KFP ingestion pipeline (chunking +
  BigQuery staging; embeddings auto-generated server-side).
  - Keywords: RAG, retrieval, vector search, embeddings, similarity search, ScaNN, semantic search, document Q&A, ingestion pipeline, chunking
- **`cross-session-memory`** — remembers user preferences and facts across sessions via Memory Bank in
  Gemini platform: written after each turn, recalled at the start of a later one.
  - Keywords: memory, cross-session, recall, remember, preferences, Memory Bank, PreloadMemoryTool
- **`oauth-user-consent-flow`** — reads a user's Google Drive on their behalf behind an OAuth 2.0 consent
  flow; the same code path works in local ADK Web and in production in the Gemini app.
  - Keywords: OAuth, user consent, authentication, Google Drive, Workspace, Agent Runtime, Gemini app, Gemini Enterprise
- **`ambient-expense-agent`** — no chat loop: Pub/Sub events drive a graph-based `Workflow`, business
  rules stay in code, and only high-value cases reach an LLM that pauses for human approval.
  - Keywords: ambient, event-driven, scheduled, cron, Pub/Sub, workflow, human-in-the-loop, approval, alerts, no UI
- **`deep-search`** — research agent that plans (with an approval step), loops search → critique → refine
  until a quality bar is met, then writes a report with inline citations.
  - Keywords: research, citations, iterative, critique, grounding, multi-agent, human-in-the-loop, web search, report
- **`safety-plugins`** — runner-wide safety guardrails as ADK `BasePlugin`s: attached to the `Runner`, they
  wrap every agent and sub-agent beneath it and keep harmful content out of session state.
  - Keywords: safety, guardrails, harmful content, moderation, content filtering, Model Armor, LLM-as-a-judge, session poisoning, plugins, runner-wide, applies to all sub-agents
- **`genmedia-for-commerce`** — full-stack multi-agent retail media: virtual try-on, 360° product spins and
  background swaps, orchestrated through an MCP tool server and Veo pipelines.
  - Keywords: MCP, media, image generation, video generation, product photography, on-model imagery, virtual try-on, 360° spin, background replacement, Veo, retail, e-commerce, catalogue, full-stack, React, Gemini app, Gemini Enterprise
- **`ambient-quality-agent`** — AQuA: an ADK agent that runs beside the agent it observes, evaluates its
  production conversations from BigQuery telemetry on a schedule or after each deploy, groups failures into
  deduplicated insights, and diagnoses root causes against the agent's source. Ships as an agents-cli extension.
  - Key files: `README.md`, `src/ambient_quality_agent/core/workflow.py`, `src/ambient_quality_agent/core/investigation/job_scheduling.py`, `src/ambient_quality_agent/tools/insights/clustering.py`, `extension/README.md`
  - Keywords: quality monitoring, production evaluation, observability, traces, telemetry, BigQuery Agent Analytics, LLM-as-a-judge, session review, custom metrics, insights, regressions, root-cause analysis, scheduled investigation, Workflow, agents-cli extension, dashboard
- **`adk-voice-agent`** — a Gemini Live voice agent that delegates coding work to Claude Code or the
  Antigravity CLI on a per-user computer: slow tools return at once and report back when idle, threads pause for
  voice or UI approval, and one MCP gateway holds each user's OAuth tokens so no token reaches the computer.
  - Keywords: voice, speech, audio, real-time, Gemini Live, run_live, bidi streaming, non-blocking tools, background tasks, coding agents, Claude Code, per-user computer, workspace, git worktrees, multi-turn threads, human-in-the-loop, approval, MCP gateway, OAuth, Google Workspace, GitHub, A2UI, Memory Bank, full-stack, React

**Nothing above matches?** [`contrib/`](https://github.com/google/adk-recipes/tree/main/contrib) holds
community- and partner-contributed recipes — broader in scope (complete solutions, not isolated patterns)
and not curated by the agents-cli team, so there is no guaranteed `AGENTS.md` to guide the lift. Check
there when you need something specific `core/` doesn't cover.
