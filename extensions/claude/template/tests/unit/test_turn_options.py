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

"""How `app/app_utils/turn_options.py` builds the options for one served turn."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import anyio
import pytest
from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, InMemorySessionStore

from app.app_utils import claude, settings, turn_options
from tests.unit.helpers import workspaces_root, write_local_transcript

pytestmark = pytest.mark.usefixtures("isolated")


def _options_for(*args, **kwargs) -> ClaudeAgentOptions:
    return anyio.run(turn_options.turn_options, *args, **kwargs)


class TestTurnOptions:
    def test_conversation_starts_then_resumes_its_session(self):
        first = _options_for(ClaudeAgentOptions(), "ctx-1")
        write_local_transcript("ctx-1")
        later = _options_for(ClaudeAgentOptions(), "ctx-1")

        session = turn_options.session_id_for("ctx-1")
        assert (first.session_id, first.resume) == (session, None)
        assert (later.session_id, later.resume) == (None, session)
        # Same conversation, same workspace: that is where the CLI keys it.
        assert first.cwd == later.cwd == str(workspaces_root() / session)
        assert first.include_partial_messages is True

    def test_a_uuid_conversation_id_is_the_session_id(self):
        # A2A clients send UUID context ids: the caller, the CLI and the spans
        # then all see the same id.
        context_id = "3e3a0114-1fa0-463c-a119-b599a1beb275"
        assert _options_for(ClaudeAgentOptions(), context_id).session_id == context_id
        # Any other spelling, or a non-UUID id, gets a stable derived UUID.
        for other in (context_id.upper(), "ctx-1"):
            session = _options_for(ClaudeAgentOptions(), other).session_id
            assert session != other
            assert str(uuid.UUID(session)) == session
            assert _options_for(ClaudeAgentOptions(), other).session_id == session

    def test_prompts_are_delivered_verbatim(self):
        # Remote callers write the prompt: no "@/path" file reads, no slash commands.
        assert _options_for(ClaudeAgentOptions(), "c").verbatim_prompts is True

    def test_agent_running_in_the_project_root_needs_no_copy(self, isolated):
        (isolated / "CLAUDE.md").write_text("Be terse.")
        options = _options_for(ClaudeAgentOptions(cwd=str(isolated)), "c")
        assert options.cwd == str(isolated)
        assert options.setting_sources == ["project"]

    def test_an_agents_own_directory_keeps_its_claude_settings(
        self, isolated, tmp_path
    ):
        (isolated / ".claude").mkdir()
        (isolated / ".claude" / "settings.json").write_text("{}")
        repo = tmp_path / "repo"
        (repo / ".claude").mkdir(parents=True)
        (repo / ".claude" / "settings.json").write_text('{"model": "repo"}')
        (repo / "CLAUDE.md").write_text("The repo's notes.")
        (repo / ".mcp.json").write_text('{"mcpServers": {}}')

        options = _options_for(ClaudeAgentOptions(cwd=str(repo)), "c")

        # Nothing is copied over or deleted in a directory the agent points at.
        assert (repo / ".claude" / "settings.json").read_text() == '{"model": "repo"}'
        assert (repo / "CLAUDE.md").read_text() == "The repo's notes."
        assert (repo / ".mcp.json").exists()
        assert options.setting_sources == []

    def test_conversations_get_their_own_workspace(self):
        a = _options_for(ClaudeAgentOptions(), "ctx-a")
        b = _options_for(ClaudeAgentOptions(), "ctx-b")
        assert a.cwd != b.cwd

    def test_server_defaults_apply_only_where_unset(self, tmp_path):
        defaults = _options_for(ClaudeAgentOptions(), "c")
        assert defaults.permission_mode == "dontAsk"
        assert defaults.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
        assert defaults.env["ENABLE_TOOL_SEARCH"] == "false"
        assert defaults.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
        # Without project settings to copy, the CLI loads none.
        assert defaults.setting_sources == []

        own = _options_for(
            ClaudeAgentOptions(
                permission_mode="default",
                cwd=str(tmp_path / "own"),
                env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"},
                setting_sources=["user"],
            ),
            "c",
        )
        assert own.permission_mode == "default"
        assert own.cwd == str(tmp_path / "own")
        assert own.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "0"
        assert own.setting_sources == ["user"]

    def test_own_permission_callback_is_not_shadowed(self):
        async def allow(tool_name, tool_input, context):
            return None

        options = _options_for(ClaudeAgentOptions(can_use_tool=allow), "c")
        assert options.permission_mode is None

    @pytest.mark.parametrize(
        ("options", "bare"),
        [
            (ClaudeAgentOptions(tools=[]), True),
            (ClaudeAgentOptions(), False),
            (ClaudeAgentOptions(tools=["WebSearch"]), False),
            (ClaudeAgentOptions(tools=[], setting_sources=["user"]), False),
            (ClaudeAgentOptions(tools=[], skills="all"), False),
            (
                ClaudeAgentOptions(tools=[], plugins=[{"type": "local", "path": "p"}]),
                False,
            ),
            (
                ClaudeAgentOptions(tools=[], hooks={"PreToolUse": [HookMatcher()]}),
                False,
            ),
            (ClaudeAgentOptions(tools=[], env={"CLAUDE_CODE_SIMPLE": "0"}), False),
        ],
    )
    def test_bare_mode_only_for_agents_that_lose_nothing_to_it(self, options, bare):
        # Bare mode drops the reminders that make the model refuse plain
        # instructions, but also built-in tools, settings, skills, plugins and hooks.
        env = _options_for(options, "c").env
        assert (env.get("CLAUDE_CODE_SIMPLE") == "1") == bare

    def test_project_settings_rule_out_bare_mode(self, isolated):
        (isolated / "CLAUDE.md").write_text("Be terse.")
        options = _options_for(ClaudeAgentOptions(tools=[]), "c")
        assert "CLAUDE_CODE_SIMPLE" not in options.env

    def test_project_claude_settings_ship_with_the_agent(self, isolated):
        (isolated / ".claude" / "agents").mkdir(parents=True)
        (isolated / ".claude" / "agents" / "reviewer.md").write_text("---\n---\n")
        (isolated / "CLAUDE.md").write_text("Always answer in French.")

        options = _options_for(ClaudeAgentOptions(), "ctx-1")

        workspace = workspaces_root() / turn_options.session_id_for("ctx-1")
        assert options.setting_sources == ["project"]
        assert (workspace / "CLAUDE.md").read_text() == "Always answer in French."
        assert (workspace / ".claude" / "agents" / "reviewer.md").is_file()

    def test_project_mcp_servers_ship_with_the_agent(self, isolated):
        (isolated / ".mcp.json").write_text('{"mcpServers": {}}')

        options = _options_for(ClaudeAgentOptions(), "ctx-1")

        workspace = workspaces_root() / turn_options.session_id_for("ctx-1")
        assert options.setting_sources == ["project"]
        assert (workspace / ".mcp.json").read_text() == '{"mcpServers": {}}'

    def test_agents_that_load_project_settings_get_the_copy(self, isolated):
        (isolated / "CLAUDE.md").write_text("Be terse.")
        options = _options_for(ClaudeAgentOptions(setting_sources=["project"]), "c")
        assert (
            workspaces_root() / turn_options.session_id_for("c") / "CLAUDE.md"
        ).is_file()
        assert options.setting_sources == ["project"]

    @pytest.mark.parametrize(
        ("setting_sources", "allowed"),
        [
            (None, ["mcp__w__get_weather", "Bash(touch:*)"]),
            (["project", "local"], ["mcp__w__get_weather", "Bash(touch:*)", "Read"]),
            ([], ["mcp__w__get_weather"]),
        ],
    )
    def test_project_permission_rules_apply_in_the_workspace(
        self, isolated, setting_sources, allowed
    ):
        # The CLI ignores allow rules from an untrusted workspace's settings,
        # and a fresh workspace is never trusted.
        (isolated / ".claude").mkdir()
        (isolated / ".claude" / "settings.json").write_text(
            '{"permissions": {"allow": ["Bash(touch:*)", "mcp__w__get_weather"]}}'
        )
        (isolated / ".claude" / "settings.local.json").write_text(
            '{"permissions": {"allow": ["Read"]}}'
        )
        options = _options_for(
            ClaudeAgentOptions(
                allowed_tools=["mcp__w__get_weather"], setting_sources=setting_sources
            ),
            "c",
        )
        assert options.allowed_tools == allowed

    def test_the_developers_own_claude_settings_are_not_loaded(self, isolated):
        # The project root's files belong to the developer's coding assistant.
        project = isolated.parent
        (project / ".claude").mkdir()
        (project / ".claude" / "settings.json").write_text(
            '{"permissions": {"allow": ["Bash(*)"]}}'
        )
        (project / "CLAUDE.md").write_text("You are a coding assistant.")

        options = _options_for(ClaudeAgentOptions(), "ctx-1")

        workspace = workspaces_root() / turn_options.session_id_for("ctx-1")
        assert options.setting_sources == []
        assert options.allowed_tools == []
        assert not (workspace / "CLAUDE.md").exists()
        assert not (workspace / ".claude").exists()

    def test_settings_the_agent_writes_into_its_workspace_are_dropped(self, isolated):
        (isolated / ".claude" / "agents").mkdir(parents=True)
        (isolated / ".claude" / "agents" / "reviewer.md").write_text("---\n---\n")
        _options_for(ClaudeAgentOptions(), "ctx-1")
        # An agent that can write files adds settings and an MCP server to its
        # workspace, hoping the next turn loads them.
        workspace = workspaces_root() / turn_options.session_id_for("ctx-1")
        (workspace / ".claude" / "settings.json").write_text(
            '{"permissions": {"allow": ["Bash(*)"]}, "hooks": {}}'
        )
        (workspace / ".mcp.json").write_text('{"mcpServers": {"x": {"command": "sh"}}}')

        options = _options_for(ClaudeAgentOptions(), "ctx-1")

        assert options.allowed_tools == []
        assert not (workspace / ".claude" / "settings.json").exists()
        assert not (workspace / ".mcp.json").exists()
        assert (workspace / ".claude" / "agents" / "reviewer.md").is_file()

    @pytest.mark.parametrize(
        "settings",
        ["{", "[]", '{"permissions": []}', '{"permissions": {"allow": "Bash"}}'],
    )
    def test_malformed_project_settings_add_no_rules(self, isolated, settings):
        (isolated / ".claude").mkdir()
        (isolated / ".claude" / "settings.json").write_text(settings)
        assert _options_for(ClaudeAgentOptions(), "c").allowed_tools == []

    def test_the_cli_does_not_get_the_cloud_sql_password(self, monkeypatch):
        from app.app_utils.session_store import PostgresSessionStore

        monkeypatch.setenv("INSTANCE_CONNECTION_NAME", "proj:us-east1:db")
        monkeypatch.setenv("DB_PASS", "secret")
        with patch.object(
            PostgresSessionStore, "exists", AsyncMock(return_value=False)
        ):
            options = _options_for(ClaudeAgentOptions(), "ctx-1")

        # The server's store has it; the CLI and its tools see it blanked.
        assert isinstance(options.session_store, PostgresSessionStore)
        assert options.env["DB_PASS"] == ""

    def test_the_cli_always_calls_gemini_platform(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-server")
        options = _options_for(
            ClaudeAgentOptions(
                env={
                    "ANTHROPIC_API_KEY": "sk-agent",
                    "ANTHROPIC_AUTH_TOKEN": "token",
                    "CLAUDE_CODE_USE_VERTEX": "0",
                }
            ),
            "ctx-1",
        )
        assert options.env["CLAUDE_CODE_USE_VERTEX"] == "1"
        assert options.env["ANTHROPIC_API_KEY"] == ""
        assert options.env["ANTHROPIC_AUTH_TOKEN"] == ""

    def test_model_calls_are_tagged_as_agents_cli_traffic(self):
        env = _options_for(ClaudeAgentOptions(), "c").env
        assert env["ANTHROPIC_CUSTOM_HEADERS"] == (
            "x-goog-api-client: "
            f"agents-cli-app/{settings.AGENTS_CLI_VERSION}+claude-agent-sdk"
        )

    @pytest.mark.parametrize("from_agent", [True, False])
    def test_the_tag_keeps_custom_headers(self, monkeypatch, from_agent):
        own = "X-Team: search"
        if from_agent:
            options = ClaudeAgentOptions(env={"ANTHROPIC_CUSTOM_HEADERS": own})
        else:
            monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", own)
            options = ClaudeAgentOptions()
        headers = _options_for(options, "c").env["ANTHROPIC_CUSTOM_HEADERS"]
        assert headers.splitlines()[0] == own
        assert headers.splitlines()[1].startswith("x-goog-api-client: agents-cli-app/")

    @pytest.mark.parametrize(
        ("var", "value"),
        [
            ("DO_NOT_TRACK", "1"),
            ("AGENTS_CLI_TELEMETRY", "0"),
            ("AGENTS_CLI_TELEMETRY", "Off"),
        ],
    )
    @pytest.mark.parametrize("from_agent", [True, False])
    def test_opting_out_of_telemetry_drops_the_tag(
        self, monkeypatch, var, value, from_agent
    ):
        if from_agent:
            options = ClaudeAgentOptions(env={var: value})
        else:
            monkeypatch.setenv(var, value)
            options = ClaudeAgentOptions()
        assert "ANTHROPIC_CUSTOM_HEADERS" not in _options_for(options, "c").env

    def test_opting_out_keeps_custom_headers(self, monkeypatch):
        # The opt-out only drops the tag; the agent's own headers still go out.
        monkeypatch.setenv("AGENTS_CLI_TELEMETRY", "0")
        options = ClaudeAgentOptions(env={"ANTHROPIC_CUSTOM_HEADERS": "X-Team: search"})
        env = _options_for(options, "c").env
        assert env["ANTHROPIC_CUSTOM_HEADERS"] == "X-Team: search"

    def test_cloud_sql_env_gives_one_postgres_store_per_process(self, monkeypatch):
        from app.app_utils.session_store import PostgresSessionStore

        monkeypatch.setenv("INSTANCE_CONNECTION_NAME", "proj:us-east1:db")
        monkeypatch.setenv("DB_PASS", "secret")
        exists = AsyncMock(side_effect=[False, True])
        with patch.object(PostgresSessionStore, "exists", exists):
            first = _options_for(ClaudeAgentOptions(), "ctx-1")
            later = _options_for(ClaudeAgentOptions(), "ctx-1")

        assert isinstance(first.session_store, PostgresSessionStore)
        assert later.session_store is first.session_store
        session = turn_options.session_id_for("ctx-1")
        assert (first.session_id, later.resume) == (session, session)
        key = exists.call_args.args[0]
        assert key["session_id"] == session
        assert key["project_key"] == claude.claude_agent_sdk.project_key_for_directory(
            workspaces_root() / session
        )

    def test_agents_own_store_wins_over_cloud_sql(self, monkeypatch):
        monkeypatch.setenv("INSTANCE_CONNECTION_NAME", "proj:us-east1:db")
        monkeypatch.setenv("DB_PASS", "secret")
        own = InMemorySessionStore()
        options = _options_for(ClaudeAgentOptions(session_store=own), "ctx-1")
        assert options.session_store is own
        assert options.session_id == turn_options.session_id_for("ctx-1")

    def test_resumes_from_an_agents_own_store(self):
        own = InMemorySessionStore()
        session = turn_options.session_id_for("ctx-1")
        project_key = claude.claude_agent_sdk.project_key_for_directory(
            workspaces_root() / session
        )
        anyio.run(
            own.append,
            {"project_key": project_key, "session_id": session},
            [{"type": "user"}],
        )
        options = _options_for(ClaudeAgentOptions(session_store=own), "ctx-1")
        assert options.resume == session

    def test_another_agent_does_not_resume_the_conversation(self, monkeypatch):
        # Services running different agents can share a machine and a database.
        store = InMemorySessionStore()
        monkeypatch.setattr(settings, "AGENT_NAME", "agent_a")
        first = _options_for(ClaudeAgentOptions(session_store=store), "ctx-1")
        key = {
            "project_key": claude.claude_agent_sdk.project_key_for_directory(first.cwd),
            "session_id": first.session_id,
        }
        anyio.run(store.append, key, [{"type": "user"}])

        monkeypatch.setattr(settings, "AGENT_NAME", "agent_b")
        other = _options_for(ClaudeAgentOptions(session_store=store), "ctx-1")
        assert other.cwd != first.cwd
        assert other.resume is None
        monkeypatch.setattr(settings, "AGENT_NAME", "agent_a")
        again = _options_for(ClaudeAgentOptions(session_store=store), "ctx-1")
        assert again.resume == first.session_id

    def test_without_cloud_sql_there_is_no_store(self):
        assert _options_for(ClaudeAgentOptions(), "ctx-1").session_store is None
