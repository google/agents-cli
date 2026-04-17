# Underlying Commands Reference

`agents-cli` wraps lower-level tools. When you need flags or behavior not exposed
by the CLI — for debugging, customization, or edge cases — use these directly.

## Dev & Testing

| `agents-cli` command | Underlying command |
|---|---|
| `agents-cli playground` | `uv run adk web .` |
| `agents-cli run "prompt"` | `uv run adk api_server .` (background), then HTTP queries to `/run_sse` |
| `agents-cli run --url URL --mode MODE "prompt"` | HTTP requests to URL (`/run_sse` for adk, A2A protocol for a2a) |
| `agents-cli playground --port PORT` | `uv run adk web . --port PORT` |
| `agents-cli lint` | `uv run ruff check . && uv run ruff format . --check` |
| `agents-cli lint --fix` | `uv run ruff check . --fix && uv run ruff format .` |
| `agents-cli lint --mypy` | `uv run ruff check . && uv run ruff format . --check && uv run mypy .` |
| `agents-cli infra single-project` | `terraform init + apply in deployment/terraform/single-project/` |
| `agents-cli deploy` | `agents-cli deploy` |

## Evaluation (ADK CLI)

| `agents-cli` command | Underlying command |
|---|---|
| `agents-cli eval run` | `uv run adk eval ./{agent_dir} {evalset} --config_file_path {config}` |
| `agents-cli eval run --evalset PATH` | `uv run adk eval ./{agent_dir} PATH --config_file_path {default_config}` |
| `agents-cli eval run --all` | `uv run adk eval ./{agent_dir} {each_evalset} --config_file_path {config}` for each `.evalset.json` in `tests/eval/evalsets/` |

For advanced eval control, use `adk eval` directly:
```bash
# Run with full flag control
adk eval ./app <evalset.json> \
  --config_file_path=tests/eval/eval_config.json \
  --print_detailed_results \
  --eval_storage_uri gs://my-bucket/evals

# Run specific cases from a set
adk eval ./app my_evalset.json:eval_1,eval_2

# Manage eval sets
adk eval_set create <agent_path> <eval_set_id>
adk eval_set add_eval_case <agent_path> <eval_set_id> \
  --scenarios_file conversation_scenarios.json \
  --session_input_file session_input.json
```

## Rollback (gcloud)

There is no `agents-cli rollback` command. Use `gcloud run services update-traffic` directly:

```bash
gcloud run services update-traffic SERVICE --to-revisions=R=100 --region=REGION
```

List available revisions:
```bash
gcloud run revisions list --service=SERVICE_NAME --region=REGION
```
