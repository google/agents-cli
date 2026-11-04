# Authentication

Agents CLI sits on top of several tools, and each one authenticates separately. This page covers the three layers (your coding agent, your agent's model calls, and deployment), what each one authenticates, and why.

---

<a id="level-1-coding-agent-auth"></a>
## Authenticate your coding agent

Your coding agent (Antigravity CLI, Claude Code, Codex, etc.) needs its own authentication to work. **Agents CLI does not control coding agent authentication.** Each coding agent handles its own credentials.

| Coding agent | How to authenticate |
|-------------|---------------------|
| [Antigravity CLI](https://antigravity.google/) | Google account |
| [Claude Code](https://code.claude.com/docs/en/overview) | Anthropic account or API key |
| [Codex](https://github.com/openai/codex) | ChatGPT account or OpenAI API key |

Refer to your coding agent's documentation for setup instructions.

---

<a id="level-2-model-auth"></a>
## Authenticate your agent's model calls

The agent you're *building* calls an LLM to generate responses. It needs credentials separate from your coding agent's.

Which models you can use depends on the template. ADK supports [multiple model providers](https://adk.dev/agents/models/), including Gemini, Claude, LiteLLM, and Ollama. The LangChain template calls Gemini. **The Claude template calls Claude on Gemini platform only, so it needs Application Default Credentials even for local runs.** The two most common setups for [Gemini models](https://adk.dev/agents/models/google-gemini/) are below.

<a id="option-a-gemini-api-key-google-ai-studio"></a>
### Use a Gemini API key (Google AI Studio)

No Google Cloud project required.

1. Go to [AI Studio](https://aistudio.google.com/apikey) and create an API key.
2. Make the key available to your agent as `GEMINI_API_KEY` (`GOOGLE_API_KEY` also works). Export it in your shell, or set it in the `.env` that scaffolded projects include. That `.env` also sets the `GOOGLE_*` variables for Gemini platform, which take precedence, so comment them out:

    ```bash
    # .env
    # GOOGLE_GENAI_USE_ENTERPRISE=true
    # GOOGLE_CLOUD_PROJECT=your-project-id
    # GOOGLE_CLOUD_LOCATION=global
    GEMINI_API_KEY="your-key-here"
    ```

    This `.env` swap works in Python projects. A Go project also needs a change to `app/agent.go`, which hard-codes the Gemini platform backend. See [`.env`](project-structure.md#env).

3. The Agents CLI dev commands load the `.env` automatically.

!!! note
    With the ADK and LangChain templates, the API key covers the local development commands: `playground`, `run` and `eval`. The Claude template can't use it, because it calls Claude through Gemini platform; use [Google Cloud auth](#use-google-cloud-gemini-platform) instead. Deployment to Google Cloud requires [deployment auth](#level-3-deployment-auth). The `agents-cli deploy` command copies the `.env` into the deployed service, so remove the key first; see [Deploy safely](security.md#deploying).

<a id="option-b-google-cloud-vertex-ai"></a>
<a id="option-b-google-cloud-agent-platform"></a>
<a id="option-b-google-cloud-gemini-platform"></a>
### Use Google Cloud (Gemini platform)

Required for Gemini platform (formerly Vertex AI) models, enterprise features, and deployment.

```bash
agents-cli login -i
# or directly: gcloud auth login --update-adc
```

Pick Google Cloud (ADC) from the `login` menu. The command then opens your browser for OAuth and signs in both the gcloud CLI and Application Default Credentials (ADC). agents-cli needs both: it calls some Google Cloud APIs directly with ADC and runs `gcloud` for others. The account you sign in with also becomes gcloud's active account, replacing any other account gcloud was using.

!!! note
    If only ADC is set up (for example, a CI runner with `GOOGLE_APPLICATION_CREDENTIALS` or a container with ADC mounted) and gcloud has no active account, agents-cli passes an ADC access token to the `gcloud` commands it runs, so they work without a separate `gcloud auth login`.

Set your project and location:

```bash
gcloud config set project YOUR_PROJECT_ID
export GOOGLE_CLOUD_LOCATION="global"
export GOOGLE_GENAI_USE_ENTERPRISE=TRUE  # Python projects
export GOOGLE_GENAI_USE_VERTEXAI=TRUE    # Go projects
```

Scaffolded projects already set `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION=global` in `.env`, plus `GOOGLE_GENAI_USE_ENTERPRISE=true` for Python or `GOOGLE_GENAI_USE_VERTEXAI=true` for Go. Not every model is served from every location, so check [model endpoint locations](https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/locations#google-models) before you pick a region.


---

<a id="level-3-deployment-auth"></a>
## Authenticate for deployment

If you set up [Google Cloud model auth](#use-google-cloud-gemini-platform), you're already authenticated for deployment, because it uses the same ADC credential. Beyond model access, ADC also lets you run:

- `agents-cli deploy` to deploy to Agent Runtime in Gemini platform, Cloud Run, or GKE
- `agents-cli infra single-project` / `agents-cli infra cicd` to provision infrastructure and CI/CD with Terraform

Deployment requires a Google Cloud project with billing enabled and the IAM permissions your deployment target needs. They're the same permissions as deploying without Agents CLI, for example `aiplatform.reasoningEngines.create` for Agent Runtime or `run.services.create` for Cloud Run, plus `iam.serviceAccounts.actAs` on the service account. To check yours before you deploy, run `agents-cli deploy --dry-run`; it checks the permissions your target needs and flags any you're missing.

---

## Check status

```bash
agents-cli login --status
```

Shows which authentication method is active and your current project.

