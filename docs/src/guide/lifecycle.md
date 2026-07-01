# The Lifecycle

Agents CLI is opinionated about one thing: the loop between **"looks good in a notebook"** and **"live in production."** This page is the map.



## Watch a single investigation

Imagine an outage-recovery agent. It's been live for a week. A pager fires:

<div id="lifecycle-anim-transcript" class="lifecycle-anim" aria-label="Auto-playing transcript of an outage investigation"></div>

That investigation took **4.3 seconds**. Nothing about *the agent itself* is unusual — most agent frameworks could express it. What's unusual is everything around it: the eval rubric that wouldn't have let it ship if it recommended a destructive remediation, the CI check that would have caught the runbook search returning the wrong section, the trace that lets you replay this exact investigation when something goes sideways tomorrow.

That's the loop.

## Four CLI verbs on rotation

<div id="lifecycle-anim-loop" class="lifecycle-anim" aria-label="The four CLI verbs in a continuous loop"></div>

`scaffold`, `eval`, `deploy`, observe — on a rotation, forever. You write the spec; the loop catches what would have shipped, ships what passes, and shows you what happens next so the next iteration is smarter.

## What goes wrong without it

Most agent demos stop at the prompt. You write a clever instruction, the model returns something that looks great in a notebook, and you screenshot it for the team. However, deploying to production brings real-world challenges.

| | Without the loop | With Agents CLI |
|---|---|---|
| **Hallucinated remediation** | Discovered customer-side, after the fact | Eval rubric blocks the PR before merge |
| **Tool API change** | 2 AM page, agent silently broken | CI integration test catches the schema drift |
| **Production misuse** | No replay, no telemetry | Cloud Trace + BigQuery analytics surface it within the hour |
| **Cost spike from a chatty tool** | Next month's bill is the alert | Per-tool span counts surface the loop in hours |

## The eight phases

The loop expands to eight phases when you walk through it slowly. Each phase has an opinion encoded in a [skill](../reference/skills.md) so your coding agent picks the right answer for you.

| # | Phase | What it does | CLI verb | Skill | Deep-dive |
|---|---|---|---|---|---|
| 0 | **Spec** | Write a `.agents-cli-spec.md`. The other phases derive from this. | — | `google-agents-cli-workflow` | [Development Guide](development.md) |
| 1 | **Scaffold** | Turn the spec into a production-shaped project (~72 files). | `scaffold create` | `google-agents-cli-scaffold` | [Templates](templates.md) |
| 2 | **Build** | Write the agent body — model, instruction, tools, `App` wrapper. | — | `google-agents-cli-adk-code` | [Project Structure](project-structure.md) |
| 3 | **Orchestrate** | Compose specialists when one agent grows into a team. | — | `google-agents-cli-adk-code` | [Project Structure](project-structure.md) |
| 4 | **Evaluate** | Score the agent against a dataset before every deploy. | `eval generate`, `eval grade`, plus `eval dataset synthesize`, `eval compare`, `eval analyze`, `eval metric list`, and `eval optimize` | `google-agents-cli-eval` | [Evaluation](evaluation.md) |
| 5 | **Deploy** | Ship to Agent Runtime, Cloud Run, or GKE. | `deploy` | `google-agents-cli-deploy` | [Deployment](deployment.md) |
| 6 | **Publish** | Register with Gemini Enterprise so other agents can find this one. | `publish` | `google-agents-cli-publish` | [CI/CD](cicd.md) |
| 7 | **Observe** | Cloud Trace + BigQuery analytics; production data feeds tomorrow's dataset. | — | `google-agents-cli-observability` | [Observability](observability/index.md) |

### 0 · Spec

A `.agents-cli-spec.md` names the agent's tools, constraints, and success criteria. The whole rest of the lifecycle reads from it: the scaffold flags, the eval rubrics, the safety guardrails, the trace attributes you'll watch in production. Don't start from blank — browse [Agent Garden](https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/agent-garden) for an existing template close to what you want, then customize.

A typical spec is one screen of markdown:

```markdown
# .agents-cli-spec.md — outage-recovery-bot

## Tools

| Tool                                    | Backing service       |
| --------------------------------------- | --------------------- |
| `query_logs(service, severity)`         | Cloud Logging         |
| `check_metrics(service, metric)`        | Cloud Monitoring      |
| `search_runbook(query)`                 | Vector Search         |

## Constraints

1. Always cite the runbook section consulted.
2. Never recommend a destructive remediation unless the runbook
   explicitly sanctions it for the observed symptom.

## Success criteria

- ≥ 80% of incidents get a diagnosis whose root cause matches ground truth
- 100% of recommendations cite a runbook section
- 0 destructive recommendations without runbook sanction
```

### 1 · Scaffold

One command takes the spec and emits the project: agent code, tests, eval boilerplate, Terraform, CI/CD workflows, deployment manifests. The flags aren't gratuitous — each one expands or contracts the scaffold to match the lifecycle you've signed up for.

