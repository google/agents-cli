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

Invokes the compiled graph in-process (no server, no ports). For remote
queries against a deployed service, bypass the override:

    AGENTS_CLI_DISABLE_OVERRIDES=1 agents-cli run --url https://... --mode a2a "hi"
"""

from __future__ import annotations

import argparse
import sys

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
    print(content_to_text(result["messages"][-1].content))
    return 0


if __name__ == "__main__":
    sys.exit(main())
