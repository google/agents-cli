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

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_EXTENSION_DIR = next(
    p / "extensions" / "langchain" / "template"
    for p in Path(__file__).resolve().parents
    if (p / "extensions" / "langchain" / "template").is_dir()
)

# Compute absolute script path at module-import time (before any cwd drift)
# so the test works regardless of which directory pytest is invoked from.
# Tests live inside the extension, so paths are relative to the extension root.
_SCRIPT_PATH = _EXTENSION_DIR / "scripts/eval_generate.py"

# The scripts import a sibling module the way python does when run as a file.
sys.path.insert(0, str(_EXTENSION_DIR / "scripts"))


def _load():
    spec = importlib.util.spec_from_file_location("lg_eval", str(_SCRIPT_PATH))
    assert spec is not None, f"could not find module spec at {_SCRIPT_PATH}"
    assert spec.loader is not None, "module spec has no loader"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class TestGenerate:
    def test_appends_agent_reply_per_turn(self):
        mod = _load()
        dataset = {
            "eval_cases": [
                {
                    "agent_data": {
                        "turns": [
                            {
                                "events": [
                                    {
                                        "author": "user",
                                        "content": {"parts": [{"text": "hi"}]},
                                    }
                                ]
                            }
                        ]
                    }
                }
            ]
        }
        out = mod.generate_traces(dataset, "agent", lambda p: f"echo:{p}")
        events = out["eval_cases"][0]["agent_data"]["turns"][0]["events"]
        assert events[-1]["author"] == "agent"
        assert events[-1]["content"]["parts"][0]["text"] == "echo:hi"

    def test_turn_without_user_event_is_skipped(self):
        mod = _load()
        dataset = {
            "eval_cases": [
                {
                    "agent_data": {
                        "turns": [
                            {
                                "events": [
                                    {
                                        "author": "agent",
                                        "content": {"parts": [{"text": "hello"}]},
                                    }
                                ]
                            }
                        ]
                    }
                }
            ]
        }
        out = mod.generate_traces(dataset, "agent", lambda p: f"echo:{p}")
        # Only the original agent event; no new event appended.
        events = out["eval_cases"][0]["agent_data"]["turns"][0]["events"]
        assert len(events) == 1
        assert events[0]["author"] == "agent"

    def test_multiple_turns_each_get_reply(self):
        mod = _load()
        dataset = {
            "eval_cases": [
                {
                    "agent_data": {
                        "turns": [
                            {
                                "events": [
                                    {
                                        "author": "user",
                                        "content": {"parts": [{"text": "first"}]},
                                    }
                                ]
                            },
                            {
                                "events": [
                                    {
                                        "author": "user",
                                        "content": {"parts": [{"text": "second"}]},
                                    }
                                ]
                            },
                        ]
                    }
                }
            ]
        }
        out = mod.generate_traces(dataset, "bot", lambda p: f"reply:{p}")
        turns = out["eval_cases"][0]["agent_data"]["turns"]
        assert turns[0]["events"][-1]["content"]["parts"][0]["text"] == "reply:first"
        assert turns[1]["events"][-1]["content"]["parts"][0]["text"] == "reply:second"
        assert turns[0]["events"][-1]["author"] == "bot"


