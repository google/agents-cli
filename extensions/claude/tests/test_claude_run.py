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

"""The `run` override: argv handling and how it reports the agent's reply."""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_TEMPLATE_DIR = next(
    p / "extensions" / "claude" / "template"
    for p in Path(__file__).resolve().parents
    if (p / "extensions" / "claude" / "template").is_dir()
)


class _AgentError(RuntimeError):
    pass


class _Seen(list):
    """Prompts the stub agent got, plus the conversation each one was in."""

    conversations: list[str | None]


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location(
        "claude_run", str(_TEMPLATE_DIR / "scripts/run.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seen(monkeypatch) -> _Seen:
    """Stub the project's `app` package; record the prompts the agent gets."""
    prompts = _Seen()
    conversations: list[str | None] = []

    async def run_turn(prompt, options, conversation_id=None):  # noqa: RUF029 - mirrors the async API
        prompts.append(prompt)
        conversations.append(conversation_id)
        if prompt == "fail":
            raise _AgentError("Claude agent turn failed: model not available")
        return types.SimpleNamespace(
            text="It's foggy.",
            tool_calls=[{"name": "get_weather", "args": {"city": "SF"}}],
        )

    claude = types.ModuleType("app.app_utils.claude")
    claude.run_turn = run_turn
    claude.AgentError = _AgentError
    sdk = types.ModuleType("claude_agent_sdk")
    sdk.ClaudeSDKError = type("ClaudeSDKError", (Exception,), {})
    agent = types.ModuleType("app.agent")
    agent.root_agent = object()
    # The template's own .env is unrendered: keep it out of the test process.
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None
    for name, module in {
        "app": types.ModuleType("app"),
        "app.agent": agent,
        "app.app_utils": types.ModuleType("app.app_utils"),
        "app.app_utils.claude": claude,
        "claude_agent_sdk": sdk,
        "dotenv": dotenv,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    prompts.conversations = conversations
    return prompts


def _run(mod, argv: list[str], monkeypatch) -> int:
    monkeypatch.setattr(sys, "argv", ["run.py", *argv])
    return mod.main()


class TestRun:
    def test_prints_the_reply_and_how_to_continue(self, mod, seen, monkeypatch, capsys):
        assert _run(mod, ["what is the weather"], monkeypatch) == 0
        assert seen == ["what is the weather"]
        out = capsys.readouterr().out
        assert out.startswith("It's foggy.")
        assert f"--session-id {seen.conversations[0]}" in out

    def test_session_id_continues_that_conversation(self, mod, seen, monkeypatch):
        assert _run(mod, ["hi again", "--session-id", "conv-7"], monkeypatch) == 0
        assert seen.conversations == ["conv-7"]

    def test_verbose_prints_the_tool_calls(self, mod, seen, monkeypatch, capsys):
        assert _run(mod, ["weather?", "-v"], monkeypatch) == 0
        assert '"name": "get_weather"' in capsys.readouterr().err

    def test_agent_error_exits_nonzero_with_the_reason(
        self, mod, seen, monkeypatch, capsys
    ):
        assert _run(mod, ["fail"], monkeypatch) == 1
        assert "model not available" in capsys.readouterr().err

    def test_agent_is_built_after_dotenv_is_loaded(self, mod, seen, monkeypatch):
        """Its tool lists depend on flags in .env, such as MEMORY_BANK_ENABLED."""
        monkeypatch.delenv("MEMORY_BANK_ENABLED", raising=False)
        monkeypatch.setattr(
            sys.modules["dotenv"],
            "load_dotenv",
            lambda: monkeypatch.setenv("MEMORY_BANK_ENABLED", "true"),
        )
        built_with = []

        def build(name):
            if name != "root_agent":
                raise AttributeError(name)
            built_with.append(os.environ.get("MEMORY_BANK_ENABLED"))
            return object()

        agent = types.ModuleType("app.agent")
        # Builds root_agent on import, as app/agent.py does.
        agent.__getattr__ = build
        monkeypatch.setitem(sys.modules, "app.agent", agent)

        assert _run(mod, ["hi"], monkeypatch) == 0
        assert built_with == ["true"]

    def test_extra_words_are_rejected_not_joined(self, mod, seen, monkeypatch):
        """Matches the built-in: the message is one argument, quoted."""
        with pytest.raises(SystemExit) as exit_info:
            _run(mod, ["hello", "there"], monkeypatch)
        assert exit_info.value.code == 2
        assert seen == []

    @pytest.mark.parametrize(
        "argv",
        [
            ["--url", "https://agent.example", "--app-name", "app", "hi"],
            ["--mode=a2a", "hi"],
        ],
    )
    @patch("subprocess.run")
    def test_remote_queries_go_to_the_built_in(
        self, mock_run, mod, seen, argv, monkeypatch
    ):
        mock_run.return_value = MagicMock(returncode=0)

        assert _run(mod, argv, monkeypatch) == 0

        command, kwargs = mock_run.call_args.args[0], mock_run.call_args.kwargs
        assert command == ["agents-cli", "run", *argv]
        assert kwargs["env"]["AGENTS_CLI_DISABLE_OVERRIDES"] == "1"
        assert seen == []
