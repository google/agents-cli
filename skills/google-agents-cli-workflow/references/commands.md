# Development Commands

| Phase | Commands |
|---|---|
| Setup | `setup` (install skills) · `update` (refresh skills) |
| Scaffold | `scaffold create <name>` · `scaffold enhance .` · `scaffold upgrade` |
| Develop | `playground` (web UI) · `run "prompt"` (one-shot; `-v` = JSON events) · `lint` · `install` |
| Evaluate | `eval dataset synthesize` · `eval generate` · `eval grade` · `eval compare` · `eval analyze` · `eval optimize` · `eval metric list` · `eval submit`/`eval results` (cloud) |
| Deploy | `deploy` (needs approval) · `infra single-project` · `infra cicd` · `publish gemini-enterprise` |
| Info / Auth | `info` · `login --interactive` · `login --status` |

`agents-cli info` prints the **CLI install path** (read it to inspect CLI internals/templates) plus, inside a scaffolded project, the project config.
