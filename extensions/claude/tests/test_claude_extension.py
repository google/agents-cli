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

"""Verify the Claude Agent SDK template: what it declares, and what it overrides.

The manifest ships at the template root, so every scaffolded project carries
these overrides; the template config is read by the CLI and never copied.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from google.agents.cli.extension._spec import parse_extension_spec
from google.agents.cli.scaffold.utils.version import get_current_version

_REPO_ROOT = next(
    p
    for p in Path(__file__).resolve().parents
    if (p / "extensions" / "claude" / "template").is_dir()
)
_TEMPLATE_DIR = _REPO_ROOT / "extensions" / "claude" / "template"


def _spec():
    data = yaml.safe_load((_TEMPLATE_DIR / "agents-cli-extension.yaml").read_text())
    return parse_extension_spec(data, root=_TEMPLATE_DIR, default_name="claude")


class TestTemplateConfig:
    def test_builds_on_the_empty_base(self):
        """empty_py is what leaves the ADK agent layer out of the project."""
        config = yaml.safe_load(
            (_TEMPLATE_DIR / ".template" / "templateconfig.yaml").read_text()
        )

        assert config["base_template"] == "empty_py"
        assert config["settings"]["tags"] == ["claude", "a2a"]
        # The empty base ships no server, so the template owes the entrypoint.
        agent_dir = config["settings"]["agent_directory"]
        assert (_TEMPLATE_DIR / agent_dir / "fast_api_app.py").is_file()


class TestExtensionSpec:
    def test_overrides_the_framework_coupled_commands(self):
        spec = _spec()
        overrides = {c.name for c in spec.commands if c.kind == "override"}

        assert {"run", "eval.generate"} <= overrides
        # scaffold.* belongs to the CLI now: the template is the scaffold.
        assert not {n for n in overrides if n.startswith("scaffold.")}

    def test_refuses_the_adk_only_commands(self):
        """Unoverridden, these die on a missing `google.adk` import or `adk` binary."""
        spec = _spec()
        refused = {
            c.name: c.run
            for c in spec.commands
            if c.kind == "override" and "scripts/unsupported.py" in c.run
        }

        assert set(refused) == {"playground", "eval.dataset", "eval.optimize"}
        assert refused["eval.dataset"][-1] == "eval dataset"
        assert (_TEMPLATE_DIR / "scripts" / "unsupported.py").is_file()

    def test_every_refusal_explains_itself(self):
        """The message is the only guidance the user gets, so it must be specific."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "claude_unsupported", _TEMPLATE_DIR / "scripts" / "unsupported.py"
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        named = {c.run[-1] for c in _spec().commands if "scripts/unsupported.py" in c.run}
        assert named == set(mod._REASONS), "a refusal has no reason written for it"

    def test_every_script_it_points_at_ships_with_the_template(self):
        for command in _spec().commands:
            for token in command.run:
                if token.startswith("scripts/"):
                    assert (_TEMPLATE_DIR / token).is_file(), token


class TestManifestMatchesPublishedSchema:
    def test_first_party_manifest_validates(self):
        """The shipped example must satisfy the document we tell authors to use."""
        import jsonschema

        from google.agents.cli.extension._schema import build_json_schema

        data = yaml.safe_load(
            (_TEMPLATE_DIR / "agents-cli-extension.yaml").read_text(encoding="utf-8")
        )
        jsonschema.validate(data, build_json_schema())


def _scaffold(out: Path, *flags: str) -> Path:
    """A project made from the template, with `flags` added to `create`."""
    from click.testing import CliRunner

    from google.agents.cli.scaffold.commands.create import create

    result = CliRunner().invoke(
        create,
        [
            "claude-probe",
            "--agent",
            f"local@{_TEMPLATE_DIR}",
            "--skip-checks",
            "--skip-deps",
            "--auto-approve",
            "--deployment-target",
            "cloud_run",
            "--cicd-runner",
            "github_actions",
            "--output-dir",
            str(out),
            *flags,
        ],
    )
    assert result.exit_code == 0, result.output
    return out / "claude-probe"


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    """One scaffold from the template, shared by the checks below."""
    return _scaffold(tmp_path_factory.mktemp("scaffold"))


