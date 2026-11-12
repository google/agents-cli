# Release Notes

All notable changes to this project will be documented in this file.


## [1.14.0] - 2026-11-12

- **`agents-cli deploy` runs pre-flight checks before source builds.**
  - Before uploading, it checks for issues that would break the remote build and explains each one: a `uv.lock` pointing at a package index the builder can't reach, a missing or stale lock, build inputs excluded by `.gcloudignore` or `.gitignore`, path dependencies outside the upload, or a base image Python that doesn't match `requires-python`.
  - `--skip-preflight` skips the checks.
  - `deploy --dry-run` exits 1 on pre-flight errors. Scripts and CI jobs that run it may need an update.
  - https://github.com/google/agents-cli/issues/95
- `agents-cli deploy` adds `--remove-env-vars KEY,...`, `--clear-env-vars`, `--remove-secrets KEY,...` and `--clear-secrets` for Cloud Run and Agent Runtime.
  - A key that is in the project `.env` can't be removed (delete it from `.env` instead), and env vars agents-cli sets itself (such as `AGENT_VERSION`) are set again on every deploy.
  - `--clear-secrets` also removes secrets set outside agents-cli (such as by Terraform) and secrets mounted as volumes.
  - https://github.com/google/agents-cli/issues/60
- `agents-cli deploy` shows the error reason and help links when an Agent Runtime request fails.
- `agents-cli extension add pypi@<package>[==<version>][#<folder>]` installs an extension from a wheel on PyPI, or on the index set in `UV_DEFAULT_INDEX`, `UV_INDEX_URL` or `PIP_INDEX_URL`, pinned to the wheel's sha256. `agents-cli extension update <name> --to <version>` or `--to :latest` moves the pin.
- `agents-cli scaffold enhance --from-adk-project` writes a minimal `agents-cli-manifest.yaml` for an existing ADK project without rendering a template, so `eval`, `info` and `playground` can find it. Use `--dry-run` to preview and `--force` to replace an existing manifest.
- `agents-cli scaffold create -a claude --memory-bank` scaffolds a Claude agent with long-term memory in Vertex AI Memory Bank, which works the way ADK's does. The agent recalls the user's memories at the start of each turn, learns from each finished turn, and can call a `search_memory` tool. Other agents reject the flag; ADK agents can follow the `cross-session-memory` recipe.
  - Memory is per user. For A2A callers without authentication, it lasts one conversation, as in ADK.
  - With `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` on, Claude agents record the system prompt the model got, recalled memories included, as `gen_ai.system_instructions` on the turn span.
- Claude Agent SDK projects send `x-goog-api-client: agents-cli-app/<version>+claude-agent-sdk` on model calls, so Google can count usage from agents-cli projects. Set `DO_NOT_TRACK=1` or `AGENTS_CLI_TELEMETRY=0` to turn it off; your own `ANTHROPIC_CUSTOM_HEADERS` are always sent. Existing projects are unchanged until they adopt the new template code.
- `agents-cli login --status`, `login -i` and `setup` warn when Google Cloud credentials work but agents can't call Gemini yet, and say how to fix it.
- `agents-cli login` with the Gemini API key option prints the `.env` changes a generated project needs to use the key, instead of reporting "Continuing without authentication".
- `agents-cli create` explains which Google Cloud setup step failed.
- `agents-cli run` and `agents-cli playground` check the agent's Google Cloud credentials before starting it.
  - Model errors from `agents-cli run`, such as permission denied, disabled API, billing disabled, quota, or an invalid API key, include a one-line fix.
- `agents-cli run` prints the local-server banner and a failed run's error once, and shows auth errors as plain text instead of Python object reprs.
- `agents-cli infra single-project` stops with `terraform import` instructions when an existing Agent Runtime engine is missing from Terraform state, instead of creating a duplicate. On a new project the first run may log "Skipping the Agent Runtime import check" until Terraform enables the Vertex AI API.
  - https://github.com/google/agents-cli/issues/2
- `google-agents-cli` no longer installs scikit-learn, scipy, litellm, jsonschema or ruamel.yaml, which roughly halves its install size. Eval commands work as before.
- New ADK Python projects pin FastAPI below 0.142.0, so traces from agents on Agent Runtime and Cloud Run are no longer dropped for requests the platform forwards as unsampled.
- New Go projects use ADK Go v2.7.1.
  - For Go projects on ADK Go v2.5.0 or later, `agents-cli playground` honors `--host` and binds `127.0.0.1` by default, the same as Python projects. Pass `--host 0.0.0.0` to reach it from another machine. Older projects keep binding all interfaces.
  - Action needed: when you run `agents-cli scaffold upgrade` on a Go project and keep your own `Dockerfile`, add `"--host", "0.0.0.0"` after `"web"` in its `ENTRYPOINT`. ADK Go v2.5.0 and later listen only on localhost by default, so the container would otherwise get no traffic.
- The `setup.md` prompt no longer stops after install to ask for a new session. The coding agent reads the installed workflow skill and continues.

## [1.13.0] - 2026-11-04

- **Cloud Run deployments support Agent Identity.**
  - `agents-cli deploy --agent-identity` deploys a Cloud Run service with Agent Identity.
  - New projects default using Agent Identity for Cloud Run deployments. To migrate an old project so that it uses Agent Identity, run `agents-cli scaffold enhance` with `--cloud-run-identity-type agent_identity`.
  - To create a new project that uses a service account instead, run `agents-cli scaffold create` with `--cloud-run-identity-type service_account`.
- `agents-cli eval run` and `agents-cli eval grade` end with a `RESULT: PASS` or `RESULT: FAIL (...)` line, preceded on failure by the metrics and cases that failed and a next step.
  - Set a minimum mean score per metric with a `thresholds:` map in the eval config, or for one run with `--fail-under METRIC=SCORE` (repeatable, one metric each). A missed threshold exits 1. `pass_rate` in the results is filled from the threshold when the eval service doesn't report one.
  - Breaking: a run where any case errored now exits 1, even without thresholds. If the errors are rate limits, lower `--qps`.
