# Underlying Commands Reference

`agents-cli` wraps lower-level tools. When you need flags or behavior not exposed
by the CLI — for debugging, customization, or edge cases — use these directly.

## Scaffolding

The `init`, `enhance`, and `upgrade` commands are the top-level CLI wrappers. They use `cookiecutter` internally to render templates from `src/google/agents/cli/scaffold/`. There is no simpler underlying command — see the source at `src/google/agents/cli/scaffold/commands/`.

Full flag reference:
```bash
agents-cli init --help
agents-cli enhance --help
agents-cli infra prod --help
```

## Dev & Testing

| `agents-cli` command | Underlying command |
|---|---|
| `agents-cli dev` | `uv run adk web .` |
| `agents-cli run "prompt"` | `echo "prompt" \| uv run adk run .` |
| `agents-cli dev --port PORT` | `uv run adk web . --port PORT` |
| `agents-cli serve` | `uv run uvicorn {agent_dir}.fast_api_app:app --host localhost --port 8000 --reload` |
| `agents-cli serve --host HOST --port PORT` | `uv run uvicorn {agent_dir}.fast_api_app:app --host HOST --port PORT --reload` |
| `agents-cli test` | `uv run pytest tests/unit tests/integration` |
| `agents-cli test --unit-only` | `uv run pytest tests/unit` |
| `agents-cli test --integration-only` | `uv run pytest tests/integration` |
| `agents-cli lint` | `uv run ruff check . && uv run ruff format . --check` |
| `agents-cli lint --fix` | `uv run ruff check . --fix && uv run ruff format .` |
| `agents-cli lint --mypy` | `uv run ruff check . && uv run ruff format . --check && uv run mypy .` |
| `agents-cli infra dev` | `terraform init + apply in deployment/terraform/dev/` |
| `agents-cli deploy` | `agents-cli deploy` |

## Evaluation (ADK CLI)

| `agents-cli` command | Underlying command |
|---|---|
| `agents-cli eval` | `uv run adk eval ./{agent_dir} {evalset} --config_file_path {config}` |
| `agents-cli eval --evalset PATH` | `uv run adk eval ./{agent_dir} PATH --config_file_path {default_config}` |
| `agents-cli eval --all` | `uv run adk eval ./{agent_dir} {each_evalset} --config_file_path {config}` for each `.evalset.json` in `tests/eval/evalsets/` |

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

## Secrets (gcloud)

| `agents-cli` command | Underlying command |
|---|---|
| `agents-cli secret set KEY` | `echo -n VALUE \| gcloud secrets create KEY --data-file=-` |
| `agents-cli secret list` | `gcloud secrets list` |

## Rollback (gcloud)

There is no `agents-cli rollback` command. Use `gcloud run services update-traffic` directly:

```bash
gcloud run services update-traffic SERVICE --to-revisions=R=100 --region=REGION
```

List available revisions:
```bash
gcloud run revisions list --service=SERVICE_NAME --region=REGION
```
