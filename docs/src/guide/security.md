# Security and trust

Agents CLI runs commands on your machine and in your Google Cloud project, and a coding agent often runs them for you. This page lists what runs, what it changes, and how to limit it.

## The one-line prompt

The prompt `Follow google.github.io/agents-cli/setup.md to build my first agent` tells your coding agent to fetch [setup.md](https://google.github.io/agents-cli/setup.md) and follow it: install Agents CLI, design an agent with you, scaffold it, connect a model and run the agent locally. The file is short, plain Markdown. Read it before you paste the prompt.

The setup.md file asks the coding agent to tell you what setup installs before it runs it, and to offer `--dry-run` and `--workspace`. Your coding agent's own permission settings decide which commands it runs without asking you.

<a id="what-agents-cli-setup-runs"></a>
## What setup runs

The `uvx google-agents-cli setup` command fetches the latest `google-agents-cli` package from PyPI into uv's cache and runs `setup`, which does this:

1. **Authentication check**: Reports whether you are signed in. With `-i` it offers Google Cloud ADC or a Gemini API key; `--skip-auth` skips the step.
2. **`uv tool install google-agents-cli`**: Installs the `agents-cli` command as a uv tool. If it is already installed, setup leaves it alone and tells you when a newer version exists.
3. **`npx -y skills@1.5.9 list --json`**: Looks for older ADK skills that can conflict, and only prints a warning.
4. **`npx -y skills@1.5.9 add https://github.com/google/agents-cli -y -g`**: Downloads the `skills` installer from npm, which fetches the GitHub repository and installs the 7 `google-agents-cli-*` skills for each coding agent it detects. `-g` means global: the skills go into `~/.agents/skills`. `--agent` limits the install to the coding agents you name.
5. **Antigravity links**: For global installs, if `~/.gemini` exists, setup links each skill into `~/.gemini/config/skills` and `~/.gemini/antigravity-cli/skills` (directory symlinks, or junctions on Windows). It replaces only links, never real files.

If the GitHub install fails, setup falls back to the skills bundled with the CLI package: first through `npx skills`, then by copying them into `.agents/skills`. Passing `--skills-source` turns the fallback off, so your chosen source is never swapped for another.

<a id="preview-with-dry-run"></a>
## Preview what setup runs

```shell
uvx google-agents-cli setup --dry-run
```

Prints the `uv` and `npx` commands setup would run, the scope, whether it would link the Antigravity folders and your auth status. It changes nothing.

<a id="keep-skills-in-one-project-with-workspace"></a>
## Keep skills in one project

```shell
uvx google-agents-cli setup --workspace
```

Installs the skills relative to the current directory instead of globally, and skips the Antigravity links. The `agents-cli` command is still installed with `uv tool install`.

## Where the skills come from, and pinning

By default the skills come from the default branch of [github.com/google/agents-cli](https://github.com/google/agents-cli) at the moment you run setup, not from the copy bundled with the CLI version you install. The CLI and the skills can therefore be at different versions.

To pin both to a release, install the CLI version first, then point `--skills-source` at the matching tag:

```shell
uv tool install google-agents-cli==1.11.0
agents-cli setup --skills-source https://github.com/google/agents-cli/tree/v1.11.0
```

Setup finds the CLI already installed and leaves it in place. The `--skills-source` flag accepts a local path, a GitHub `owner/repo` or a URL, and passes it to `npx skills add`, which checks out the tag named in a GitHub `/tree/TAG` URL.

The command `uvx google-agents-cli@1.11.0 setup` runs that version of setup, but setup still runs `uv tool install google-agents-cli` without a version, so install the pinned CLI first as above.

!!! warning "`agents-cli update` moves off a pin"

    The `agents-cli update` command reinstalls the skills at their latest version with `npx skills update` and runs `uv tool upgrade google-agents-cli`.

## Credentials

Commands that touch Google Cloud use your gcloud credentials and Application Default Credentials (ADC). When your coding agent runs them, it acts as you, with every IAM role you hold. For first runs, use a sandbox project that holds no production data. Local development can use a Gemini API key from AI Studio instead, with no Google Cloud project. See [Authentication](authentication.md).

<a id="deploying"></a>
## Deploy safely

- **Project confirmation**: If you don't pass `--project`, `agents-cli deploy` takes the project from `gcloud config` and asks you to confirm it. Without `-i` it stops and lists the ways to proceed. The `google-agents-cli-deploy` skill tells coding agents that they must pass `--no-confirm-project` when they rely on that automatic project, which skips the check. To stay in control, name the project for your coding agent so it passes `--project`, or check `gcloud config get-value project` first.
- **Dry run**: `agents-cli deploy --project YOUR_PROJECT_ID --dry-run` prints what it would run without deploying.
- **`.env` goes to production**: `agents-cli deploy` copies every entry in the project-root `.env` into the deployed service's environment variables on Agent Runtime, Cloud Run, and GKE. Remove API keys and other secrets from `.env` before you deploy. On Agent Runtime and Cloud Run, pass secrets from Secret Manager with `--secrets ENV=SECRET` instead.
- **Agent Identity on Agent Runtime**: Projects created with version 1.10.0 or later deploy a new Agent Runtime agent under its own Agent Identity principal instead of a service account. To use a service account, pass `--agent-runtime-identity-default service_account` to `agents-cli create`.
- **Cloud Run is private by default**: Every Cloud Run deploy passes `--no-allow-unauthenticated`, so callers need an identity token. `agents-cli run --url SERVICE_URL --mode a2a` (any template) or `--mode adk` (ADK templates) handles the token for you.
- **No prompt content in traces**: A bare `agents-cli deploy` sets `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false` unless you set it yourself.

## Prompt-response logging

For ADK Python agents, prompt-response logging writes full prompts, responses and token counts as JSONL to a GCS bucket and to a BigQuery `completions` table. It is on whenever a logs bucket is configured (`LOGS_BUCKET_NAME` plus the `OTEL_INSTRUMENTATION_GENAI_*` upload variables).

| Environment | Cloud Trace spans | Prompt-response logging |
|---|---|---|
| Local (`agents-cli playground`) | On, no content | Off |
| Deployed with the generated Terraform | On, no content | On, full prompts and responses |
| Bare `agents-cli deploy`, no bucket | On, no content | Off |

To turn it off for a Terraform-provisioned Python agent, remove the upload block (`OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK`, `OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH`, `LOGS_BUCKET_NAME`) from `deployment/terraform/single-project/service.tf` (or the `cicd/` variant) and re-apply Terraform. See [Observability](observability/index.md).