- `agents-cli login` signs in both the gcloud CLI and Application Default Credentials (`gcloud auth login --update-adc`), so commands that run `gcloud` work right after login.
  - When only Application Default Credentials are available and gcloud has no credentials of its own, agents-cli passes an ADC access token to the `gcloud` commands it runs. GKE deploys in that setup are not covered yet.
  - Note: the account you sign in with also becomes gcloud's active account.
- New projects record `create_params.app_name` in `agents-cli-manifest.yaml`: the name the server serves the agent under (`/apps/{app_name}/...`). `run`, `eval`, `playground`, `deploy` and `publish` use it; projects without it keep using the basename of `agent_directory`.
  - `eval generate --app-name` and `eval run --app-name` default to the project's app name instead of `app`.
- `agents-cli deploy --image <artifact-registry-uri>` works for Agent Runtime. The Agent Runtime service agent needs `roles/artifactregistry.reader` on the repository. `--build-args` can't be combined with `--image`, and an existing agent can't switch between a source build and `--image`.
- Agent Runtime deploys of A2A agents register the agent card in Agent Registry, so the agent shows up as A2A-capable in Gemini Enterprise.
  - https://github.com/google/agents-cli/issues/63
- `agents-cli deploy --dry-run` also validates a GKE deployment against Google Cloud.
- `agents-cli extension add <name>` pins a first-party extension to the latest release compatible with your agents-cli instead of `main`. Pass `--ref main` to track `main`.
  - `agents-cli extension update --to <ref|:latest>` moves a pin. First-party extensions installed from `main` move to the latest release on their next `update`.
- `agents-cli extension add langchain` and `agents-cli extension add claude` install instead of failing with "has no agents-cli-extension.yaml".
- `agents-cli extension add` installs extensions from agents-cli pre-approved repositories without a trust prompt, whether given as a shorthand, an `org/repo` reference or a URL.
- Usage telemetry names an extension when it serves a command if the extension comes from Google.
- Extensions can share files through symlinks that stay inside their repository. These links are copied as regular files when the extension is vendored and when its skills are installed.
- A project, template, or deployment metadata that declares a language this version of agents-cli doesn't support fails with an error listing the supported languages and suggesting `uv tool upgrade google-agents-cli`, instead of being treated as Python.
- `agents-cli scaffold enhance .` in a project without a manifest records the base template it rendered (`adk_python`, `adk_go`), so later `enhance` and `upgrade` runs work. Before, they failed with "Base template 'local_<dir>' not found". In projects already affected, set `base_template` in `agents-cli-manifest.yaml` to `adk_python` (or `adk_go`) by hand.
- `agents-cli scaffold enhance --region <region>` writes the region to `agents-cli-manifest.yaml` and to the files it renders, such as the Terraform variables. Enhancing a project without `--region` keeps the region recorded in its manifest.
- `agents-cli publish gemini` rejects a response that isn't a valid A2A agent card.
- Local A2A agent cards advertise the server's real port instead of `http://localhost:8000`.
- Claude Agent SDK projects: a turn fails when its transcript can't be saved to the session store (Cloud SQL, or the agent's own `session_store`), instead of succeeding while the conversation can't be fully resumed on another instance.
- `agents-cli setup` tells a coding agent running it mid-session to read the workflow skill directly, since newly installed skills only load in the next session.
- The bundled ADK code skill no longer points coding agents straight at `scaffold create` for a new project. It has them load the workflow skill and agree the agent's spec with you before scaffolding.
- The bundled deploy skill now points to `GoogleCloudPlatform/genai-factory` and its `agents-cli` extension for enterprise agent infrastructure.
- The docs site has a new homepage and layout, a "Debug a failing agent" guide, a "Control cost" page, and guides updated to match the bundled skills.

## [1.12.0] - 2026-10-27

- **ADK Go is now Generally Available.** The `adk_go` template is out of Preview: it is stable, production-ready and supported on the same terms as `adk_python`. This release also brings:
  - `agents-cli create --agent <template>` supports ADK Go v2 templates whose `manifest.yaml` declares `language: go`. Templates built for other ADK Go versions are rejected. A declared language that contradicts the base template (or `--base-template`) fails with an error explaining how to fix it.
  - The server e2e tests in new ADK Go projects time out after 120s instead of 60s, so multi-step agents no longer time out.
- **The Python agent template is now `adk_python`**, selected with `--agent adk_python` (or `-a adk_python`). `adk` still works as an alias. New projects record `adk_python` in `agents-cli-manifest.yaml`, and upgrading a project recorded as `adk` records `adk_python` too. Enhancing or upgrading these projects requires this CLI version or newer.
- **New experimental Claude Agent SDK extension.** Create a project with `agents-cli scaffold create --agent google/agents-cli/extensions/claude/template@<version>`, or migrate an existing agent with `scaffold enhance` and that template. It deploys to Agent Runtime or Cloud Run over A2A, calls Claude on Gemini platform only, and comes with Cloud Trace, Cloud Logging, Cloud Monitoring, optional BigQuery Agent Analytics and Cloud SQL sessions. `playground`, `eval dataset synthesize` and `eval optimize` aren't supported, and `publish gemini` is refused on Agent Runtime.
  - On Agent Runtime, these agents also serve `:query` (`async_create_session`) and `:streamQuery` (`async_stream_query`), so an ingress Agent Gateway can govern them (for example with Model Armor) and `agents-cli run --mode adk` works.
