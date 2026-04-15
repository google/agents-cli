# ADK Reference Implementations

Production patterns from [adk-samples](https://github.com/google/adk-samples)
worth studying when building your own agents.

**How to use:**
- Clone a specific sample and read the source:
  ```bash
  # Replace <sample-name> with: deep-search, data-science, or fomc-research
  git clone --filter=tree:0 --sparse https://github.com/google/adk-samples /tmp/adk-samples \
    && cd /tmp/adk-samples \
    && git sparse-checkout set python/agents/<sample-name>
  ```
- To scaffold a new project from a sample (only if the user asks):
  `agents-cli init my-project --agent adk@<sample-name>`

## Curated Samples

### deep-search

Full-stack agent app with React UI + FastAPI backend.

**Worth studying for:** LoopAgent + EscalationChecker for iterative refinement, BuiltInPlanner with ThinkingConfig, grounding metadata for citation extraction, SequentialAgent composition.

**Key files to read:**
- `app/agent.py` — root agent wiring, LoopAgent + EscalationChecker, `collect_research_sources_callback` (grounding metadata → citations), `citation_replacement_callback`
- `app/config.py` — model configuration pattern (separate worker/critic models)
- `frontend/` — React UI wired to the FastAPI backend

### data-science

Multi-agent NL2SQL system with BigQuery, AlloyDB, and code execution.

**Worth studying for:** `VertexAiCodeExecutor` (managed sandbox — different from `BuiltInCodeExecutor`), `after_tool_callback` for capturing tool results into session state for downstream agents.

**Key files to read:**
- `data_science/sub_agents/analytics/agent.py` — `VertexAiCodeExecutor` usage with `optimize_data_file` and `stateful` params
- `data_science/sub_agents/bigquery/agent.py` — `after_tool_callback` (`store_results_in_context`) capturing query results into state
- `data_science/agent.py` — root orchestrator with `before_agent_callback` for loading config into state

### fomc-research

Financial event analysis with rate-limited multi-agent pipeline.

**Worth studying for:** Rate limiting via `before_model_callback` using session state, wrapping deterministic computation (PDF extraction, financial math) in ADK tools.

**Key files to read:**
- `fomc_research/shared_libraries/callbacks.py` — `rate_limit_callback` using session state to track request counts and timestamps
- `fomc_research/tools/compare_statements.py` — wrapping real computation (PDF text extraction + HTML redline diffing) in a FunctionTool
- `fomc_research/shared_libraries/price_utils.py` — deterministic financial math exposed as tool logic
