# MemoryOS Development Roadmap & Todo List

This document tracks the evolution of MemoryOS from its current memory-centric implementation to a production-grade, event-centric cognitive memory framework.

---

## 🛠️ Architectural Pivot: Event-Centric Substrate

- [/] **Shift from Sentence-Centric to Event-Centric design:**
  - Transition the primary ingestion entity from raw text sentences to structured **Atomic Events**.
  - Embeddings, graphs, and memories should be derived views over this event log rather than primary representations.

```
Incoming Conversation
        ↓
  Atomic Events (Immutable Event Log)
        ↓
  Entity Graph & Canonicalization
        ↓
  Episodes (Contextual Clusters)
        ↓
  Semantic Knowledge & Profiles
```

---

## 📋 Modular Enhancements

### 1. Ingestion Pipeline
Re-order the ingestion pipeline to extract structure and graph context *before* embedding and indexing.
- [/] Implement **Event Parser** to extract atomic events from raw sentences.
- [ ] Implement **Fact Extraction** on top of the parsed events.
- [ ] Generate **Embeddings** specifically for the extracted facts rather than raw text.
- [ ] Populate the **Graph** using the facts.
- [ ] Build **Episodes** out of event clusters.

### 2. Entity Extraction & Canonicalization
Prevent node explosion and improve relationship retrieval by mapping entity variations.
- [x] Design and implement an **Entity Resolver**.
- [x] Support **Canonical Entities** as primary nodes (e.g., `Microsoft`).
- [x] Maintain an **Aliases** map linking variations (e.g., `MSFT`, `Microsoft Corp`, `microsoft`) to the canonical node.

### 3. Rich Edge Metadata
Add context and provenance to every graph relationship.
- [x] Extend Neo4j relationships to store:
  - `source_memory_id` (Provenance - where was it asserted?)
  - `timestamp` (When was it asserted?)
  - `confidence` (How certain is the model?)
  - `workspace_id` (Multi-tenant partition)
  - `version` (Version history of the fact)
  - `is_active` / `evidence_count` (Validation state)

### 4. Contradiction Engine
Preserve historical truths rather than deleting superseded facts.
- [x] Transition from deleting/deactivating to **Versioned Deactivation**.
- [x] Add `valid_from` and `valid_to` timestamps or metadata markers.
- [x] Use a `superseded_by` relationship pointer linking the outdated fact to the newer fact (e.g., `LivesIn Austin (invalid, superseded_by LivesIn Berlin)`).

### 5. Vector Memory
Avoid embedding raw conversations indefinitely.
- [x] Split representation into:
  - **Conversation Logs** (Raw input, ephemeral/short retention).
  - **Episodes** (Temporal blocks of interaction).
  - **Episode Summaries** (Dense summaries of the block).
  - **Embeddings** (Calculated over the summaries/facts rather than raw chat).

### 6. Retrieval Pipeline
Restructure retrieval order so that the planner and entity graph guide vector search.
- [x] Build a **Retrieval Planner** to parse user intent and goals.
- [x] Implement **Entity Graph Expansion** as the first retrieval step.
- [x] Retrieve **Episodes** containing the expanded entities.
- [x] Perform **Vector Search** and **Keyword Search** filtered/scoped by the retrieved episodes and entities.
- [x] Combine results with **Reflection Memory**, **RRF**, and **Cross-Encoder Reranking**.

### 7. Reflection System (Offline Daemon)
Synthesize raw experiences into consolidated knowledge.
- [x] Build a background **Reflection Daemon** that runs periodically (e.g., hourly/daily).
- [x] Read recent episodes and identify repeated patterns.
- [x] Generate **Semantic Knowledge** (e.g., *“User specializes in backend AI infrastructure”* from repeated tasks).
- [x] Update the Graph with synthesized nodes.
- [x] Archive raw episodes to reduce retrieval noise.

### 8. Memory Consolidation
Define a clear representation hierarchy to mature episodic memories into semantic knowledge.
- [x] Build a pipeline to consolidate:
  `Episode` (e.g., *“Built LangGraph”*) → `Learning Topic` (e.g., *“Learning Rust”*) → `Semantic Profile` (e.g., *“Rust Developer”*).

### 9. Temporal Memory
Enable timelines to handle time-based queries.
- [x] Build a chronological event timeline.
- [x] Enable support for temporal queries (e.g., *“Where was the user working in June?”*).

### 10. Planner Memory
Align retrieved context with agent execution goals.
- [x] Make retrieval **Goal-Aware** (inject current task/goal constraints into the search context).
- [x] Dynamically retrieve different classes of memory (e.g., code repositories/languages for developer goals vs. budget/history for shopping goals).

### 11. Procedural Memory
Track and learn user workflows.
- [x] Extract and store procedural steps (e.g., *“How the user deploys: Docker -> Railway -> Postgres -> GitHub Actions”*).
- [x] Retrieve recipes and workflows when similar goals are active.

### 12. Working Memory
Upgrade the STM cache from simple LRU to a structured CPU-style register.
- [x] Structure Working Memory to contain:
  - Current Goal
  - Constraints
  - Current Plan
  - Scratchpad
  - Retained retrieved facts

### 13. Immutable Event Store
- [x] Implement a system-wide transaction log or **Event Store** to allow complete state reproducibility of all derived memory graphs and vector indices.

### 14. Confidence Engine
- [x] Replace single scores with multidimensional scoring: `[confidence, importance, frequency, recency, verification, source, decay]`.

### 15. Graph Traversal Upgrades
- [x] Utilize Neo4j's native Graph Data Science capabilities:
  - Multi-hop / Variable Length Paths
  - Community Detection (identifying user interest clusters)
  - Shortest Path (identifying links between concepts)
  - Semantic Expansion

---

## 🗺️ Long-Term Roadmap

### Phase 1: Strengthen the Foundation (2–3 Weeks)
- [x] Implement Canonical Entity Resolution & Aliasing maps.
- [x] Integrate rich edge metadata (timestamps, confidence, provenance) in Neo4j.
- [x] Implement versioned/superseded fact tracking instead of overwriting.
- [ ] Expand spaCy graph extraction coverage.
- [ ] Set up Event IDs and provenance tracking in PostgreSQL/SQLite schema.

### Phase 2: Cognitive Layer (3–4 Weeks)
- [x] Build the offline Reflection Daemon.
- [x] Implement the Episode Builder & Episode Summarization pipelines.
- [x] Design the hierarchy for automatic Semantic Consolidation.
- [x] Implement Temporal Timeline generation.
- [ ] Refine memory decay, significance scoring, and archival schedules.

### Phase 3: Retrieval Intelligence (3–4 Weeks)
- [x] Implement Goal-Aware Planner Memory.
- [x] Support multi-hop graph expansion & semantic neighborhoods during search.
- [x] Develop Procedural Memory capture (workflow tracking).
- [x] Design adaptive retrieval policies based on intent classification.

### Phase 4: Research Differentiators (4–6 Weeks)
- [ ] Build hybrid vector + symbolic + graph query execution planners.
- [ ] Enable community detection and sub-graph semantic clustering.
- [ ] Track user identity evolution over long durations.
- [ ] Implement secure multi-agent shared memory with cryptographic provenance.
- [ ] Benchmark performance against MemGPT, GraphRAG, Graphiti, and Mem0.
