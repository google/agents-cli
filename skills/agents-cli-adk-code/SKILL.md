---
name: agents-cli-adk-code
description: >
  This skill should be used when the user wants to "write agent code",
  "add a tool", "create a callback", "define an agent", "use state management",
  or needs ADK Python API patterns and code examples.
  It provides a quick reference for agent types, tool definitions, orchestration
  patterns, callbacks, and state management.
  Do NOT use for creating new projects (use agents-cli-scaffold) or deployment
  (use agents-cli-deploy).
metadata:
  author: Google
  license: Apache-2.0
  version: 0.0.3
  requires:
    bins:
      - agents-cli
    install: "uv tool install agents-cli"
---

# ADK Cheatsheet

> **Python only for now.** This cheatsheet currently covers the Python ADK SDK.
> Support for other languages is coming soon.

## Quick Reference — Most Common Patterns

### Agent Creation

```python
from google.adk.agents import Agent

root_agent = Agent(
    name="my_agent",
    model="gemini-3-flash-preview",
    instruction="You are a helpful assistant that ...",
    tools=[my_tool],
)
```

### Basic Tool

```python
from google.adk.tools import FunctionTool

def get_weather(city: str) -> dict:
    """Get current weather for a city."""
    return {"city": city, "temp": "22°C", "condition": "sunny"}

weather_tool = FunctionTool(func=get_weather)
```

### Simple Callback

```python
from google.adk.agents.callback_context import CallbackContext

async def initialize_state(callback_context: CallbackContext) -> None:
    state = callback_context.state
    if "history" not in state:
        state["history"] = []

root_agent = Agent(
    name="my_agent",
    model="gemini-3-flash-preview",
    instruction="...",
    before_agent_callback=initialize_state,
)
```

---

## Reference Files

| File | Contents |
|------|----------|
| `references/adk-python.md` | Python ADK API quick reference — agents, tools, auth, orchestration, callbacks, plugins, state, artifacts, context caching/compaction, session rewind |
| `references/reference-implementations.md` | Production patterns from ADK samples — code executors, planners, grounding metadata, advanced callbacks |
Read `references/adk-python.md` for the full API quick reference.

For the ADK docs index (titles and URLs for fetching documentation pages), use `curl https://google.github.io/adk-docs/llms.txt`.

> **Creating a new agent project?** Use `/agents-cli-scaffold` instead — this skill is for writing code in existing projects.

---

## Related Skills

- `/agents-cli-workflow` — Development workflow, coding guidelines, and operational rules
- `/agents-cli-scaffold` — Project creation and enhancement with `agents-cli init` / `enhance`
- `/agents-cli-eval` — Evaluation methodology, evalset schema, and the eval-fix loop
- `/agents-cli-deploy` — Deployment targets, CI/CD pipelines, and production workflows