<div id="lifecycle-anim-scaffold" class="lifecycle-anim" aria-label="Scaffold wizard — toggle flags, watch the command and file count update"></div>

The full setup ships **~72 files** across agent code, eval boilerplate, Terraform, GitHub Actions workflows, and deploy manifests. Trim it down by skipping pieces you don't need. See [Templates](templates.md) for the full list.

### 2 · Build

Every ADK agent boils down to four ingredients: a model, an instruction, a list of tools, and an `App` that wraps them. The body is barely 30 lines of meaningful code — the interesting work happens inside the tools.

```python
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini

root_agent = Agent(
    name="root_agent",
    model=Gemini(model="gemini-flash-latest"),
    instruction="You are an SRE outage-recovery assistant...",
    tools=[query_logs, check_metrics, search_runbook],
)

app = App(root_agent=root_agent, name="app")
```

You're not locked to Gemini — swap the model line for any provider supported by ADK ([Model Garden](https://cloud.google.com/model-garden) covers Anthropic Claude, OpenAI GPT, and others). The rest of the lifecycle behaves the same regardless.

Stateful agents reach for two more pieces of Agent Platform:

- **Managed session storage** for conversation state that survives restarts and scales horizontally — pick it at scaffold time via `--session-type agent_platform_sessions` instead of the in-memory default.
- **[Memory Bank](https://cloud.google.com/agent-builder/docs/memory)** for *long-term* memory across sessions (the SRE bot recognizing "this looks like that incident from last quarter"). Wire it in via `from google.adk.memory import VertexAiMemoryBankService` and the agent gets a persistent store keyed to user, session, or app.

For workflows that don't fit in a single HTTP request — long investigations, multi-step batch jobs — Agent Runtime persists the agent's state so a deploy or restart doesn't lose progress.

<div id="lifecycle-anim-models" class="lifecycle-anim" aria-label="Same prompt, three model providers — illustrative side-by-side"></div>

Here's the same agent body answering a different incident, end-to-end:

<div id="lifecycle-anim-playground" class="lifecycle-anim" aria-label="Inline playground — payments triage scenario, click to step through"></div>

### 3 · Orchestrate

The single-agent body works while the problem is small. Real production agents grow into **teams** — an orchestrator that routes work to a handful of specialists, each with its own narrow tool surface.

<div id="lifecycle-anim-team" class="lifecycle-anim" aria-label="Team diagram — orchestrator routes work to investigator, diagnoser, and remediator"></div>

Splitting helps for three reasons that show up in eval, deploy, and observe: smaller prompts make each agent more reliable, separate tool surfaces let you apply per-agent guardrails, and the trace tells you exactly which sub-agent took the bad turn.

When the team needs to span processes — or call agents your team doesn't own — use the **[A2A protocol](https://a2a-protocol.org/)** as the wire format. A2A is built into every ADK agent, so just scaffold normally (`--agent adk`): any A2A-compatible agent (built with Agents CLI or not) can call yours, and yours can call theirs.

### 4 · Evaluate

This is the phase most agent demos skip. `agents-cli eval generate` followed by `agents-cli eval grade` can execute your dataset against the live agent, ask an LLM judge to score each response against a rubric, and give you a number you can defend.

<div id="lifecycle-anim-eval" class="lifecycle-anim" aria-label="Eval-fix loop — click 'apply fix' to see one case flip from failing to passing"></div>

Expect 5–10+ iterations of the `agents-cli eval grade` loop. Every fix nudges the score, you re-run, you ship when it crosses the threshold. Below: the four failure modes the rubrics catch most often.

<div id="lifecycle-anim-failures" class="lifecycle-anim" aria-label="Common agent failures and the eval rubric that catches each"></div>

See the [Evaluation Guide](evaluation.md) for metrics, dataset schemas, and the full methodology.

### 5 · Deploy

The same agent code can land in three different places. `agents-cli deploy` dispatches based on the target you scaffolded with. **Pick one to see what `--dry-run` would print and the steps that would follow:**

<div id="lifecycle-anim-deploy" class="lifecycle-anim" aria-label="Deploy target picker — choose a runtime to see the dry-run + pipeline"></div>

```bash
agents-cli deploy --dry-run        # preview the pipeline
agents-cli deploy                  # ship it
agents-cli deploy --no-wait        # return immediately; check later with --status
```

Each target inherits the surrounding production primitives:

- **Per-agent service account** — opt in with `agents-cli deploy --agent-identity`, and the deployed agent runs as its own GCP identity. Scope what it can actually call (which BigQuery datasets, which buckets, which APIs) with normal IAM. The eval rubrics that block destructive remediations have a fallback: the agent literally can't `kubectl delete` if its identity isn't allowed to.
- **[Identity-Aware Proxy (IAP)](https://cloud.google.com/iap)** — gate a Cloud Run deploy behind your Google Workspace SSO with the `--iap` flag. Internal-only agents stop being a public-internet concern.
- **[Workload Identity Federation](https://cloud.google.com/iam/docs/workload-identity-federation)** — the scaffolded `pr_checks.yaml` authenticates GitHub Actions to GCP via WIF, so no service-account keys live in your repo.

See [Deployment](deployment.md) for full per-target walkthroughs.

### 6 · Publish

Deploying the agent makes it reachable at a URL. Publishing is the separate step that lists it in Gemini Enterprise so other agents (or humans browsing the catalog) can actually find it.

<div id="lifecycle-anim-publish" class="lifecycle-anim" aria-label="The agent's listing in Gemini Enterprise after publish"></div>

Two registration modes: **ADK** (publishes a deployed Agent Runtime instance) and **[A2A](https://a2a-protocol.org/)** (publishes an A2A-compatible HTTP endpoint, no ADK required — works with agents built on any framework).

### 7 · Observe

Once the agent is live, every invocation emits a Cloud Trace span. Every tool call, model generation, and sub-agent handoff is visible. **Hover any span below to see its attributes.**

<div id="lifecycle-anim-trace" class="lifecycle-anim" aria-label="Trace waterfall — bars draw in left-to-right showing the orchestrator and its sub-agents; hover to inspect"></div>

Observability is essential for any agent running in production, as it helps you catch regressions your evaluation might have missed, cost spikes from chatty tools, or cases where users bypass safety prompts. With `--bq-analytics` turned on at scaffold time, every prompt and response also lands in BigQuery for offline analysis.

The same data closes the loop: production traffic feeds tomorrow's dataset. Eval scores get re-computed continuously, so regressions surface in days, not months.

<div id="lifecycle-anim-rolling" class="lifecycle-anim" aria-label="Rolling production eval score over the last ten days, with annotated regression and deploy events"></div>

See [Observability](observability/index.md) for the full setup.

## Two ways to drive it

<div class="lc-tabs-bare" markdown>

=== "Ask your coding agent"

    The canonical path. Your coding agent reads the skills and picks the right CLI command at the right phase.

    ```
    Build me an outage-recovery agent. It should investigate incidents
    using logs, metrics, and runbooks, and recommend remediations
    that cite a runbook section. Deploy it to Agent Runtime.
    ```

    Your coding agent will:

    1. Write a `.agents-cli-spec.md` describing the tools and constraints
    2. Run `agents-cli scaffold create … --agent adk --deployment-target agent_runtime` (RAG is a clone-and-study recipe — adapt a RAG sample; see [Templates](templates.md#rag-retrieval-augmented-generation))
    3. Author the agent body and tools
    4. Write dataset cases
    5. Run `agents-cli eval generate` followed by `agents-cli eval grade` and iterate with `eval grade` until the score crosses threshold
    6. Run `agents-cli deploy`
    7. Wire up trace + analytics, hand you the URL

=== "Drive the CLI yourself"

    Every command works standalone. Skip the coding agent entirely if you'd rather type.

    ```bash
    # Phase 1: scaffold
    agents-cli scaffold create outage-recovery-bot \
      --agent adk \
      --deployment-target agent_runtime \
      --cicd-runner github_actions \
      --bq-analytics
    cd outage-recovery-bot && agents-cli install

    # Phase 2-3: build & orchestrate (edit app/agent.py)
    agents-cli playground       # local web playground at :8080

    # Phase 4: evaluate
    agents-cli eval dataset synthesize --count 10  # optional: cold-start a dataset
    agents-cli eval generate
    agents-cli eval grade                          # repeat until eval score crosses threshold
    agents-cli eval compare prev.json latest.json  # confirm fixes actually helped
    agents-cli eval analyze --eval-result latest.json  # cluster remaining failures
    agents-cli eval optimize                       # optional: auto-tune prompts using eval data

    # Phase 5: deploy
    agents-cli deploy --dry-run
    agents-cli deploy

    # Phase 6: publish (optional)
    agents-cli publish gemini-enterprise
    ```

    See the [Manual Workflow Tutorial](hands-on-tutorial.md) for the full end-to-end walkthrough.

</div>

## Where to dig deeper

- [Templates](templates.md) — scaffold templates (`adk`) and the RAG clone-and-study recipe
- [Project Structure](project-structure.md) — what each generated file does
- [Development Guide](development.md) — day-to-day workflow
- [Evaluation Guide](evaluation.md) — dataset schema, the eval-fix loop
- [Deployment](deployment.md) — per-target walkthroughs
- [CI/CD & Production](cicd.md) — the full PR-to-prod path
- [Observability](observability/index.md) — Cloud Trace, BigQuery analytics, third-party tools
- [CLI Reference](../cli/index.md) — every command and flag
