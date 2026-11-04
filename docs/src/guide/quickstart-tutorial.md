# Tutorial: Build your first agent

*For beginners who want to build, evaluate, and deploy an agent using a coding agent.*

This tutorial shows the full Agents CLI workflow. You talk to your coding agent, and it builds, evaluates, and deploys an ADK agent for you.

You'll build a **caveman compressor**: an agent that takes verbose text and grunts it down to terse, caveman-style summaries. Inspired by [caveman](https://github.com/JuliusBrussee/caveman).

Here's what it looks like end to end:

![agents-cli demo](https://raw.githubusercontent.com/google/agents-cli/assets/agents-cli-demo.gif)

---

<a id="setup"></a>
## Install Agents CLI

The setup command is the only command you run yourself. Everything else goes through your coding agent.

```bash
uvx google-agents-cli setup
```

Then open your coding agent: [Antigravity CLI](https://antigravity.google/), [Claude Code](https://code.claude.com/docs/en/overview), [Codex](https://github.com/openai/codex), or any other.

---

<a id="1-scaffold"></a>
## Scaffold the project

Tell your coding agent:

> *"Use agents-cli to build a caveman-style agent that compresses verbose text into terse, technical grunts"*

Your coding agent activates the `google-agents-cli-workflow` and `google-agents-cli-scaffold` skills. It will:

- Ask clarifying questions (deployment target, safety constraints, etc.)
- Save a spec at `.agents-cli-spec.md` capturing the agent's purpose
- Scaffold the project:

```
agents-cli create caveman-agent --agent adk_python --prototype --yes
cd caveman-agent && agents-cli install
```

You now have a working project with boilerplate agent code, tests, and eval datasets.

---

<a id="2-build"></a>
## Build the agent

Your coding agent edits `app/agent.py` and replaces the default agent with your caveman compressor. It uses the `google-agents-cli-adk-code` skill for ADK patterns.

The agent definition ends up looking something like:

```python title="app/agent.py"
root_agent = Agent(
    name="caveman_agent",
    model=Gemini(model="gemini-3.8-flash"),
    instruction="""You caveman compressor. Human give long words,
    you make short. Rules:
    - No articles. No filler. No fluff.
    - Short grunts. Simple words.
    - Keep technical terms but grunt around them.
    - Funny but meaning stays.

    Example input:  "I would like to deploy the application to production"
    Example output: "Me deploy. Production. Now."
    """,
)
```

Your coding agent then smoke-tests it:

```
agents-cli run "Please help me understand the deployment options available for my project"
```

Output:

```
Deploy options: Agent Runtime in Gemini platform, Cloud Run, GKE. Pick one. Ship.
```

---

<a id="3-evaluate"></a>
## Evaluate the agent

Tell your coding agent:

> *"Write evals for the caveman agent and run them"*

Your coding agent activates the `google-agents-cli-eval` skill and:

- Creates `tests/eval/datasets/caveman-dataset.json` with test cases (compression quality, technical term preservation, caveman tone)
- Runs the evaluation:

```bash
agents-cli eval run
```

If cases fail, tell your coding agent what to fix:

> *"The response to the greeting test is too polite. Make it more caveman."*

Your coding agent adjusts the instruction, re-runs the evaluation, and iterates until every case meets the bar you agreed on. Your coding agent records that bar as a threshold in the eval config, so `agents-cli eval run` ends with `RESULT: PASS` or `RESULT: FAIL` and exits 1 on a miss.

Besides `eval run`, the `eval dataset synthesize` (ADK Python), `eval compare`, `eval analyze`, and `eval optimize` (ADK Python) commands cover synthetic case generation, regression diffing, failure clustering, and prompt auto-tuning. See the [Evaluation guide](evaluation.md#beyond-eval-run) for details.

---

<a id="4-deploy"></a>
## Deploy the agent

Tell your coding agent:

> *"Deploy this to Cloud Run"*

To also log the prompts and responses your agent handles in production, ask for the observability infrastructure in the same prompt, before the first deploy:

> *"Set up observability infrastructure for my agent, then deploy it to Cloud Run"*

Your coding agent activates the `google-agents-cli-deploy` skill and:

- Adds deployment infrastructure:

```
agents-cli scaffold enhance --deployment-target cloud_run
```

- Provisions observability infrastructure, if you asked for it:

```
agents-cli infra single-project --apply --project PROJECT_ID
```

The command runs Terraform to create the GCS bucket and BigQuery dataset that prompt-response logging writes to. It has to run before the first deploy: the observability skill tells your coding agent never to apply Terraform on top of an agent it already deployed with a bare `agents-cli deploy`.

- Deploys:

```
agents-cli deploy --no-confirm-project
```

The deploy skill tells your coding agent to pass `--no-confirm-project` when it takes the project from `gcloud config`. To pick the project yourself, name it in your prompt so your coding agent passes `--project`.

If your coding agent provisioned observability infrastructure, it also passes the Terraform-created service account, because the deploy skill notes that `agents-cli deploy` doesn't use it on its own:

```
agents-cli deploy --no-confirm-project --service-account SA_EMAIL
```

The service account email is the `app_service_account_email` value that `agents-cli infra show` prints.

Your caveman agent is now live. The output shows the Cloud Run URL.

---

<a id="5-observe"></a>
## Observe the agent

Cloud Trace is on by default and needs no setup. Open the [Trace explorer](https://console.cloud.google.com/traces) in the Google Cloud Console and send a few requests to your agent. You'll see spans for each LLM call and tool execution.

If you asked for observability infrastructure before the deploy, your agent also writes the prompts and responses it handles to the GCS bucket and BigQuery dataset. See the [Observability guide](observability/index.md) for verification steps and advanced options.

---

## What happened

Here's what each prompt triggered under the hood:

| You said | Your coding agent did |
|----------|----------------------|
| *"Build a caveman compressor agent"* | Scaffolded project, wrote agent code, tried it locally |
| *"Write evals and run them"* | Created dataset, ran evaluation using `generate` and `grade` |
| *"Deploy this to Cloud Run"* | Added deployment target, deployed to Cloud Run |
| *"Set up observability, then deploy"* (optional) | Added deployment target, provisioned the GCS bucket and BigQuery dataset, then deployed |

At each step, the skills told your coding agent which ADK patterns to use, how to structure evals, and which deploy target flags to pass.

---

## Next steps

Try building something more complex:

- Add tools: *"Add a Google Search tool so the caveman can grunt about current events"*
- Multi-agent: *"Create an A2A agent that other agents can talk to"* (the `adk_python` and `adk_go` templates have A2A built in)
- RAG: *"Build an agent that answers questions from our docs"* (your coding agent studies a RAG recipe; see [Extend the starter agent](development.md#extend-the-starter-agent))

See the [Development guide](development.md) for the full workflow.
