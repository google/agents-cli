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

"""Trace generation for `eval generate`: the shape `eval grade` reads."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

_TEMPLATE_DIR = next(
    p / "extensions" / "claude" / "template"
    for p in Path(__file__).resolve().parents
    if (p / "extensions" / "claude" / "template").is_dir()
)


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location(
        "claude_eval_generate", str(_TEMPLATE_DIR / "scripts/eval_generate.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _user_turn(text: str) -> dict:
    return {"events": [{"author": "user", "content": {"parts": [{"text": text}]}}]}


class FakeAgent:
    """Answers each prompt and records the conversation it belongs to."""

    def __init__(self, tool_calls=None):
        self.calls: list[tuple[str, str]] = []
        self.tool_calls = tool_calls or []

    def __call__(self, prompt, conversation_id):
        self.calls.append((prompt, conversation_id))
        return f"reply to {prompt}", self.tool_calls


class TestGenerateTraces:
    def test_tool_calls_become_function_parts_before_the_reply(self, mod):
        agent = FakeAgent(
            [
                {
                    "id": "t1",
                    "name": "get_weather",
                    "args": {"city": "SF"},
                    "result": "60F",
                }
            ]
        )
        dataset = {"eval_cases": [{"agent_data": {"turns": [_user_turn("weather?")]}}]}

        agents = {"weather_agent": {"agent_id": "weather_agent"}}
        out, failures = mod.generate_traces(dataset, "weather_agent", agent, agents)
        assert failures == []

        case = out["eval_cases"][0]
        events = case["agent_data"]["turns"][0]["events"]
        parts = [e["content"]["parts"][0] for e in events[1:]]
        assert parts == [
            {
                "function_call": {
                    "id": "t1",
                    "name": "get_weather",
                    "args": {"city": "SF"},
                }
            },
            {
                "function_response": {
                    "id": "t1",
                    "name": "get_weather",
                    "response": {"result": "60F"},
                }
            },
            {"text": "reply to weather?"},
        ]
        assert {e["author"] for e in events[1:]} == {"weather_agent"}
        assert case["responses"][-1]["response"]["parts"] == [
            {"text": "reply to weather?"}
        ]
        assert case["agent_data"]["turns"][0]["turn_index"] == 0
        # Tool metrics grade calls against the agent's declared tools.
        assert case["agent_data"]["agents"] == agents

    def test_a_subagents_calls_follow_its_delegation_under_its_name(self, mod):
        agent = FakeAgent(
            [
                {"id": "a1", "name": "Agent", "args": {}, "result": "Foggy"},
                {
                    "id": "t1",
                    "name": "get_weather",
                    "args": {},
                    "agent": "forecaster",
                    "parent": "a1",
                    "result": "60F",
                },
            ]
        )
        dataset = {"eval_cases": [{"agent_data": {"turns": [_user_turn("weather?")]}}]}

        out, _ = mod.generate_traces(dataset, "weather_agent", agent)

        events = out["eval_cases"][0]["agent_data"]["turns"][0]["events"][1:]
        steps = [
            (e["author"], *next(iter(e["content"]["parts"][0].items()))) for e in events
        ]
        assert [(author, kind) for author, kind, _ in steps] == [
            ("weather_agent", "function_call"),
            ("weather_agent", "function_response"),
            ("forecaster", "function_call"),
            ("forecaster", "function_response"),
            ("weather_agent", "text"),
        ]
        assert steps[2][2] == {"id": "t1", "name": "get_weather", "args": {}}

    def test_a_cases_turns_share_one_conversation(self, mod):
        agent = FakeAgent()
        dataset = {
            "eval_cases": [
                {"agent_data": {"turns": [_user_turn("I'm Ada"), _user_turn("Name?")]}},
                {"agent_data": {"turns": [_user_turn("hi")]}},
            ]
        }

        mod.generate_traces(dataset, "a", agent)

        (_, first), (_, second), (_, other) = agent.calls
        assert first == second != other

    def test_prompt_shaped_case_gets_a_reply_turn(self, mod):
        agent = FakeAgent()
        dataset = {
            "eval_cases": [{"prompt": {"role": "user", "parts": [{"text": "hi"}]}}]
        }

        out, _ = mod.generate_traces(dataset, "a", agent)

        turns = out["eval_cases"][0]["agent_data"]["turns"]
        assert turns[0]["events"][-1]["content"]["parts"] == [{"text": "reply to hi"}]
        assert [prompt for prompt, _ in agent.calls] == ["hi"]

    def test_a_failing_case_is_dropped_not_the_run(self, mod):
        def infer(prompt, conversation_id):
            if prompt == "boom":
                raise RuntimeError("Reached maximum number of turns (1)")
            return f"reply to {prompt}", []

        dataset = {
            "eval_cases": [
                {"agent_data": {"turns": [_user_turn("hi")]}},
                {"agent_data": {"turns": [_user_turn("boom")]}},
                {"agent_data": {"turns": [_user_turn("bye")]}},
            ]
        }

        out, failures = mod.generate_traces(dataset, "a", infer)

        assert [
            c["responses"][0]["response"]["parts"][0]["text"] for c in out["eval_cases"]
        ] == [
            "reply to hi",
            "reply to bye",
        ]
        assert failures == [(1, "RuntimeError: Reached maximum number of turns (1)")]


_ROOT_AGENT = SimpleNamespace(mcp_servers={}, tools=None, system_prompt=None, agents=None)


@pytest.fixture
def project(monkeypatch, tmp_path) -> Path:
    """Stub the project's `app` package and pass a one-case dataset; returns the output."""

    async def run_turn(prompt, options, conversation_id):  # noqa: RUF029
        return SimpleNamespace(
            text=f"reply to {prompt}", tool_calls=[], subagent_calls=[]
        )

    project_modules = {
        "app": ModuleType("app"),
        "app.app_utils": ModuleType("app.app_utils"),
        "app.agent": ModuleType("app.agent"),
        "app.app_utils.claude": ModuleType("app.app_utils.claude"),
        "app.app_utils.settings": ModuleType("app.app_utils.settings"),
        # The template's own .env is unrendered: keep it out of the test process.
        "dotenv": ModuleType("dotenv"),
    }
    project_modules["app.agent"].root_agent = _ROOT_AGENT
    project_modules["app.app_utils.claude"].run_turn = run_turn
    project_modules["app.app_utils.settings"].AGENT_NAME = "bookshop_agent"
    project_modules["dotenv"].load_dotenv = lambda: None
    for name, module in project_modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    dataset = tmp_path / "dataset.json"
    dataset.write_text(
        json.dumps({"eval_cases": [{"agent_data": {"turns": [_user_turn("hi")]}}]})
    )
    output = tmp_path / "traces.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["eval_generate.py", "--dataset", str(dataset), "--output", str(output)],
    )
    return output


class TestMain:
    def test_traces_are_authored_by_the_agent_name_setting(self, mod, project):
        """The author matches the spans' gen_ai.agent.name and BigQuery's agent."""
        assert mod.main() == 0

        [case] = json.loads(project.read_text())["eval_cases"]
        events = case["agent_data"]["turns"][0]["events"]
        assert [e["author"] for e in events] == ["user", "bookshop_agent"]

    def test_agent_is_built_after_dotenv_is_loaded(self, mod, project, monkeypatch):
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
            return _ROOT_AGENT

        agent = ModuleType("app.agent")
        # Builds root_agent on import, as app/agent.py does.
        agent.__getattr__ = build
        monkeypatch.setitem(sys.modules, "app.agent", agent)

        assert mod.main() == 0
        assert built_with == ["true"]

    def test_url_is_refused(self, mod, monkeypatch, capsys):
        """A served Claude agent speaks A2A, not the built-in's /run_sse."""
        monkeypatch.setattr(
            sys, "argv", ["eval_generate.py", "--url", "https://agent.run.app"]
        )

        assert mod.main() == 2
        assert "--url is not supported" in capsys.readouterr().err