- The agent template directory can be a nested folder, such as `src/my_agent`, both in remote templates and with `--agent-directory`.
- `agents-cli scaffold enhance <template>` on a project agents-cli didn't create writes the template's files as `create` would, and moves each of your files it replaces (such as `pyproject.toml`, `README.md`, `Dockerfile`, `.env`) to `.agents-cli-original/`, keeping its path. Your other files stay where they are. Port what you need from the folder, then delete it; enhance won't run again while it exists. New projects' `.gitignore` and `.gcloudignore` exclude the folder.
- Extension templates can set `settings.session_types`, so `create` offers and accepts only those session types, with the first as the default. A template whose list is empty or names an unknown session type is rejected.
- `agents-cli scaffold enhance --dry-run` exits with code 2 (was 0) when combined with `--force`, when run with `-y` and no override flags, or when run without a saved `agents-cli-manifest.yaml`.
- Cloud Run projects run their Cloud Run service on the `hashicorp/google-beta` provider, pinned to `~> 8.7`. Other resources keep `hashicorp/google ~> 7.28`. Existing projects pick it up through `agents-cli scaffold upgrade`; if `terraform init` then reports a locked `google-beta` older than 8.7, run `terraform init -upgrade` once.
- The server e2e tests in new Python projects time out after 120s instead of 60s, so multi-step agents no longer time out.
- `agents-cli deploy --dry-run` for Agent Runtime prints the plan even when a check fails. It still exits with code 1.
- `agents-cli deploy --dry-run` for Agent Runtime warns that it could not verify the secrets when the Secret Manager API is disabled, instead of skipping the check silently.
- `agents-cli deploy --dry-run` also validates a Cloud Run deployment against Google Cloud.
- A malformed `--env-vars`, `--secrets` or `--labels` value fails before the project prompt.
- A failed `agents-cli publish gemini` reports the API's reason (for example a SERVICE_DISABLED message with the enable link) instead of a bare HTTP error.
- A failed terraform step in `agents-cli infra` exits with a short error naming the step and how to rerun it, instead of a Python traceback.
- `agents-cli info` lists globally installed skills.
  - https://github.com/google/agents-cli/issues/96
- `agents-cli run --mode a2a` no longer prints a streamed reply twice when the agent ends with the full reply as its last chunk.
- The bundled skills point to `google/adk-recipes` and call its agents "recipes". The ADK catalog is now `references/recipes.md`.
  - https://github.com/google/agents-cli/issues/97

## [1.11.0] - 2026-10-20

- Breaking: The `adk@<name>` shortcut has been removed. Pass the recipe's GitHub URL to `agents-cli create` instead, and browse recipes at https://github.com/google/adk-recipes.
  - The scaffold's "Browse ADK recipes" option lists recipes from both `core/python/` and `contrib/python/` in google/adk-recipes.
  - Projects created with `adk@` still upgrade. Their `base_template` is rewritten to the recipe URL on the first applied change.
- New projects use `google-adk>=2.12.0` and `google-cloud-aiplatform>=2.2,<3`. Agent Runtime projects also depend on `google-cloud-agentplatform`, and the LangChain extension template moves to `google-cloud-aiplatform>=2.2,<3`.
- `agents-cli deploy` to Agent Runtime registers the ADK artifact methods (`async_save_artifact`, `async_load_artifact`, and others).
- `agents-cli deploy` to Agent Runtime shows the last 40 lines of the build log when a build fails, instead of only "Build failed". `deploy --status` shows it too.
- agents-cli's `uv` commands use public PyPI even when a machine-wide `uv` config points at a mirror, so `deploy` no longer breaks. `agents-cli install` warns when `uv.lock` uses a mirror and shows the command to re-lock. `install --locked` now fails on such a lock. Scripts and CI jobs that run `install --locked` may need an update.
  - https://github.com/google/agents-cli/issues/95
- `agents-cli create` finds the agent code in any remote template, not only adk-recipes ones. When both `app/` and a folder-named package exist, `app/` wins; use `--agent-directory` to pick the other. A top-level `agent_directory` in a template's `agents-cli-manifest.yaml` is now respected.
- LangChain agents are now named after your project, or the name you pass with `--root-agent-name`, like ADK agents.
  - Template authors can list files under `render_files` in `.template/templateconfig.yaml`. Placeholders such as the agent name are then filled in in those files when a project is created.
- Traces from LangChain agents now record which conversation each request belongs to (`gen_ai.conversation.id`), so you can follow a conversation across requests, as you already can for ADK agents.
- `agents-cli run --mode a2a` shows why an agent's task failed, was rejected or was canceled (`[error]: <reason>`) instead of "(no response content)".
- ADK Go agents no longer lose replies for turns that take longer than 15 seconds on `/run` or A2A.
- The Cloud Build pipelines and Dockerfile in generated ADK Go projects retry Go module downloads, so a dropped connection to the Go module proxy no longer fails the build.
- ADK Go projects include only the session backend chosen at creation time.
- `agents-cli scaffold upgrade` stops instead of reporting success when it cannot build the template of the project's original version. It shows why, and suggests `--force` when that can help.
- `agents-cli scaffold upgrade` no longer warns about a legacy eval file that an earlier, stopped upgrade already migrated.
- `agents-cli setup --workspace` lists each installed skill's folder once.
- Extensions can no longer override `agents-cli info`, `login`, `setup` or `update`.
- `agents-cli --help`, the docs and error hints show `create` commands that work as written: `-i` to choose options in prompts, `-y` to accept the defaults (`adk` template, `agent_runtime` target).
- Warnings print as `⚠️  Warning: <message>` with a yellow prefix on stderr. Some warnings that used to go to stdout now go to stderr. Scripts that read warnings from stdout may need an update.
- Errors that agents-cli logs print with the same red `❌ Error:` prefix as other errors, and `--debug` output prints as dim `Debug: …` lines.
- agents-cli no longer prints the google-auth quota-project warning or google-genai's automatic function calling advice. `eval run` no longer prints "Could not obtain credentials" when it talks to the local agent server.

