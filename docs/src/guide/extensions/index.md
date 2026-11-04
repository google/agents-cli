# Extensions

An extension changes what an `agents-cli` command does, or adds a new command, without forking the tool. Use one to run a different agent framework, add your org's checks to any command, or give your team its own commands.

## In short

- **One YAML file.** An `agents-cli-extension.yaml` maps a command name to the program to run instead, and can ship skills for your coding agent.
- **Your arguments pass through.** When you run the command, `agents-cli` runs that program with your arguments appended, so you type the same command and flags as before.
- **Commit it or add it.** Commit the file at your project root and it loads automatically for everyone, CI included. To use someone else's, run `agents-cli extension add <org>/<repo>`.
- **Pinned to a commit.** An added extension records its commit SHA, like a lockfile, so it never updates in the background. Extensions from outside `google/agents-cli`, `google/agents-cli-extensions` and `google/adk-recipes` ask before install, because their commands run code. See [Trust](using.md#trust).
- **The repair commands stay built-in.** No extension can override `info`, `login`, `setup`, `update`, `install` or `extension`, so you can always inspect `agents-cli` or remove a broken extension.

!!! note "Experimental"
    The extension schema is `v1alpha1` and can still change in a breaking way. See [Compatibility](authoring.md#compatibility).

## See an example

With this file at the project root, every deploy runs a check first:

```yaml
schema: agents-cli-extension/v1alpha1
name: acme-policy
commands:
  override:
    deploy:
      run: ["bash", "scripts/check_then_deploy.sh"]
```

Running `agents-cli deploy --project my-proj` now runs `bash scripts/check_then_deploy.sh --project my-proj`. The script runs its checks, then calls `agents-cli deploy "$@"`, which runs the built-in deploy instead of looping back into the override. In `agents-cli --help`, the command is tagged `[↑ acme-policy]`. Any other command works the same way, and a dotted name such as `eval.run` targets a subcommand.

## Find the right page

| You want to | Do this | Read |
|---|---|---|
| Build on LangChain or the Claude Agent SDK | Scaffold from the framework's template. It carries its own extension, so there's nothing to install. | [First-party frameworks](first-party.md) |
| Add a step to a command for your team | Commit an `agents-cli-extension.yaml` at the project root. | [Authoring extensions](authoring.md) |
| Use an extension someone published | Run `agents-cli extension add <org>/<repo>`. | [Using extensions](using.md#adopt-an-extension) |
| Share your extension across repos | Move the file and its scripts to their own git repo and tag a release. | [Authoring extensions](authoring.md) |
| Ship your own agent framework | Build a template on the `empty_py` base with an extension at its root. | [Authoring extensions](authoring.md#rules-that-matter) |
