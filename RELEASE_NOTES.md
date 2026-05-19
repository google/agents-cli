# Release Notes

All notable changes to this project will be documented in this file.

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
