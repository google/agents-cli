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

"""Subprocess runner for ``agents-cli eval generate``.

Loads the user's ADK agent and runs inference over each eval case in a
dataset, producing populated traces ready for scoring.

This file is staged into the user's agent project by ``agents-cli eval
generate`` and is only intended to be invoked by that command. It is
overwritten on each run, so do not modify it — edits will be lost.

``GOOGLE_CLOUD_PROJECT`` and ``GOOGLE_CLOUD_LOCATION`` are loaded from the
agent's ``.env`` (see ``_resolve_gcp_env``) and consumed by ``vertexai.Client``.

Failure handling contract (so downstream ``agents-cli eval grade`` never
sees a corrupt artifact):

  * All cases succeed -> write artifact, exit 0.
  * Some cases succeed -> write artifact with ONLY successful cases,
    print a partial-success summary to stderr, exit 0.
  * Zero cases succeed -> do NOT write any artifact, print a failure
    summary to stderr, exit 1.
"""

import copy
import json
import os
import sys
import traceback
from pathlib import Path


def _unwrap_agent(loaded):
    """Return the root agent if ``loaded`` is an ADK ``App`` wrapper."""
    try:
        from google.adk.apps import App

        if isinstance(loaded, App):
            return loaded.root_agent
    except ImportError:
        pass
    return loaded


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

    Both ``run_inference`` and ``eval dataset synthesize`` crash in the SDK's
    shared ``_get_tool_declarations_from_agent``. Best-effort: a no-op if the
    SDK layout changes. See
    https://github.com/googleapis/python-aiplatform/issues/6865.
    """
    try:
        from vertexai._genai.types.evals import AgentConfig
    except Exception:
        return
    AgentConfig._get_tool_declarations_from_agent = staticmethod(  # ty: ignore[invalid-assignment]
        _safe_tool_declarations
    )


def _load_fresh_agent(agents_dir, agent_name):
    """Load an agent from disk, bypassing AgentLoader's in-process cache.

    A fresh load is required between cases so the agent's services
    (session, memory, etc.) construct new ``asyncio.Lock`` objects bound
    to the next event loop.
    """
    from google.adk.cli.utils.agent_loader import AgentLoader

    loader = AgentLoader(agents_dir=agents_dir)
    loader.remove_agent_from_cache(agent_name)
    return _unwrap_agent(loader.load_agent(agent_name))


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


def _normalize_agent_data(case_dict, root_agent_name):
    """Rewrite ``author == "model"`` events to use the root agent's name."""
    agent_data = case_dict.get("agent_data") or {}
    for turn in agent_data.get("turns", []) or []:
        for event in turn.get("events", []) or []:
            if event.get("author") == "model":
                event["author"] = root_agent_name
    return case_dict


def _strip_thought_signatures(events):
    """Remove ``thought_signature`` keys from event content parts."""
    for event in events:
        content = event.get("content") or {}
        for part in content.get("parts") or []:
            part.pop("thought_signature", None)
    return events


def _agent_data_from_partial(partial):
    """Return the SDK's per-case ``agent_data`` as a plain dict, or None."""
    df = getattr(partial, "eval_dataset_df", None)
    if df is None or df.empty:
        return None
    agent_data = df.iloc[0].to_dict().get("agent_data")
    if not agent_data:
        return None
    if hasattr(agent_data, "model_dump"):
        agent_data = agent_data.model_dump(exclude_unset=True)
    return agent_data


def _extract_new_events_from_partial(partial):
    """Extract just the new agent-produced events from a per-case run_inference result.

    The SDK returns its result as ``EvaluationDataset(eval_dataset_df=df)``
    where the row's ``agent_data.turns[0].events`` looks like
    ``[user_query_event, agent_event_1, agent_event_2, ...]`` — the user
    query is the prompt we already had in the input. This helper returns
    ``[agent_event_1, agent_event_2, ...]`` (the user event is dropped) with
    Gemini's internal ``thought_signature`` fields stripped from each part.

    Returns an empty list if the SDK output has no usable agent data.
    """
    agent_data = _agent_data_from_partial(partial)
    if not agent_data:
        return []
    turns = agent_data.get("turns") or []
    if not turns:
        return []
    events = list(turns[0].get("events") or [])
    if events and events[0].get("author") == "user":
        events = events[1:]
    return _strip_thought_signatures(events)


def _final_response_content_from_events(events):
    """Extract the final agent text response from a list of events.

    Walks ``events`` in reverse looking for the most recent event whose first
    text-bearing part has a non-empty ``text``. Returns a ``Content``-shaped
    dict ``{"role": "model", "parts": [{"text": ...}]}`` suitable for
    ``EvalCase.responses[i].response``, or ``None`` if no text was found.
    """
    for event in reversed(events):
        content = event.get("content") or {}
        parts = content.get("parts") or []
        texts = [p.get("text") for p in parts if p.get("text")]
        if texts:
            return {
                "role": content.get("role") or "model",
                "parts": [{"text": "".join(texts)}],
            }
    return None


