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

"""One-shot agent-graph runner — drop-in for `agents-cli run`.

Invokes the compiled graph in-process (no server, no ports) and prints the turn
the way the built-in prints an ADK agent's: the prompt, then each tool call and
tool response, then the reply. For remote queries against a deployed service,
bypass the override:

    AGENTS_CLI_DISABLE_OVERRIDES=1 agents-cli run --url https://... --mode a2a "hi"
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from _content import content_to_text


def main() -> int:
    parser = argparse.ArgumentParser(prog="agents-cli run", description=__doc__)
    parser.add_argument("message", help="The prompt to send to the agent.")
    # Declared so the two remote flags get the bypass hint rather than
    # argparse's bare "unrecognized arguments"; neither means anything here.
    parser.add_argument("--url", default=None, help="Not supported by this override.")
    parser.add_argument("--mode", default=None, help="Not supported by this override.")
    args = parser.parse_args()

    if args.url or args.mode:
        print(
            "langchain: --url/--mode are not supported; this project's `run` invokes "
            "the graph in process. To query a deployed agent, bypass the override: "
            'AGENTS_CLI_DISABLE_OVERRIDES=1 agents-cli run --url ... --mode a2a "hi"',
            file=sys.stderr,
        )
        return 2

    from app.agent import root_agent

    agent = root_agent
    if getattr(agent, "store", None) is None and hasattr(agent, "copy"):
        from langgraph.store.memory import InMemoryStore

        agent = agent.copy(update={"store": InMemoryStore()})

    result = agent.invoke({"messages": [{"role": "user", "content": args.message}]})
    name = getattr(root_agent, "name", None) or "agent"
    print(_render_turn(args.message, result["messages"], name))
    return 0


def _render_turn(prompt: str, messages: Sequence[object], agent_name: str) -> str:
    """The turn as `agents-cli run` prints an ADK agent's.

    Tool lines use the built-in's `[tool_call: name({...})]` and
    `[tool_response: name -> {...}]` shapes, so scripts that read `run` output
    handle both kinds of project the same way.
    """
    lines = [f"[user]: {prompt}"]
    body: list[str] = []
    # A ToolMessage can come back without a name; its call id still says which tool.
    names_by_call_id: dict[str, str] = {}
    for message in messages:
        kind = _kind(message)
        if kind in ("human", "system"):
            continue
        if kind == "tool":
            call_id = _field(message, "tool_call_id") or ""
            tool = _field(message, "name") or names_by_call_id.get(call_id, "tool")
            response = _tool_response(_field(message, "content"))
            body.append(f"[tool_response: {tool} -> {json.dumps(response)}]")
            continue
        text = content_to_text(_field(message, "content")).strip()
        if text:
            body.append(text)
        for call in _field(message, "tool_calls") or []:
            tool = call.get("name", "")
            if call.get("id"):
                names_by_call_id[call["id"]] = tool
            body.append(f"[tool_call: {tool}({json.dumps(call.get('args') or {})})]")
    if not body:
        return "\n".join([*lines, "(no response content)"])
    # Like the built-in: a reply that starts with text shares the tag's line.
    tag = f"[{agent_name}]: "
    if body[0].startswith("[tool_"):
        lines.append(tag)
    else:
        body[0] = tag + body[0]
    return "\n".join([*lines, *body])


def _kind(message: object) -> str | None:
    """A message's `type`, or a dict message's role in LangChain's terms.

    LangGraph state can hold OpenAI-style dicts, which say `user` and
    `assistant` where LangChain messages say `human` and `ai`.
    """
    role = _field(message, "role")
    roles = {"user": "human", "assistant": "ai"}
    return _field(message, "type") or roles.get(role, role)


def _field(message: object, key: str) -> Any:
    """A message attribute; LangGraph state can also hold plain dicts.

    `Any`: the override reads messages without importing langchain_core's types.
    """
    if isinstance(message, dict):
        return message.get(key)
    return getattr(message, key, None)


def _tool_response(content: object) -> object:
    """A tool's output as the built-in shows it: a JSON object.

    Tools return strings to LangChain; one holding JSON is shown parsed. ADK
    wraps a non-dict return value in {"result": ...}, and so does this.
    """
    text = content_to_text(content)
    try:
        value = json.loads(text)
    except ValueError:
        value = text
    return value if isinstance(value, dict) else {"result": value}


if __name__ == "__main__":
    sys.exit(main())
