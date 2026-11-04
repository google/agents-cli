# Authoring extensions

The quickest path is ad-hoc: drop a single `agents-cli-extension.yaml` at your project root (next to `agents-cli-manifest.yaml`). It's **auto-loaded** at project scope, so you don't need `extension add`. Commit it, and teammates and CI get the same overrides. Only one file at that exact path is auto-loaded per project. The same file placed in `~/.config/agents-cli/` is auto-loaded at user scope. For a second extension, or to install a local one globally, move it into its own directory and run `agents-cli extension add local@./path --global`. To share it across repos later, move the same file and its scripts into its own git repo, tag it, and have others run `agents-cli extension add <org>/<repo>#<name> --ref v1.0.0`. The schema doesn't change.

!!! warning "Auto-loaded extensions run with the repo's trust"
    A project-root `agents-cli-extension.yaml` executes its own local code the first time you run an overridden command, with no trust prompt, because you're trusted to run code in a repo you've opened. Treat it like any other code in the repo: review it before running `agents-cli` commands in an untrusted checkout. As a safeguard, each invocation logs which extension took over the command.

---

## Schema (`agents-cli-extension/v1alpha1`)

Everything is optional except `run` on a command, which must be a non-empty list. Unknown keys are rejected, so a typo fails loudly instead of silently doing nothing. For editor validation, point a `yaml-language-server` modeline at the machine-readable schema, `schemas/agents-cli-extension-v1alpha1.schema.json` in the agents-cli repo.

```yaml
schema: agents-cli-extension/v1alpha1
name: my-extension
description: What this extension does.
requires:                 # optional: the agents-cli range this extension supports
  agents_cli: ">=1.11,<2" # optional; omitted means no constraint
  on_incompatible: warn   # warn (install + warn) | error (refuse at add/update)

commands:
  override:               # replace a built-in; user argv passes through verbatim
    deploy:
      run: ["uv", "run", "scripts/custom_deploy.py"]
      description: SBOM upload, then the built-in deploy.
    eval.generate:        # dotted name = a subcommand (group.sub)
      run: ["uv", "run", "scripts/eval_generate.py"]
      description: Framework-specific inference runner.
  add:                    # a brand-new command that doesn't exist yet
    compliance-report:
      run: ["bash", "scripts/compliance_report.sh"]
      description: Generate the quarterly compliance report.

skills:                   # coding-agent skills, copied into .agents/skills on add
  my-guide:               # skill name; should match `name` in its SKILL.md
    path: skills/guide    # a directory with a SKILL.md
```

---

## Rules that matter

- **`run:` is a command vector** executed with **no shell**; your argv is appended verbatim. Relative paths resolve against the extension directory, and `$AGENTS_CLI_EXTENSION_DIR` locates sibling scripts and templates.
- **Start `run:` with a program, not a script.** Because no shell runs the vector, `["python", "scripts/x.py"]` or `["uv", "run", "python", "scripts/x.py"]` works everywhere, while a bare `["scripts/x.py"]` relies on a shebang and never runs on Windows. The CLI warns if the vector starts with a script.
- **You can't override a top-level command *group*** (for example, `eval`). Override a specific subcommand (`eval.generate`) instead. Peers like `eval grade` keep their built-in behavior.
- **You can't override `info`, `login`, `setup`, `update`, `install`, or the `extension` commands.** Users run them to inspect, set up, or repair `agents-cli` in a repo they don't trust yet, so `extension add` refuses an extension that claims them.
- **Re-invoke the built-in safely.** An override runs with `AGENTS_CLI_DISABLE_OVERRIDES=1` set, so calling `agents-cli deploy` inside your wrapper runs the built-in instead of recursing into your override.
- **Chaining lives in a wrapper script**, since `run:` is a single vector, not a shell line. Point `run:` at a script that sequences the steps (check, then `agents-cli deploy "$@"`).
- **`name` is a single path component** matching `[A-Za-z0-9._-]+` (it becomes a directory under `extensions/`); slashes, `.`, and `..` are rejected.
- **Conflicts** (same scope, surfaced in `agents-cli extension list` and `agents-cli info`): two extensions claiming one command is first-wins, and the later one is ignored. Across scopes there is no conflict: project wins over user (`--global`).
- **Skill keys and paths:**
    - The key is the skill's folder name in `.agents/skills/`.
    - The key follows the Agent Skills name rule: at most 64 lowercase letters, digits, and single hyphens, with no hyphen at the start or end.
    - The key must not be a Windows device name such as `con` or `nul`, and must not start with `google-agents-cli-`.
    - Each `path` is relative to the directory that holds `agents-cli-extension.yaml` and must stay inside it, because only that directory is vendored.
    - A `path` must not be or go through a folder that vendoring drops (for example, `build`, `dist`, `venv`, or `node_modules`), and must not be a link. Folders with these names inside a skill folder are dropped too.
- **Skill lifecycle:**
    - `extension add` copies each skill folder without its links (as vendoring does) and adds a `.agents-cli-extension` marker file with the extension name and the skill name.
    - `agents-cli install` re-copies all of an extension's skills when any of them is missing or differs from the vendored copy. It also deletes the skills of extensions that are no longer recorded. See [Pin and update extensions](using.md#lifecycle-lockfile-style) for when it deletes none.
    - `extension update` replaces them with the new version's skills and removes any it no longer declares. If the new skills can't be installed, it keeps the old pin.
    - `extension remove` deletes the skill folders owned by the extension.
