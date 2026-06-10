# Release Notes

All notable changes to this project will be documented in this file.

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
