# Using extensions

Extensions adapt `agents-cli` to a workflow it doesn't ship with, such as a different agent framework, an org's deploy policy, or a command of your own, without forking the tool.

---

## What an extension can do

- **Override a built-in command.** Replace what `create`, `run`, `deploy`, or `eval generate` does. Your command-line arguments pass straight through to the replacement, so the command still feels native.
- **Add a new command.** Expose a workflow the tool doesn't have as `agents-cli <your-command>`.
- **Ship coding-agent skills.** Skills the extension declares are copied into `.agents/skills/` when you add it, so your coding agent knows how to use the extension.

An extension adds skills but can't change existing ones. Skill names starting with `google-agents-cli-` are reserved for the skills `agents-cli setup` installs, and `extension add` stops if a skill folder with the same name exists that this extension didn't install.

No extension can override `info`, `login`, `setup`, `update`, `install`, or the `extension` commands. They always run the built-in, so you can inspect, set up, or repair `agents-cli` in a repo you don't trust yet, and remove a broken extension.

A few things extensions make possible:

- **Swap the agent framework.** Run the lifecycle (`run`, `eval`, `deploy`) on LangChain or the Claude Agent SDK instead of ADK by scaffolding from a framework template that carries its own extension. See [First-party frameworks](first-party.md).
- **Wrap a command with policy.** Run a compliance or SBOM check before the built-in `deploy`.
- **Standardize a team workflow.** Commit an `agents-cli-extension.yaml` so everyone, including CI, gets the same overrides.

---

## How extensions work

An extension contributes two things:

- **Command overrides**: the extension maps a command name to a `run:` command vector, a list such as `["uv", "run", "scripts/custom_deploy.py"]`. When you invoke the command, `agents-cli` runs that vector (no shell) with your arguments appended.
- **Skills**: directories with a `SKILL.md`. The `extension add` command copies them into the skills folder: `<project>/.agents/skills/` at project scope, or `~/.agents/skills/` with `--global`.
    - A skill folder that an extension copied belongs to the extension. The `extension add`, `extension update`, and `agents-cli install` commands replace the whole folder, so your own files there are lost. To change a skill, copy its folder under a new name. The copy is yours: `agents-cli` never replaces or deletes it.
    - Many coding agents read the project folder, for example Gemini CLI, Antigravity, Codex, and Cursor. Fewer read `~/.agents/skills/`. Claude Code reads `.claude/skills/` instead, so link or copy the skill there yourself.
    - For Antigravity, `agents-cli` links each `--global` skill into its global skills folder.
    - If your home folder is a project, both scopes share `~/.agents/skills/`, so an extension with skills can be installed at only one of them.

Extensions come from these places: any `org/repo` on GitHub, a URL on any git host, a local path, a first-party shorthand name, or an `agents-cli-extension.yaml` committed at your project root (auto-loaded, no install step). A framework template ships that last kind, so scaffolding from one needs no install. A template ships its skills as files under `.agents/skills/`, and scaffolding copies them like any other file. Only `extension add`, `extension update`, and `agents-cli install` read the `skills:` field.

---

## Adopt an extension

```bash
agents-cli extension add <ref> [--global] [--ref <branch|tag|sha|:latest>] [--yes]
agents-cli extension list                      # active extensions, their scope, and the commands they contribute
agents-cli extension update [<name>] [--to <branch|tag|sha|:latest>]  # advance or move the pin and replace its skills
agents-cli extension remove <name> [--global]  # drop it, its vendored copy, and skills no other scope uses
agents-cli info                                # active extensions, their sources, and any conflicts
```

An overridden or added command is tagged in `agents-cli --help` with `[↑ <extension>]`, and `agents-cli <command> --help` shows its source, scope, and the `run:` vector it executes.

### Reference forms

| Form | Meaning |
|------|---------|
| `acme/acli-extensions` | any `org/repo` on GitHub |
| `acme/acli-extensions#soc2-deploy` | select one extension from a multi-extension repo |
| `https://git.example.com/acme/acli-extensions` | any git host, over `https://`, `http://`, or `ssh://` |
| `git@git.example.com:acme/acli-extensions` | the same host, in scp form |
| `local@../my-extension` | a local path (for development) |
| `<name>` | first-party shorthand that pins the latest release tag of the `google/agents-cli` repo that isn't newer than your agents-cli (for example `v1.12.0`) |
| `--ref <branch\|tag\|sha>` | pin a branch, tag, or commit SHA |
| `--ref :latest` | pin the latest release, as the shorthand does without `--ref` (first-party only) |

Agents CLI clones a URL with your existing git configuration, so whatever already authenticates you to that host (a credential helper or an SSH agent) applies. Agents CLI doesn't ask for or store credentials.

---

## Scopes

- **Project (default)**: recorded in this repo's `agents-cli-extensions.yaml`, with the working copy vendored under `extensions/`. It affects **only this project**, and committing both means teammates and CI get the same overrides.
- **User (`--global`)**: stored in `~/.config/agents-cli/` (`%APPDATA%\agents-cli` on Windows). Applies to every project on the machine, and works *before a project exists*, so you can use it to bootstrap an override.

When both scopes define the same command, **project wins**; `agents-cli info` shows the source.

!!! tip "Prefer project scope"
    A global extension changes commands across every project on the machine. Reach for `--global` only when you need it before a project exists (framework bootstrap) or want it machine-wide.

---

## Trust

