# Product Name Mapping

The platform formerly known as "Vertex AI" is now **Gemini Enterprise Agent Platform** (short: **Agent Platform**). Users may refer to products by different names. Map them to the correct CLI values:

| User may say | CLI value |
|-------------|-----------|
| Agent Engine, Vertex AI Agent Engine, Agent Runtime | `--deployment-target agent_runtime` |
| Agent Engine sessions, Agent Platform Sessions | `--session-type agent_platform_sessions` |
| Vertex AI Search, Vertex AI Vector Search, RAG | clone-and-study recipe, not a flag (see `references/samples.md`) |

The `vertexai` Python SDK package name is unchanged.
