# The lifecycle

Agents CLI is opinionated about the loop that takes an agent from "looks good in a notebook" to "live in production." This page maps that loop.



## Watch a single investigation

Imagine an outage-recovery agent. It's been live for a week. A pager fires:

<div id="lifecycle-anim-transcript" class="lifecycle-anim" aria-label="Auto-playing transcript of an outage investigation"></div>

That investigation took 4.3 seconds. Most agent frameworks could define the same agent. Agents CLI adds what surrounds it: the eval rubric that would have blocked the release if the agent recommended a destructive remediation, the CI check that would have caught the runbook search returning the wrong section, and the trace that lets you replay this exact investigation when something goes wrong tomorrow.

<a id="four-cli-verbs-on-rotation"></a>

## Run the loop

<div id="lifecycle-anim-loop" class="lifecycle-anim" aria-label="The four steps of the loop in a continuous cycle"></div>

The loop runs three CLI commands, `scaffold`, `eval`, and `deploy`, then you observe production in Cloud Trace and BigQuery, and the loop starts again. You write the spec. The loop catches problems before they ship, ships what passes, and shows you what happens in production so you can fix it in the next iteration.

## What goes wrong without it

Most agent demos stop at the prompt. You write a clever instruction, the model returns something that looks great in a notebook, and you screenshot it for the team. Then production breaks it in ways the notebook didn't show.

| | Without the loop | With Agents CLI |
|---|---|---|
| **Hallucinated remediation** | Discovered customer-side, after the fact | Eval rubric blocks the PR before merge |
| **Tool API change** | 2 AM page, agent silently broken | CI integration test catches the schema drift |
| **Production misuse** | No replay, no telemetry | Cloud Trace + BigQuery analytics show it within the hour |
| **Cost spike from a chatty tool** | Next month's bill is the alert | Per-tool span counts show the loop in hours |

## The eight phases

Broken down step by step, the loop has eight phases. Each phase has a [skill](../reference/skills.md) that tells your coding agent how Agents CLI recommends doing that phase.

