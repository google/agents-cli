# Development guide

This guide covers the full development workflow, from defining what you're building to monitoring it in production. It follows the same phases your coding agent uses through the `google-agents-cli-workflow` skill. Agents CLI has two built-in templates: `adk_python` (Python, the default) and `adk_go` (Go). Other frameworks, such as LangChain and the Claude Agent SDK, ship as templates you scaffold from; see [First-party frameworks](extensions/first-party.md). Where the steps differ by language, this guide says so.

---

<a id="phase-0-understand"></a>

## Define what you're building

Before writing any code, define what you're building.

If you're working with a coding agent, it asks you these questions one at a time and gets your approval on the spec before it scaffolds anything. If you're working manually, answer them yourself:

1. **What problem will the agent solve?** Core purpose and capabilities.
2. **External APIs or data sources needed?** Tools, integrations, auth requirements.
3. **Safety constraints?** What the agent must not do.
4. **Deployment preference?** Prototype first (recommended), or full deployment (Agent Runtime in Gemini platform, Cloud Run, GKE)?

Save your answers to `.agents-cli-spec.md` in the current directory. Include an overview, example use cases, tools required, constraints, and success criteria.

---

<a id="phase-1-scaffold"></a>

## Scaffold a project

Create a new project from a template:

```bash
agents-cli create my-agent -i
```

Choose a template and a deployment target during creation. The default template is `adk_python` (`adk` still works as an alias). For a Go project, pass `--agent adk_go`. For another framework, pass a template path ([First-party frameworks](extensions/first-party.md)) or your own ([Authoring](extensions/authoring.md)). For fast prototyping without infrastructure decisions:

```bash
agents-cli create my-agent --prototype --yes
```

You can add deployment support later with `agents-cli scaffold enhance`.

<a id="extending-the-starter-agent"></a>

### Extend the starter agent

