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

"""Argv handling and output of the `run` override, which replaces the built-in."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_EXTENSION_DIR = next(
    p / "extensions" / "langchain" / "template"
    for p in Path(__file__).resolve().parents
    if (p / "extensions" / "langchain" / "template").is_dir()
)
_SCRIPT_PATH = _EXTENSION_DIR / "scripts/run.py"

# The script imports a sibling module the way python does when run as a file.
sys.path.insert(0, str(_EXTENSION_DIR / "scripts"))


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location("lc_run", str(_SCRIPT_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seen(monkeypatch) -> list[str]:
    """Record the prompt the stub graph is invoked with."""
    prompts: list[str] = []

    def invoke(state):
        prompts.append(state["messages"][-1]["content"])
        return {"messages": [types.SimpleNamespace(content="ok")]}

    app = types.ModuleType("app")
    agent = types.ModuleType("app.agent")
    agent.root_agent = types.SimpleNamespace(invoke=invoke)
    monkeypatch.setitem(sys.modules, "app", app)
    monkeypatch.setitem(sys.modules, "app.agent", agent)
    return prompts


def _run(mod, argv: list[str], monkeypatch) -> int:
    monkeypatch.setattr(sys, "argv", ["run.py", *argv])
    return mod.main()


class TestArgv:
    def test_sends_the_single_message(self, mod, seen, monkeypatch):
        assert _run(mod, ["what is the weather"], monkeypatch) == 0
        assert seen == ["what is the weather"]

    def test_extra_words_are_rejected_not_joined(self, mod, seen, monkeypatch):
        """Matches the built-in: the message is one argument, quoted."""
        with pytest.raises(SystemExit) as exit_info:
            _run(mod, ["hello", "there"], monkeypatch)
        assert exit_info.value.code == 2
        assert seen == []

    def test_no_message_is_a_usage_error(self, mod, monkeypatch):
        with pytest.raises(SystemExit) as exit_info:
            _run(mod, [], monkeypatch)
        assert exit_info.value.code == 2

    @pytest.mark.parametrize(
        "argv", [["--url", "https://agent.example", "hi"], ["--mode", "a2a", "hi"]]
    )
    def test_remote_flags_are_refused_with_a_way_out(
        self, mod, argv, monkeypatch, capsys
    ):
        assert _run(mod, argv, monkeypatch) == 2
        assert "AGENTS_CLI_DISABLE_OVERRIDES=1" in capsys.readouterr().err


def _message(kind: str, content: object = "", **fields) -> types.SimpleNamespace:
    """A stand-in for a LangChain message: the attributes the override reads."""
    return types.SimpleNamespace(type=kind, content=content, **fields)


@pytest.fixture
def replies(monkeypatch):
    """Make the stub graph return the given messages after the user's."""

    def set_messages(messages, name="office_hours"):
        def invoke(state):
            prompt = state["messages"][-1]["content"]
            return {"messages": [_message("human", prompt), *messages]}

        agent = types.ModuleType("app.agent")
        agent.root_agent = types.SimpleNamespace(invoke=invoke, name=name)
        monkeypatch.setitem(sys.modules, "app", types.ModuleType("app"))
        monkeypatch.setitem(sys.modules, "app.agent", agent)

    return set_messages


class TestOutput:
    """`run` prints the turn in the built-in's ADK shape, tool calls included."""

    def test_prints_tool_calls_and_responses_before_the_reply(
        self, mod, replies, monkeypatch, capsys
    ):
        call = {"name": "get_hours", "args": {"date": "2026-11-18"}, "id": "c1"}
        replies(
            [
                _message("ai", "", tool_calls=[call]),
                _message(
                    "tool", '{"is_open": true}', name="get_hours", tool_call_id="c1"
                ),
                _message("ai", [{"type": "text", "text": "Open, 8:30 to 17:30."}]),
            ]
        )
        assert _run(mod, ["Are we open?"], monkeypatch) == 0
        assert capsys.readouterr().out.splitlines() == [
            "[user]: Are we open?",
            "[office_hours]: ",
            '[tool_call: get_hours({"date": "2026-11-18"})]',
            '[tool_response: get_hours -> {"is_open": true}]',
            "Open, 8:30 to 17:30.",
        ]

    def test_a_reply_without_tools_shares_the_tag_line(
        self, mod, replies, monkeypatch, capsys
    ):
        replies([_message("ai", "Hello.")], name="")
        assert _run(mod, ["hi"], monkeypatch) == 0
        assert capsys.readouterr().out.splitlines() == [
            "[user]: hi",
            "[agent]: Hello.",
        ]

    @pytest.mark.parametrize(
        ("content", "shown"),
        [
            ("It's 60 degrees.", '{"result": "It\'s 60 degrees."}'),
            ("[1, 2]", '{"result": [1, 2]}'),
        ],
    )
    def test_a_tool_output_that_is_not_an_object_is_wrapped(
        self, mod, replies, monkeypatch, capsys, *, content, shown
    ):
        call = {"name": "get_weather", "args": {"query": "sf"}, "id": "c1"}
        # No name on the ToolMessage: the call id says which tool answered.
        replies(
            [
                _message("ai", "", tool_calls=[call]),
                _message("tool", content, tool_call_id="c1"),
                _message("ai", "Foggy."),
            ]
        )
        assert _run(mod, ["weather in sf?"], monkeypatch) == 0
        assert f"[tool_response: get_weather -> {shown}]" in capsys.readouterr().out

    def test_says_so_when_the_agent_returns_nothing(
        self, mod, replies, monkeypatch, capsys
    ):
        replies([_message("ai", "")])
        assert _run(mod, ["hi"], monkeypatch) == 0
        assert capsys.readouterr().out.splitlines() == [
            "[user]: hi",
            "(no response content)",
        ]

    def test_reads_dict_messages_by_role(self, mod, monkeypatch, capsys):
        # LangGraph state can hold OpenAI-style dicts; the prompt must not echo as the reply.
        def invoke(state):
            return {
                "messages": [
                    *state["messages"],
                    {"role": "assistant", "content": "Hello."},
                ]
            }

        agent = types.ModuleType("app.agent")
        agent.root_agent = types.SimpleNamespace(invoke=invoke, name="office_hours")
        monkeypatch.setitem(sys.modules, "app", types.ModuleType("app"))
        monkeypatch.setitem(sys.modules, "app.agent", agent)
        assert _run(mod, ["hi"], monkeypatch) == 0
        assert capsys.readouterr().out.splitlines() == [
            "[user]: hi",
            "[office_hours]: Hello.",
        ]
