# Underlying Commands Reference

`agents-cli` wraps lower-level tools. When you need flags or behavior not exposed
by the CLI — for debugging, customization, or edge cases — use these directly.

The dev commands dispatch on the project's `language`, recorded in the manifest.

## Dev & Testing

| `agents-cli` command | Python | Go |
|---|---|---|
| `agents-cli playground` | `uv run adk web . --host 127.0.0.1 --port 8080` | `go run . web --host 127.0.0.1 --port 8080 api -path_prefix / webui --api_server_address=` |
| `agents-cli playground --port 9000` | `uv run adk web . --host 127.0.0.1 --port 9000` | `go run . web --host 127.0.0.1 --port 9000 api -path_prefix / webui --api_server_address=` |
| `agents-cli run "prompt"` | Starts a local server, queries it, then shuts it down (unless using `--start-server`) | starts the Go server above, queries it, then shuts it down |
| `agents-cli run --url URL --mode MODE "prompt"` | HTTP requests to URL (`/run_sse` for adk, A2A protocol for a2a) | the run client is language-agnostic — identical requests |
| `agents-cli lint` | `uv run ruff check .` + `ruff format . --check` + `ty check .` + codespell (skip via `--skip-ty` / `--skip-codespell`) | `golangci-lint run` |
| `agents-cli lint --fix` | `uv run ruff check . --fix && uv run ruff format .` | `golangci-lint run --fix` |
| `agents-cli lint --mypy` | the default checks plus `uv run mypy .` | not applicable — `--mypy`, `--skip-ty` and `--skip-codespell` are Python only |
| `agents-cli infra single-project --apply` | `terraform init + apply in deployment/terraform/single-project/` | `terraform init + apply in deployment/terraform/single-project/` |
| `agents-cli deploy` | Dispatches by target: `gcloud run deploy` (Cloud Run), `terraform` + `docker build` + `kubectl apply` (GKE), `agentplatform` SDK (`Client().runtimes`) in-process (Agent Runtime in Gemini platform) | identical dispatch; the container is built from the project `Dockerfile` either way |
| `agents-cli install` | `uv sync` (`--clean` deletes `.venv`) | `go mod tidy` (`--clean` ignored) |
| `agents-cli install --locked` | `uv sync --locked` | `go mod tidy -diff` then `go mod download` |

## Rollback

Use the native rollback tooling for your deployment target — e.g.,
`gcloud run services update-traffic` for Cloud Run, `kubectl rollout undo`
for GKE, or the Agent Runtime page in the Google Cloud console for Agent Runtime.
