<div align="center">
  <img src="docs/src/assets/logo_sm.png" alt="agents-cli logo" width="120" />
  <h1><code>agents-cli</code></h1>
  <p>The CLI and skills for building agents on Gemini Enterprise Agent Platform.</p>

  <p>
    <a href="#get-started">Get Started</a> &nbsp;&nbsp;|&nbsp;&nbsp;
    <a href="#agent-skills">Skills</a> &nbsp;&nbsp;|&nbsp;&nbsp;
    <a href="#cli-commands">Commands</a> &nbsp;&nbsp;|&nbsp;&nbsp;
    <a href="https://pypi.org/project/google-agents-cli/">PyPI</a> &nbsp;&nbsp;|&nbsp;&nbsp;
    <a href="https://github.com/google/agents-cli/issues">Issues</a> &nbsp;&nbsp;|&nbsp;&nbsp;
    <a href="https://google.github.io/agents-cli/">Docs</a> &nbsp;&nbsp;|&nbsp;&nbsp;
    <a href="https://github.com/google/agents-cli/stargazers">Star us</a>
  </p>
</div>

---

Turn your favorite coding assistant into an expert at building and deploying agents on Google Cloud.

**Agents CLI in Agent Platform** (`agents-cli`) gives your coding agent the skills and commands to build, scale, govern, and optimize enterprise-grade agents — so you don't have to learn every CLI and service yourself.

