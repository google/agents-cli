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

"""Fixtures shared by the tests of `app/app_utils/claude.py` and `turn_options.py`."""

import pytest

from app.app_utils import (
    claude,
    memory_bank,
    project_settings,
    turn_options,
    workspaces,
)


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Gemini platform env, and workspaces and project root inside tmp_path."""
    # Keep the project's .env out of the tests: load_dotenv() finds it from
    # claude.py's location, not cwd.
    monkeypatch.setattr(claude, "load_dotenv", lambda: None)
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "test-project")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Most tests run a bare ClaudeAgentOptions(), which names no model.
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-test")
    for name in (
        "INSTANCE_CONNECTION_NAME",
        "DB_PASS",
        "DB_USER",
        "DB_NAME",
        "BQ_ANALYTICS_DATASET_ID",
        "MEMORY_BANK_ENABLED",
        "MEMORY_BANK_ENGINE_NAME",
        "GOOGLE_CLOUD_AGENT_ENGINE_ID",
        "ANTHROPIC_CUSTOM_HEADERS",
        "DO_NOT_TRACK",
        "AGENTS_CLI_TELEMETRY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    turn_options._cloud_sql_store.cache_clear()
    monkeypatch.setattr(memory_bank, "_engine_name", None)
    monkeypatch.setattr(memory_bank, "_pending", set())
    monkeypatch.setattr(workspaces, "WORKSPACES", tmp_path / "workspaces")
    monkeypatch.setattr(workspaces, "_workspaces", {})
    project = tmp_path / "project"
    agent_config = project / "agent_config"
    agent_config.mkdir(parents=True)
    monkeypatch.setattr(project_settings, "PROJECT_ROOT", project)
    monkeypatch.setattr(project_settings, "AGENT_CONFIG_DIR", agent_config)
    return agent_config
