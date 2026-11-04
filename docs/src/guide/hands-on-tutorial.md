# Tutorial: Manual workflow

*For developers who prefer to type every command themselves, without a coding agent.*

This tutorial walks you through building, running, and evaluating an ADK agent by typing every command yourself, without a coding agent.

!!! tip
    Prefer to let your coding agent do the work? See [Tutorial: Build your first agent](quickstart-tutorial.md) instead.

---

## What you'll build

You'll start with the default agent template, an assistant that can look up the weather and tell the time. Then you'll customize it with a new persona and a custom tool.

## Prerequisites

- Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/) installed
- Agents CLI installed with `uvx google-agents-cli setup` or `uv tool install google-agents-cli` (see [Getting started](getting-started.md#install))
- Authentication set up with either a [Gemini API key](authentication.md#option-a-gemini-api-key-google-ai-studio) or [Google Cloud credentials](authentication.md#use-google-cloud-gemini-platform)

---

<a id="1-create-the-project"></a>
## Create the project

```bash
agents-cli create my-first-agent --prototype --yes
cd my-first-agent
agents-cli install
```

- `--prototype` skips Terraform and CI/CD, so you get only agent code, tests, and eval datasets.
- `--yes` auto-accepts defaults (ADK template, in-memory session storage).
- `agents-cli install` installs all Python dependencies via `uv sync`.

---

<a id="2-explore-the-project"></a>
## Explore the project

Your project contains:

```
my-first-agent/
├── app/
│   ├── __init__.py       # Registers the app
│   ├── agent.py          # Agent definition, where your logic lives
│   └── app_utils/        # A2A and session service helpers
├── tests/
│   ├── eval/
│   │   ├── datasets/
│   │   │   └── basic-dataset.json   # Test cases for evaluation
│   │   └── eval_config.yaml         # Metrics configuration
│   ├── integration/
│   │   └── test_agent.py
│   └── unit/
│       └── test_dummy.py
├── pyproject.toml        # Project config and dependencies
└── GEMINI.md             # Guidance file for coding agents
```

The important file is `app/agent.py`. Open it and you'll see two tool functions (`get_weather`, `get_current_time`) and an agent definition:

```python title="app/agent.py"
MODEL = "gemini-3.8-flash"

root_agent = Agent(
    name="my_first_agent",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="You are a helpful AI assistant designed to provide accurate and useful information.",
    tools=[get_weather, get_current_time],
)
```

For a full breakdown of every file, see [Project structure](project-structure.md).

---

<a id="3-run-the-agent-locally"></a>
## Run the agent locally

Start the ADK web playground:

```bash
agents-cli playground
```

Open the URL the command prints (`http://127.0.0.1:8080/...` by default) in your browser. You'll see a chat interface. Try sending:

> What's the weather in San Francisco?

The agent calls the `get_weather` tool and responds with something like: *"It's 60 degrees and foggy in San Francisco."*

!!! tip
    The playground has hot reload. Changes you save to `app/agent.py` take effect immediately.

---

<a id="4-test-from-the-terminal"></a>
<a id="test-from-the-terminal"></a>
## Send a prompt from the terminal

You can also send a prompt without the browser:

```bash
agents-cli run "What's the weather in San Francisco?"
```

The run command sends a single prompt and prints the agent's response.

---

<a id="5-customize-the-agent"></a>
## Customize the agent

Give the agent a personality. Open `app/agent.py` and change the instruction:

```python title="app/agent.py"
root_agent = Agent(
    name="my_first_agent",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="""You are a cheerful weather reporter who speaks in short, 
    punchy sentences. Always include a fun weather-related pun in your responses. 
    When asked about time, relate it back to weather somehow.""",
    tools=[get_weather, get_current_time],
)
```

Save the file. If the playground is still running, it reloads automatically. Try the same question again. The response should now have a different tone.

---

<a id="6-add-a-custom-tool"></a>
## Add a custom tool

Add a tool that counts words. Add this function above the `root_agent` definition in `app/agent.py`:

```python title="app/agent.py"
def count_words(text: str) -> str:
    """Count the number of words in the given text.

    Args:
        text: The text to count words in.

    Returns:
        A string with the word count.
    """
    word_count = len(text.split())
    return f"The text contains {word_count} words."
```

Then register it in the agent's `tools` list:

```python
    tools=[get_weather, get_current_time, count_words],
```

Try it:

```bash
agents-cli run "How many words are in: The quick brown fox jumps over the lazy dog"
```

The agent calls `count_words` and responds with the word count.

!!! tip
    ADK tools are plain Python functions. The docstring becomes the description the LLM sees. The model reads it to decide when and how to use the tool, so write it clearly.

For more on adding tools, see the [ADK custom tools documentation](https://adk.dev/tools-custom/).

---

<a id="7-run-an-evaluation"></a>
## Run an evaluation

Evaluations validate that your agent behaves correctly. Your project comes with a default dataset at `tests/eval/datasets/basic-dataset.json`. Its first case:

```json title="tests/eval/datasets/basic-dataset.json"
{
  "eval_cases": [
    {
      "eval_case_id": "greeting",
      "prompt": {
        "role": "user",
        "parts": [{"text": "Hello, what can you help me with?"}]
      }
    }
  ]
}
```

Each eval case defines a user message. The evaluation system sends the message to your agent and grades the response using metrics specified in `eval_config.yaml`.

Run it:

```bash
agents-cli eval run
```

The output shows scores for each eval case against the configured metrics.

The [Evaluation guide](evaluation.md) covers the full evaluation workflow: writing test cases, adding metrics, the eval-fix loop, and the other eval commands (`eval dataset synthesize` and `eval optimize` in ADK Python, `eval compare`, `eval analyze`, and `eval metric list`).

---

<a id="8-deploy-to-google-cloud"></a>
## Deploy to Google Cloud

Once your agent passes evals, deploy it. First, add a deployment target (prototype projects don't include one):

```bash
agents-cli scaffold enhance --deployment-target cloud_run
```

To log prompts and responses in production (see [View content logs](#view-content-logs)), provision the observability infrastructure now, before the first deploy:

```bash
agents-cli infra single-project --apply --project YOUR_DEV_PROJECT_ID
```

The command runs Terraform to create a GCS bucket and BigQuery dataset, and the Cloud Run service that the deploy then updates. Skip it if you only need Cloud Trace. Don't run it after a bare `agents-cli deploy`: Terraform then tries to create a service that already exists.

Set your Google Cloud project and deploy:

```bash
gcloud config set project YOUR_DEV_PROJECT_ID
agents-cli deploy --project YOUR_DEV_PROJECT_ID
```

New projects run the agent as its own Agent Identity principal. If your project runs on a service account instead (created with `--cloud-run-identity-default service_account`, or before Agent Identity became the default) and you provisioned the observability infrastructure, deploy with the Terraform-created service account. Its email is the `app_service_account_email` value that `agents-cli infra show` prints:

```bash
agents-cli infra show
agents-cli deploy --project YOUR_DEV_PROJECT_ID --service-account SA_EMAIL
```

Passing `--project` skips the confirmation that `agents-cli deploy` asks for when it reads the project from `gcloud config`. Without `--project` or `-i`, the command stops and lists the ways to proceed.

Verify it's running:

```bash
agents-cli deploy --status
```

!!! note
    Deployment requires [Google Cloud credentials](authentication.md#use-google-cloud-gemini-platform). See the [Deployment guide](deployment.md) for Agent Runtime in Gemini platform, GKE, and other options.

---

<a id="9-observe-your-agent"></a>
## Observe your agent

Cloud Trace is on by default and needs no configuration. Send a few requests to your agent, then open the [Trace explorer](https://console.cloud.google.com/traces) in the Google Cloud Console. You'll see spans for each LLM call and tool execution, with latency breakdowns.

### View content logs

If you ran `agents-cli infra single-project --apply` before the first deploy (see [Deploy to Google Cloud](#deploy-to-google-cloud)), your agent writes the prompts and responses it handles to the GCS bucket and the BigQuery dataset. If you deployed without it, the infrastructure for content logs isn't there, and applying Terraform now conflicts with the service you already deployed. The [Observability guide](observability/index.md) covers that case.

See the [Observability guide](observability/index.md) for verification steps, full content capture, and BigQuery Agent Analytics.

---

## What you've done

| Step | What happened |
|------|--------------|
| `agents-cli create --prototype --yes` | Created a project with agent code, tests, and eval datasets |
| `agents-cli playground` | Started the ADK playground to chat with the agent |
| `agents-cli run "..."` | Ran a smoke test from the terminal |
| Edited `agent.py` | Customized the persona and added a tool |
| `agents-cli eval run` | Validated agent behavior with structured evaluations |
| `agents-cli infra single-project --apply` | Provisioned the GCS bucket and BigQuery dataset for content logs (optional, before the first deploy) |
| `agents-cli deploy` | Deployed the agent to Google Cloud |
| Trace explorer + content logs | Verified tracing and prompt-response logging |

---

## Next steps

- [ADK custom tools](https://adk.dev/tools-custom/): More tool patterns and advanced usage
- [Evaluation guide](evaluation.md): Write better evals and understand metrics
- [Deployment guide](deployment.md): Agent Runtime, GKE, secrets, and CI/CD
- [Observability guide](observability/index.md): BigQuery Agent Analytics and third-party integrations
