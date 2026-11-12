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

"""Generate eval traces for `agents-cli eval generate`.

Output is the EvaluationDataset shape `agents-cli eval grade` reads, with tool
calls as `function_call` / `function_response` parts.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

# `app` may not be installed as a package, so import it from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _resolve_output_path(output: str) -> Path:
    p = Path(output)
    # A trailing separator means a directory that may not exist yet.
    if p.is_dir() or output.endswith(("/", os.sep)):
        p.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return p / f"eval_dataset_{stamp}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _user_prompt(turn: dict) -> str | None:
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


def _agent_events(
    agent_name: str, reply: str, tool_calls: list[dict[str, Any]]
) -> list[dict]:
    """The agent's side of a turn: each tool call and result, then the reply.

    A subagent's calls follow the call that delegated to them.
    """
    events = _call_events(agent_name, None, tool_calls)
    events.append(
        {"author": agent_name, "content": {"role": "model", "parts": [{"text": reply}]}}
    )
    return events


def _call_events(
    agent_name: str, parent: str | None, tool_calls: list[dict[str, Any]]
) -> list[dict]:
    events: list[dict] = []
    for call in tool_calls:
        if call.get("parent") != parent:
            continue
        author = call.get("agent", agent_name)
        events.append(
            {
                "author": author,
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "function_call": {
                                "id": call["id"],
                                "name": call["name"],
                                "args": call["args"],
                            }
                        }
                    ],
                },
            }
        )
        events.append(
            {
                "author": author,
                "content": {
                    "role": "user",
                    "parts": [
                        {
                            "function_response": {
                                "id": call["id"],
                                "name": call["name"],
                                "response": {"result": call.get("result", "")},
                            }
                        }
                    ],
                },
            }
        )
        events.extend(_call_events(agent_name, call["id"], tool_calls))
    return events


async def _tool_declarations(options: Any) -> list[dict]:
    """Function declarations for the agent's tools.

    External MCP servers are skipped; built-in tools get a name-only declaration.
    """
    import anyio
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    declarations: list[dict] = []
    for server_name, config in (options.mcp_servers or {}).items():
        if not isinstance(config, dict) or config.get("type") != "sdk":
            continue
        server = config["instance"]
        async with create_client_server_memory_streams() as (client, served):
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    server.run, *served, server.create_initialization_options()
                )
                async with ClientSession(*client) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                tg.cancel_scope.cancel()
        declarations.extend(
            {
                "name": f"mcp__{server_name}__{tool.name}",
                "description": tool.description or "",
                "parameters": tool.model_dump(by_alias=True)["inputSchema"],
            }
            for tool in listed.tools
        )
    if isinstance(options.tools, list):
        declarations.extend({"name": name} for name in options.tools)
    return declarations


def agents_metadata(options: Any, agent_name: str) -> dict:
    """`agent_data.agents` for the traces: instructions and tools per agent.

    Tool metrics grade calls against these; on failure this warns and returns {}.
    """
    import anyio

    try:
        declarations = anyio.run(_tool_declarations, options)
    except Exception as e:
        logging.warning(
            "Could not list the agent's tools (%s: %s); traces will omit "
            "agent_data.agents -- tool metrics will degrade.",
            type(e).__name__,
            e,
        )
        return {}
    config: dict[str, Any] = {"agent_id": agent_name, "agent_type": "ClaudeAgent"}
    if isinstance(options.system_prompt, str):
        config["instruction"] = options.system_prompt
    if declarations:
        config["tools"] = [{"function_declarations": declarations}]
    agents = {agent_name: config}
    subagents = options.agents or {}
    if subagents:
        config["sub_agents"] = list(subagents)
    by_name = {d["name"]: d for d in declarations}
    for name, definition in subagents.items():
        sub: dict[str, Any] = {
            "agent_id": name,
            "agent_type": "ClaudeAgent",
            "description": definition.description,
            "instruction": definition.prompt,
        }
        # A subagent without its own tool list gets all of the agent's tools.
        tools = (
            declarations
            if definition.tools is None
            else [by_name.get(tool, {"name": tool}) for tool in definition.tools]
        )
        if tools:
            sub["tools"] = [{"function_declarations": tools}]
        agents[name] = sub
    return agents


def _fill_case(
    case: dict,
    agent_name: str,
    infer: Callable[[str, str], tuple[str, list[dict[str, Any]]]],
    agents: dict | None,
) -> None:
    """Run one case through the agent and write its replies into it."""
    agent_data = case.setdefault("agent_data", {})
    if agents:
        agent_data.setdefault("agents", agents)
    turns = agent_data.setdefault("turns", [])
    reply: str | None = None
    conversation_id = uuid.uuid4().hex

    for turn in turns:
        prompt = _user_prompt(turn)
        if prompt is None:
            continue
        reply, tool_calls = infer(prompt, conversation_id)
        turn.setdefault("events", []).extend(
            _agent_events(agent_name, reply, tool_calls)
        )

    # A prompt-shaped case has no turns: the reply becomes turn 0.
    if not turns:
        prompt = _prompt_text(case.get("prompt"))
        if prompt is not None:
            reply, tool_calls = infer(prompt, conversation_id)
            turns.append({"events": _agent_events(agent_name, reply, tool_calls)})

    for index, turn in enumerate(turns):
        turn.setdefault("turn_index", index)
        turn.setdefault("turn_id", f"turn_{index}")

    if reply is not None:
        case.setdefault("responses", []).append(
            {"response": {"role": "model", "parts": [{"text": reply}]}}
        )


def generate_traces(
    dataset: dict,
    agent_name: str,
    infer: Callable[[str, str], tuple[str, list[dict[str, Any]]]],
    agents: dict | None = None,
) -> tuple[dict, list[tuple[int, str]]]:
    """Fill each eval case with the agent's replies.

    Returns the dataset with the cases that succeeded, and `(index, error)` for
    each case that failed. `eval grade` rejects a trace whose turns lack
    `turn_index`/`turn_id` and scores nothing without `responses`.
    """
    succeeded: list[dict] = []
    failures: list[tuple[int, str]] = []
    for index, case in enumerate(dataset.get("eval_cases") or []):
        try:
            _fill_case(case, agent_name, infer, agents)
        except Exception as e:
            logging.warning("Eval case %d failed: %s", index, e)
            failures.append((index, f"{type(e).__name__}: {e}"))
            continue
        succeeded.append(case)
    return {**dataset, "eval_cases": succeeded}, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="tests/eval/datasets/basic-dataset.json")
    parser.add_argument("--output", "-o", default="artifacts/traces/")
    # `eval run` forwards these flags, so accept them.
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
        # The built-in queries ADK's /run_sse, which this A2A-only server lacks.
        print(
            "claude: --url is not supported; this project's `eval generate` runs "
            "the agent in process. Run it without --url to evaluate the local code.",
            file=sys.stderr,
        )
        return 2

    import anyio
    from dotenv import load_dotenv

    # .env first: app.agent's tool lists depend on flags such as MEMORY_BANK_ENABLED.
    load_dotenv()
    from app.agent import root_agent
    from app.app_utils.claude import run_turn
    from app.app_utils.settings import AGENT_NAME

    def infer(prompt: str, conversation_id: str) -> tuple[str, list[dict[str, Any]]]:
        reply = anyio.run(run_turn, prompt, root_agent, conversation_id)
        return reply.text, reply.tool_calls + reply.subagent_calls

    raw = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    out, failures = generate_traces(
        raw, AGENT_NAME, infer, agents_metadata(root_agent, AGENT_NAME)
    )
    n_cases = len(raw.get("eval_cases") or [])
    n_succeeded = n_cases - len(failures)
    if failures:
        print(
            f"\nInference summary: {n_succeeded}/{n_cases} succeeded, "
            f"{len(failures)} failed.",
            file=sys.stderr,
        )
        for index, error in failures:
            print(f"  case {index}: {error}", file=sys.stderr)
    if n_cases and not n_succeeded:
        print(
            f"No artifact written: 0 of {n_cases} cases produced output.",
            file=sys.stderr,
        )
        return 1
    out_path = _resolve_output_path(args.output)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"[claude-generate] wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
