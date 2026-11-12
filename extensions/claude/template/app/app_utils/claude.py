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

"""Run `root_agent` through the Claude Agent SDK.

`stream_turn` runs one turn and yields every SDK message; `run_turn` runs one
to completion. The helpers they use live in:

- `turn_options`: the turn's session, workspace and server defaults.
- `workspaces`: per-conversation working directories and their eviction.
- `project_settings`: the served agent's CLAUDE.md, .claude/ and .mcp.json,
  from agent_config/.
- `replies`: reading the reply and tool calls out of the messages.
- `conversation`: the running turn's conversation and user, for tools to key
  state on.
- `memory_bank`: the user's memories, recalled into the turn and learned from it.
- `turn_events`: parsing the turn's messages into events, for the observers below.
- `bq_analytics`, `completions`, `telemetry`: the turn's BigQuery rows, prompt
  logs, subagent spans and call-count metrics.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import shutil
import weakref
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import ExitStack, aclosing
from pathlib import Path
from typing import Protocol

import anyio
import claude_agent_sdk
from claude_agent_sdk import ClaudeAgentOptions, Message, MirrorErrorMessage
from dotenv import load_dotenv
from opentelemetry import trace

from app.app_utils import bq_analytics, completions, memory_bank, project_settings
from app.app_utils.conversation import current_conversation, current_user
from app.app_utils.replies import AgentError, TurnReply, record
from app.app_utils.telemetry.call_counts import CallCounts
from app.app_utils.telemetry.config import agent_name
from app.app_utils.telemetry.subagent_spans import (
    SubagentSpans,
    current_subagent_spans,
)
from app.app_utils.telemetry.traces import system_prompt_attributes
from app.app_utils.turn_events import Event, TurnParser
from app.app_utils.turn_options import turn_options
from app.app_utils.workspaces import claim_workspace, release_workspace

__all__ = [
    "AgentError",
    "TurnReply",
    "configure_model_access",
    "current_conversation",
    "current_user",
    "run_turn",
    "stream_turn",
]

# Serializes turns of one conversation: two at once would fork its transcript.
_turn_locks: weakref.WeakValueDictionary[str, anyio.Lock] = (
    weakref.WeakValueDictionary()
)


async def stream_turn(
    prompt: str,
    options: ClaudeAgentOptions,
    conversation_id: str,
    user_id: str | None = None,
) -> AsyncIterator[Message]:
    """Yield every SDK message for one turn of the agent.

    Args:
        user_id: The caller; scopes the conversation and memories to them.
    """
    configure_model_access()
    if options.cli_path is None:
        # The CLI bundled with the SDK always matches it, so skip the `claude -v`
        # subprocess the SDK otherwise runs before every turn. The SDK reads
        # this from the server's environment, not options.env.
        os.environ.setdefault("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    _check_mcp_commands(options)
    _check_model(options)
    scoped_key = f"{user_id}\x00{conversation_id}" if user_id else conversation_id
    memory_user = user_id or f"A2A_USER_{conversation_id}"
    # An agent's own cwd is not a workspace: it is never claimed or evicted.
    kept = not options.cwd
    async with _turn_locks.setdefault(scoped_key, anyio.Lock()):
        # Before turn_options: nothing may await between it and claim_workspace.
        system_prompt = await memory_bank.recall(
            options.system_prompt, prompt, memory_user
        )
        options = await turn_options(options, conversation_id, user_id)
        # Observers log the memories too; AGENT_STARTING only the agent's own prompt.
        options_with_memories = dataclasses.replace(
            options, system_prompt=system_prompt
        )
        with ExitStack() as cleanup:
            # Claimed before the next await, as turn_options requires, so the
            # workspace can't be evicted first.
            workspace = Path(options.cwd)
            if kept:
                claim_workspace(workspace)
                cleanup.callback(release_workspace, workspace)
            cleanup.callback(
                current_conversation.reset, current_conversation.set(scoped_key)
            )
            cleanup.callback(current_user.reset, current_user.set(memory_user))
            parser = TurnParser(main_agent=agent_name())
            subagent_spans = SubagentSpans(parser.agent_name)
            cleanup.callback(
                current_subagent_spans.reset, current_subagent_spans.set(subagent_spans)
            )
            session_id = options.resume or options.session_id or conversation_id
            observers: list[_TurnObserver | None] = [
                bq_analytics.start_turn(
                    prompt,
                    options_with_memories,
                    session_id,
                    conversation_id,
                    user_id,
                    parser.agent_name,
                    instruction=options.system_prompt,
                ),
                completions.start_turn(
                    prompt,
                    options_with_memories,
                    session_id,
                    parser.agent_name,
                    subagent_spans,
                ),
                CallCounts(parser.agent_name),
                memory_bank.start_turn(prompt, session_id, memory_user),
            ]
            consumers = [c for c in observers if c is not None]
            error: BaseException | None = None
            store_error: MirrorErrorMessage | None = None
            span_attributes = system_prompt_attributes(
                options_with_memories.system_prompt
            )
            try:
                # Call claude_agent_sdk.query via the module so the OpenTelemetry
                # wrapper applies. aclosing stops the CLI subprocess when a turn
                # ends early.
                async with aclosing(
                    claude_agent_sdk.query(prompt=prompt, options=options_with_memories)
                ) as stream:
                    async for message in stream:
                        if span_attributes:
                            # OpenInference's turn span, current once query()
                            # starts; its tool spans never are.
                            trace.get_current_span().set_attributes(span_attributes)
                            span_attributes = {}
                        if isinstance(message, MirrorErrorMessage):
                            store_error = store_error or message
                            continue
                        events = parser.feed(message)
                        subagent_spans.observe(message)
                        _dispatch(events, consumers)
                        yield message
                if store_error is not None:
                    # The SDK carries on after a failed session store write, but
                    # the store is now missing part of this turn, and every
                    # later turn resumes from the store. Raised only once the
                    # turn ends, so its result still reaches the observers.
                    raise AgentError(
                        "Claude agent turn failed: couldn't save its "
                        f"transcript to the session store: {store_error.error}"
                    )
            except BaseException as e:
                error = e
                raise
            finally:
                _dispatch(parser.finish(error), consumers)


async def run_turn(
    prompt: str, options: ClaudeAgentOptions, conversation_id: str
) -> TurnReply:
    """Run one turn to completion and return what it produced."""
    reply = TurnReply()
    async with aclosing(stream_turn(prompt, options, conversation_id)) as stream:
        async for message in stream:
            reply = record(reply, message)
    # The caller's process may end next, which would drop the ingest.
    await memory_bank.flush()
    return reply


def configure_model_access() -> None:
    """Point the Claude Code CLI at Claude on Gemini platform using ADC.

    The region and project in the environment or `.env` win.
    """
    load_dotenv()
    os.environ.setdefault("CLAUDE_CODE_USE_VERTEX", "1")
    os.environ.setdefault("CLOUD_ML_REGION", "global")
    if os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID"):
        return
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        import google.auth

        _, project = google.auth.default()
    if project:
        os.environ["ANTHROPIC_VERTEX_PROJECT_ID"] = project


def _check_mcp_commands(options: ClaudeAgentOptions) -> None:
    """Fail early, and say why, when a stdio MCP server's command is missing."""
    servers = dict(options.mcp_servers) if isinstance(options.mcp_servers, dict) else {}
    mcp_json = project_settings.AGENT_CONFIG_DIR / ".mcp.json"
    sources = options.setting_sources
    if (sources is None or "project" in sources) and mcp_json.is_file():
        try:
            project_servers = json.loads(mcp_json.read_text()).get("mcpServers")
        except (ValueError, AttributeError) as err:
            raise AgentError(
                f"agent_config/.mcp.json is not a JSON object: {err}"
            ) from err
        if isinstance(project_servers, dict):
            servers |= project_servers
    for name, config in servers.items():
        command = config.get("command") if isinstance(config, dict) else None
        # shortcut: a command using ${VAR} expansion is left for the CLI to
        # resolve, unchecked; expand it here if those need checking too.
        if command and "$" not in command and shutil.which(command) is None:
            raise AgentError(
                f"MCP server {name!r} runs {command!r}, which is not installed here. "
                "To deploy it, install it in the Dockerfile (for npx-based "
                "servers: RUN apt-get update && apt-get install -y nodejs npm)."
            )


