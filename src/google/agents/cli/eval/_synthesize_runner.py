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

"""Subprocess runner for ``agents-cli eval dataset synthesize``.

Loads the user's ADK agent and generates synthetic evaluation traces
by running the agent against a user simulator.

This file is added into the user's agent project by ``agents-cli eval
dataset synthesize`` and is only intended to be invoked by that command.
Do not modify it — edits will be lost.

``GOOGLE_CLOUD_PROJECT`` and ``GOOGLE_CLOUD_LOCATION`` are loaded from the
agent's ``.env`` (see ``_resolve_gcp_env``) and consumed by ``vertexai.Client``.
"""

import asyncio
import datetime
import json
import os
import sys
import traceback
import uuid
from pathlib import Path

import vertexai
from google.adk.cli.utils.agent_loader import AgentLoader
from google.adk.evaluation.conversation_scenarios import (
    ConversationScenario,
)
from google.adk.evaluation.eval_case import (
    SessionInput as ADK_SessionInput,
)
from google.adk.evaluation.evaluation_generator import (
    EvaluationGenerator,
)
from google.adk.evaluation.simulation.llm_backed_user_simulator import (
    LlmBackedUserSimulator,
    LlmBackedUserSimulatorConfig,
)
from google.genai import types as genai_types
from vertexai import types


def _safe_tool_declarations(agent):
    """Return eval tool declarations for ``agent``, skipping what can't be introspected.

    A drop-in for the Vertex eval SDK's ``_get_tool_declarations_from_agent``
    with two guards so building eval metadata never crashes the runner:

    * a missing ``tools`` attribute (workflow agents like ``SequentialAgent``)
      yields no declarations instead of ``AttributeError``;
    * entries that aren't introspectable callables (ADK toolsets, e.g. an MCP
      toolset) are skipped instead of raising ``TypeError``.

    Skipped toolsets stay on the live agent, so their tool calls still run and
    show up in the resulting traces.
    """
    from google.genai import types as genai_types

    declarations = []
    for tool in getattr(agent, "tools", []) or []:
        get_decl = getattr(tool, "_get_declaration", None)
        if callable(get_decl):
            decl = get_decl()
            if decl is not None:
                declarations.append({"function_declarations": [decl]})
            continue
        try:
            decl = genai_types.FunctionDeclaration.from_callable_with_api_option(
                callable=tool
            )
            declarations.append({"function_declarations": [decl]})
        except Exception:
            continue  # toolsets aren't introspectable here; the live run keeps them
    return declarations


def _patch_eval_tool_introspection():
    """Make the eval SDK tolerate ADK toolsets / tool-less workflow agents.

    ``AgentInfo.load_from_agent`` (-> ``get_agents_map`` -> ``from_agent``)
    crashes in the SDK's shared ``_get_tool_declarations_from_agent``.
    Best-effort: a no-op if the SDK layout changes. See
    https://github.com/googleapis/python-aiplatform/issues/6865.
    """
    try:
        from vertexai._genai.types.evals import AgentConfig
    except Exception:
        return
    AgentConfig._get_tool_declarations_from_agent = staticmethod(  # ty: ignore[invalid-assignment]
        _safe_tool_declarations
    )


def _final_response_from_invocations(invocations):
    """Extract the final agent text response across all invocations.

    Walks invocations in reverse to find the most recent ``final_response``
    that contains a non-empty ``text`` part. Returns a ``ResponseCandidate``
    wrapping a ``Content`` object suitable for ``EvalCase.responses[0]``,
    or ``None`` if no text was found.

    Ensures custom metric handlers (LLMMetric and custom_function) can
    read ``instance.response`` without erroring on missing response content.
    """
    for invocation in reversed(invocations):
        final = getattr(invocation, "final_response", None)
        if not final:
            continue
        parts = getattr(final, "parts", None) or []
        texts = [getattr(p, "text", None) for p in parts]
        texts = [t for t in texts if t]
        if texts:
            return types.ResponseCandidate(
                response=genai_types.Content(
                    role=getattr(final, "role", None) or "model",
                    parts=[genai_types.Part(text="".join(texts))],
                )
            )
    return None


def _invocations_to_turns(invocations):
    """Converts ADK ``Invocation`` objects to trace turn dicts."""
    turns = []
    for i, invocation in enumerate(invocations):
        events = []
        ts = datetime.datetime.fromtimestamp(
            invocation.creation_timestamp, tz=datetime.UTC
        )

        if invocation.user_content:
            events.append(
                {
                    "author": "user",
                    "content": invocation.user_content.model_dump(
                        mode="json", exclude_none=True
                    ),
                    "event_time": ts,
                }
            )

        intermediate = invocation.intermediate_data
        if intermediate is not None:
            inv_events = getattr(intermediate, "invocation_events", None)
            tool_uses = getattr(intermediate, "tool_uses", None)
            if inv_events:
                for ie in inv_events:
                    events.append(
                        {
                            "author": ie.author,
                            "content": (
                                ie.content.model_dump(mode="json", exclude_none=True)
                                if ie.content
                                else None
                            ),
                            "event_time": ts,
                        }
                    )
            elif tool_uses:
                for tool_call in tool_uses:
                    events.append(
                        {
                            "author": "tool_call",
                            "content": tool_call.model_dump(
                                mode="json", exclude_none=True
                            ),
                            "event_time": ts,
                        }
                    )

        if invocation.final_response:
            events.append(
                {
                    "author": "agent",
                    "content": invocation.final_response.model_dump(
                        mode="json", exclude_none=True
                    ),
                    "event_time": ts,
                }
            )

        turns.append(
            {
                "turn_index": i,
                "turn_id": invocation.invocation_id or str(uuid.uuid4()),
                "events": events,
            }
        )
    return turns


