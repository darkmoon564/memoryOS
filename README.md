# MemoryOS

MemoryOS is a local-first, modular long-term memory framework designed to give AI agents persistent, consistent memory beyond an LLM's context window.

By combining dense semantic vector search, sparse keyword matching, and a relational knowledge graph, MemoryOS behaves like an externalized hippocampal-cortex system for your agents.

---

## 🌟 Key Features

* **Hybrid Retrieval Pipeline:** Merges pgvector cosine search, GIN trigram keyword matching, Neo4j graph context, and Short-Term Memory (STM) recency caching using **Reciprocal Rank Fusion (RRF)**, reranked via a local **Cross-Encoder**.
* **Grammatical SVO Entity Fallback:** When a local LLM is offline, a lightweight **spaCy dependency-parsing engine** extracts Subject-Verb-Object relationships, normalizes pronouns, and skips negated assertions.
* **Graph-Backed Contradiction Resolution:** Graph relationships determine single-valued facts (like jobs or locations). When contradicting updates occur (e.g., *"I live in Austin"* followed by *"I live in Berlin"*), old references are automatically deactivated in both PostgreSQL and the active cache block.
* **Cognitive Scoring & Decay:** Implements an RFI (Recency, Frequency, Importance) scoring formula to exponentially decay unused memories, archiving them when they drop below relevance thresholds.
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
```
*Note: If no LLM is available, MemoryOS falls back to a local **spaCy dependency parser** for entity extraction. PostgreSQL and Neo4j are required for a durable deployment; the in-memory adapters are test-only and must be explicitly enabled.*

### 3. Launch the Server
```bash
uvicorn memoryos.main:app --host 127.0.0.1 --port 8088
```

For the complete local deployment, including a migration job and durable graph worker, run `docker compose up --build`. The API is available only on `127.0.0.1:8088` by default. The migration job must complete before the API or worker starts.

The Compose environment intentionally uses deterministic offline models for fast, repeatable integration testing. Build the full semantic-model image for production with `docker build --build-arg INSTALL_ML=true -t memoryos:full .`.

### Upgrading from a plaintext API-key database

MemoryOS uses tracked, forward-only migrations. For a new database run `python -m memoryos.migrations bootstrap`; for an existing database, back it up and run `python -m memoryos.migrations upgrade`. Use `python -m memoryos.migrations status` in deployment checks. `0001` replaces stored plaintext keys with SHA-256 lookup hashes; `0002` adds the durable graph-projection queue. Callers continue sending the original API key in the Bearer header.

Create an initial workspace key after startup. The command prints the secret once and stores only its hash:

```bash
python -m memoryos.manage create-api-key production_workspace --description "initial deployment key"
```

Run durable graph recovery as a separate process in production:

```bash
python -m memoryos.worker --interval 5 --batch-size 100
```

The API only writes durable outbox work in production. For local development without a worker, set `MEMORYOS_PROCESS_OUTBOX_INLINE=true`; `MEMORYOS_EMBEDDED_WORKER=true` additionally enables periodic in-process recovery. Production deployments should run one or more dedicated workers.

---

## 📡 API Endpoints

`GET /health` is a liveness probe. `GET /readyz` verifies PostgreSQL and Neo4j before traffic is accepted. `GET /metrics` exposes Prometheus-compatible service metrics. Every HTTP response includes an `X-Request-ID` correlation header; send one to retain an upstream trace ID.

### Ingest Memory
`POST /v1/memories`
```json
{
  "user_id": "agent_user_1",
  "content": "Bob works at Microsoft.",
  "workspace_id": "production_workspace"
}
```

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

### Consolidate & Deduplicate
`POST /v1/memories/consolidate`
*Merges near-duplicate postgres vector memories and merges overlapping Neo4j nodes.*

### Apply Decay
`POST /v1/memories/decay`
*Manually runs the decay sweep to archive stale records.*

---

## 🧪 Running Tests

```bash
# Core accuracy tests (recall under noise, contradiction resolution, decay)
python tests/test_accuracy.py

# Synthetic multi-session dialogue evaluation
python tests/synthetic_multisession_eval.py
```

The CI suite also runs `python tests/test_durable_recovery.py` against real PostgreSQL and Neo4j. It verifies workspace-key isolation, recovery of committed-but-unprojected graph work, bounded retries, and dead-letter handling.

---

## 📄 License
Licensed under the MIT License.
