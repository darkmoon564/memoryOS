# MemoryOS
## A Local-First Long-Term Memory Layer for AI Agents

### Vision

MemoryOS is a local-first memory framework designed to give AI agents persistent long-term memory beyond an LLM's context window.

Instead of continually expanding prompts, MemoryOS stores, retrieves, updates, and maintains knowledge through a hybrid memory architecture that combines semantic search, lexical search, and relational reasoning.

---

# Why MemoryOS?

Modern memory systems often optimize for one or two aspects of agent memory:

- Semantic vector retrieval
- User preference storage
- Hosted memory APIs

MemoryOS explores combining these capabilities into a reusable memory layer with:

- Local-first deployment
- Hybrid retrieval
- Knowledge graph reasoning
- Automatic contradiction resolution
- Memory decay & lifecycle management
- Offline execution

The goal is to provide a reusable memory layer that any AI agent can integrate with.

---

# Core Architecture

```text
                  User / Agent
                       │
                  MemoryOS API
                       │
          ┌────────────┴────────────┐
          │                         │
      Ingestion                Retrieval
          │                         │
 Classification            Hybrid Retrieval
          │                         │
Embedding + Entity      Vector + Keyword +
Extraction              Graph + STM
          │                         │
 PostgreSQL + Neo4j     Reciprocal Rank Fusion
          │                         │
 Background Tasks      Cross-Encoder Reranker
          │                         │
 Memory Lifecycle      Context Builder
                                │
                               LLM
```

---

# Storage Layer

## PostgreSQL (pgvector + GIN)

Purpose

- Dense semantic retrieval
- Exact keyword matching
- Durable storage

Indexes

- HNSW vector index
- Trigram GIN index

---

## Neo4j

Purpose

- Entity relationships
- Multi-hop traversal
- State consistency
- Relationship updates

---

## SQLite

Fallback backend for local development and offline experimentation.

---

# Retrieval Pipeline

```text
Incoming Query
      │
Vector Search
      │
Keyword Search
      │
Knowledge Graph Expansion
      │
STM Retrieval
      │
Reciprocal Rank Fusion
      │
Cross-Encoder Reranking
      │
Context Builder
      │
LLM
```

Each retrieval stage addresses a different failure mode, improving recall while keeping context compact.

---

# Memory Lifecycle

```text
New Memory
     │
Classification
     │
Importance Assignment
     │
Embedding
     │
Entity Extraction
     │
Storage
     │
Retrieval
     │
Access Updates
     │
Decay
     │
Archive
```

Rather than treating memory as immutable, MemoryOS continuously updates memory relevance over time.

---

# Cognitive Scoring

Each memory receives a retention score based on:

- Importance
- Recency
- Access frequency

Current scoring uses heuristic weights derived from early experimentation. These weights are expected to evolve as larger evaluation datasets become available.

Inactive memories are archived instead of deleted, reducing retrieval noise while preserving history.

---

# State Consistency

When newer information contradicts an older single-valued fact, MemoryOS automatically deactivates the outdated record.

Example

Before

Bob → WORKS_AT → Microsoft

After

Bob → WORKS_AT → Google

The previous relationship is marked inactive while preserving history.

Multi-valued relationships (interests, skills, etc.) are intentionally excluded from automatic replacement.

---

# Project Structure

```
memoryos/
 ├── api/
 ├── core/
 ├── db/
 ├── models/
 ├── services/
 ├── tests/
 └── schema/
```

The architecture separates retrieval, storage, scoring, lifecycle management, and APIs into independent modules to simplify extension and open-source contributions.

---

# Current Status

Implemented

- Hybrid retrieval pipeline
- Vector + lexical search
- Knowledge graph integration
- Memory lifecycle management
- Offline execution

Currently Validating

- Large-scale retrieval quality
- High-concurrency performance
- Long-running memory consistency
- Retrieval latency at scale

---

# Roadmap

Phase 1

- Open-source release

Phase 2

- Production benchmarking
- Concurrency testing
- Memory quality evaluation

Phase 3

- Multi-node deployment
- Larger memory stores
- Adaptive scoring models

Feedback on the architecture and design decisions is highly appreciated while the project is still in its early stages.
