# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Claude Agent SDK agent: a weather assistant with one custom tool.

Uses Claude on Gemini platform with Application Default Credentials; the model
must be enabled in Model Garden.
"""

from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, tool

from app.feature_tools import FEATURE_MCP_SERVERS, FEATURE_TOOLS


@tool("get_weather", "Get the current weather for a city.", {"city": str})
async def get_weather(args: dict[str, Any]) -> dict[str, Any]:
    city = args["city"]
    if "sf" in city.lower() or "san francisco" in city.lower():
        forecast = "It's 60 degrees and foggy."
    else:
        forecast = "It's 90 degrees and sunny."
    return {"content": [{"type": "text", "text": forecast}]}


weather_server = create_sdk_mcp_server(name="weather", tools=[get_weather])

root_agent = ClaudeAgentOptions(
    model="claude-haiku-4-5",
    # The CLI prepends "You are Claude Code...": give the agent its own name.
    system_prompt="You are Skye, a helpful assistant. Use get_weather for weather questions.",
    mcp_servers={"weather": weather_server, **FEATURE_MCP_SERVERS},
    # No built-in Claude Code tools (Bash, Read, Write, ...); allow only these.
    tools=[],
    allowed_tools=["mcp__weather__get_weather", *FEATURE_TOOLS],
    max_turns=10,
)