class TestGradeContract:
    """`eval grade` (Vertex) rejects a trace missing these fields."""

    def test_turns_get_index_and_id(self):
        mod = _load()
        dataset = {
            "eval_cases": [
                {
                    "agent_data": {
                        "turns": [
                            {
                                "events": [
                                    {
                                        "author": "user",
                                        "content": {"parts": [{"text": "hi"}]},
                                    }
                                ]
                            }
                        ]
                    }
                }
            ]
        }
        turn = mod.generate_traces(dataset, "bot", lambda p: "yo")["eval_cases"][0][
            "agent_data"
        ]["turns"][0]
        assert turn["turn_index"] == 0
        assert turn["turn_id"] == "turn_0"

    def test_output_parses_as_the_model_grade_reads(self):
        """Drift guard against the built-in: `eval grade` loads a trace with this
        model (see cmd_grade.py), so a shape the service would reject fails here.
        """
        from agentplatform import types

        mod = _load()
        dataset = {
            "eval_cases": [{"prompt": {"role": "user", "parts": [{"text": "hi"}]}}]
        }
        out = mod.generate_traces(dataset, "bot", lambda p: "yo")

        case = types.EvaluationDataset.model_validate(out).eval_cases[0]
        assert case.agent_data.turns[0].turn_index == 0
        assert case.responses[0].response.parts[0].text == "yo"

    def test_case_carries_the_final_response(self):
        mod = _load()
        dataset = {
            "eval_cases": [{"prompt": {"role": "user", "parts": [{"text": "hi"}]}}]
        }
        case = mod.generate_traces(dataset, "bot", lambda p: "yo")["eval_cases"][0]
        assert case["responses"][0]["response"]["parts"][0]["text"] == "yo"

    def test_prompt_shaped_case_gets_a_reply_turn(self):
        """A top-level `prompt` case used to pass through with no agent reply."""
        mod = _load()
        dataset = {
            "eval_cases": [{"prompt": {"role": "user", "parts": [{"text": "hi"}]}}]
        }
        turns = mod.generate_traces(dataset, "bot", lambda p: f"reply:{p}")["eval_cases"][
            0
        ]["agent_data"]["turns"]
        assert len(turns) == 1
        assert turns[0]["events"][-1]["content"]["parts"][0]["text"] == "reply:hi"
        assert turns[0]["events"][-1]["author"] == "bot"


@pytest.fixture
def mod():
    return _load()


class TestBuiltinArgv:
    """An override replaces `eval generate`, so it must take the built-in's argv.

    `eval run` forwards whatever the user passed; an unknown flag would make
    argparse exit 2 in the middle of the chain.
    """

    @pytest.mark.parametrize(
        "argv",
        [
            ["--app-name", "app"],
            ["--concurrency", "4"],
            ["--header", "X-Key: v"],
        ],
    )
    def test_accepts_the_flags_the_builtin_takes(self, mod, tmp_path, argv, monkeypatch):
        dataset = tmp_path / "cases.json"
        dataset.write_text('{"eval_cases": []}', encoding="utf-8")
        monkeypatch.setattr(mod, "_agent_name", lambda: "bot")
        _stub_app_agent(monkeypatch)
        code = _run_main(
            mod,
            ["--dataset", str(dataset), "--output", str(tmp_path / "t.json"), *argv],
        )
        assert code == 0

    def test_url_is_refused_with_a_way_out(self, mod, tmp_path, capsys):
        code = _run_main(mod, ["--url", "https://agent.example"])
        assert code == 2
        assert "AGENTS_CLI_DISABLE_OVERRIDES=1" in capsys.readouterr().err


class TestOutputPath:
    def test_trailing_separator_names_a_directory_to_create(
        self, mod, tmp_path, monkeypatch
    ):
        """`--output traces/` means "write inside traces", even before it exists.

        os.sep so this covers the native separator on the platform it runs on;
        `traces\\` is what a Windows user types.
        """
        dataset = tmp_path / "cases.json"
        dataset.write_text('{"eval_cases": []}', encoding="utf-8")
        monkeypatch.setattr(mod, "_agent_name", lambda: "bot")
        _stub_app_agent(monkeypatch)
        target = tmp_path / "traces"

        code = _run_main(
            mod, ["--dataset", str(dataset), "--output", f"{target}{os.sep}"]
        )

        assert code == 0
        written = list(target.iterdir())
        assert len(written) == 1 and written[0].name.startswith("eval_dataset_")


def _stub_app_agent(monkeypatch) -> None:
    """Stand in for the scaffolded project's `app.agent` module."""
    import types

    graph = types.SimpleNamespace(
        invoke=lambda state: {"messages": [types.SimpleNamespace(content="hi")]}
    )
    app = types.ModuleType("app")
    agent = types.ModuleType("app.agent")
    agent.root_agent = graph
    monkeypatch.setitem(sys.modules, "app", app)
    monkeypatch.setitem(sys.modules, "app.agent", agent)


def _run_main(mod, argv: list[str]) -> int:
    import sys

    old = sys.argv
    sys.argv = ["eval_generate.py", *argv]
    try:
        return mod.main()
    finally:
        sys.argv = old
