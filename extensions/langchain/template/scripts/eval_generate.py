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

"""LangChain trace generator — drop-in for `agents-cli eval generate`.

Produces the same EvaluationDataset shape the built-in `agents-cli eval generate`
emits, so `agents-cli eval grade` consumes the output unchanged.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

from _content import content_to_text


def _resolve_output_path(output: str) -> Path:
    p = Path(output)
    # A trailing separator signals a directory that does not exist yet, so
    # is_dir() alone misses it. Same rule as the built-in's `resolve_output_path`
    # (google/agents/cli/eval/_paths.py), including os.sep for `traces\` on Windows.
    if p.is_dir() or output.endswith(("/", os.sep)):
        p.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return p / f"eval_dataset_{stamp}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _agent_name() -> str:
    default_name = "agent"
    manifest = Path("agents-cli-manifest.yaml")
    if not manifest.exists():
        logging.warning(
            "%s not found; using %r as the agent name.", manifest, default_name
        )
        return default_name
    try:
        import yaml

        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        return data.get("name") or default_name
    except Exception as e:
        logging.warning(
            "Could not read the agent name from %s (%s); using %r.",
            manifest,
            e,
            default_name,
        )
        return default_name


def _user_prompt(turn: dict) -> str | None:
    # Walk newest-first so the most recent user message in the turn wins.
    for event in reversed(turn.get("events") or []):
        if event.get("author") == "user":
            for part in event.get("content", {}).get("parts") or []:
                if "text" in part:
                    return part["text"]
    return None


def _prompt_text(prompt: object) -> str | None:
    """Text of a case's top-level ``prompt`` (a Content: role + parts)."""
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, dict):
        for part in prompt.get("parts") or []:
            if isinstance(part, dict) and part.get("text"):
                return part["text"]
    return None


def generate_traces(
    dataset: dict, agent_name: str, infer: Callable[[str], str]
) -> dict:
    """Fill each eval case with the agent's replies. Returns the same shape.

    Matches what `eval grade` (the Eval Service in Gemini platform) requires of a trace:
    every turn carries `turn_index`/`turn_id`, and the case carries the final
    reply under `responses`. Without those the service rejects the file with
    `turns[0].turn_index: Required field is not set` or scores nothing because
    the response candidate is empty.

    Handles both input shapes: a case with a top-level `prompt`, and a case
    whose `agent_data.turns` end in a user event.
    """
    for case in dataset.get("eval_cases") or []:
        agent_data = case.setdefault("agent_data", {})
        turns = agent_data.setdefault("turns", [])
        reply: str | None = None

        for turn in turns:
            prompt = _user_prompt(turn)
            if prompt is None:
                continue
            reply = infer(prompt)
            turn.setdefault("events", []).append(
                {
                    "author": agent_name,
                    "content": {"role": "model", "parts": [{"text": reply}]},
                }
            )

        # A prompt-shaped case has no turns to extend: the prompt stays where it
        # is and the reply becomes turn 0, which is what the built-in generate
        # produces for the same input.
        if not turns:
            prompt = _prompt_text(case.get("prompt"))
            if prompt is not None:
                reply = infer(prompt)
                turns.append(
                    {
                        "events": [
                            {
                                "author": agent_name,
                                "content": {
                                    "role": "model",
                                    "parts": [{"text": reply}],
                                },
                            }
                        ]
                    }
                )

        for index, turn in enumerate(turns):
            turn.setdefault("turn_index", index)
            turn.setdefault("turn_id", f"turn_{index}")

        if reply is not None:
            case.setdefault("responses", []).append(
                {"response": {"role": "model", "parts": [{"text": reply}]}}
            )
    return dataset


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # Same defaults as the built-in, so bare `eval generate` works here too.
    parser.add_argument("--dataset", default="tests/eval/datasets/basic-dataset.json")
    parser.add_argument("--output", "-o", default="artifacts/traces/")
    # An override replaces the built-in, so it has to accept the built-in's argv:
    # `eval run` forwards these, and argparse would otherwise exit 2 on them.
    parser.add_argument("--url", default=None)
    parser.add_argument("--app-name", default=None, help="Ignored; no server here.")
    parser.add_argument(
        "--concurrency", default=None, help="Ignored; cases run in order."
    )
    parser.add_argument(
        "--header", action="append", default=[], help="Ignored; no server."
    )
    args = parser.parse_args()

    if args.url:
        print(
            "langchain: --url is not supported; this project's `eval generate` invokes "
            "the graph in process. To query a deployed agent, bypass the override: "
            "AGENTS_CLI_DISABLE_OVERRIDES=1 agents-cli eval generate --url ...",
            file=sys.stderr,
        )
        return 2

    from app.agent import root_agent

    def infer(prompt: str) -> str:
        agent = root_agent
        if getattr(agent, "store", None) is None and hasattr(agent, "copy"):
            from langgraph.store.memory import InMemoryStore

            agent = agent.copy(update={"store": InMemoryStore()})
        result = agent.invoke({"messages": [{"role": "user", "content": prompt}]})
        last = result["messages"][-1]
        # getattr handles LangChain BaseMessage objects; fall back to dict access
        # for plain dicts. Avoid calling .get() on a BaseMessage (no such method).
        content = getattr(last, "content", None)
        if content is None and isinstance(last, dict):
            content = last.get("content", "")
        return content_to_text(content)

    raw = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    out = generate_traces(raw, _agent_name(), infer)
    out_path = _resolve_output_path(args.output)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"[langchain-generate] wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
