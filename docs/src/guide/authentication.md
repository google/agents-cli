# Authentication

agents-cli sits on top of multiple tools, each with its own authentication needs. This page breaks down the three distinct levels so you understand exactly what you're authenticating and why.

---

## Level 1: Coding Agent Auth

Your coding agent (Antigravity CLI, Claude Code, Codex, etc.) needs its own authentication to function. **agents-cli does not control this** — each agent handles its own credentials.

| Coding Agent | How to authenticate |
|-------------|---------------------|
| [Antigravity CLI](https://antigravity.google/) | Google account |
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | Anthropic account or API key |
| [Codex](https://github.com/openai/codex) | OpenAI API key |

Refer to your coding agent's documentation for setup instructions. This is independent of agents-cli.

---

## Level 2: Model Auth

The agent you're *building* calls an LLM to generate responses. This requires separate credentials from your coding agent.

ADK supports [multiple model providers](https://adk.dev/agents/models/) — Gemini, Claude, LiteLLM, Ollama, and more. The two most common setups for [Gemini models](https://adk.dev/agents/models/google-gemini/) are below.

### Option A: Gemini API Key (Google AI Studio)

No Google Cloud project required.

1. Go to [AI Studio](https://aistudio.google.com/apikey) and create an API key.
2. Export it:

    ```bash
    # Scaffolded Python projects include a .env. To use AI Studio, edit it:
    #   comment the GOOGLE_* lines and uncomment GEMINI_API_KEY (GOOGLE_API_KEY is also accepted.)
    GEMINI_API_KEY="your-key-here"
    ```

3. Save the file — the `.env` is loaded automatically when you run `agents-cli` dev commands.

!!! note
    The API key supports local development commands: `dev`, `run`, `eval`. Deployment to Google Cloud requires Level 3 auth.

### Option B: Google Cloud (Vertex AI)

Required for Vertex AI models, enterprise features, and deployment.

```bash
agents-cli login -i
# or directly: gcloud auth application-default login
```

This opens your browser for OAuth and sets up Application Default Credentials.

Set your project and location:

```bash
gcloud config set project YOUR_PROJECT_ID
export GOOGLE_CLOUD_LOCATION="us-east1"
export GOOGLE_GENAI_USE_VERTEXAI=TRUE
```


---

## Level 3: Deployment Auth

If you set up Level 2, Option B (Vertex AI), you're already authenticated for deployment — it's the same ADC credential. Beyond model access, ADC also unlocks:

- `agents-cli deploy` — deploy to Agent Runtime, Cloud Run, or GKE
- `agents-cli infra single-project` / `agents-cli infra cicd` — provision infrastructure and CI/CD with Terraform

Deployment requires a Google Cloud project with billing enabled and appropriate IAM permissions (varies by target).

---

## Check Status

```bash
agents-cli login --status
```

Shows which authentication method is active and your current project.

