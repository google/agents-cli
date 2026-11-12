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

"""The tools `app/feature_tools.py` adds to root_agent for the features that are on."""

import importlib

import pytest

from app import feature_tools


@pytest.fixture
def reload_feature_tools(monkeypatch):
    """Re-evaluates feature_tools, which reads the env at import, under the test's env.

    Afterwards it is evaluated again under the original env, so no other test
    sees the test's tools.
    """
    yield lambda: importlib.reload(feature_tools)
    monkeypatch.undo()
    importlib.reload(feature_tools)


@pytest.mark.parametrize(
    "enabled, servers, tools",
    [
        ("true", ["memory"], ["mcp__memory__search_memory"]),
        (None, [], []),
    ],
    ids=["memory_on", "memory_off"],
)
def test_memory_bank_adds_its_search_tool_only_while_on(
    monkeypatch, reload_feature_tools, enabled, servers, tools
):
    if enabled is None:
        monkeypatch.delenv("MEMORY_BANK_ENABLED", raising=False)
    else:
        monkeypatch.setenv("MEMORY_BANK_ENABLED", enabled)

    module = reload_feature_tools()

    assert list(module.FEATURE_MCP_SERVERS) == servers
    assert module.FEATURE_TOOLS == tools
