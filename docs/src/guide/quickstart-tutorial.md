# Tutorial: Build Your First Agent

*For beginners who want to build, evaluate, and deploy an agent using a coding agent.*

This tutorial shows the full Agents CLI in Agent Platform experience — you talk to your coding agent, and it builds, evaluates, and deploys an ADK agent for you.

You'll build a **caveman compressor**: an agent that takes verbose text and grunts it down to terse, caveman-style summaries. Inspired by [caveman](https://github.com/JuliusBrussee/caveman).

Here's what it looks like end to end:

![agents-cli demo](https://raw.githubusercontent.com/google/agents-cli/assets/agents-cli-demo.gif)

---

## Setup

The only command you run yourself. Everything else goes through your coding agent.

```bash
uvx google-agents-cli setup
```

Then open your coding agent — [Gemini CLI](https://github.com/google-gemini/gemini-cli), [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex](https://github.com/openai/codex), or any other.

---

## 1. Scaffold

Tell your coding agent:

> *"Use agents-cli to build a caveman-style agent that compresses verbose text into terse, technical grunts"*

Your coding agent activates the `google-agents-cli-workflow` and `google-agents-cli-scaffold` skills. It will:

- Ask clarifying questions (deployment target, safety constraints, etc.)
- Write a `DESIGN_SPEC.md` capturing the agent's purpose
- Scaffold the project:

```
agents-cli create caveman-agent --prototype --yes
cd caveman-agent && agents-cli install
```

You now have a working project with boilerplate agent code, tests, and eval sets.

---

## 2. Build

Your coding agent edits `app/agent.py` — replacing the default agent with your caveman compressor. It uses the `google-agents-cli-adk-code` skill for ADK patterns.

The agent definition ends up looking something like:

```python title="app/agent.py"
root_agent = Agent(
    name="caveman_agent",
    model=Gemini(model="gemini-flash-latest"),
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
Deploy options: Agent Engine, Cloud Run, GKE. Pick one. Ship.
```

---

## 3. Evaluate

Tell your coding agent:

> *"Write evals for the caveman agent and run them"*

Your coding agent activates the `google-agents-cli-eval` skill and:

- Creates `tests/eval/evalsets/caveman.evalset.json` with test cases (compression quality, technical term preservation, caveman tone)
- Configures LLM-as-judge criteria in `tests/eval/eval_config.json`
- Runs the evaluation:

```
agents-cli eval run
```

If cases fail, tell your coding agent what to fix:

> *"The response to the greeting test is too polite. Make it more caveman."*

Your coding agent adjusts the instruction, re-runs `agents-cli eval run`, and iterates until quality thresholds pass.

---

## 4. Deploy

Tell your coding agent:

> *"Deploy this to Cloud Run"*

Your coding agent activates the `google-agents-cli-deploy` skill and:

- Adds deployment infrastructure:

```
agents-cli scaffold enhance --deployment-target cloud_run
```

- Deploys:

```
agents-cli deploy
```

Your caveman agent is now live. Cloud Run URL in the output.

---

## 5. Observe

Cloud Trace is enabled by default — no setup needed. Open the [Trace explorer](https://console.cloud.google.com/traces) in the Google Cloud Console and send a few requests to your agent. You'll see spans for each LLM call and tool execution.

To go further and inspect the actual prompts and responses your agent handles in production, tell your coding agent:

> *"Set up observability infrastructure for my agent"*

Your coding agent runs `infra single-project`, which provisions the service account, GCS bucket, and BigQuery dataset — and updates the deployed service to use them. See the [Observability Guide](observability/index.md) for verification steps and advanced options.

---

## What happened

Here's what each prompt triggered under the hood:

| You said | Your coding agent did |
|----------|----------------------|
| *"Build a caveman compressor agent"* | Scaffolded project, wrote agent code, tested locally |
| *"Write evals and run them"* | Created evalset, configured LLM-as-judge, ran `agents-cli eval run` |
| *"Deploy this to Cloud Run"* | Added deployment target, deployed to Cloud Run |
| *"Set up observability"* | Provisioned service account, GCS bucket, and BigQuery dataset |

The skills gave your coding agent the context to make the right decisions at each step — which ADK patterns to use, how to structure evals, which deploy target flags to pass.

---

## Next steps

Try building something more complex:

- Add tools — *"Add a Google Search tool so the caveman can grunt about current events"*
- Multi-agent — *"Create an A2A agent that other agents can talk to"* (use `adk_a2a` template)
- RAG — *"Build an agent that answers questions from our docs"* (use `agentic_rag` template)

See [Agent Templates](templates.md) for all options, or jump to the [Development Guide](development.md) for the full workflow.
