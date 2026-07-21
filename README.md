# MemoryOS

MemoryOS is a local-first, modular long-term memory framework designed to give AI agents persistent, consistent memory beyond an LLM's context window.

By combining dense semantic vector search, sparse keyword matching, and a relational knowledge graph, MemoryOS behaves like an externalized hippocampal-cortex system for your agents.

## Hackathon demo: FounderOS

FounderOS is a small startup-CEO agent built on top of MemoryOS for this hackathon. It is a demonstration of the core MemoryOS capability: an AI application can retrieve connected, durable context instead of relying only on the current chat window.

The demo brings together a company's customers, roadmap, investor meetings, metrics, Slack discussions, and GitHub issues. It then uses MemoryOS retrieval to answer questions such as “Which customer needs my attention?” with context that spans those sources. The FounderOS frontend lives in [`startup-ceo-agent`](startup-ceo-agent/).

## How Codex and GPT-5.6 were used

We used Codex with GPT-5.6 as a development collaborator to build FounderOS on top of MemoryOS. Codex helped implement the CEO question endpoint, connect retrieval-grounded context to the LLM response flow, add durable daily LLM usage limits, configure the local frontend-to-API integration, troubleshoot Docker and database startup issues, and prepare the project documentation. GPT-5.6 and Codex were used during development for implementation, debugging, iteration, and integration; the resulting API routes, migration, safeguards, and tests are inspectable in this repository.

---

## 🌟 Key Features

* **Hybrid Retrieval Pipeline:** Merges pgvector cosine search, GIN trigram keyword matching, Neo4j graph context, and Short-Term Memory (STM) recency caching using **Reciprocal Rank Fusion (RRF)**, reranked via a local **Cross-Encoder**.
* **Grammatical SVO Entity Fallback:** When a local LLM is offline, a lightweight **spaCy dependency-parsing engine** extracts Subject-Verb-Object relationships, normalizes pronouns, and skips negated assertions.
* **Graph-Backed Contradiction Resolution:** Graph relationships determine single-valued facts (like jobs or locations). When contradicting updates occur (e.g., *"I live in Austin"* followed by *"I live in Berlin"*), old references are automatically deactivated in both PostgreSQL and the active cache block.
* **Cognitive Scoring & Decay:** Uses recency, frequency, and importance to gradually deprioritize unused memories while keeping ordinary long-term knowledge recoverable.
* **Ingestion Idempotency:** SHA-256 fingerprint matching prevents duplicate ingestion rows due to agent retry loops.

---

## 🚀 Quick Start

### 1. Clone & Setup Environment
```bash
git clone https://github.com/darkmoon564/memoryOS.git
cd memoryOS

# Create and activate python virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment Variables
Copy `.env.example` to `.env` and replace every placeholder with a unique local secret. Do not use the example values outside local development.
```env
# Database Settings
POSTGRES_HOST=localhost
POSTGRES_DB=memoryos
POSTGRES_USER=postgres
POSTGRES_PASSWORD=local_dev_password
POSTGRES_PORT=5432

NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=local_dev_password

# Entity Extraction LLM (choose one)

# Option A: Any OpenAI-compatible API (OpenAI, Groq, Together, vLLM, LiteLLM)
LLM_API_BASE=https://api.openai.com/v1
LLM_API_KEY=sk-your-key-here
LLM_MODEL=gpt-4o-mini

# Option B: Ollama (local LLM, no API key needed)
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2

# Timeout for LLM calls in seconds (default: 15)
LLM_TIMEOUT=15