class TestScaffoldedProject:
    """What a project made from this template ends up with."""

    def test_it_carries_its_own_overrides(self, project):
        # The overrides ride in the project, so no install step is needed.
        assert (project / "agents-cli-extension.yaml").is_file()
        # .template is the CLI's to read, not the project's to carry.
        assert not (project / ".template").exists()
        assert (project / ".agents/skills/agents-cli-claude/SKILL.md").is_file()

    def test_its_guide_replaces_the_adk_one(self, project):
        """The template ships AGENTS.md; the copy renames it to the project's."""
        guide = (project / "GEMINI.md").read_text()
        assert "Claude Agent SDK project" in guide and "google.adk" not in guide
        assert not (project / "AGENTS.md").exists()

    def test_ships_no_adk_shaped_tests(self, project):
        """The deployment targets' e2e and load tests drive ADK-only routes."""
        adk_import = re.compile(r"^\s*(from|import) google\.adk", re.MULTILINE)
        leaked = [
            path.relative_to(project)
            for path in project.rglob("*.py")
            if adk_import.search(path.read_text(encoding="utf-8"))
        ]
        assert not leaked, f"ADK-coupled files in a Claude project: {leaked}"
        assert "google-adk" not in (project / "pyproject.toml").read_text()
        # cicd_runner keeps tests/load_test, so the template's copy must win.
        assert (project / "tests" / "load_test" / "load_test.py").is_file()

    def test_it_names_the_agent_after_the_project(self, project):
        settings = (project / "app/app_utils/settings.py").read_text()
        assert 'AGENT_NAME: str = "claude_probe"' in settings

    def test_memory_bank_is_off(self, project):
        assert "MEMORY_BANK_ENABLED" not in (project / ".env").read_text()

    def test_it_records_the_cli_version_for_the_traffic_tag(self, project):
        settings = (project / "app/app_utils/settings.py").read_text()
        assert f'AGENTS_CLI_VERSION: str = "{get_current_version()}"' in settings

    def test_it_names_the_package_after_the_project(self, project):
        """The lock must name the package too, or `uv sync --locked` fails."""
        pyproject = tomllib.loads((project / "pyproject.toml").read_text())
        assert pyproject["project"]["name"] == "claude-probe"
        lock = tomllib.loads((project / "uv.lock").read_text())
        root = [p for p in lock["package"] if p["source"] == {"editable": "."}]
        assert [p["name"] for p in root] == ["claude-probe"]


def test_memory_bank_flag_turns_memory_on(tmp_path):
    project = _scaffold(tmp_path, "--memory-bank")

    assert "\nMEMORY_BANK_ENABLED=true\n" in (project / ".env").read_text()


@patch("google.agents.cli._runner.subprocess.run")
def test_enhance_renders_the_agent_name_into_a_project_without_it(
    mock_run, tmp_path, monkeypatch
):
    """enhance moves the user's agent.py aside and renders the settings."""
    from click.testing import CliRunner

    from google.agents.cli.scaffold.commands.enhance import enhance

    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    project = tmp_path / "bookshop-agent"
    user_agent = "root_agent = ClaudeAgentOptions()\n"
    (project / "app").mkdir(parents=True)
    (project / "app" / "agent.py").write_text(user_agent)
    (project / "pyproject.toml").write_text(
        '[project]\nname = "bookshop-agent"\nversion = "0.1.0"\ndependencies = []\n'
    )
    monkeypatch.chdir(project)

    result = CliRunner().invoke(
        enhance,
        [
            f"local@{_TEMPLATE_DIR}",
            "--deployment-target",
            "cloud_run",
            "--auto-approve",
            "--skip-checks",
            "--skip-deps",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (project / ".agents-cli-original" / "app" / "agent.py").read_text() == (
        user_agent
    )
    manifest = yaml.safe_load((project / "agents-cli-manifest.yaml").read_text())
    name = manifest["create_params"]["root_agent_name"]
    assert name == "bookshop_agent"
    settings = (project / "app/app_utils/settings.py").read_text()
    assert f'AGENT_NAME: str = "{name}"' in settings
    pyproject = tomllib.loads((project / "pyproject.toml").read_text())
    assert pyproject["project"]["name"] == "bookshop-agent"


class TestShippedSkill:
    def test_it_is_not_installable_from_this_repo(self):
        """`npx skills add <repo>` only reads the repo's own skills/ folder.

        A skill that leaked out of the template would be installed machine-wide
        on anyone who runs `agents-cli setup`.
        """
        assert not (_REPO_ROOT / "skills" / "agents-cli-claude").exists()