def _check_model(options: ClaudeAgentOptions) -> None:
    """Fail early when no model is set.

    The CLI's own default model is rarely enabled in Model Garden.
    """
    env = {**os.environ, **options.env}
    if options.model or env.get("ANTHROPIC_MODEL"):
        return
    # shortcut: settings files and `settings` may name the model too; skip the
    # check whenever the CLI could load them rather than parse them here.
    if options.settings or options.setting_sources:
        return
    if (
        options.setting_sources is None
        and (project_settings.AGENT_CONFIG_DIR / ".claude").is_dir()
    ):
        return
    raise AgentError(
        "No model is set, so the Claude Code CLI would use its own default, which "
        "is usually not enabled in Model Garden. Set model= in root_agent "
        "(app/agent.py) to a Claude model enabled for the project, e.g. "
        'model="claude-haiku-4-5".'
    )


class _TurnObserver(Protocol):
    def handle(self, event: Event) -> None: ...


def _dispatch(events: Iterable[Event], consumers: Sequence[_TurnObserver]) -> None:
    """Hand each parsed turn event to every consumer, in order.

    A consumer's failure is logged, never raised: it would fail the turn, or
    replace the turn's own exception when the turn ends with one.
    """
    for event in events:
        for consumer in consumers:
            try:
                consumer.handle(event)
            except Exception:
                logging.warning(
                    "%s failed on %s",
                    type(consumer).__name__,
                    type(event).__name__,
                    exc_info=True,
                )
