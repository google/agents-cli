# Command Flag Reference

## `agents-cli init` Flags

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--agent` | `-a` | `adk` | Agent template — local name (e.g. `adk`), local path (`local@/path`), adk-samples shortcut (`adk@data-science`), or remote Git URL |
| `--deployment-target` | `-d` | `agent_engine` | Deployment target (`agent_engine`, `cloud_run`, `gke`, `none`) |
| `--region` | | `us-central1` | GCP region |
| `--prototype` | `-p` | off | Skip CI/CD and Terraform (recommended for first pass) |
| `--datastore` | `-ds` | — | RAG datastore type (`vertex_ai_search`, `vertex_ai_vector_search`). Use with `--agent agentic_rag`. |
| `--session-type` | | — | Session storage (`in_memory`, `cloud_sql`, `agent_engine`). Use with `cloud_run` target. |
| `--cicd-runner` | | — | CI/CD runner (`github_actions`, `google_cloud_build`, `skip`) |
| `--agent-directory` | `-dir` | `app/` | Custom agent code directory inside the project |
| `--agent-guidance-filename` | | `GEMINI.md` | Coding agent guidance file (`GEMINI.md`, `CLAUDE.md`, or `AGENTS.md`) |
| `--output-dir` | `-o` | `.` | Output directory for the project |
| `--bq-analytics` | | off | Enable BigQuery Agent Analytics plugin (requires `--deployment-target agent_engine` or `cloud_run`) |
| `--google-api-key` / `--api-key` | `-k` | — | Use Google AI Studio API key instead of Vertex AI |
| `--skip-checks` | `-s` | off | Skip verification checks for GCP and Vertex AI |
| `--adk` | | off | Quickstart mode: adk + agent_engine + prototype, skips prompts |
| `--auto-approve` / `--yes` | `-y` | off | Non-interactive: skip prompts, use defaults for missing params |
| `--interactive` | `-i` | off | Interactive mode: show menus and prompts (for use in terminals) |

For all available flags, run `agents-cli init --help`.

## `agents-cli enhance` Flags

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--deployment-target` | `-d` | — | Add deployment target (`agent_engine`, `cloud_run`, `gke`, `none`) |
| `--cicd-runner` | | — | Add CI/CD runner (`github_actions`, `google_cloud_build`, `skip`) |
| `--agent-directory` | `-dir` | `app/` | Agent code directory. Pass if not using the default. |
| `--datastore` | `-ds` | — | RAG datastore type (`vertex_ai_search`, `vertex_ai_vector_search`) |
| `--session-type` | | — | Session storage (`in_memory`, `cloud_sql`, `agent_engine`) |
| `--region` | | `us-central1` | GCP region |
| `--dry-run` | | off | Preview changes without applying them (requires saved metadata) |
| `--force` | | off | Force overwrite all files (skip smart-merge comparison) |
| `--prefer-new` | | off | Resolve conflicts in favor of the new template version |
| `--agent-guidance-filename` | | `GEMINI.md` | Coding agent guidance file (e.g. `CLAUDE.md` for Claude Code) |
| `--bq-analytics` | | off | Add BigQuery Agent Analytics plugin |
| `--google-api-key` / `--api-key` | `-k` | — | Use Google AI Studio API key instead of Vertex AI |
| `--skip-checks` | `-s` | off | Skip verification checks for GCP and Vertex AI |
| `--prototype` | `-p` | off | Prototype mode (skips CI/CD runner prompt) |
| `--auto-approve` / `--yes` | `-y` | off | Non-interactive: skip prompts, use defaults for missing params |
| `--interactive` | `-i` | off | Interactive mode: show menus and prompts (for use in terminals) |

For all available flags, run `agents-cli enhance --help`.
