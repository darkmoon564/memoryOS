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
Create a `.env` file in the root directory:
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

# LLM Extractor Settings
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2
```
*Note: If Postgres or Neo4j are not running, MemoryOS automatically spins up a **zero-dependency fallback** using in-memory SQLite and a local mock graph database.*

### 3. Launch the Server
```bash
uvicorn memoryos.main:app --host 127.0.0.1 --port 8088
```

---

## 📡 API Endpoints

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

## 🧪 Running Benchmarks & Tests

To run the accuracy and dialogue-session tests locally:
```bash
# Run SVO / contradiction accuracy tests
python tests/test_accuracy.py

# Run LoCoMo multi-session dialogue memory benchmark
python tests/locomo_benchmark.py
```

---

## 📄 License
Licensed under the MIT License.
