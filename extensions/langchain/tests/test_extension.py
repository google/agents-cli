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

"""Verify the LangChain template: what it declares, and what it overrides.

The manifest ships at the template root, so every scaffolded project carries
these overrides; the template config is read by the CLI and never copied.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from google.agents.cli.extension._spec import parse_extension_spec

_REPO_ROOT = next(
    p
    for p in Path(__file__).resolve().parents
    if (p / "extensions" / "langchain" / "template").is_dir()
)
_TEMPLATE_DIR = _REPO_ROOT / "extensions" / "langchain" / "template"


def _spec():
    data = yaml.safe_load((_TEMPLATE_DIR / "agents-cli-extension.yaml").read_text())
    return parse_extension_spec(data, root=_TEMPLATE_DIR, default_name="langchain")


class TestTemplateConfig:
    def test_builds_on_the_empty_base(self):
        """empty_py is what leaves the ADK agent layer out of the project."""
        config = yaml.safe_load(
            (_TEMPLATE_DIR / ".template" / "templateconfig.yaml").read_text()
        )

        assert config["base_template"] == "empty_py"
        assert config["settings"]["tags"] == ["langchain", "a2a"]
        assert config["settings"]["requires_session"] is True
        assert config["settings"]["session_types"] == ["in_memory", "cloud_sql"]
        # The empty base ships no server, so the template owes the entrypoint.
        agent_dir = config["settings"]["agent_directory"]
        assert (_TEMPLATE_DIR / agent_dir / "fast_api_app.py").is_file()


class TestExtensionSpec:
    def test_overrides_the_framework_coupled_commands(self):
        spec = _spec()
        overrides = {c.name for c in spec.commands if c.kind == "override"}

        assert {"playground", "run", "eval.generate"} <= overrides
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

        assert set(refused) == {"eval.dataset", "eval.optimize"}
        assert refused["eval.dataset"][-1] == "eval dataset"
        assert (_TEMPLATE_DIR / "scripts" / "unsupported.py").is_file()

    def test_every_refusal_explains_itself(self):
        """The message is the only guidance the user gets, so it must be specific."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "lc_unsupported", _TEMPLATE_DIR / "scripts" / "unsupported.py"
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


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    """One scaffold from the template, shared by the checks below."""
    from click.testing import CliRunner

    from google.agents.cli.scaffold.commands.create import create

    out = tmp_path_factory.mktemp("scaffold")
    result = CliRunner().invoke(
        create,
        [
            "langchain-probe",
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
        ],
    )
    assert result.exit_code == 0, result.output
    return out / "langchain-probe"


class TestScaffoldedProject:
    """What a project made from this template ends up with."""

    def test_it_carries_its_own_overrides(self, project):
        # The overrides ride in the project, so no install step is needed.
        assert (project / "agents-cli-extension.yaml").is_file()
        # .template is the CLI's to read, not the project's to carry.
        assert not (project / ".template").exists()
        assert (project / ".agents/skills/agents-cli-langchain/SKILL.md").is_file()

    def test_its_guide_replaces_the_adk_one(self, project):
        """The template ships AGENTS.md; the copy renames it to the project's."""
        guide = (project / "GEMINI.md").read_text()
        assert "LangChain project" in guide and "google.adk" not in guide
        assert not (project / "AGENTS.md").exists()

    def test_ships_no_adk_shaped_tests(self, project):
        """The deployment targets' e2e and load tests drive ADK-only routes."""
        adk_import = re.compile(r"^\s*(from|import) google\.adk", re.MULTILINE)
        leaked = [
            path.relative_to(project)
            for path in project.rglob("*.py")
            if adk_import.search(path.read_text(encoding="utf-8"))
        ]
        assert not leaked, f"ADK-coupled files in a LangChain project: {leaked}"
        assert "google-adk" not in (project / "pyproject.toml").read_text()
        # cicd_runner keeps tests/load_test, so the template's copy must win.
        assert (project / "tests" / "load_test" / "load_test.py").is_file()


def test_cloud_sql_session_type_renders_database_infra(tmp_path):
    """`--session-type cloud_sql` wires Cloud SQL into the generated Terraform."""
    from click.testing import CliRunner

    from google.agents.cli.scaffold.commands.create import create

    result = CliRunner().invoke(
        create,
        [
            "langchain-sql",
            "--agent",
            f"local@{_TEMPLATE_DIR}",
            "--skip-checks",
            "--skip-deps",
            "--auto-approve",
            "--deployment-target",
            "cloud_run",
            "--session-type",
            "cloud_sql",
            "--output-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    project = tmp_path / "langchain-sql"
    manifest = yaml.safe_load((project / "agents-cli-manifest.yaml").read_text())
    assert manifest["create_params"]["session_type"] == "cloud_sql"
    service_tf = (project / "deployment/terraform/single-project/service.tf").read_text()
    assert 'resource "google_sql_database_instance" "session_db"' in service_tf
    assert 'name  = "INSTANCE_CONNECTION_NAME"' in service_tf


class TestShippedSkill:
    def test_it_is_not_installable_from_this_repo(self):
        """`npx skills add <repo>` only reads the repo's own skills/ folder.

        A skill that leaked out of the template would be installed machine-wide
        on anyone who runs `agents-cli setup`.
        """
        assert not (_REPO_ROOT / "skills" / "agents-cli-langchain").exists()