The scaffold creates a starter agent: a model, an instruction, and two sample tools, `get_weather` and `get_current_time`. Anything beyond that comes from a recipe, not a scaffold flag. For ADK Python, recipes are working agents in [google/adk-recipes](https://github.com/google/adk-recipes) that you clone and adapt. For ADK Go, the equivalent is the upstream [adk-go examples](https://github.com/google/adk-go/tree/main/examples).

Recipes cover:

- Retrieval over your own documents
- Sandboxed code execution
- Memory across sessions
- OAuth consent
- Approval gates before risky actions
- Event-driven runs

Your coding agent picks the recipe for you. The `google-agents-cli-adk-code` skill contains a [topic index](../reference/skills.md#google-agents-cli-adk-code) (or the [recipes reference](https://github.com/google/agents-cli/blob/main/skills/google-agents-cli-adk-code/references/recipes.md)) that maps each need to a Python recipe, and the workflow skill tells the coding agent to study the matching recipe during design, before it scaffolds or writes code. Ask for an agent that answers questions from your docs, and the coding agent opens `rag-agent-search`.

---

<a id="phase-2-build-iterate"></a>

## Build and iterate

<a id="with-a-coding-agent"></a>

### Build with a coding agent

Open your coding agent and activate the workflow skill:

```
/google-agents-cli-workflow
```

Describe what you want to build. Your coding agent uses the installed skills to write agent logic, create tools, and test changes, following the best practices of your template's framework.

<a id="manually"></a>

### Build manually

Edit your agent logic in `app/agent.py` (Python) or `app/agent.go` (Go), then try it:

- Run `agents-cli playground` to open the ADK web playground at `localhost:8080` (ADK templates). In a Python project, it reloads when your agent code changes. The Go playground (`go run . web`) doesn't reload, so restart it after you edit. The LangChain template starts `langgraph dev`. The Claude template has no playground, so use `agents-cli run`.
- Run `agents-cli run "your prompt"` for a quick smoke test from the terminal.
- When the agent gives a wrong answer, follow [Debug a failing agent](debugging.md).

<a id="code-quality"></a>

### Check code quality

```bash
agents-cli lint                                # Ruff checks and formatting
uv run pytest tests/unit tests/integration     # Run unit and integration tests
```

In a Python project, `agents-cli lint` runs Ruff, ty, and codespell. In a Go project, it runs `golangci-lint`, and you run tests with `go test ./...`. Tests check code correctness. Check agent behavior with evaluations instead, because LLM output isn't deterministic.

<a id="package-management"></a>

### Manage dependencies

In a Python project, add and remove dependencies with [uv](https://docs.astral.sh/uv/):

- `uv add <package>`
- `uv remove <package>`

In a Go project, dependencies live in `go.mod`. The `agents-cli install` command runs `go mod tidy`.

---

<a id="phase-3-evaluate"></a>

## Evaluate the agent

Run structured evaluations to validate agent behavior. The eval commands use the [GenAI Eval SDK](https://docs.cloud.google.com/gemini-enterprise-agent-platform/optimize/evaluation/agent-evaluation) under the hood. The `eval run`, `eval grade`, `eval compare`, and `eval analyze` commands work with any template.

```bash
agents-cli eval run
```

Expect 5 to 10 or more iterations of the eval-fix loop before your agent consistently passes. Start with 1-2 core eval cases, fix failures, then expand coverage. The `eval run` command ends with a `RESULT:` line and exits 1 when a metric misses its threshold or a case errors. It gates only on the `thresholds` in your eval config, so record the bar you agreed on there ([details](evaluation.md#gating-on-scores)).

See the [Evaluation guide](evaluation.md) for metrics, dataset schemas, and the full methodology.

---

<a id="phase-4-deploy"></a>

## Deploy the agent

Once the eval scores are good enough, deploy to Google Cloud.

1. **Add a deployment target** (if you started with `--prototype`):

    ```bash
    agents-cli scaffold enhance --deployment-target cloud_run
    ```

2. **Provision observability infrastructure** (optional):

    ```bash
    agents-cli infra single-project --apply
    ```

    The command uses Terraform to create the service account, Cloud Storage bucket, and BigQuery dataset that prompt-response logging and BigQuery Agent Analytics need. Run it before your first `agents-cli deploy`, never after. If you already deployed without it, don't apply Terraform afterward: on Agent Runtime, Terraform manages the whole Agent Runtime instance and can't take over an existing deployment. See the [Observability guide](observability/index.md) for the alternatives.

3. **Deploy**:

    ```bash
    agents-cli deploy --project <PROJECT_ID>
    ```

For production pipelines with staging, approval gates, and CI/CD, see [Deployment](deployment.md) and [CI/CD and production](cicd.md).

---

<a id="phase-5-publish-optional"></a>
<a id="publish-to-gemini-enterprise-optional"></a>
<a id="publish-to-gemini-optional"></a>

## Publish to the Gemini app (optional)

Register your deployed agent with the Gemini app:

```bash
agents-cli publish gemini --gemini-app-id <APP_ID>
```

The app ID is the full resource name of your Gemini app (`projects/.../engines/...`). Run `agents-cli publish gemini --list` to find it.

You only need to publish if you distribute your agent through the Gemini app.

The old names `publish gemini-enterprise` and `--gemini-enterprise-app-id` still work as aliases. Projects generated by earlier versions call `publish gemini-enterprise` from their Makefile.

---

<a id="phase-6-observe"></a>

## Monitor the agent in production

Monitor your agent in production. Cloud Trace is always on in scaffolded agents, with no configuration needed.

- **Cloud Trace** gives you distributed tracing, latency analysis, and error visibility.
- **Prompt-response logging** (ADK Python and Claude templates) exports full prompts and responses to Cloud Storage and BigQuery. It needs the infrastructure that `agents-cli infra single-project --apply` provisions, and is off locally.
- **BigQuery Agent Analytics** is opt-in (`--bq-analytics` at scaffold time, ADK Python and Claude Agent SDK templates) and adds analytics for token usage, conversation patterns, and LLM-as-judge scoring.

See the [Observability guide](observability/index.md) for setup and usage.

---

For all commands and flags, see the [CLI reference](../cli/index.md). For details on the skills your coding agent uses at each phase, see [Skills](../reference/skills.md).
