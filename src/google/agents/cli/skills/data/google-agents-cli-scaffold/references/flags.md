# Command Flag Reference

## `agents-cli scaffold create` Flags

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--agent` | `-a` | `adk_python` with `-y`; otherwise required | Agent template — local name (`adk_python` for Python, `adk_go` for Go; `adk` is an alias for `adk_python`), local path (`local@/path`), or remote Git URL (`<org>/<repo>[/<path>][@<ref>]`) |
| `--deployment-target` | `-d` | `agent_runtime` with `-y`; otherwise required unless `--prototype` | Deployment target (`agent_runtime`, `cloud_run`, `gke`, `none`) |
| `--region` | | `us-east1` | GCP region |
| `--prototype` | `-p` | off | Skip CI/CD and Terraform (recommended for first pass) |
| `--session-type` | | — | Session storage (`in_memory`, `cloud_sql`, `agent_platform_sessions`). Use with `cloud_run` or `gke` targets (Agent Runtime in Gemini platform manages sessions itself). |
| `--cicd-runner` | | — | CI/CD runner (`github_actions`, `google_cloud_build`, `skip`) |
| `--agent-directory` | `-dir` | `app/` | Custom agent code directory inside the project; may be nested, e.g. `src/my_agent` |
| `--agent-guidance-filename` | | `GEMINI.md` | Coding agent guidance file (`GEMINI.md`, `CLAUDE.md`, or `AGENTS.md`) |
| `--output-dir` | `-o` | `.` | Output directory for the project |
| `--bq-analytics` | | off | Enable BigQuery Agent Analytics plugin (supported on `agent_runtime`, `cloud_run`, and `gke`) |
| `--skip-checks` | `-s` | off | Skip verification checks for GCP and Gemini platform |
| `--adk` | | off | Quickstart mode: adk_python + agent_runtime + prototype, skips prompts. It forces `--agent adk_python` (Python). For a Go quickstart use `--agent adk_go --prototype --yes` instead. |
| `--auto-approve` / `--yes` | `-y` | off | Non-interactive: skip prompts, use defaults for missing params |
| `--interactive` | `-i` | off | Interactive mode: show menus and prompts (for use in terminals) |

For all available flags, run `agents-cli scaffold create --help`.

> **ADK-specific:** `--adk` is a shortcut for the built-in ADK template, and `adk_python` is the only
> built-in one (`adk` is accepted as an alias). `--agent` also takes a template repo (`<org>/<repo>[/<path>][@<ref>]` or `local@<path>`),
> which is how third-party templates and other frameworks ship.

> **Go templates:** a remote template whose `manifest.yaml` says `language: go` renders on `adk_go`.
> It must use ADK Go v2 (`google.golang.org/adk/v2`); other versions are rejected. Its agent package
> exports `NewRootAgent(ctx context.Context) (agent.Agent, error)`.
> - The project keeps the generated `main.go`, `main_test.go`, `go.mod`, `go.sum` and `Dockerfile`.
>   The template's requirements are merged into `go.mod` and `go.sum` (`replace` and `toolchain`
>   directives aren't copied), then `go mod tidy` runs. Both need Go; if it wasn't installed at
>   create time, run `agents-cli install` once it is, before building or deploying.
> - A flat template's root `.go` files become the agent directory's package (`app/` by default). In
>   other layouts, root `.go` files aren't copied, so shared code belongs in a package folder.
> - A Go package the template ships (any folder with `.go` files) replaces the generated one at that
>   path. A replaced `sessions/` or `appinfo/` must keep the API `main.go` calls
>   (`sessions.NewService(ctx)`, `appinfo.NewLauncher()`).
> - The image copies only top-level folders that contain Go code, so files the agent reads at runtime
>   belong inside one (or embedded with `go:embed`). It builds with `CGO_ENABLED=0`.
> - Deploy sends the values in `.env`, so copy any variables from the template's `.env.example` into `.env`.

> To use Google AI Studio instead of Gemini platform, edit the generated `.env`: comment the `GOOGLE_*` lines and uncomment `GEMINI_API_KEY`.

## `agents-cli scaffold enhance` Flags

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--deployment-target` | `-d` | — | Add deployment target (`agent_runtime`, `cloud_run`, `gke`, `none`) |
| `--cicd-runner` | | — | Add CI/CD runner (`github_actions`, `google_cloud_build`, `skip`) |
| `--agent-directory` | `-dir` | `app/` | Agent code directory. Pass if not using the default. |
| `--session-type` | | — | Session storage (`in_memory`, `cloud_sql`, `agent_platform_sessions`) |
| `--region` | | `us-east1` | GCP region |
| `--dry-run` | | off | Preview changes without applying them (requires saved metadata) |
| `--force` | | off | Force overwrite all files (skip smart-merge comparison) |
| `--prefer-new` | | off | Resolve conflicts in favor of the new template version |
| `--agent-guidance-filename` | | `GEMINI.md` | Coding agent guidance file (e.g. `CLAUDE.md` for Claude Code) |
| `--bq-analytics` | | off | Add BigQuery Agent Analytics plugin |
| `--skip-checks` | `-s` | off | Skip verification checks for GCP and Gemini platform |
| `--prototype` | `-p` | off | Prototype mode (skips CI/CD runner prompt) |
| `--auto-approve` / `--yes` | `-y` | off | Non-interactive: skip prompts, use defaults for missing params |
| `--interactive` | `-i` | off | Interactive mode: show menus and prompts (for use in terminals) |

For all available flags, run `agents-cli scaffold enhance --help`.
