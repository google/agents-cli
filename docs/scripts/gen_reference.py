# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Generate the CLI and skills reference pages for the docs site.

Run from the repo root:

    uv run python docs/scripts/gen_reference.py

Writes two gitignored Markdown files; `npm run build` and `npm run dev` run
this first:

- docs/src/cli/index.md: every command of the `agents-cli` click group, with
  the ids mkdocs-click 0.9.0 produced, so old anchors keep working.
- docs/src/reference/skills.md: one section per bundled skill, from the front
  matter of src/google/agents/cli/skills/data/*/SKILL.md.
"""

from __future__ import annotations

import inspect
import os
import re
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = ROOT / "src/google/agents/cli/skills/data"
CLI_OUTPUT = ROOT / "docs/src/cli/index.md"
SKILLS_OUTPUT = ROOT / "docs/src/reference/skills.md"

PROG_NAME = "agents-cli"
# The root command is an h2 under the page h1, so no heading level is skipped.
BASE_DEPTH = 1
# Fixed width so usage lines wrap the same way on every machine.
TERMINAL_WIDTH = 80

# Old mkdocs-click ids for commands that were renamed, kept as extra anchors.
ALIAS_IDS = {
    "info": ["cmd-info"],
    "infra cicd": ["setup-cicd"],
    "publish gemini-enterprise": ["gemini"],
}
# Help text of a command that only prints a removal notice.
_REMOVED = re.compile(r"^\s*(removed|deprecated)\b", re.IGNORECASE)
# Help text that already names the default, so the Default column stays empty.
_STATES_DEFAULT = re.compile(r"\bdefault(s to|:| is)", re.IGNORECASE)

# toc_levels: the page TOC nests three levels so subcommands show (convert-docs.mjs).
CLI_HEADER = """---
toc_levels: 3
---

# CLI reference

Complete reference for every command in Agents CLI in Gemini platform (the `agents-cli` binary).

"""

SKILLS_HEADER = """# Skills reference

Skills are context files that `agents-cli setup` installs into coding agents (Antigravity CLI, Claude Code, Codex, Cursor and others). They provide domain-specific guidance for working with generated agent projects.

```bash
agents-cli setup      # Install all skills
agents-cli update     # Reinstall / update skills
```

---

"""


def _fail(message: str) -> None:
    print(f"gen_reference: {message}", file=sys.stderr)
    sys.exit(1)


# --- CLI reference ---------------------------------------------------------

_CODE_SPAN = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_INLINE_SPECIAL = re.compile(r"([\\`*_\[\]<>|&~])")
_BLOCK_START = re.compile(r"^([#>]|[+-](?=\s|$)|\d+(?=[.)](?:\s|$))|[=-]+\s*$)")
# Click's no-rewrap marker; some docstrings carry it as a literal backslash-b.
_NO_REWRAP = {"\b", "\\b"}


def _escape_inline(text: str) -> str:
    """Escape Markdown syntax in plain text, keeping `code spans` as written."""
    out = []
    pos = 0
    for match in _CODE_SPAN.finditer(text):
        out.append(_INLINE_SPECIAL.sub(r"\\\1", text[pos : match.start()]))
        out.append(match.group(0))
        pos = match.end()
    out.append(_INLINE_SPECIAL.sub(r"\\\1", text[pos:]))
    return "".join(out)


def _escape_line(line: str) -> str:
    line = _escape_inline(line.strip())
    # A line that starts with a list, heading or quote marker would start a block.
    return _BLOCK_START.sub(r"\\\1", line, count=1)


def _fenced(paras: list[str]) -> list[str]:
    body = textwrap.dedent("\n\n".join(paras))
    fence = "````" if "```" in body else "```"
    return [f"{fence}text", body, fence, ""]


def _description(help_text: str) -> list[str]:
    """Render click help text as Markdown.

    Paragraphs marked `\\b` or laid out with indentation (command lists,
    examples) become one text block per run; the rest become escaped prose.
    """
    help_text = inspect.cleandoc(help_text.split("\f", 1)[0])
    lines: list[str] = []
    pre: list[str] = []
    for para in re.split(r"\n[ \t]*\n", help_text):
        para_lines = para.splitlines()
        marked = bool(para_lines) and para_lines[0].strip() in _NO_REWRAP
        if marked:
            para_lines = para_lines[1:]
        if not any(line.strip() for line in para_lines):
            continue
        if marked or any(line[:1].isspace() for line in para_lines):
            pre.append("\n".join(para_lines))
            continue
        if pre:
            lines += _fenced(pre)
            pre = []
        lines += [_escape_line(line) for line in para_lines]
        lines.append("")
    if pre:
        lines += _fenced(pre)
    return lines


def _usage(ctx) -> list[str]:
    formatter = ctx.make_formatter()
    pieces = ctx.command.collect_usage_pieces(ctx)
    formatter.write_usage(ctx.command_path, " ".join(pieces), prefix="")
    # A titled fence keeps the label quieter than the command heading above it.
    return ['```text title="Usage"', formatter.getvalue().strip(), "```", ""]


def _cell(text: str) -> str:
    return _escape_inline(" ".join(text.split()))


def _option_type(option) -> str:
    import click

    typ = option.type
    if isinstance(typ, click.Choice):
        return f"{typ.name} ({', '.join(f'`{c}`' for c in typ.choices)})"
    if isinstance(typ, (click.IntRange, click.FloatRange)):
        if typ.min is not None and typ.max is not None:
            return f"{typ.name} (between `{typ.min}` and `{typ.max}`)"
        if typ.min is not None:
            return f"{typ.name} (`{typ.min}` and above)"
        return f"{typ.name} (`{typ.max}` and below)"
    return typ.name


def _option_default(option) -> str:
    if option.required:
        return "required"
    if option.help and _STATES_DEFAULT.search(option.help):
        return ""
    # A string show_default describes a default computed on the user's machine.
    if isinstance(option.show_default, str):
        return _cell(option.show_default)
    default = option.default
    if option.is_flag and default is False:
        return ""
    # Callable defaults are evaluated at run time, often from the environment.
    if callable(default):
        return "dynamic"
    if default is None or default == () or default == []:
        return ""
    values = default if isinstance(default, (list, tuple)) else [default]
    # Backslash escapes are literal inside code spans; only the table pipe needs one.
    return ", ".join("`" + str(v).replace("|", "\\|") + "`" for v in values)


def _options(ctx) -> list[str]:
    import click

    help_option = ctx.command.get_help_option(ctx)
    options = [
        p
        for p in ctx.command.get_params(ctx)
        if isinstance(p, click.Option) and not p.hidden and p is not help_option
    ]
    if not options:
        return []
    # The first column header names the table, so no separate "Options" label.
    lines = [
        "| Option | Type | Description | Default |",
        "| ------ | ---- | ----------- | ------- |",
    ]
    for option in options:
        names = ", ".join(f"`{o}`" for o in option.opts)
        if option.secondary_opts:
            names += " / " + ", ".join(f"`{o}`" for o in option.secondary_opts)
        desc = _cell(option.help) if option.help else ""
        lines.append(
            f"| {names} | {_option_type(option)} | {desc} | {_option_default(option)} |"
        )
    lines.append("")
    return lines


def _subcommands(ctx) -> list[tuple[str, object]]:
    """Visible subcommands under the name a user types, sorted by that name."""
    import click

    group = ctx.command
    if not isinstance(group, click.Group):
        return []
    found = []
    for name in group.list_commands(ctx):
        sub = group.get_command(ctx, name)
        if sub is None or sub.hidden or sub.deprecated:
            continue
        if _REMOVED.match(sub.help or sub.short_help or ""):
            continue
        found.append((name, sub))
    return sorted(found, key=lambda item: item[0])


def _context(command, name: str, parent):
    return command.context_class(
        command,
        info_name=name,
        parent=parent,
        terminal_width=TERMINAL_WIDTH if parent is None else None,
        **command.context_settings,
    )


def _collect(command, name: str, parent, depth: int, out: list) -> None:
    ctx = _context(command, name, parent)
    out.append((ctx, depth))
    for sub_name, sub in _subcommands(ctx):
        _collect(sub, sub_name, ctx, depth + 1, out)


def _cli_reference() -> str:
    # Keep the page independent of extensions in the caller's project or
    # ~/.config and of experiment overrides in their shell.
    os.environ["AGENTS_CLI_DISABLE_OVERRIDES"] = "1"
    os.environ.pop("AGENTS_CLI_EXPERIMENTS", None)
    try:
        from google.agents.cli.main import main
    except Exception as exc:
        _fail(
            f"cannot import google.agents.cli.main ({exc!r}); run from the repo root with `uv run`"
        )

    contexts: list = []
    _collect(main, PROG_NAME, None, BASE_DEPTH, contexts)

    # The same command object registered at two paths is an alias.
    paths: dict[int, list[str]] = {}
    for ctx, _ in contexts:
        paths.setdefault(id(ctx.command), []).append(ctx.command_path)

    lines = [CLI_HEADER.rstrip("\n"), ""]
    # Python-Markdown's unique(): repeated names get _1, _2, as on the old site.
    ids = {"cli-reference"}
    for ctx, depth in contexts:
        if not re.fullmatch(r"[a-z0-9_-]+", ctx.info_name):
            _fail(f"unexpected command name {ctx.info_name!r}")
        anchor = ctx.info_name
        while anchor in ids:
            m = re.fullmatch(r"(.*)_(\d+)", anchor)
            anchor = f"{m.group(1)}_{int(m.group(2)) + 1}" if m else f"{anchor}_1"
        ids.add(anchor)
        sub_path = ctx.command_path.removeprefix(PROG_NAME).strip()
        for alias in ALIAS_IDS.get(sub_path, []):
            lines += [f'<div id="{alias}"></div>', ""]
        label = sub_path or PROG_NAME
        lines += [
            f'{"#" * (depth + 1)} `{ctx.command_path}` {{#{anchor} data-toc-label="{label}"}}',
            "",
        ]
        help_text = ctx.command.help or ctx.command.short_help
        if help_text:
            lines += _description(help_text)
        others = [p for p in paths[id(ctx.command)] if p != ctx.command_path]
        if others:
            lines += [
                "Also available as " + ", ".join(f"`{p}`" for p in others) + ".",
                "",
            ]
        lines += _usage(ctx)
        lines += _options(ctx)
    return "\n".join(lines).rstrip("\n") + "\n"


# --- Skills reference (ported from docs/hooks/skills_reference.py) ---------


def _parse_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.+?)\n---", text, re.DOTALL)
    if not match:
        return {}
    fm = {}
    current_key = None
    for line in match.group(1).splitlines():
        kv = re.match(r"^(\w[\w-]*):\s*(.*)", line)
        if kv:
            current_key = kv.group(1)
            fm[current_key] = kv.group(2).strip()
        elif current_key and line.startswith("  "):
            fm[current_key] = (fm[current_key] + " " + line.strip()).strip()
    return fm


def _clean_description(desc: str) -> str:
    """Extract the user-facing description, drop routing instructions."""
    desc = re.split(r"\s*Do NOT use for", desc)[0].strip()
    # Descriptions open with routing text ("This skill should be used when...");
    # the user-facing part starts at one of these phrases.
    match = re.search(r"(?:Covers |Always active|It provides |Provides )", desc)
    if match:
        desc = desc[match.start() :]
    # Folded YAML strings (`>`) leave a stray marker at the start.
    desc = desc.strip().strip(">").strip()
    if desc:
        desc = desc[0].upper() + desc[1:]
    return desc


def _skills_reference() -> str:
    if not SKILLS_DIR.is_dir():
        _fail(f"skills directory not found: {SKILLS_DIR}")

    skills = []
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        fm = _parse_frontmatter(skill_md)
        if not fm.get("name"):
            _fail(f"{skill_md.relative_to(ROOT)}: missing front matter or `name`")
        skills.append(fm)

    if not skills:
        _fail(f"no SKILL.md files found under {SKILLS_DIR.relative_to(ROOT)}")

    lines = [SKILLS_HEADER]
    for skill in skills:
        desc = _clean_description(skill.get("description", ""))
        version = skill.get("version", "")
        lines.append(f"## `{skill['name']}`\n\n")
        if desc:
            lines.append(f"{desc}\n\n")
        if version:
            lines.append(f"**Version:** {version}\n\n")
        lines.append("---\n\n")
    return "".join(lines)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    print(f"wrote {path.relative_to(ROOT)}")


def main() -> None:
    _write(CLI_OUTPUT, _cli_reference())
    _write(SKILLS_OUTPUT, _skills_reference())


if __name__ == "__main__":
    main()