- **Ship your own agent** as a template built on the `empty_py` base. That base is the shared project scaffolding (infra and dependencies, no `app/`, no ADK). Drop your own `app/` onto it and keep the `app.fast_api_app:app` entrypoint so deploy is unchanged. Put an `agents-cli-extension.yaml` at the template root and users get its overrides by scaffolding from it, with nothing installed. To ship skills with a template, put them under `.agents/skills/` in the template, and scaffolding copies them as-is. A `skills:` map in the template's manifest is ignored, because nothing installs it. The [LangChain template](first-party.md#langchain) is the worked example.
- **A root `AGENTS.md` in your template becomes the project's coding-agent guide**, written under whatever the project calls it (`GEMINI.md` by default, or `--agent-guidance-filename`), so it replaces the base guide instead of landing beside it. Keep it to what differs; the rest belongs in the skill your template ships.
- **Fill in scaffold values with `render_files`.** Your template's files are copied into the project after the base is rendered, so a `{{cookiecutter.*}}` placeholder in them stays as written unless the file is listed under `render_files` in `.template/templateconfig.yaml`. Listed files are rendered with the same values as the base. For example, `name="{{cookiecutter.root_agent_name}}"` picks up `--root-agent-name` or the name derived from the project, as an ADK project does. A listed file must not contain any other `{{`, `{%` or `{#`; wrap such text in `{% raw %}...{% endraw %}`. The LangChain template lists `app/agent.py`. The Claude template lists `app/app_utils/settings.py` instead, a file it owns: a migrated project's `app/agent.py` is the user's, and `ClaudeAgentOptions` has no name field. `scaffold enhance` renders listed files too.
- **Commit a `uv.lock` in your template.** It is copied into the project as-is, so without one two scaffolds a week apart resolve differently, and so do two Docker builds of the same commit.
- **List every deployment target you support**, including `none`, in your template's `.template/templateconfig.yaml` under `settings.deployment_targets`. The CLI enforces that list, and `--prototype` resolves to `none`, so a template that omits it cannot be scaffolded in prototype mode.
- **List the session types you support** under `settings.session_types`, with `settings.requires_session: true`, for example `["in_memory", "cloud_sql"]`. On `cloud_run` and `gke` the CLI offers only these, rejects any other `--session-type`, and defaults to the first entry. Without the list, all of `in_memory`, `cloud_sql` and `agent_platform_sessions` are offered; leave out `agent_platform_sessions` unless your agent uses ADK's session service. The CLI refuses to scaffold from a template whose list is empty or has any other entry. `cloud_sql` provisions a Postgres instance and sets `INSTANCE_CONNECTION_NAME`, `DB_NAME`, `DB_USER` and `DB_PASS` on the service; your agent must read them.

---

## Authentication

Use the credentials `agents-cli login` already set up, so users sign in once for agents-cli and every extension.

- **Get tokens from Application Default Credentials (ADC).** Call `google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])`, then `credentials.refresh(google.auth.transport.requests.Request())` before you read `credentials.token`: the credentials come back without a token, and invalid ones only fail on refresh. If the refresh fails, fall back to `gcloud auth application-default print-access-token`. ADC also covers CI (service account keys, Workload Identity Federation) and Google Cloud runtimes.
- **Prefer client libraries or REST calls to `gcloud` subprocesses.** They start faster and use ADC directly, so they don't fail when gcloud has no account of its own.
- **When you must run `gcloud` and it has no credentials, pass the ADC token.** gcloud reads only its own sign-in, never ADC, so in environments that only have ADC it fails. There, set `CLOUDSDK_AUTH_ACCESS_TOKEN` to the ADC token in the subprocess environment. The token takes precedence over every other gcloud credential, so set it only when all of these hold; otherwise you silently switch gcloud to another identity:
    - `gcloud auth list --filter=status:ACTIVE --format="value(account)"` prints no account.
    - `gcloud config list --format="json(auth)"` sets neither `access_token_file` nor `credential_file_override`.
    - None of `CLOUDSDK_AUTH_ACCESS_TOKEN`, `CLOUDSDK_AUTH_ACCESS_TOKEN_FILE` or `CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE` is set.
    - The command isn't a `gcloud auth ...` command (for example, `print-identity-token` fails with a token set).

---

## Compatibility

The extension system is experimental. The manifest schema and the command surface may still change in a breaking way, and the schema stays at `v1alpha1` until they stop doing so. Your extension runs its own SHA-pinned code, so a CLI upgrade never silently changes that code.

Always declare the range of CLI versions you support in `requires`. Run `agents-cli --version`, set the lower bound to that `major.minor`, and set the upper bound to the next major (`>=1.11,<2` if the CLI reports 1.11.x). Then choose `on_incompatible`:

- **`warn`** (default): the extension installs and runs, and warns when the CLI is out of range.
- **`error`**: `extension add` and `extension update` refuse an out-of-range install, and if a later CLI upgrade moves the extension out of range, its commands fail with the range and the fix instead of running the built-in.
