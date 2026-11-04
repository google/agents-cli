# Getting started

Paste one line into your coding agent. It installs Agents CLI, designs a first
agent with you, and runs it locally.

<div class="paste" role="group" aria-label="How to start"><p class="paste-text"><span class="sr-only">Paste into your coding agent: </span>Follow <a href="../../setup.md"><span>google.github.io/</span><wbr><span>agents-cli/setup.md</span></a> to build my first agent</p><button type="button" class="paste-copy" data-copy="Follow google.github.io/agents-cli/setup.md to build my first agent"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2" /><path d="M5 15V5a2 2 0 0 1 2-2h10" /></svg><span data-copy-label aria-live="polite">Copy prompt</span></button></div>

Agents CLI is a CLI and a set of skills for building, evaluating, and deploying
agents on Google Cloud, with [ADK](https://adk.dev/), LangChain or the Claude
Agent SDK. Agents CLI handles everything around your agent: scaffolding,
evaluation, deployment, and observability.

## What your coding agent does

<div class="steps" markdown>

1.  **Installs Agents CLI**

    Reads [setup.md](../../setup.md) and runs `uvx google-agents-cli setup`,
    which installs the `agents-cli` command and its skills.

1.  **Asks for your language**

    Python or Go, with ADK as the framework. For LangChain or the Claude Agent
    SDK, it scaffolds from a first-party template.

1.  **Designs the agent with you**

    Asks what the agent should do, which tools it calls and how to judge
    success, then scaffolds the project and writes the agent with an eval
    dataset.

1.  **Connects a model**

    A Gemini API key from AI Studio, or a Google Cloud project. See
    [Authentication](authentication.md). The API key works without code
    changes in ADK Python and LangChain projects. An ADK Go project also needs
    a change to `app/agent.go` ([details](project-structure.md#env)). The
    Claude template needs a Google Cloud project.

1.  **Opens the playground**

    The `agents-cli playground` command starts the local playground and
    prints its URL (`http://127.0.0.1:8080/...` by default) so you can talk to
    your agent. ADK templates serve the ADK web UI and the LangChain template
    runs the LangGraph dev server. The Claude template has no playground
    ([First-party frameworks](extensions/first-party.md)).

</div>

## Evaluate and deploy

Keep going in the same session:

```text
Write evals for this agent and run them
```

```text
Deploy this to Cloud Run
```

See [Evaluation](evaluation.md) and [Deployment](deployment.md) for what
happens under the hood.

## What the skills teach your coding agent

Agents CLI installs 7 skills:

| Skill | What your coding agent learns |
|-------|-------------------------------|
| `google-agents-cli-workflow` | Development lifecycle, code preservation, model selection |
| `google-agents-cli-adk-code` | ADK Python and Go APIs: agents, tools, orchestration, callbacks |
| `google-agents-cli-scaffold` | Project scaffolding: `create`, `enhance`, `upgrade` |
| `google-agents-cli-eval` | Evaluation lifecycle: datasets, metrics, generate and grade, compare, analyze, optimize |
| `google-agents-cli-deploy` | Deployment: Agent Runtime in Gemini platform, Cloud Run, GKE, CI/CD |
| `google-agents-cli-publish` | Registration in the Gemini app |
| `google-agents-cli-observability` | Cloud Trace, logging, third-party integrations |

See the [skills reference](../reference/skills.md) for details.

## Run the steps yourself

### Prerequisites

**Required:** [uv](https://docs.astral.sh/uv/getting-started/installation/). It downloads Python 3.11 or later if you don't have it.

**Recommended:** [Node.js](https://nodejs.org/en/download), so setup installs the skills with `npx skills` and `agents-cli update` can update them. Without it, setup copies the skills instead.

**Optional (for deployment):** [Google Cloud SDK](https://cloud.google.com/sdk/docs/install), [Terraform](https://developer.hashicorp.com/terraform/downloads)

### Install

```bash
uvx google-agents-cli setup
```

The setup command installs the CLI and the skills for the coding agents it finds on your
machine.

??? info "Alternative installation methods"
    **pipx:** `pipx install google-agents-cli && npx skills add google/agents-cli`

    **venv + pip:** `pip install google-agents-cli && npx skills add google/agents-cli`

    **Skills only:** `npx skills add google/agents-cli`

**Platform support:** macOS, Linux, and Windows (WSL 2). Native Windows is not officially supported.

### Authenticate

If you're already authenticated with `gcloud`, Agents CLI picks up your
Application Default Credentials automatically.

Otherwise, the quickest option is a Gemini API key from [AI Studio](https://aistudio.google.com/apikey):

```bash
export GEMINI_API_KEY="your-key-here"
```

In a scaffolded Python project, also comment out the `GOOGLE_*` lines in `.env` so the agent calls the Gemini API instead of Google Cloud. A Go project also needs a change to `app/agent.go`; see [`.env`](project-structure.md#env). See [Authentication](authentication.md) for full details.

### Build with your coding agent

Open your coding agent and check that it lists the `google-agents-cli-*` skills.
In Claude Code, run `/skills`. Most other coding agents have a similar command
or a settings panel. Then ask it to build something:

```
Build a support agent that answers questions from our docs
```

<a id="prefer-to-type-commands-yourself"></a>
### Build without a coding agent

You can drive the entire workflow from your terminal, with no coding agent.

```bash
# Create a minimal agent project
agents-cli create my-agent --prototype --yes

# Install dependencies and start the dev playground
cd my-agent
agents-cli install
agents-cli playground
```

In ADK templates, the playground command starts the ADK web playground, with hot reload in Python projects. Other templates differ; see [First-party frameworks](extensions/first-party.md). Open the URL the command prints (`http://127.0.0.1:8080/...` by default).

For a full walkthrough, see the [manual workflow tutorial](hands-on-tutorial.md).

<a id="languages-and-status"></a>

## Templates and status

Status as of October 2026.

| Language | Template | Status | Notes |
|----------|----------|--------|-------|
| Python | `adk_python` (alias `adk`) | Default | ADK; A2A built in |
| Go | `adk_go` | Supported | ADK Go; API key needs a code change |
| Python | First-party template path | See [First-party frameworks](extensions/first-party.md) | LangGraph, Claude Agent SDK |
| Your choice | Your own template | Supported | `--agent github.com/org/repo/path@tag`; see [Authoring](extensions/authoring.md) |

Pass the template with `--agent`. Without it, `agents-cli create` uses `adk_python`.

## Demo

<div align="center">
  <iframe width="100%" height="450" src="https://www.youtube.com/embed/ECYKo70pPNc" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>
</div>

## Next steps

- [Tutorial: Build your first agent](quickstart-tutorial.md): build, evaluate, and deploy with your coding agent
- [Tutorial: Manual workflow](hands-on-tutorial.md): type every command yourself
- [Use cases](use-cases.md): agent patterns people build
- [Project structure](project-structure.md): what each generated file does
- [Extend the starter agent](development.md#extend-the-starter-agent): the recipes your coding agent studies to add capabilities
- [Development guide](development.md): the full development workflow
- [CLI reference](../cli/index.md): all commands and flags
- [Security and trust](security.md): what setup installs and runs, and how to limit it

!!! note "Share what you build"
    Built something interesting with Agents CLI? Share your project at [agents-cli@google.com](mailto:agents-cli@google.com).