## [1.10.0] - 2026-10-12

- **New projects use Agent Identity by default.** `agents-cli scaffold create` records `agent_runtime_identity_default: agent_identity`. Then `agents-cli deploy` creates the Agent Runtime agent under its own Agent Identity principal, and the generated Terraform (`infra single-project`, `infra cicd`) does the same. To keep a service account, pass `--agent-runtime-identity-default service_account` to `scaffold create` or `scaffold enhance`. Existing projects are not changed: if the setting is missing, it reads as `service_account`, and `scaffold upgrade` does not migrate it.
  - `deploy` uses this default only when it creates a new agent. A redeploy without `--agent-identity` or `--no-agent-identity` does not change the identity of an existing agent.
- **Extensions can ship skills.** An extension declares skills in the `skills:` field of `agents-cli-extension.yaml`:
  - `agents-cli extension add` copies them to `<project>/.agents/skills/`, or to `~/.agents/skills/` with `--global`.
  - `agents-cli extension update` replaces them with the new version's skills and lists the skills it removed.
  - `agents-cli extension remove` deletes them. The new `--global` flag removes the user-scope install.
  - `agents-cli install` restores the skills of every recorded extension.
  - A skill folder copied by an extension is managed by that extension, and manual modifications to it might be overwritten.
- `agents-cli extension update` keeps the old local copy when it refuses a new version or a copy fails.
- New `agents-cli deploy` flags for Agent Runtime and Cloud Run: `--remove-labels KEY,...` and `--clear-labels`. New aliases that match `gcloud run deploy`: `--update-labels`, `--update-secrets` and `--env-vars`.
- `agents-cli deploy` now rejects `--iap`, `--port`, `--service-account` and `--cluster-name` on targets that ignored them. Before, they were silently ignored. Scripts that pass them may need an update.
- `agents-cli run` fails with `Agent run failed: <reason>` and a non-zero exit code when the ADK stream sends an error. Before, the run looked successful.
- `agents-cli scaffold create` shows the dependency changes, like `enhance` and `upgrade` do.
- `agents-cli scaffold create` keeps the detected project in `.env` when credential checks fail, instead of writing the `your-gcp-project-id` placeholder. It also warns when `--session-type` is ignored for `--deployment-target none`.
- `agents-cli scaffold enhance` creates missing config and dependency files instead of skipping them.
- Values in `agents-cli-manifest.yaml` are now escaped, so a quote or newline in a value no longer breaks the file. Project names are limited to `a-z`, `0-9` and `-`.
- Terminology has been adjusted in various places to use appropriate names for the Gemini ecosystem. No functional change, as old variants are kept as aliases where applicable.
- The CLI now uses `google-cloud-aiplatform` 2.x, and `gcsfs` is pinned to `>=2026.10.0`. The template lock files are updated.
- The bundled skills list the Ambient Quality Agent (AQuA) recipe.

## [1.9.0] - 2026-10-06

- **Usage telemetry is now on by default.** Each command writes one log entry to your own Google Cloud project with the command name, exit code, error class, duration, and CLI, Python and OS versions, never arguments or file contents. Its purpose is to provide users with analytics regarding their tool use, process efficiency, and help diagnose potential issues. The request also sends Google the CLI, Python and OS versions, CPU architecture and command name for usage analytics; no sensitive data is sent. Opt out with `AGENTS_CLI_TELEMETRY=0` or `DO_NOT_TRACK=1`. `agents-cli info` now always shows whether telemetry is on.
- `agents-cli deploy --key` encrypts an Agent Runtime agent with a customer-managed Cloud KMS key (CMEK), so deploys work in projects that enforce `constraints/gcp.restrictNonCmekServices`. The key can only be set when the agent is created, and `deploy --list` shows it in a new Encryption column.
  - https://github.com/google/agents-cli/issues/83
- `agents-cli deploy --dry-run` checks an Agent Runtime deployment against Google Cloud: enabled APIs, the target engine, IAM permissions, `actAs` on the service account, and secrets. A definite problem exits with code 1, and a check that cannot run only warns. A dry run now makes read-only network calls. A real deploy is unchanged.
- `agents-cli deploy` reports unsupported flags in one format, `<flag> is only supported for <targets> deployments (current target: <target>)`, and names only the flags you passed. An empty value for such a flag, such as `--key ""` on Cloud Run, is now rejected instead of ignored. Scripts that match the old error text may need updating.
- New ADK Python projects set `GOOGLE_GENAI_USE_ENTERPRISE=true` instead of the deprecated `GOOGLE_GENAI_USE_VERTEXAI`. Existing projects keep working: `agents-cli deploy` to Agent Runtime respects whichever name your `.env` sets. Go projects keep the old name.
- New ADK Go projects enable the `gofmt` and `goimports` formatters, so `agents-cli lint` reports formatting issues and `lint --fix` fixes them.
- Re-applying the Terraform generated by this release, for example with `agents-cli infra single-project` or `infra cicd`, no longer replaces the BigQuery log sink tables once logs have arrived. The replacement deleted every exported log. `completions_view` also gains a `service_version` column with the deployment revision. Run `agents-cli scaffold upgrade` to get this in an existing project.
- Scaffolded ADK projects no longer append a duplicate `JSONRPC 0.3` interface to the agent card on every request to `.well-known/agent-card.json`.
  - https://github.com/google/agents-cli/issues/84
- A tool that exists but cannot run, such as `uv.exe` blocked by AppLocker on Windows, now fails with a clear error naming the tool and the OS reason instead of a `PermissionError` traceback.
  - https://github.com/google/agents-cli/issues/61
- Installed extensions no longer print a warning on every command. A notice appears only when a command overridden by an extension runs.
  - https://github.com/google/agents-cli/issues/91