def _merge_partial_into_case(original_case, partial):
    """Merge new agent events from ``partial`` into ``original_case``.

    Preserves all prior turns/history from the input case and appends the
    new agent events to the last turn (the one ending with the user query
    that this run answered). If the input case has no turns, the new events
    are added as a fresh turn 0.

    Also overwrites the case's ``agent_data.agents`` map with the SDK's
    fully-populated version (which includes tool declarations, sub-agents,
    descriptions, etc., introspected from the loaded ADK agent).

    Finally, populates ``responses`` with the final agent text wrapped in a
    single ``ResponseCandidate``, so SDK metric handlers (LLMMetric and
    custom_function alike) can read ``instance.response`` without erroring
    on ``Response content missing``. If the agent produced no text (tool
    calls only, etc.), ``responses`` is left untouched.
    """
    merged = copy.deepcopy(original_case)
    partial_agent_data = _agent_data_from_partial(partial) or {}

    agent_data = merged.setdefault("agent_data", {})

    sdk_agents = partial_agent_data.get("agents")
    if sdk_agents:
        agent_data["agents"] = sdk_agents

    new_events = _extract_new_events_from_partial(partial)
    if not new_events:
        return merged

    turns = agent_data.setdefault("turns", [])
    if turns:
        last_turn = turns[-1]
        last_turn.setdefault("events", []).extend(new_events)
    else:
        turns.append(
            {
                "turn_index": 0,
                "turn_id": "turn_0",
                "events": new_events,
            }
        )

    final_response = _final_response_content_from_events(new_events)
    if final_response is not None:
        merged.setdefault("responses", []).append({"response": final_response})

    return merged


def _format_failure_summary(failures, n_cases, n_succeeded):
    """Render a human-readable summary of per-case failures."""
    lines = [
        "",
        f"[generate] Inference summary: {n_succeeded}/{n_cases} succeeded, "
        f"{len(failures)} failed.",
        "[generate] Failed cases:",
    ]
    for case_index, err in failures:
        lines.append(f"  - case[{case_index}]: {err}")
    return "\n".join(lines)


def main(argv=None):
    """Entry point.

    Args:
        argv: Optional list of CLI args (excluding program name) for tests.
            Defaults to ``sys.argv[1:]``.

    Returns:
        Exit code (0 on success / partial success, 1 if zero cases succeeded).
    """
    import vertexai
    from vertexai import types

    _patch_eval_tool_introspection()

    argv = list(sys.argv[1:] if argv is None else argv)
    agent_dir = argv[0]
    dataset_path = argv[1]
    output_path = argv[2]

    # Load the agent's own .env (all of it) BEFORE importing the agent module
    # so eval sees the same environment the agent uses at runtime -- model
    # config, GOOGLE_CLOUD_*, GEMINI_API_KEY, app-specific vars. Reading
    # project/location before import also avoids agent modules that mutate
    # os.environ at import time clobbering the values used for the eval client.
    _load_agent_dotenv(agent_dir)
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or None
    location = os.environ.get("GOOGLE_CLOUD_LOCATION") or None
    # Only meaningful on Vertex AI; with an API key (AI Studio) both are unset.
    if project or location:
        print(f"[generate] project={project} location={location}", flush=True)

    print(f"[generate] loading dataset from {dataset_path}", flush=True)
    raw = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    cases = raw.get("eval_cases") or []
    n_cases = len(cases)
    print(f"[generate] loaded {n_cases} eval case(s)", flush=True)

    resolved = Path(agent_dir).resolve()
    agents_dir = str(resolved.parent)
    agent_name = resolved.name

    # Probe-load the agent once just to read the root agent's name (needed by
    # _normalize_agent_data). Discard immediately so its locks don't outlive
    # the current event loop.
    probe_agent = _load_fresh_agent(agents_dir, agent_name)
    root_agent_name = getattr(probe_agent, "name", None) or "root_agent"
    print(f"[generate] root_agent_name={root_agent_name}", flush=True)
    del probe_agent

    client = vertexai.Client(project=project, location=location)

    merged_cases = []
    failures: list[tuple[int, str]] = []
    for i, case in enumerate(cases):
        print(f"[generate] inference {i + 1}/{n_cases}", flush=True)
        case = _normalize_agent_data(case, root_agent_name)
        agent = _load_fresh_agent(agents_dir, agent_name)
        single = types.EvaluationDataset(eval_cases=[types.EvalCase.model_validate(case)])
        try:
            partial = client.evals.run_inference(src=single, agent=agent)
        except Exception as exc:
            failures.append((i, f"{type(exc).__name__}: {exc}"))
            print(
                f"[generate] inference {i + 1} FAILED: {exc}",
                flush=True,
            )
            traceback.print_exc()
            continue

        new_events = _extract_new_events_from_partial(partial)
        if not new_events:
            err = (
                "Inference returned no agent events. The agent's underlying "
                "calls may have failed silently (check stderr above for "
                "retried errors like PERMISSION_DENIED, NOT_FOUND, or quota)."
            )
            failures.append((i, err))
            print(f"[generate] inference {i + 1} FAILED: {err}", flush=True)
            continue

        merged_case_dict = _merge_partial_into_case(case, partial)
        merged_cases.append(types.EvalCase.model_validate(merged_case_dict))
        print(f"[generate] inference {i + 1} done", flush=True)

    n_succeeded = len(merged_cases)

    if n_succeeded == 0:
        summary = _format_failure_summary(failures, n_cases, n_succeeded)
        print(summary, file=sys.stderr, flush=True)
        print(
            f"[generate] No artifact written: 0 of {n_cases} cases produced output.",
            file=sys.stderr,
            flush=True,
        )
        return 1

    result = types.EvaluationDataset(eval_cases=merged_cases)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(
        result.model_dump_json(indent=2, exclude_none=True),
        encoding="utf-8",
    )
    print(f"[generate] wrote {output_path}", flush=True)

    if failures:
        summary = _format_failure_summary(failures, n_cases, n_succeeded)
        print(summary, file=sys.stderr, flush=True)
        print(
            f"[generate] Artifact contains only the {n_succeeded} successful "
            f"case(s); {len(failures)} failed case(s) were dropped.",
            file=sys.stderr,
            flush=True,
        )

    return 0


if __name__ == "__main__":
    exit_code = main()
    os._exit(exit_code)
