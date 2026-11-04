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

"""Helpers shared by the tests of `app/app_utils/claude.py` and `turn_options.py`."""

import pathlib

from app.app_utils import settings, turn_options, workspaces


def workspaces_root() -> pathlib.Path:
    """Where the agent's conversation workspaces go."""
    return workspaces.WORKSPACES / settings.AGENT_NAME


def write_local_transcript(conversation_id: str) -> None:
    """What the CLI leaves on disk after a conversation's first turn."""
    session = turn_options.session_id_for(conversation_id)
    transcript = workspaces.transcripts_dir(workspaces_root() / session)
    transcript.mkdir(parents=True, exist_ok=True)
    (transcript / f"{session}.jsonl").write_text('{"type": "user"}\n')
