# Evaluating Agents with `google_search` and Built-in Tools

## google_search Behavior (IMPORTANT)

`google_search` is NOT a regular tool — it's a **model-internal grounding feature**.

**Key behavior:**
- Custom tools (`save_preferences`, `save_feedback`) → appear as `function_call` in trajectory
- `google_search` → NEVER appears in trajectory (happens inside the model)

**How google_search works internally:**
```python
llm_request.config.tools.append(
    types.Tool(google_search=types.GoogleSearch())  # Injected into model config
)
```

Search results come back as `grounding_metadata`, not function call/response events. But the evaluator STILL detects it at the session level:
```json
{
  "error_code": "UNEXPECTED_TOOL_CALL",
  "error_message": "Unexpected tool call: google_search"
}
```

This causes `tool_trajectory_avg_score` to ALWAYS fail for agents using `google_search`.

**Metric compatibility for `google_search` agents:**

| Metric | Usable? | Why |
|--------|---------|-----|
| `tool_trajectory_avg_score` | NO | Always fails due to unexpected google_search |
| `rubric_based_final_response_quality_v1` | YES | Evaluates output quality semantically |
| `final_response_match_v2` | Maybe | Works for stable expected outputs |

**Evalset best practices for `google_search` agents:**

```json
{
  "eval_id": "news_digest_test",
  "conversation": [{
    "user_content": { "parts": [{"text": "Give me my news digest."}] }
    // NO intermediate_data.tool_uses for google_search - it won't match anyway
  }]
}
```

For custom tools alongside google_search, still include them (but NOT google_search):
```json
{
  "intermediate_data": {
    "tool_uses": [
      { "name": "save_feedback" }
      // Do NOT include google_search here
    ]
  }
}
```

**Config for `google_search` agents:**

```json
{
  "criteria": {
    // REMOVE this - incompatible with google_search:
    // "tool_trajectory_avg_score": 1.0,

    // Use rubric-based evaluation instead:
    "rubric_based_final_response_quality_v1": {
      "threshold": 0.6,
      "rubrics": [
        { "rubricId": "has_citations", "rubricContent": { "textProperty": "Response includes source citations or references" } },
        { "rubricId": "relevance", "rubricContent": { "textProperty": "Response directly addresses the user's query" } }
      ]
    }
    // Note: both camelCase and snake_case field names are accepted (Pydantic aliases).
    // Examples elsewhere use snake_case (`rubric_id`, `rubric_content`, `text_property`).
  }
}
```

**Bottom line:** `google_search` is a model feature, not a function tool. You cannot test it with trajectory matching. Use rubric-based LLM-as-judge evaluation to verify the agent produces grounded, cited responses.

---

## ADK Built-in Tools: Trajectory Behavior Reference

**Model-Internal Tools (DON'T appear in trajectory):**

| Tool | In Trajectory? | Eval Strategy |
|------|----------------|---------------|
| `google_search` | No | Rubric-based |
| `google_search_retrieval` | No | Rubric-based |
| `BuiltInCodeExecutor` | No | Check output |
| `VertexAiSearchTool` | No | Rubric-based |
| `url_context` | No | Rubric-based |

These inject into `llm_request.config.tools` as model capabilities:
```python
types.Tool(google_search=types.GoogleSearch())
types.Tool(code_execution=types.ToolCodeExecution())
types.Tool(retrieval=types.Retrieval(...))
```

**Function-Based Tools (DO appear in trajectory):**

| Tool | In Trajectory? | Eval Strategy |
|------|----------------|---------------|
| `load_web_page` | Yes | `tool_trajectory_avg_score` works |
| Custom tools | Yes | `tool_trajectory_avg_score` works |
| AgentTool | Yes | `tool_trajectory_avg_score` works |

These generate `function_call` and `function_response` events:
```python
types.Tool(function_declarations=[...])
```

**Quick Reference — Can I use `tool_trajectory_avg_score`?**
- `google_search` → NO (model-internal)
- `code_executor` → NO (model-internal)
- `VertexAiSearchTool` → NO (model-internal)
- `url_context` → NO (model-internal)
- `load_web_page` → YES (FunctionTool)
- Custom functions → YES (FunctionTool)

**When mixing both types** (e.g., `google_search` + `save_preferences`):
1. Remove `tool_trajectory_avg_score` entirely, OR
2. Only test function-based tools in `tool_uses` and accept the trajectory will be incomplete

**Rule of Thumb:**
- If a tool provides grounding/retrieval/execution capabilities built into Gemini → model-internal, won't appear in trajectory
- If it's a Python function you can call → appears in trajectory

### Model thinking mode may bypass tools

Models with "thinking" enabled may decide they have sufficient information and skip tool calls. Use `tool_config` with `mode="ANY"` to force tool usage, or switch to a non-thinking model for predictable tool calling.

### Mock mode for external APIs

When your agent calls external APIs, add mock mode so evals can run without real credentials:
```python
def call_external_api(query: str) -> dict:
    api_key = os.environ.get("EXTERNAL_API_KEY", "")
    if not api_key or api_key == "dummy_key":
        return {"status": "success", "data": "mock_response"}
    # Real API call here
```
