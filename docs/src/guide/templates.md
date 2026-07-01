# Agent Templates

`agents-cli` creates projects from agent templates. Each template provides a working agent with the right dependencies, tools, and project structure for its use case.

---

## Available Templates

| Template | Description | Use Case |
|----------|-------------|----------|
| `adk` | ReAct agent using ADK | General-purpose conversational agent with tool use |

> **RAG is not a template** — it's a clone-and-study recipe. See [RAG](#rag-retrieval-augmented-generation) below.

### adk

The default template. Creates a ReAct agent using the [Agent Development Kit](https://google.github.io/adk-docs/) with a sample tool. Start here if you are new to ADK or building a general-purpose agent.

```bash
agents-cli create my-agent --agent adk
```

Every Python ADK agent serves the [Agent-to-Agent (A2A) protocol](https://a2a-protocol.org) out of the box — the A2A routes (agent card + JSON-RPC) are mounted automatically. Use this when your agent needs to interoperate with agents built on other frameworks (LangGraph, CrewAI, etc.) or when building a distributed multi-agent system; no separate template or hand-written A2A code is required.

## RAG (Retrieval-Augmented Generation)

RAG is **not** a template — it's a clone-and-study recipe. Scaffold a base `adk` project, then study and adapt one of the RAG samples in [google/adk-samples](https://github.com/google/adk-samples), copying its retriever and `infra/terraform/` into your project:

- **`rag-vector-search`** — Vertex AI Vector Search 2.0 with a custom ingestion pipeline (embeddings, similarity search).
- **`rag-agent-search`** — Agent Platform Search (Discovery Engine) with a fully-managed GCS Data Connector — drop files in a bucket, no ingestion code to write.

The workflow skill's `references/samples.md` lists both with their key files, and each sample's `AGENTS.md` is the study-and-adapt guide. Provisioning and ingestion run from the sample's own `Makefile` (`make setup-infra`, `make data-ingestion`).
