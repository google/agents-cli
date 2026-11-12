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

"""The local server: `app/fast_api_app.py` and the `.env` it loads."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parents[2] / "app"


def test_features_turned_on_only_in_dotenv_add_their_tools(tmp_path):
    # A copy of the app next to its own .env, run in a fresh interpreter: the
    # tool lists are fixed when app.agent is first imported.
    shutil.copytree(
        _APP_DIR, tmp_path / "app", ignore=shutil.ignore_patterns("__pycache__")
    )
    (tmp_path / ".env").write_text(
        "MEMORY_BANK_ENABLED=true\nGOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=false\n"
    )
    env = {
        name: value
        for name, value in os.environ.items()
        if name
        not in ("MEMORY_BANK_ENABLED", "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY")
    }
    env["PYTHONPATH"] = str(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import app.fast_api_app\n"
            "from app.agent import root_agent\n"
            "print(app.__file__, root_agent.allowed_tools)",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.startswith(str(tmp_path / "app" / "__init__.py"))
    assert "mcp__memory__search_memory" in result.stdout