def _find_project_dotenv(agent_dir):
    """Return the nearest ``.env`` at or above ``agent_dir``, or ``None``.

    Mirrors ADK's ``load_dotenv_for_agent`` walk-up so eval loads the same
    file the agent uses at runtime.
    """
    start = Path(agent_dir).resolve()
    for folder in (start, *start.parents):
        candidate = folder / ".env"
        if candidate.is_file():
            return candidate
    return None


def _load_agent_dotenv(agent_dir):
    """Load the agent project's *entire* ``.env`` into ``os.environ``.

    Eval runs the user's agent in this subprocess, so it must see the same
    environment the agent uses at runtime -- *every* ``.env`` var (model
    config, ``GOOGLE_CLOUD_*``, ``GEMINI_API_KEY``, app-specific settings),
    not just a chosen few. Pre-existing OS env vars win over ``.env``
    (``override=False``), matching ADK's ``load_dotenv_for_agent``.
    """
    from dotenv import load_dotenv

    dotenv_path = _find_project_dotenv(agent_dir)
    if dotenv_path:
        load_dotenv(dotenv_path)


def main():
    agent_dir = sys.argv[1]
    config_json = sys.argv[2]
    output_path = sys.argv[3]
    max_turns = int(sys.argv[4])

    config = json.loads(config_json)

    # Load the agent's full .env (all vars, not just GOOGLE_CLOUD_*) so the
    # agent and eval client see the same environment it uses at runtime.
    _load_agent_dotenv(agent_dir)
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or None
    location = os.environ.get("GOOGLE_CLOUD_LOCATION") or None
    # Only meaningful on Vertex AI; with an API key (AI Studio) both are unset.
    if project or location:
        print(f"[synthesize] project={project} location={location}", flush=True)

    resolved = Path(agent_dir).resolve()
    loader = AgentLoader(agents_dir=str(resolved.parent))
    loaded = loader.load_agent(resolved.name)

    try:
        from google.adk.apps import App

        if isinstance(loaded, App):
            agent = loaded.root_agent
        else:
            agent = loaded
    except ImportError:
        agent = loaded

    _patch_eval_tool_introspection()
    agent_info = types.evals.AgentInfo.load_from_agent(agent=agent)

    client = vertexai.Client(project=project, location=location)
    print(
        f"Calling Vertex AI generate_conversation_scenarios "
        f"(project={project}, location={location})...",
        flush=True,
    )
    eval_dataset = client.evals.generate_conversation_scenarios(
        agent_info=agent_info,
        config=config,
        allow_cross_region_model=True,
    )
    n_cases = len(eval_dataset.eval_cases or [])
    print(f"Got {n_cases} scenarios; running user simulations...", flush=True)

    async def _run_simulation():
        eval_cases = []
        failures = 0
        for case in eval_dataset.eval_cases or []:
            scenario = case.user_scenario
            if not scenario:
                continue
            conv = ConversationScenario(
                starting_prompt=scenario.starting_prompt,
                conversation_plan=scenario.conversation_plan,
            )
            sim_cfg = LlmBackedUserSimulatorConfig(
                max_allowed_invocations=max_turns,
            )
            sim = LlmBackedUserSimulator(
                conversation_scenario=conv,
                config=sim_cfg,
            )
            try:
                invocations = await (
                    EvaluationGenerator._generate_inferences_from_root_agent(
                        root_agent=agent,
                        user_simulator=sim,
                        reset_func=getattr(agent, "reset_data", None),
                        initial_session=ADK_SessionInput(
                            app_name="user_simulation_app",
                            user_id="user_simulation_default_user",
                            state={},
                        ),
                    )
                )
            except Exception as exc:
                failures += 1
                print(
                    f"Warning: simulation failed for scenario: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                traceback.print_exc(file=sys.stderr)
                invocations = []

            turns = _invocations_to_turns(invocations)
            agent_data = types.evals.AgentData(
                turns=[types.evals.ConversationTurn(**t) for t in turns]
            )
            existing_id = getattr(case, "eval_case_id", None)
            final_response = _final_response_from_invocations(invocations)
            responses = [final_response] if final_response is not None else None
            eval_case = types.EvalCase(
                eval_case_id=existing_id or str(uuid.uuid4()),
                user_scenario=types.evals.UserScenario(
                    starting_prompt=scenario.starting_prompt,
                    conversation_plan=scenario.conversation_plan,
                ),
                agent_data=agent_data,
                responses=responses,
            )
            eval_cases.append(eval_case)
        return eval_cases, failures

    eval_cases, failures = asyncio.run(_run_simulation())

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    output_dataset = types.EvaluationDataset(eval_cases=eval_cases)
    out_path.write_text(
        output_dataset.model_dump_json(
            indent=2,
            exclude_none=True,
            by_alias=False,
        ),
        encoding="utf-8",
    )
    if failures:
        print(
            f"Warning: {failures}/{n_cases} scenarios failed during "
            "simulation; their agent_data will have empty turns.",
            file=sys.stderr,
            flush=True,
        )


if __name__ == "__main__":
    main()
    os._exit(0)
