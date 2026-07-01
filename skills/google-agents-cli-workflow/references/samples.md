# Notable samples

Samples live in [google/adk-samples](https://github.com/google/adk-samples). To reuse one, clone it
with a sparse, partial checkout, read its docs (`README.md`, or `AGENTS.md` for the RAG samples),
then adapt the patterns into your own scaffolded project. **Don't scaffold from a sample** — there
is no `adk@<sample>`.

```bash
git clone --filter=tree:0 --sparse https://github.com/google/adk-samples /tmp/adk-samples
cd /tmp/adk-samples
git sparse-checkout add python/agents/<sample>   # most samples
git sparse-checkout add core/<sample>            # RAG samples (see below)
```

Pull in another sample later with one more `git sparse-checkout add <path>`. Start from the key
files listed under each sample below.

## RAG samples (`core/`)

Both ground an agent on your own documents. Pick **`rag-agent-search`** for managed search over
unstructured files (PDF/HTML/TXT), or **`rag-vector-search`** when you need custom
chunking/embeddings or direct vector-store control. In each, the value is mostly the Terraform +
ingestion plumbing, not the agent itself.

Each RAG sample ships a curated **`AGENTS.md`** — read it first; it is the source of truth for
*how* to adapt the sample (intent, a ranked "study in this order" file tour, what to copy as-is,
and gotchas).

- **`rag-agent-search`** — managed document search via Agent Platform Search (Discovery Engine) with
  a fully-managed GCS Data Connector: drop files in a bucket, no ingestion code to maintain.
  - Key files: `AGENTS.md`, `app/agent.py`, `app/retrievers.py`, `infra/terraform/agent_platform_search.tf`, `infra/terraform/scripts/setup_data_connector.py`
  - Keywords: RAG, document search, Discovery Engine, Agent Platform Search, managed ingestion, GCS data connector, PDF, HTML, grounding
- **`rag-vector-search`** — RAG with Vertex AI Vector Search 2.0 and a KFP ingestion pipeline
  (chunking + BigQuery staging; embeddings auto-generated).
  - Key files: `AGENTS.md`, `app/agent.py`, `app/retrievers.py`, `data_ingestion/data_ingestion_pipeline/pipeline.py`, `infra/terraform/`
  - Keywords: RAG, retrieval, vector search, embeddings, similarity search, ScaNN, semantic search, document Q&A, ingestion pipeline, chunking

## Other samples (`python/agents/`)

- **`ambient-expense-agent`** — runs on a schedule or reacts to events, with no interactive user.
  - Key files: `expense_agent/fast_api_app.py`, `expense_agent/agent.py`, `expense_agent/config.py`, `terraform/`
  - Keywords: scheduled, cron, daily, pubsub, event-driven, alerts, email, ambient
- **`adk-ae-oauth`** — OAuth 2.0 user consent, deployed to Agent Runtime with Gemini Enterprise.
  - Key files: `README.md`, `adk_ae_oauth/tools.py`, `adk_ae_oauth/auths.py`
  - Keywords: OAuth, authentication, user consent, Google Drive, Agent Runtime, Gemini Enterprise
- **`genmedia-for-commerce`** — full-stack agent with React UI, MCP tools, media/image handling, and
  Gemini Enterprise registration.
  - Key files: `genmedia4commerce/agent.py`, `genmedia4commerce/agent_utils.py`, `genmedia4commerce/fast_api_app.py`
  - Keywords: MCP, media, video generation, Veo, virtual try-on, retail, full-stack, React, Gemini Enterprise
- **`deep-search`** — research agent that iterates until quality is met, with source citations.
  - Key files: `app/agent.py`, `app/config.py`
  - Keywords: research, citations, iterative, grounding, multi-agent, human-in-the-loop, web search, report
- **`safety-plugins`** — reusable safety guardrails that plug into any agent runner.
  - Key files: `safety_plugins/plugins/model_armor.py`, `safety_plugins/plugins/agent_as_a_judge.py`, `safety_plugins/main.py`
  - Keywords: safety, guardrails, model armor, filters
- **`memory-bank`** — conversational agent with cross-session memory via Memory Bank (Cloud Run and
  Agent Runtime).
  - Key files: `app/agent.py`, `app/fast_api_app.py`
  - Keywords: memory, cross-session, recall, context, remember, Memory Bank