# Maximum provider-backed LLM requests shared by this deployment each day
# (default: 50). Local Ollama calls are not counted.
LLM_DAILY_REQUEST_LIMIT=50
```
*Note: If no LLM is available, MemoryOS falls back to a local **spaCy dependency parser** for entity extraction. PostgreSQL and Neo4j are required for a durable deployment; the in-memory adapters are test-only and must be explicitly enabled.*

### 3. Launch the Server
```bash
uvicorn memoryos.main:app --host 127.0.0.1 --port 8088
```

For the complete local deployment, including a migration job and durable graph worker, run `docker compose up --build`. The API is available only on `127.0.0.1:8088` by default. The migration job must complete before the API or worker starts.

Compose defaults to a lightweight deterministic integration image. It exercises the real PostgreSQL, pgvector schema, Neo4j graph, migration job, API, worker, outbox, and replay paths without pulling large local ML packages. It is suitable for validating durability and operational behavior, but not for reporting semantic-retrieval quality.

For a full local semantic runtime, run this before `docker compose up --build`:

```powershell
$env:MEMORYOS_INSTALL_ML = "true"
$env:MEMORYOS_OFFLINE_MODE = "false"
```

That image downloads CPU PyTorch, sentence-transformer models, and spaCy assets, so it requires substantially more disk and memory. Non-container production deployments fail closed if semantic models are unavailable; `MEMORYOS_ALLOW_MOCK_MODELS=true` remains only an explicitly non-semantic development escape hatch.

### Upgrading from a plaintext API-key database

MemoryOS uses tracked, forward-only migrations. For a new database run `python -m memoryos.migrations bootstrap`; for an existing database, back it up and run `python -m memoryos.migrations upgrade`. Use `python -m memoryos.migrations status` in deployment checks. `0001` replaces stored plaintext keys with SHA-256 lookup hashes; `0002` adds the durable graph-projection queue. Callers continue sending the original API key in the Bearer header.

Version `0005` adds durable, per-user graph-erasure work and all newly written graph nodes carry both `workspace_id` and `user_id`. Graph nodes produced by releases before this migration cannot be safely attributed retrospectively; rebuild those legacy graphs from the event store after upgrading rather than attempting an automated shared-workspace cleanup. Version `0006` adds source-event timestamps and durable memory-to-source provenance. Existing records are backfilled from their creation timestamps; new ingests should provide the original event time whenever it is known. Version `0007` adds durable deployment-wide daily LLM usage tracking; configure `LLM_DAILY_REQUEST_LIMIT` to control the request budget.

Older development builds could also create ignored `archive/archived_logs_*.jsonl` files during reflection. MemoryOS no longer writes them because they bypass durable deletion. Review and remove any legacy archive files under your own retention policy before treating an upgraded deployment as fully purged.

Create an initial workspace key after startup. The command prints the secret once and stores only its hash:

```bash
python -m memoryos.manage create-api-key production_workspace --description "initial deployment key"
```

Run durable graph recovery as a separate process in production:

```bash
python -m memoryos.worker --interval 5 --batch-size 100
```

The API only writes durable outbox work in production. For local development without a worker, set `MEMORYOS_PROCESS_OUTBOX_INLINE=true`; `MEMORYOS_EMBEDDED_WORKER=true` additionally enables periodic in-process recovery. Production deployments should run one or more dedicated workers.

### Data lifecycle and tenant isolation

PostgreSQL is the durable source of truth. Neo4j is a derived, user- and workspace-scoped projection. A memory purge deletes the requesting user's relational records (including raw event payloads and failed-job payloads) in one transaction and writes a graph-erasure outbox record. The request returns `accepted` if Neo4j is temporarily unavailable; the worker retries that erasure until it completes. Keep the worker running whenever graph-backed memory is enabled.

### Retrieval ranking and retention

MemoryOS retrieves candidates from vector, sparse, episode, and graph paths. Vector/sparse/episode candidates are fused with Reciprocal Rank Fusion; the cross-encoder then contributes a rank-based signal rather than its raw, model-specific score. This makes the final blend stable across queries and model versions. Current goals influence retrieval, but they do not replace the user's query as the reranker input.

Decay is a retrieval-time signal, not an automatic deletion rule. It uses a 180-day access half-life with importance and frequency support. The score only makes close candidates trade places: an old exact match remains stronger than a fresh unrelated memory. The response exposes both `recency` and `decay` so callers can apply a stricter product policy if needed.

---

## 📡 API Endpoints

`GET /health` is a liveness probe. `GET /readyz` verifies PostgreSQL and Neo4j before traffic is accepted. `GET /metrics` exposes Prometheus-compatible service metrics. Every HTTP response includes an `X-Request-ID` correlation header; send one to retain an upstream trace ID.

### Ingest Memory
`POST /v1/memories`
```json
{
  "user_id": "agent_user_1",
  "content": "Bob works at Microsoft.",
  "workspace_id": "production_workspace",
  "occurred_at": "2026-07-19T09:30:00Z",
  "source_event_id": "conversation-42:turn-7"
}
```

`occurred_at` is optional but, when supplied, must include a timezone. It
describes when the source event happened rather than when MemoryOS received
it. `source_event_id` is an optional stable turn or message identifier used to
make replay safe and returned as `source_event_ids` with retrieved memories.

### Retrieve Context
`POST /v1/memories/retrieve`
```json
{
  "user_id": "agent_user_1",
  "query": "Where does Bob work?",
  "limit": 3,
  "workspace_id": "production_workspace"
}
```

### FounderOS CEO Agent

`POST /v1/ceo/ask` retrieves tenant-scoped MemoryOS context before generating a
concise CEO-copilot answer. It requires the same workspace Bearer key as the
memory APIs.

```json
{
  "user_id": "founder_demo",
  "workspace_id": "production_workspace",
  "query": "Which customer needs my attention?",
  "limit": 6
}
```

`GET /v1/ceo/usage?workspace_id=production_workspace` returns the current
deployment-wide LLM request budget. The daily limit is shared by the configured
LLM provider deployment rather than enforced separately per user or workspace.

### Consolidate & Deduplicate
`POST /v1/memories/consolidate`
*Merges near-duplicate postgres vector memories and merges overlapping Neo4j nodes.*

### Apply Decay
`POST /v1/memories/decay`
*Evaluates active memories for retrieval-time freshness. Ordinary memories are deprioritized by age rather than automatically hidden.*

---

## 🧪 Running Tests

```bash
# Core accuracy tests (recall under noise, contradiction resolution, decay)
python tests/test_accuracy.py

# Synthetic multi-session dialogue evaluation
python tests/synthetic_multisession_eval.py
```

The CI suite also runs `python tests/test_durable_recovery.py` against real PostgreSQL and Neo4j. It verifies workspace-key isolation, recovery of committed-but-unprojected graph work, bounded retries, and dead-letter handling.

`python tests/test_data_lifecycle.py` is also run in CI against the real services. It verifies that purging one user leaves another user's same-workspace data intact, removes raw durable payloads, erases only that user's graph projection, and keeps manual decay within the authorized workspace.

`python tests/test_ranking_decay.py` is a fast, no-service regression check for reranker tie handling, invalid model output, ranking normalization, and retention behavior.

`python tests/test_event_time_provenance.py` checks timezone-safe event time,
historical retrieval of superseded facts, source-turn provenance, replay
idempotency, and durable graph-projection enqueueing. Locally it needs
`MEMORYOS_ALLOW_IN_MEMORY_FALLBACK=true` and `OFFLINE_MODE=true`; CI runs the
same test against its real services.

---

## 📄 License
Licensed under the MIT License.