Extensions from `google/agents-cli`, `google/agents-cli-extensions` and `google/adk-recipes` are verified and trusted automatically, whether you add them with the shorthand form (`agents-cli extension add <name>`), an `org/repo` reference, or an https or ssh URL. Every other reference **prompts before install**, since its commands run arbitrary code when invoked. The `--yes` flag skips the prompt and trusts every extension, so use it only for automation and bootstrap.

---

<a id="lifecycle-lockfile-style"></a>

## Pin and update extensions

Extensions pin to an exact commit, like a lockfile:

- **`extension add`** resolves the ref to a commit SHA, records `source` / `ref` / `sha` under `extensions:` in `agents-cli-extensions.yaml`, vendors a working copy under `extensions/`, and copies the extension's skills into the skills folder.
    - Each copied skill folder gets a `.agents-cli-extension` marker file that holds the extension name and the skill name. A skill folder without this file, or copied under a new folder name, is yours. A folder whose file names another extension belongs to that extension. Neither is overwritten. Don't delete the marker: without it, `agents-cli` treats the folder as yours and no longer replaces or removes it.
    - If a skill folder is yours or another extension's, `extension add` stops with an error, changes no skill, and records nothing.
    - While it copies or deletes skills, `agents-cli` uses hidden folders named `.<name>.incoming` and `.<name>.outgoing` in the skills folder. Don't keep your own files under these names, because they can be overwritten.
- **`agents-cli install`** re-materializes any missing or stale vendored copy from the pinned SHA, so CI and fresh checkouts get the exact reviewed code, and commands and overrides work immediately. It never advances a pin.
    - It re-copies all of an extension's skills when any of them is missing or differs from the vendored copy. Caches such as `__pycache__` and `.DS_Store` don't count.
    - It deletes the skills of extensions that are no longer recorded, for example after a teammate removed one. It deletes none in these cases:
        - An extension list for that skills folder can't be fully read. Fix that list first.
        - The project reaches its `.agents/skills` folder through a link, or `~/.agents/skills` leads to it. Then it deletes none at either scope, because that folder can hold skills that another list records.
        - `~/.agents/skills` leads somewhere else, for example into a project. Then it deletes none from that folder, in any project.
    - If project A links its `.agents` folder to `~/.agents`, A's skills land in `~/.agents/skills`. Running `agents-cli install` in another project doesn't read A's list, so it can delete them. Run `agents-cli install` in A to bring them back.
    - If a skill folder is yours or another extension's, `agents-cli install` warns, skips that extension's skills, and installs everything else, so CI doesn't break.
- **`extension update [name]`** advances the pin to the latest commit of the **same tracked ref** (for example, a branch), re-prompting for trust unless the extension is verified by the agents-cli team. A pinned tag or SHA stays where it is. Nothing updates in the background.
    - `--to <branch|tag|sha>` moves the pin of the named extension to another ref, for example `extension update langchain --to v1.13.0`.
    - `--to :latest` moves a first-party extension to the latest release that is not newer than your agents-cli. Without a name it moves every first-party extension and skips the others. It records the tag, so run it again to pick up later releases.
    - It replaces the extension's skills with the new version's skills, and removes any it no longer declares. If it can't delete an old skill folder, it warns and leaves the folder with its marker. The update still counts, and a later `extension update` tries the delete again.
    - If the new skills can't be installed (for example, a new skill would replace a folder that is yours or another extension's), `extension update` skips the extension, changes no skill, and keeps the old pin.
        - In a batch `extension update`, the other extension may drop the skill in its own update. If it's at the same scope and comes later in the list, `extension update` tries this extension once more after it, but only if the old copy could be put back.
        - If copying the new skills fails and putting the old skills back also fails, `extension update` warns which skills may be changed and says to run `extension update` again, which installs the new skills.
- **`extension remove <name> [--global]`** drops the entry and deletes the working copy and the skills it owns. It acts on one scope per call, so an extension installed at both scopes needs a second `remove`.
    - Without `--global`, it checks project scope first, then user scope.
    - With `--global`, it acts on user scope only. Inside a project that also has the extension, use it to remove the user-scope copy.
    - If your home folder is a project and the extension is recorded at both scopes, `remove` keeps the shared skills, because the other install still uses them. To delete them, also run the command that `remove` prints for the other scope.

!!! note "Commit both files"
    For a project-scoped extension, commit both the `extensions:` entry in `agents-cli-extensions.yaml` and the vendored copy under `extensions/`. Then it works offline: `agents-cli install` restores the vendored copy, command overrides, and skills on a fresh checkout.

    If you also commit `.agents/skills/`, keep the `.agents-cli-extension` files in it: they tell `agents-cli` which skill folders belong to which extension.

---

## Compatibility

An extension can declare the range of `agents-cli` versions it supports in `requires.agents_cli` (see [Authoring](authoring.md#compatibility)). What you see as a user:

- **`agents-cli extension add` / `update`** refuse an out-of-range install when the extension is `error`-mode (and roll back), so you can't install something that won't run on your CLI. A `warn`-mode extension installs with a warning.
- **After a CLI upgrade** that moves an extension out of its range, nothing breaks silently. The extension warns, and an `error`-mode one fails with the range and the fix instead of running the built-in. Run `agents-cli extension update <name> --to <tag>` (or `--to :latest` for a first-party extension) to move to a version that supports your CLI. The commands no extension can override (`info`, `login`, `setup`, `update`, `install`, and `extension`) keep working either way.
- **`agents-cli extension list` and `agents-cli info`** show each extension's `requires` range and flag any that are `! incompatible` with the running CLI.