| # | Phase | What it does | CLI verb | Skill | Deep-dive |
|---|---|---|---|---|---|
| 0 | **Spec** | Write a `.agents-cli-spec.md`. The other phases derive from the spec. | None | `google-agents-cli-workflow` | [Development guide](development.md) |
| 1 | **Scaffold** | Turn the spec into a production-shaped project (about 60 files). | `create` | `google-agents-cli-scaffold` | [Development guide](development.md#scaffold-a-project) |
| 2 | **Build** | Write the agent body: model, instruction, and tools. | None | `google-agents-cli-adk-code` (ADK templates; other templates ship their own skill) | [Project structure](project-structure.md) |
| 3 | **Orchestrate** | Compose specialists when one agent grows into a team. | None | `google-agents-cli-adk-code` | [Project structure](project-structure.md) |
| 4 | **Evaluate** | Score the agent against a dataset before every deploy. | `eval run` (or `eval generate` + `eval grade`), plus `eval compare`, `eval analyze`, `eval metric list`, and, in ADK Python projects, `eval dataset synthesize` and `eval optimize` | `google-agents-cli-eval` | [Evaluation](evaluation.md) |
| 5 | **Deploy** | Ship to Agent Runtime in Gemini platform, Cloud Run, or GKE. | `deploy` | `google-agents-cli-deploy` | [Deployment](deployment.md) |
| 6 | **Publish** | Register with the Gemini app so its users can use this agent. | `publish` | `google-agents-cli-publish` | [Development guide](development.md#publish-to-gemini-optional) |
| 7 | **Observe** | Cloud Trace + BigQuery analytics; add production cases to your dataset and re-run evals. | None | `google-agents-cli-observability` | [Observability](observability/index.md) |

<a id="0-spec"></a>

### Write the spec

A `.agents-cli-spec.md` names the agent's tools, constraints, and success criteria. The whole rest of the lifecycle reads from it: the scaffold flags, the eval rubrics, the safety guardrails, the trace attributes you'll watch in production. Don't start from blank. Browse [Agent Garden](https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/agent-garden) for an existing template close to what you want, then customize it.

A typical spec is one screen of markdown:

```markdown
# .agents-cli-spec.md: outage-recovery-bot

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

<a id="1-scaffold"></a>

### Scaffold the project

One command takes the spec and emits the project: agent code, tests, eval boilerplate, Terraform, CI/CD workflows, deployment manifests. Each flag adds or removes part of the scaffold to match the lifecycle you've signed up for.

<div id="lifecycle-anim-scaffold" class="lifecycle-anim" aria-label="Scaffold wizard: toggle flags and watch the command and file count update"></div>

The full setup ships about 60 files across agent code, eval boilerplate, Terraform, CI/CD workflows, and deploy manifests. Trim it down by skipping pieces you don't need. See [Project structure](project-structure.md) for what each file does.

<a id="2-build"></a>

### Build the agent

Every ADK Python agent boils down to four ingredients: a model, an instruction, a list of tools, and an `App` that wraps them. The body is barely 30 lines of meaningful code. The interesting work happens inside the tools.

```python
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini

root_agent = Agent(
    name="root_agent",
    model=Gemini(model="gemini-3.8-flash"),
    instruction="You are an SRE outage-recovery assistant...",
    tools=[query_logs, check_metrics, search_runbook],
)

app = App(root_agent=root_agent, name="app")
```

An ADK Go agent (`--agent adk_go`) has the same model, instruction, and tools, built with `llmagent.New` in `app/agent.go`.

LangChain and Claude Agent SDK agents use their own APIs; see [First-party frameworks](extensions/first-party.md).

You're not locked to Gemini. Swap the model line for another provider supported by ADK. In ADK Python, models such as Anthropic Claude and OpenAI GPT go through LiteLLM, for example `LiteLlm(model="openai/gpt-5")`. ADK Go has its own model packages, such as `openaimodel`. The rest of the lifecycle behaves the same regardless.

Stateful agents use two more Gemini platform services:

- **Managed session storage** (ADK templates) for conversation state that survives restarts and scales horizontally. On Cloud Run or GKE, pick it at scaffold time with `--session-type agent_platform_sessions` instead of the in-memory default. Agent Runtime manages sessions itself.
- **[Memory Bank](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank)** (ADK templates) for *long-term* memory across sessions (the SRE bot recognizing "this looks like that incident from last quarter"). Wire it in with `PreloadMemoryTool` and an `after_agent_callback` that sends the session to Memory Bank. The `cross-session-memory` recipe has a complete implementation (see [Extend the starter agent](development.md#extend-the-starter-agent)).

For workflows that don't fit in a single HTTP request, such as long investigations or multi-step batch jobs, Agent Runtime persists the agent's state so a deploy or restart doesn't lose progress.

<div id="lifecycle-anim-models" class="lifecycle-anim" aria-label="Illustrative side-by-side of the same prompt on three model providers"></div>

Here's the same agent body answering a different incident, end-to-end:

<div id="lifecycle-anim-playground" class="lifecycle-anim" aria-label="Inline playground with a payments triage scenario, click to step through"></div>

<a id="3-orchestrate"></a>

### Orchestrate a team

The single-agent body works while the problem is small. Real production agents grow into teams, where an orchestrator routes work to a handful of specialists and each specialist has its own narrow set of tools.

<div id="lifecycle-anim-team" class="lifecycle-anim" aria-label="Team diagram: the orchestrator routes work to investigator, diagnoser, and remediator"></div>

Splitting helps for three reasons that show up in eval, deploy, and observe: smaller prompts make each agent more reliable, separate tool sets let you apply per-agent guardrails, and the trace tells you exactly which sub-agent went wrong.

When the team needs to span processes, or call agents your team doesn't own, use the [A2A protocol](https://a2a-protocol.org/) as the wire format. A2A is built into every scaffolded agent, so scaffold normally (`--agent adk_python` for Python, `--agent adk_go` for Go). Any A2A-compatible agent, built with Agents CLI or not, can call yours, and yours can call theirs.

<a id="4-evaluate"></a>

### Evaluate the agent

The `agents-cli eval run` command runs your dataset against the agent, asks an LLM judge to score each response against a rubric, and gives you a number you can defend.

<div id="lifecycle-anim-eval" class="lifecycle-anim" aria-label="Eval-fix loop. Click Apply fix and re-run to see one case flip from failing to passing"></div>

Expect 5 to 10 or more iterations of the `agents-cli eval run` loop. Each fix moves the score. You re-run, and you ship when it crosses the threshold. With `thresholds` set in the eval config, the command ends with `RESULT: PASS` or `RESULT: FAIL` and exits 1 on a miss. Below are the four failure modes the rubrics catch most often.

<div id="lifecycle-anim-failures" class="lifecycle-anim" aria-label="Common agent failures and the eval rubric that catches each"></div>

See the [Evaluation guide](evaluation.md) for metrics, dataset schemas, and the full methodology.

<a id="5-deploy"></a>

### Deploy the agent

The same agent code can deploy to three targets. The `agents-cli deploy` command runs the steps for the target you scaffolded with. Pick one to see what `--dry-run` would print and the steps that would follow:

<div id="lifecycle-anim-deploy" class="lifecycle-anim" aria-label="Deploy target picker. Choose a runtime to see the dry-run output and pipeline"></div>

```bash
agents-cli deploy --project PROJECT_ID --dry-run   # preview the pipeline
agents-cli deploy --project PROJECT_ID             # ship it
agents-cli deploy --project PROJECT_ID --no-wait   # return immediately; check later with --status
```

Deployments can also use these identity and access controls:

- **[Agent Identity](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/agent-identity)**: on Agent Runtime and Cloud Run, the deployed agent runs as its own Agent Identity principal instead of a shared service account. New projects use Agent Identity by default; opt out at scaffold time with `--agent-runtime-identity-default service_account` or `--cloud-run-identity-default service_account`. Projects scaffolded before that default opt in with `agents-cli deploy --agent-identity`. Scope what it can call (which BigQuery datasets, which buckets, which APIs) with normal IAM. Agent Identity backs up the eval rubrics that block destructive remediations: the agent can't run `kubectl delete` if its identity isn't allowed to.
- **[Identity-Aware Proxy (IAP)](https://cloud.google.com/iap)**: put a Cloud Run deploy behind your Google Workspace SSO with the `--iap` flag, so internal-only agents aren't open to the public internet.
- **[Workload Identity Federation](https://cloud.google.com/iam/docs/workload-identity-federation)**: in projects scaffolded with `--cicd-runner github_actions`, the GitHub Actions workflows, such as `pr_checks.yaml`, authenticate to GCP with WIF, so no service-account keys live in your repo.

See [Deployment](deployment.md) for full per-target walkthroughs.

<a id="6-publish"></a>

### Publish the agent

Deploying the agent makes it reachable at a URL. Publishing is the separate step that registers it in the Gemini app, so the app's users can find and use it.

<div id="lifecycle-anim-publish" class="lifecycle-anim" aria-label="The agent's listing in Gemini after publish"></div>

Publishing has two registration modes. **ADK** publishes a deployed Agent Runtime instance and is the default there. **[A2A](https://a2a-protocol.org/)** publishes an A2A-compatible HTTP endpoint, is the only mode on Cloud Run and GKE, and works with any A2A-compatible agent, no ADK required. The LangChain and Claude templates support A2A registration only, and `publish gemini` is refused on Agent Runtime.

<a id="7-observe"></a>

### Observe production

Once the agent is live, every invocation emits a Cloud Trace span. Every tool call, model generation, and sub-agent handoff is visible. Hover over any span below to see its attributes.

<div id="lifecycle-anim-trace" class="lifecycle-anim" aria-label="Trace waterfall. Bars draw in left to right, showing the orchestrator and its sub-agents. Hover to inspect"></div>

In production, this telemetry shows you regressions your evaluation missed, cost spikes from chatty tools, and cases where users bypass safety prompts. If you run `agents-cli infra single-project --apply` before deploying, ADK Python and Claude agents also log full prompts and responses to Cloud Storage and BigQuery. With `--bq-analytics` turned on at scaffold time (ADK Python and Claude Agent SDK templates), structured agent events such as model calls, tool use, and outcomes also land in BigQuery for offline analysis.

To close the loop, add the production cases that went wrong to your dataset and re-run `agents-cli eval run`. Adding them is a manual step: nothing adds production traffic to your dataset for you. Compare the score across eval runs to see whether a change caused a regression.

<div id="lifecycle-anim-rolling" class="lifecycle-anim" aria-label="Eval score per run over the last 10 runs, with annotated regression and deploy events"></div>

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
    2. Run `agents-cli create outage-recovery-bot --agent adk_python --deployment-target agent_runtime` (for RAG, your coding agent clones a RAG recipe and adapts it; see [Extend the starter agent](development.md#extend-the-starter-agent))
    3. Author the agent body and tools
    4. Write dataset cases
    5. Run `agents-cli eval run` and iterate until the score crosses threshold
    6. Run `agents-cli infra single-project --apply --project PROJECT_ID` if you want Terraform-managed logging and analytics
    7. Run `agents-cli deploy` and hand you the URL

=== "Drive the CLI yourself"

    Every command works standalone. Skip the coding agent entirely if you'd rather type.

    ```bash
    # Phase 1: scaffold
    agents-cli create outage-recovery-bot \
      --agent adk_python \
      --deployment-target agent_runtime \
      --cicd-runner github_actions \
      --bq-analytics
    cd outage-recovery-bot && agents-cli install

    # Phase 2-3: build & orchestrate (edit app/agent.py)
    agents-cli playground       # local web playground at :8080

    # Phase 4: evaluate
    agents-cli eval dataset synthesize --count 10  # optional: cold-start a dataset
    agents-cli eval run                            # repeat until eval score crosses threshold
    agents-cli eval compare prev.json latest.json  # confirm fixes actually helped
    agents-cli eval analyze --eval-result latest.json  # cluster remaining failures
    agents-cli eval optimize \
      --dataset tests/eval/datasets/basic-dataset.json \
      --target-metric final_response_quality       # optional, ADK Python only: auto-tune prompts using eval data

    # Phase 5: deploy
    agents-cli infra single-project --apply --project PROJECT_ID  # provision BigQuery Agent Analytics infra before the first deploy, never after
    agents-cli deploy --project PROJECT_ID --dry-run
    agents-cli deploy --project PROJECT_ID

    # Phase 6: publish (optional)
    agents-cli publish gemini --gemini-app-id <APP_ID>
    ```

    See the [Manual workflow tutorial](hands-on-tutorial.md) for the full end-to-end walkthrough.

</div>

## Where to dig deeper

- [Extend the starter agent](development.md#extend-the-starter-agent) has the recipes your coding agent studies to add capabilities.
- [Project structure](project-structure.md) explains what each generated file does.
- [Development guide](development.md) covers the day-to-day workflow.
- [Evaluation guide](evaluation.md) covers the dataset schema and the eval-fix loop.
- [Debug a failing agent](debugging.md) walks one wrong answer from reproduction to a fix.
- [Deployment](deployment.md) has per-target walkthroughs.
- [CI/CD and production](cicd.md) walks through the full PR-to-prod path.
- [Observability](observability/index.md) covers Cloud Trace, BigQuery analytics, and third-party tools.
- [CLI reference](../cli/index.md) lists every command and flag.