## [1.8.0] - 2026-09-30

- New flag for`agents-cli scaffold upgrade`:  `--force`. A fallback for when the 3-way merge cannot run or leaves too many conflicts, similar to the existing flag for `agents-cli scaffold enhance`. It generates only the new template and overwrites the scaffolding files:
  - Agent code, config files and files you own are kept: your tests, custom eval metrics, eval datasets, READMEs, agent guidance files, `.gitignore`, `deployment_metadata.json` and Terraform `env.tfvars` files.
  - Dependencies are merged as a union. For a dependency in both files the template's version wins, and dependencies only your project has are kept.
- `agents-cli scaffold upgrade` keeps your Terraform `env.tfvars` files instead of treating them as template files.
- `agents-cli scaffold upgrade` and `agents-cli scaffold enhance` stop with an error when writing the merged dependencies fails. Previously they reported success and stamped the new version, so a rerun said "already at version" and could not retry.
- `agents-cli run --mode a2a` sends messages and your bearer token only to the `--url` host, not to the endpoint declared in the agent card.
  - https://github.com/google/agents-cli/issues/46
- Two concurrent `agents-cli deploy` deploys no longer overwrite each other's deployment state.
  - https://github.com/google/agents-cli/issues/76
- Clarified the documentation on when to use `agents-cli infra single-project --apply` for deployments.
  - https://github.com/google/agents-cli/issues/79
- Generated CI/CD Terraform for Cloud Build now waits for the CI/CD service account's IAM grants to propagate before creating build triggers, so the first push after `agents-cli infra cicd` no longer fails on missing permissions.
- Generated CI/CD Terraform grants Artifact Registry read access on the app's own repository instead of the whole CI/CD project. Destroying one project's infrastructure no longer breaks image pulls for other projects sharing the CI/CD project. A deploy project that relied on the project-wide grant to read another repository loses that access.
- `agents-cli deploy` prints pod state, pod descriptions and namespace events when a GKE rollout does not finish.
- Changed the formatting of some error messages so that they stand out more.
- Updated the ADK version in scaffolded projects to `google-adk>=2.9.2,<3.0.0`.
- Added experimental usage telemetry, off by default. Enable it with `AGENTS_CLI_EXPERIMENTS='{"cli_telemetry": true}'`. It writes one log entry per command to your own Cloud project, with the command, exit code, error class, duration, and CLI, Python and OS versions (but not arguments). Opt out with `AGENTS_CLI_TELEMETRY=0` or `DO_NOT_TRACK=1`.

## [1.7.0] - 2026-09-22

- **Live (voice) agents now work in `run` and `eval`.** Pass `--mode adk_live` wherever you already pass `--mode adk`:
  - `agents-cli run --mode adk_live` sends a prompt over ADK's `/run_live` WebSocket, against a local server or any deployment target. A turn that answers only in audio says so instead of printing nothing.
  - `agents-cli eval generate` and `agents-cli eval run` play each case over one persistent Live socket, with the same dataset format, trace output and grading as the SSE path.
  - The `gemini-3.8-live` models answer one prompt with several model turns. The CLI reads the end of a turn from `interactionStatus` instead of stopping at the first `turnComplete`.
  - The bundled skills cover Live: an ADK Live reference, which models support it, and how to deploy a voice agent.
- `agents-cli run --mode a2a` talks A2A to the local server. A local run used to ignore `--mode` and always use ADK SSE.
- `agents-cli deploy --ingress` sets Cloud Run ingress to `all`, `internal` or `internal-and-cloud-load-balancing`, so a deploy works in a project where `constraints/run.allowedIngress` rejects a public service.
  - https://github.com/google/agents-cli/issues/87
- `agents-cli deploy` updates the Agent Runtime engine whose resource id is recorded in `deployment_metadata.json`, and scans display names only when no id is recorded. A deleted engine, a display name that no longer matches, or a scan that finds duplicates stops the deploy instead of mutating the wrong engine.
  - https://github.com/google/agents-cli/issues/75
- `agents-cli deploy` and scaffolded Terraform both set `identity_type = SERVICE_ACCOUNT` on the Agent Runtime engine, so an agent keeps its service account when Agent Runtime changes that default to Agent Identity.
- `agents-cli scaffold create --agent <remote URL>` handles symlinks again. Since 1.4.1 every symlink in a remote template was dropped, so a template that shared a library across variants scaffolded a project that failed at import. Links inside the cloned repo are copied as real file contents, and a link pointing outside it stops the scaffold with an error.
  - https://github.com/google/agents-cli/issues/89
- `agents-cli deploy` gets a new flag `--framework`, which sets the `agent_framework` property on the deployed agents.
  - https://github.com/google/agents-cli/issues/63
- The generated Cloud SQL password no longer lands in Terraform state. State lives in `gs://{cicd_project}-terraform-state`, whose default IAM let anyone with Viewer on the CI/CD project read the prod database password. Generated Terraform now needs Terraform 1.11.0 or later.
- Scaffolded projects need `google-adk>=2.8.0`. 2.7.x does not emit `interactionStatus`, so a Live turn boundary falls back to a heuristic there. The `<2.9.0` cap from 1.6.1 is unchanged.
- The deprecated `--trace-to-cloud` alias is gone from `agents-cli playground` and `agents-cli run`. Use `--otel-to-cloud`.

## [1.6.1] - 2026-09-16

- **ADK Go is now available to everyone (Preview).** The `adk_go` template ("Go agent with A2A") now appears in `agents-cli scaffold create` with no experiment override, and Go projects get the toolchain Python already has:
  - Deploy to Agent Runtime, Cloud Run and GKE, with session methods exposed to `agent_engines.get()` clients.
  - CloudSQL, Agent Platform and in-memory session services.
  - Prompts and responses in BigQuery Agent Analytics, and telemetry through OTLP.
  - CI/CD templates at parity with Python.
  - The bundled skills cover Go and Python.
