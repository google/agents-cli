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


"""Run `root_agent` in process for `agents-cli run`.

With `--url` or `--mode`, the built-in `agents-cli run` queries the served agent.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

# `app` may not be installed as a package, so import it from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_REMOTE_FLAGS = ("--url", "--mode")


def main() -> int:
    argv = sys.argv[1:]
    if any(arg.split("=", 1)[0] in _REMOTE_FLAGS for arg in argv):
        env = {**os.environ, "AGENTS_CLI_DISABLE_OVERRIDES": "1"}
        return subprocess.run(["agents-cli", "run", *argv], env=env).returncode

    parser = argparse.ArgumentParser(prog="agents-cli run", description=__doc__)
    parser.add_argument("message", help="The prompt to send to the agent.")
    parser.add_argument(
        "--session-id",
        default=None,
        help="Continue this conversation (printed after each run).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print each tool call (name, arguments and result) as JSON.",
    )
    args = parser.parse_args(argv)

    import anyio
    from claude_agent_sdk import ClaudeSDKError
    from dotenv import load_dotenv

    # .env first: app.agent's tool lists depend on flags such as MEMORY_BANK_ENABLED.
    load_dotenv()
    from app.agent import root_agent
    from app.app_utils.claude import AgentError, run_turn

    conversation_id = args.session_id or uuid.uuid4().hex
    try:
        reply = anyio.run(run_turn, args.message, root_agent, conversation_id)
    except (AgentError, ClaudeSDKError) as e:
        print(e, file=sys.stderr)
        return 1
    if args.verbose:
        for call in reply.tool_calls:
            print(json.dumps(call, indent=2, default=str), file=sys.stderr)
    print(reply.text)
    print(
        f"\nSession: {conversation_id}\n"
        f'  Resume with: agents-cli run "<message>" --session-id {conversation_id}'
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