**Works seamlessly with:**
[Gemini CLI](https://github.com/google-gemini/gemini-cli) &nbsp;•&nbsp; [Claude Code](https://docs.anthropic.com/en/docs/claude-code) &nbsp;•&nbsp; [Codex](https://github.com/openai/codex) &nbsp;•&nbsp; [Antigravity](https://antigravity.google/) &nbsp;•&nbsp; *and any other coding agent.*

## Get Started

**Prerequisites:** Python 3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/), and [Node.js](https://nodejs.org/en/download).

### 1. Install

```bash
uvx google-agents-cli setup
```

<details>
<summary>Or just the skills — your coding agent will handle the rest</summary>

```bash
npx skills add google/agents-cli
```

</details>

### 2. Open your coding agent

Launch [Gemini CLI](https://github.com/google-gemini/gemini-cli), [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex](https://github.com/openai/codex), or any coding agent you prefer.

### 3. Build your first agent

Ask your coding agent to build something — e.g. *"Use agents-cli to build a caveman-style agent that compresses verbose text into terse, technical grunts"*

See the [full tutorial](https://google.github.io/agents-cli/guide/quickstart-tutorial/) for a step-by-step walkthrough.

**[Browse the full documentation →](https://google.github.io/agents-cli/)**

---

## Agent Skills

| Skill | What your coding agent learns |
|-------|-------------------------------|
| `google-agents-cli-workflow` | Development lifecycle, code preservation rules, model selection |
| `google-agents-cli-adk-code` | ADK Python API — agents, tools, orchestration, callbacks, state |
| `google-agents-cli-scaffold` | Project scaffolding — `create`, `enhance`, `upgrade` |
| `google-agents-cli-eval` | Evaluation methodology — metrics, evalsets, LLM-as-judge, trajectory scoring |
| `google-agents-cli-deploy` | Deployment — [Agent Runtime](https://cloud.google.com/vertex-ai/generative-ai/docs/agent-engine/overview), [Cloud Run](https://cloud.google.com/run), [GKE](https://cloud.google.com/kubernetes-engine), CI/CD, secrets |
| `google-agents-cli-publish` | Gemini Enterprise registration |
| `google-agents-cli-observability` | Observability — Cloud Trace, logging, third-party integrations |

---

## CLI Commands

| Command | What it does |
|---------|-------------|
| `agents-cli setup` | Install CLI + skills to coding agents |
| `agents-cli scaffold <name>` | Create a new agent project |
| `agents-cli eval run` | Run agent evaluations |
| `agents-cli deploy` | Deploy to Google Cloud |
| `agents-cli publish gemini-enterprise` | Register with Gemini Enterprise |

<details>
<summary>See all commands</summary>

| Command | Description |
|---------|-------------|
| `agents-cli login` | Authenticate with Google Cloud or AI Studio |
| `agents-cli login --status` | Show authentication status |
| **Scaffold** | |
| `agents-cli scaffold <name>` | Create a new agent project |
| `agents-cli scaffold enhance` | Add deployment, CI/CD, or RAG to an existing project |
| `agents-cli scaffold upgrade` | Upgrade project to a newer agents-cli version |
| **Develop** | |
| `agents-cli run "prompt"` | Run agent with a single prompt |
| `agents-cli install` | Install project dependencies |
| `agents-cli lint` | Run code quality checks (Ruff) |
| **Evaluate** | |
| `agents-cli eval run` | Run agent evaluations |
| `agents-cli eval compare` | Compare two eval result files |
| **Deploy & Publish** | |
| `agents-cli deploy` | Deploy to Google Cloud |
| `agents-cli publish gemini-enterprise` | Register with Gemini Enterprise |
| `agents-cli infra single-project` | Provision single-project infrastructure |
| `agents-cli infra cicd` | Set up CI/CD pipeline + staging/prod infrastructure |
| **Data** | |
| `agents-cli infra datastore` | Provision datastore infrastructure for RAG |
| `agents-cli data-ingestion` | Run data ingestion pipeline |
| **Other** | |
| `agents-cli info` | Show project config and CLI version |
| `agents-cli update` | Force reinstall skills to all IDEs |

</details>

## How it works

<div align="center">
  <img src="https://raw.githubusercontent.com/google/agents-cli/assets/agents-cli-demo.gif" alt="agents-cli demo" width="100%" />
</div>

---

## Architecture

The Google Cloud agent stack that `agents-cli` builds on:

![Architecture](docs/src/assets/architecture.png "Architecture")

## FAQ

**Is this an alternative to Gemini CLI, Claude Code, or Codex?**<br>
No. **`agents-cli` is a tool *for* coding agents, not a coding agent itself.** It provides the CLI commands and skills that make your coding agent better at building, evaluating, and deploying ADK agents on Google Cloud.

**How is this different from just using `adk` directly?**<br>
[ADK](https://adk.dev) is an agent framework. `agents-cli` gives your coding agent the skills and tools to build, evaluate, and deploy ADK agents end-to-end.

**Do I need Google Cloud?**<br>
For local development (`create`, `run`, `eval`), no — you can use an [AI Studio API key](https://aistudio.google.com/apikey) to run Gemini with [ADK](https://adk.dev) locally. For deployment and cloud features, yes.

**Can I use this with an existing agent project?**<br>
Yes. `agents-cli scaffold enhance` adds deployment and CI/CD to existing projects.

**Can I use `agents-cli` without a coding agent?**<br>
Yes. The CLI works standalone — you can run `agents-cli scaffold`, `eval`, `deploy`, and every other command directly from your terminal. The skills just make it easier for coding agents to do it for you.

## Feedback

We value your input — it helps us improve `agents-cli` for the community.

- **Issues & suggestions:** [raise an issue](https://github.com/google/agents-cli/issues) on GitHub
- **Share your experience:** reach out at <a href="mailto:agents-cli@google.com">agents-cli@google.com</a>

## Contributing

Found a bug or have a suggestion? [Open an issue](https://github.com/google/agents-cli/issues). See the [contributing guide](CONTRIBUTING.md) for details.

## Terms of Service

`agents-cli` leverages Google Cloud APIs. When you deploy agents, you'll be deploying resources in your own Google Cloud project and will be responsible for those resources. Please review the [Google Cloud Service Terms](https://cloud.google.com/terms/service-terms) for details.
