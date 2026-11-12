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

"""`root_agent`'s options for one served turn: its session, workspace and defaults."""

from __future__ import annotations

import dataclasses
import functools
import logging
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import claude_agent_sdk
from claude_agent_sdk import ClaudeAgentOptions

from app.app_utils import settings, workspaces
from app.app_utils.project_settings import (
    copy_project_settings,
    settings_allow_rules,
)
from app.app_utils.telemetry import cli_telemetry_env

# Claude session ids must be UUIDs; other conversation ids map to one derived from them.
# Never change it: transcripts are stored under the derived ids, so a new namespace
# would leave existing conversations unresumable.
_SESSION_NAMESPACE = uuid.UUID("5b0e7f2a-3c4d-4e8f-9a1b-2c3d4e5f6a7b")


async def turn_options(
    options: ClaudeAgentOptions,
    conversation_id: str,
    user_id: str | None = None,
) -> ClaudeAgentOptions:
    """`options` for one turn of a served agent.

    Starts or resumes the session for `conversation_id`, scoped to `user_id`.
    Defaults apply only where `root_agent` leaves a field unset.
    """
    session_store = _session_store(options)
    session_id = session_id_for(conversation_id)
    cwd = Path(options.cwd) if options.cwd else _workspace_for(session_id, user_id)
    session_fields = await _session_fields(session_store, session_id, cwd)

    # No await from here until stream_turn claims the workspace, so it can't be
    # evicted first.
    cwd.mkdir(parents=True, exist_ok=True)

    setting_sources = _setting_sources(options, cwd)
    return dataclasses.replace(
        options,
        include_partial_messages=True,
        # Remote prompts must not read local files ("@/path") or run slash commands.
        verbatim_prompts=True,
        cwd=str(cwd),
        setting_sources=setting_sources,
        allowed_tools=list(
            dict.fromkeys(
                [*options.allowed_tools, *settings_allow_rules(setting_sources)]
            )
        ),
        session_store=session_store,
        # No one can answer permission prompts on a server: deny tools not in
        # allowed_tools, unless the agent has its own can_use_tool callback.
        permission_mode=options.permission_mode
        or (None if options.can_use_tool else "dontAsk"),
        env={
            # Auto memory loads even with setting_sources=[].
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            # Behind ToolSearch, the model often misses the tool it needs.
            "ENABLE_TOOL_SEARCH": "false",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            # The CLI and every tool it runs inherit the server's environment;
            # the Cloud SQL password is read by the server, not them.
            "DB_PASS": "",
            **(
                {"CLAUDE_CODE_SIMPLE": "1"}
                if _is_bare(options, setting_sources)
                else {}
            ),
            **cli_telemetry_env(),
            **options.env,
            **_traffic_tag_env(options.env),
            # After options.env, so model calls always go to Gemini platform.
            "CLAUDE_CODE_USE_VERTEX": "1",
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_AUTH_TOKEN": "",
        },
        stderr=options.stderr or _log_cli_stderr,
        **session_fields,
    )


def session_id_for(conversation_id: str) -> str:
    """The Claude session id for a conversation; a canonical UUID is used as is."""
    try:
        if str(uuid.UUID(conversation_id)) == conversation_id:
            return conversation_id
    except ValueError:
        pass
    return str(uuid.uuid5(_SESSION_NAMESPACE, conversation_id))


def _session_store(options: ClaudeAgentOptions) -> Any:
    """The agent's own store, else the Cloud SQL store when it is configured."""
    if options.session_store is not None:
        return options.session_store
    return _cloud_sql_store()


@functools.cache
def _cloud_sql_store() -> Any:
    """The Cloud SQL session store, or None when it isn't configured."""
    from app.app_utils.session_store import PostgresSessionStore

    return PostgresSessionStore.from_env()


def _workspace_for(session_id: str, user_id: str | None) -> Path:
    """The conversation's workspace."""
    if user_id is not None:
        # A workspace per (caller, conversation) keeps callers apart. Hex, so
        # no conversation id can collide with a caller's workspace name.
        name = uuid.uuid5(_SESSION_NAMESPACE, f"{session_id}{user_id}").hex
    else:
        name = session_id
    return workspaces.WORKSPACES / settings.AGENT_NAME / name


async def _session_fields(
    session_store: Any, session_id: str, cwd: Path
) -> dict[str, Any]:
    """The options that resume the session if it has a transcript, else start it."""
    if await _session_exists(session_store, session_id, cwd):
        return {"resume": session_id}
    return {"session_id": session_id}


async def _session_exists(session_store: Any, session_id: str, cwd: Path) -> bool:
    """Whether the session has a transcript to resume, without reading it."""
    transcript = workspaces.transcripts_dir(cwd) / f"{session_id}.jsonl"
    if transcript.is_file() and transcript.stat().st_size > 0:
        return True
    if session_store is None:
        return False
    key = {
        "project_key": claude_agent_sdk.project_key_for_directory(cwd),
        "session_id": session_id,
    }
    if hasattr(session_store, "exists"):
        return await session_store.exists(key)
    return await session_store.load(key) is not None


def _setting_sources(options: ClaudeAgentOptions, cwd: Path) -> list[str]:
    """The setting sources the CLI loads, after copying project settings to `cwd`."""
    setting_sources = options.setting_sources
    wants_project = setting_sources is None or bool(
        {"project", "local"} & set(setting_sources)
    )
    copied = copy_project_settings(cwd) if wants_project else False
    if setting_sources is None:
        # Unset, the CLI would also load parent directories and ~/.claude.
        return ["project"] if copied else []
    return setting_sources


def _is_bare(options: ClaudeAgentOptions, setting_sources: list[str]) -> bool:
    """Whether the turn can run in the CLI's bare mode.

    Bare mode drops the CLI's per-prompt reminders, which can make the model
    treat user instructions as prompt injection. It also disables settings,
    skills, plugins and hooks.
    """
    return (
        options.tools == []
        and not setting_sources
        and options.skills is None
        and not options.plugins
        and not options.hooks
    )


def _traffic_tag_env(env: Mapping[str, str]) -> dict[str, str]:
    """Adds the agents-cli tag to the headers the CLI sends on model calls.

    Gemini platform attributes model calls to a tool by their x-goog-api-client
    header, and the Anthropic client sends none. Headers the agent or the
    server already sets are kept.
    """
    # The CLI's own environment: the agent's env wins over the server's.
    cli_env = {**os.environ, **env}
    opt_out = cli_env.get("AGENTS_CLI_TELEMETRY", "").strip().lower()
    if cli_env.get("DO_NOT_TRACK") or opt_out in ("0", "false", "no", "off"):
        return {}
    tag = (
        "x-goog-api-client: "
        f"agents-cli-app/{settings.AGENTS_CLI_VERSION}+claude-agent-sdk"
    )
    own = cli_env.get("ANTHROPIC_CUSTOM_HEADERS")
    return {"ANTHROPIC_CUSTOM_HEADERS": f"{own}\n{tag}" if own else tag}


def _log_cli_stderr(line: str) -> None:
    logging.getLogger("claude_cli").warning(line.rstrip())
