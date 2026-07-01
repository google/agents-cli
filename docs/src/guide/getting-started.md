# Getting Started

**Agents CLI in Agent Platform** is a CLI and skills package for building, evaluating, and deploying AI agents on Google Cloud. Agents are built with Google's [Agent Development Kit (ADK)](https://google.github.io/adk-docs/) — Agents CLI handles everything around it: scaffolding, evaluation, deployment, and observability.

It works two ways:

1. **With a coding agent** — install skills into Antigravity CLI, Claude Code, Codex, or others. Your coding agent uses them to make the right decisions at every step.
2. **Without a coding agent** — run CLI commands directly from your terminal. Every command works standalone.

Agents CLI bundles **7 skills** that give your coding agent deep knowledge across the full ADK lifecycle:

| Skill | What your coding agent learns |
|-------|-------------------------------|
| `google-agents-cli-workflow` | Development lifecycle, code preservation, model selection |
| `google-agents-cli-adk-code` | ADK Python API — agents, tools, orchestration, callbacks |
| `google-agents-cli-scaffold` | Project scaffolding — `create`, `enhance`, `upgrade` |
| `google-agents-cli-eval` | Evaluation lifecycle — datasets, metrics, generate/grade, compare, analyze, optimize |
| `google-agents-cli-deploy` | Deployment — Agent Runtime, Cloud Run, GKE, CI/CD |
| `google-agents-cli-publish` | Gemini Enterprise registration |
| `google-agents-cli-observability` | Cloud Trace, logging, third-party integrations |

---

## Prerequisites

**Required:** [Python 3.11+](https://www.python.org/downloads/), [uv](https://docs.astral.sh/uv/getting-started/installation/), [Node.js](https://nodejs.org/en/download) (for skills installation)

**Optional (for deployment):** [Google Cloud SDK](https://cloud.google.com/sdk/docs/install), [Terraform](https://developer.hashicorp.com/terraform/downloads)

---

## Install

```bash
uvx google-agents-cli setup
```

This installs the CLI and context-aware skills for your coding agent.

??? info "Alternative installation methods"
    **pipx:** `pipx install google-agents-cli && agents-cli setup`

    **venv + pip:** `pip install google-agents-cli && agents-cli setup`

    **Skills only:** `npx skills add google/agents-cli`

**Platform support:** macOS, Linux, and Windows (WSL 2). Native Windows is not officially supported.

---

## Authenticate

If you're already authenticated with `gcloud`, it just works — Agents CLI picks up your Application Default Credentials automatically.

Otherwise, the quickest option is a Gemini API key from [AI Studio](https://aistudio.google.com/apikey):

```bash
export GEMINI_API_KEY="your-key-here"
```

See [Authentication](authentication.md) for full details.

---

## Start Building with Your Coding Agent

=== "Antigravity CLI"

    1. **Open Antigravity CLI**

        Launch Antigravity from your IDE or terminal.

    2. **Verify skills are installed**

        Check that Agents CLI skills are available in your environment.

    3. **Ask it to build something**

        ```
        Build a support agent that answers questions from our docs
        ```

        Antigravity will use the installed skills to scaffold, build, and evaluate your agent.

=== "Claude Code"

    1. **Open Claude Code**

        ```bash
        claude
        ```

    2. **Verify skills are installed**

        ```
        /skills
        ```

        You should see `google-agents-cli-workflow` and other Agents CLI skills listed.

    3. **Ask it to build something**

        ```
        Build a support agent that answers questions from our docs
        ```

        Claude will use the installed skills to scaffold, build, and evaluate your agent.

=== "Codex"

    1. **Open Codex**

        ```bash
        codex
        ```

    2. **Verify skills are installed**

        Check that Agents CLI skills are available in your environment.

    3. **Ask it to build something**

        ```
        Build a support agent that answers questions from our docs
        ```

        Codex will use the installed skills to scaffold, build, and evaluate your agent.

=== "Any Other Agent"

    Agents CLI works with any coding agent that supports [skills](https://agentskills.io/what-are-skills).

    1. **Install skills**

        ```bash
        uvx google-agents-cli setup
        ```

    2. **Verify skills are visible**

        Check that your agent can see `google-agents-cli-workflow` and other Agents CLI skills. Most agents expose this via a `/skills` command or settings panel.

    3. **Ask it to build something**

        ```
        Build a support agent that answers questions from our docs
        ```

        As long as the skills are installed and visible, your agent will use them automatically.

---

## Prefer to Type Commands Yourself?

You can drive the entire workflow from your terminal — no coding agent needed.

```bash
# Create a minimal agent project
agents-cli create my-agent --prototype --yes

# Install dependencies and start the dev playground
cd my-agent
agents-cli install
agents-cli playground
```

This starts the ADK web playground at `http://localhost:8080` with hot reload.

For a full walkthrough, see the [Manual Workflow Tutorial](hands-on-tutorial.md).

---

## Demo

<div align="center">
  <iframe width="100%" height="450" src="https://www.youtube.com/embed/ECYKo70pPNc" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>
</div>

---

## Next Steps

- [Tutorial: Build Your First Agent](quickstart-tutorial.md) — build, evaluate, and deploy with your coding agent
- [Tutorial: Manual Workflow](hands-on-tutorial.md) — type every command yourself
- [Use Cases](use-cases.md) — get inspired by real agent patterns people build
- [Project Structure](project-structure.md) — understand what each generated file does
- [Agent Templates](templates.md) — the `adk` template and the RAG clone-and-study recipe
- [Development Guide](development.md) — full development workflow
- [CLI Reference](../cli/index.md) — all commands and flags

---

!!! tip "Coming from Agent Starter Pack?"
    See the [migration guide](../reference/from-agent-starter-pack.md).

!!! note "Share what you build"
    Built something interesting with Agents CLI? We'd love to hear about it! Share your project at [agents-cli@google.com](mailto:agents-cli@google.com).