- Scaffolds now default to `gemini-3.8-flash`.
- `agents-cli scaffold create` no longer asks for a region when the deployment target is `none`.
- The one-off `agents-cli run` no longer prints stop and resume hints for a server it did not leave running.
- `agents-cli deploy` prints how to talk to a Cloud Run agent once the deploy finishes.
- `agents-cli deploy` can change the identity type on an existing agent.
- `agents-cli deploy` now reports the correct identity after a deploy, instead of the wrong principal for agents using Agent Identity.
- Exclude vendored extensions from the deploy payload, where they could add megabytes to every deploy.
- `agents-cli infra single-project` now correctly enables the Service Usage API, so a new project bootstraps without enabling it by hand.
- Fix remote ADK samples generating two agent directories and an unbuildable project.
- Scaffolded Dockerfiles install uv from a pinned image instead of `pip install`.
- `agents-cli run` and `agents-cli eval` no longer stop a background server they only borrowed.
  - https://github.com/google/agents-cli/issues/71
- Startup and version notices now go to stderr, so `--json` output stays machine-readable.
  - https://github.com/google/agents-cli/issues/69
- `agents-cli eval generate` no longer silently drops per-case session state.
  - https://github.com/google/agents-cli/issues/52
- `agents-cli run --mode a2a` now shows status updates, errors and empty turns instead of staying silent.
- The `run` resume hint includes `--url` and the other routing flags.
- `agents-cli info --json` reports `root_agent_name`.
- `agents-cli extension add` no longer copies ignored files from a `local@` source.
- Pin `google-adk` below 2.9.0. On 2.9.0 a Cloud Run agent's prompts and responses stopped reaching BigQuery Agent Analytics.
- Fix the macOS-only `sed` in the Starter Pack migration guide and spell out the dry-run and apply steps.
  - https://github.com/google/agents-cli/issues/78

## [1.5.0] - 2026-09-01

- **Extension system with a LangChain template.** `agents-cli` can now be extended with custom commands and add-on agent templates, shipped with a LangChain extension and authoring guides. Extensions install from GitHub, other git hosts (including self-hosted and enterprise servers), or a local path for development.
- Add `agents-cli infra show` to inspect the Terraform configuration provisioned by `agents-cli infra single-project`.
- Add `--update-only` to `agents-cli deploy`, so a deploy updates an existing Agent Runtime engine instead of creating a duplicate.
- Add a `--labels` flag to `agents-cli deploy` to tag Agent Runtime and Cloud Run deployments.
- Add `--qps` to `agents-cli eval` that controls the request rate instead of being throttled to the default value.
- Name the scaffolded root agent after its project instead of a generic default.
- `agents-cli deploy` now retries throttled Agent Platform calls.
- `agents-cli create` now reports precondition failures as clear errors instead of exiting successfully or printing a traceback.
  - https://github.com/google/agents-cli/issues/68
- Fix background server reuse ignoring the requested session mode.
  - https://github.com/google/agents-cli/issues/70
- Fix `reasoning_engine_adapter` streaming returning an empty response.
  - https://github.com/google/agents-cli/issues/80
- Prevent multi-region deployments in scaffold and deploy.
  - https://github.com/google/agents-cli/issues/81
- Give a clear error for malformed project manifests instead of silently defaulting or printing a traceback.
  - https://github.com/google/agents-cli/issues/74
- Fix stale command references in docs and CLI hints.
  - https://github.com/google/agents-cli/issues/77
- Scaffolded A2A agents now forward tool calls and responses to clients, not just text.
- `agents-cli setup` now works without network or git access, with skills bundled into the package.
- Fix a Windows failure loading a scaffolded project whose config points at a local path.

## [1.4.2] - 2026-08-28

- Adds upper-bound for google-cloud-aiplatform dependency.

## [1.4.1] - 2026-08-24

- Scaffolds now default to `gemini-3.7-flash`.
- `agents-cli eval generate` no longer fails to generate traces for some agents.
- `agents-cli deploy` now defaults to `--min-instances 0` (was `1`) on Agent Runtime and Cloud Run, so that idle dev and demo agents don't take up resources.
- `agents-cli scaffold upgrade` now creates a project backup before applying changes, just like `agents-cli scaffold enhance`.
- Fixed `npx skills find` to use the pinned `skills` version.
  - https://github.com/google/agents-cli/issues/72
- Fixed an arbitrary file write vulnerability in `agents-cli scaffold create`.
  - https://github.com/google/agents-cli/issues/50

## [1.4.0] - 2026-08-17

- Add Agent Gateway support to `agents-cli deploy`
- Improve A2A-on-Agent-Runtime and MCP Agent Registry guidance in skills
- Use token-based context compaction in ADK cheatsheet
- Add held-out case guidance during iteration to the eval skill
- Remove built-in feedback endpoint, add guidance on building a feedback mechanism to the observability skill instead
- Display project language in `agents-cli info`
- Use $GH_TOKEN and $GITHUB_TOKEN environment variables as fallbacks for `--github-pat`
  - https://github.com/google/agents-cli/issues/18
- Populate class_methods contract on agent_runtime deploy
  - https://github.com/google/agents-cli/issues/55

## [1.3.1] - 2026-08-04

- Add agent-plugins.org plugin manifest
- Smarter backoff handling for CICD command
- Rollback to npx skills 1.5.9 to avoid a spurious error message during `setup`
  - https://github.com/google/agents-cli/issues/59

## [1.3.0] - 2026-07-31

- Update to use A2A 1.0 throughout, with a compatibility layer for A2A 0.3 clients
- Improve retry for Cloud Run deploy
- Streamline retry handing for `infra cicd` command
- Fix `npx skills` output formatting during setup and update
- Print Cloud Logging URL at Agent Runtime deploy start
- Add --concurrency and --header flags to eval generate/run
- Tweak messages around `--session-type` when used with `agent_runtime`

## [1.2.1] - 2026-07-23

- Fix an import problem on `uv sync` caused by a yanked `opentelemetry-resourcedetector-gcp` version
- Add optional HTTP-based path for `eval generate` using `--url` (in-progress feature, stay tuned)

## [1.2.0] - 2026-07-21

- **Cloud telemetry moved to ADK's `otel_to_cloud`, across the CLI and deploys.** `playground` and `run` gain an `--otel-to-cloud` flag that forwards ADK's current `--otel_to_cloud`; the old `--trace-to-cloud` stays as a hidden, still-functional alias that warns when used. On the deployment side, Agent Runtime now exports through `otel_to_cloud` (gated on `GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY`) and generated projects configure Cloud telemetry declaratively in Terraform instead of at runtime. Bundled `google-adk` moves to `>=2.2.0` with the `otel-gcp` extra, and the observability skill was updated to match.
- Scaffolds now default to `gemini-3.6-flash`.
- **Agent Analytics now captures GenAI completions on Agent Runtime.** The BigQuery telemetry log sink is configured per deployment target, so Agent Runtime deployments route their GenAI request/response logs into BigQuery instead of leaving the completions view empty.
- **Managed Agents guidance in the ADK code skill.** The bundled ADK cheatsheet gained a Managed Agents section covering when to use `ManagedAgent`, Gemini API vs Agent Platform (GEAP) setup, and a runnable create-and-use example.

## [1.1.0] - 2026-07-10

- **Guided brainstorming for new agents.** The workflow skill's Phase 0 is now an interactive brainstorming dialogue that helps you shape an agent's spec before any code is written, and surfaces its assumptions for review when it can't ask.
- `eval generate` and `eval grade` no longer clutter output with benign third-party warnings and progress bars that never affected results. This output is still shown on failure and can be re-enabled for debugging.
- Generated projects now name the default scaffolded eval metric module `tests/eval/response_quality.py` (was `metrics.py`) to match the metric it implements.
- Broad Windows compatibility fixes across the CLI.
- Fixed a command-name typo in user-facing hints so they point to the correct `agents-cli` command.
- Refreshed the bundled skills, including pointing the RAG samples at `core/python/` in `google/adk-samples`.

## [1.0.0] - 2026-06-30

**Agents CLI is now 1.0 — Generally Available.** This is our first GA release: a stable, production-ready CLI for scaffolding, evaluating, and deploying ADK agents on Google Cloud.

- Redeploys now preserve the existing deployment spec on Agent Runtime and Cloud Run instead of resetting unspecified settings.
- Agent Runtime deploys now honor `.gcloudignore` and `.gitignore` when packaging source, so uploads no longer include ignored files.
- RAG is now a clone-and-study recipe: start from the `rag-vector-search` / `rag-agent-search` samples in `google/adk-samples` (surfaced via the workflow skill). The `agentic_rag` template, the `--datastore` flag, and the `infra datastore` / `data-ingestion` commands were removed and now print a redirect.
- Generated projects now consolidate Python environment configuration into a single templated `.env` file.
- Eval commands now tolerate ADK toolsets when introspecting eval metadata, so agents that use toolsets no longer fail metadata collection.
- GKE Cloud Build deploys are now resilient to log-streaming limits and no longer fail when the build log stream is truncated.
- Refreshed the bundled skills: RAG samples point to `core/` on adk-samples `main`, the always-active workflow skill was generalized and trimmed, and the ADK code guidance notes `streaming_agent_run_with_events` for debugging on Agent Runtime.

## [0.6.1] - 2026-06-28
- `publish gemini-enterprise` now registers Agent Runtime deployments via ADK by default, which Gemini Enterprise invokes natively and reliably. A2A registration remains the default for Cloud Run and GKE; requesting A2A on Agent Runtime now warns and recommends ADK. Re-publishing an A2A agent no longer creates duplicate registrations, and A2A agent cards now carry the correct public URL on the first deploy.
- `agents-cli update` now exits non-zero and clearly reports when a skill fails to update, instead of always printing a misleading green "Skills updated." banner. Also fixes failure messages rendering in the wrong color on Windows PowerShell.
- Refreshed the generated project `uv.lock` files for all templated agents, updating bundled `google-adk` from 2.2.0 to 2.3.0.

## [0.6.0] - 2026-06-23
- Agent Runtime deploys now serve ADK web, A2A, and the reasoning engine from a single unified container app.
- Cloud Trace spans no longer capture LLM prompts and responses, keeping sensitive content out of traces.
- Refreshed the bundled skills: correctness fixes, de-duplication, and a leaner always-active workflow guide, plus a2ui documented in the ADK code cheatsheet.

## [0.5.1] - 2026-06-18
- Fixed run and playground commands on Windows
  - https://github.com/google/agents-cli/issues/34
  - https://github.com/google/agents-cli/issues/35
  - Thanks to @Abdullah-k0de for discovering and reporting these!
- Fixed stale GCS bucket in failure-investigation guide
- Added Agent Registry fleet management to publish skill

## [0.5.0] - 2026-06-15
- `deploy` now surfaces machine-shape parameters as flags for Agent Runtime and Cloud Run.
- `deploy` adds a `--service-name` override.
- `run` prints a copy-pasteable resume command in the session footer.
- `run` no longer tears down a reused local server on a plain run.
- `scaffold upgrade` now builds the prior-version template via `uvx`.
- Skills setup/update no longer hangs on large `npx` output (a pipe-buffer deadlock).
- The project-root notice now only prints when the command actually changes directory.
- Fixed pre-existing inaccuracies in the bundled skills and generated project READMEs.
- Source code is now published to the public GitHub repo: https://github.com/google/agents-cli

## [0.4.0] - 2026-06-10
- Scaffolded Python templates now use **ADK 2.0 GA**. New `adk`, `adk_a2a`, and `agentic_rag` projects pin `google-adk[gcp]>=2.0.0,<3.0.0`; the `[gcp]` extra restores the OpenTelemetry GCP exporters and bundles the BigQuery client, so the separate `[bigquery-analytics]` extra is no longer needed. Cloud SQL sessions on Cloud Run and GKE keep working under 2.0. The bundled ADK coding skill and its reference docs were refreshed for 2.0.
  - https://github.com/google/agents-cli/issues/24
- Agent Runtime deploys no longer overwrite a user-supplied `AGENT_VERSION` (or `NUM_WORKERS`) passed via `--update-env-vars`, matching Cloud Run behavior. The "version not found" warning now names the `pyproject.toml` field to set.
- Fixed a stale `deployment/terraform/dev/` path in the Cloud Trace observability guide so it matches the current `single-project` terraform layout.

## [0.3.1] - 2026-06-04
- `eval generate` now works on ADK 2.x projects that use built-in tools such as `VertexAiSearchTool`. Raised the `google-cloud-aiplatform` floor to 1.156.0, which carries the SDK fix.
  - https://github.com/google/agents-cli/issues/27
- Skills installed via `agents-cli setup` are now visible to Antigravity. Global skills are mirrored into the Antigravity skill directories.
  - https://github.com/google/agents-cli/issues/26
- `update` now surfaces errors clearly instead of failing silently.
- Agent deploys tolerate a corrupt or malformed `deployment_metadata.json` instead of crashing.
- Deployment timestamps are now timezone-aware.
- A malformed `AGENTS_CLI_EXPERIMENTS` value no longer crashes the CLI.
- `agents-cli install` now runs with `--locked`, so a drifted `uv.lock` fails fast instead of silently resolving new dependency versions.

## [0.3.0] - 2026-05-29
### Breaking
- The eval data format changed from ADK `EvalSet` to Vertex AI `EvaluationDataset`. Existing `tests/eval/evalsets/*.evalset.json` files are no longer read by `agents-cli eval generate` and friends. See [Migrating Eval Datasets](docs/src/reference/eval-dataset-migration.md) for the conversion. `scaffold upgrade` now prints a notice when legacy files are detected.

### Eval - Quality Flywheel (preview)
- Added `eval dataset synthesize` for LLM-driven user-simulation dataset generation.
- Added `eval generate` to run agent inference over an `EvaluationDataset` and emit traces.
- Added `eval grade` to score agent traces against built-in or custom metrics.
- Added `eval submit` to submit an end-to-end cloud-side evaluation run on Vertex AI Eval Service.
- Added `eval results` to fetch results from a completed cloud evaluation run.
- Added `eval analyze` for failure-mode analysis over graded results.
- Added `eval metric list` to discover built-in evaluation metrics.
- Rewrote the `eval` skill end-to-end to cover the Quality Flywheel workflow (dataset, generate, grade, analyze, optimize).

### Other
- Minor skills consistency fixes

## [0.2.1] - 2026-05-28
- Add --dryrun as an alias for --dry-run
- Smarter skills installation
  - https://github.com/google/agents-cli/issues/23
- Cache credentials for better performance
- Fix is_authenticated to work without gcloud
  - https://github.com/google/agents-cli/issues/16
- Fix agent runtime deploy error to be clearer
- Remove 'beta' from gcloud commands that no longer need them
- Fix broken doc links
- Auto gen lockfile if it is missing before trying to export it in deploy
  - https://github.com/google/agents-cli/issues/17

## [0.2.0] - 2026-05-15
- Moved agent-cli project config into a language-independent agents-cli-manifest.yaml file
  - Old config embedded in pyproject.toml can be automatically migrated with `agents-cli scaffold upgrade`
- Added `eval optimize` command
- add --network-attachment and --dns-peering-* flags to deploy
- Misc startup performance improvements
- Avoid crashes related to terminal encodings
  - Fixes https://github.com/google/agents-cli/issues/15
- Smarter tool path resolution, especially for Windows
  - Fixes https://github.com/google/agents-cli/issues/14
- Updated dependency version locks
  - Fixes https://github.com/google/agents-cli/issues/13
- Added manifest support for Claude and Gemini CLI plugin support
- Fix some bugs around preserving the right config metadata when scaffolding and enhancing and/or upgrading
- Misc doc and skill fixes
- Visual Explainer page for Agents CLI lifecycle at https://google.github.io/agents-cli/
- Cleaned up some dead template code

## [0.1.3] - 2026-05-06
- Default `infra` commands to terraform plan instead of apply
- Fix `playground` to work for Cloud Shell and other similar envs and be more transparent about the underlying command
- Update skills to cover need for cloud sql role
- Make `agents-cli info` print OS info for easier bug reporting
- Make `run` only start a background server when requested with `--start-server`
- Clearer display string for ADC auth
- Fix broken doc links
- Fix missing target description for agent_runtime

## [0.1.2] - 2026-04-29
- Document & image fixes
- Project metadata fixes
- Preserve multi-hop traces in completions_view BigQuery SQL
- Detect legacy ADK skills during setup
- Save inline artifacts to .google-agents-cli/artifacts/
- Fix some Windows shell interaction issues
- Remove unprocessed pass-through args for `deploy`, updated skills and --help text
- Fix agents-cli considering the user as authenticated when auth got stale
- Auto stop local `run` server on error

## [0.1.1] - 2026-04-22
- Performance improvements, particularly for CLI startup time
- Doc cleanups

## [0.1.0] - 2026-04-21
- Initial public release
