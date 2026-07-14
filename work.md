# Architecture Document Generation Prompt

You are a Principal AI Architect and Distributed Systems Engineer.

Create a comprehensive Architecture Design Document (ADD) for a production-grade AI Memory Operating System inspired by MemoryOS and Supermemory.

The system should act as a persistent memory layer for AI agents, enabling long-term personalization, contextual reasoning, memory retrieval, memory evolution, and cross-session continuity.

The document should be written as if it were intended for engineering leadership, staff engineers, and implementation teams.

---

## System Vision

Design an AI Memory Operating System (MemoryOS) that functions similarly to how modern operating systems manage RAM, cache, and disk storage.

Instead of treating memory as simple vector embeddings, the system should maintain:

* Short-Term Memory (STM)
* Mid-Term Memory (MTM)
* Long-Term Memory (LTM)

The memory system must:

* Continuously learn from interactions
* Consolidate memories
* Forget stale information
* Resolve contradictions
* Build user profiles
* Support multiple AI agents
* Scale to millions of users

---

## Core Requirements

### Functional Requirements

1. Persistent user memory
2. Cross-session memory recall
3. Multi-agent shared memory
4. Memory retrieval
5. Semantic search
6. Knowledge graph construction
7. User profile generation
8. Memory consolidation
9. Memory decay and forgetting
10. Contradiction detection
11. Event timeline generation
12. Multi-modal memory support
13. Real-time updates
14. Memory versioning
15. MCP-compatible APIs

---

### Non-Functional Requirements

1. <200ms retrieval latency
2. Horizontal scalability
3. Fault tolerance
4. High availability
5. GDPR-compliant deletion
6. Multi-tenancy
7. Observability
8. Cost-efficient storage
9. Security and encryption
10. Eventual consistency

---

## Architecture Sections To Generate

Generate the following sections in detail:

### 1. Executive Summary

Explain:

* Problem statement
* Business value
* Why traditional RAG is insufficient
* Why memory operating systems are needed

---

### 2. High-Level Architecture

Provide:

* Component overview
* Layered architecture
* Data flow
* Service boundaries

Include ASCII diagrams.

---

### 3. Memory Hierarchy Design

Design:

#### Short-Term Memory (STM)

* Conversation context
* Active reasoning state
* Working memory

#### Mid-Term Memory (MTM)

* Topic clusters
* Episodic memory
* Session memories

#### Long-Term Memory (LTM)

* User profile
* Preferences
* Persistent facts
* Behavioral patterns

Explain promotion and demotion policies.

---

### 4. Memory Lifecycle

Describe:

1. Ingestion
2. Extraction
3. Classification
4. Scoring
5. Storage
6. Consolidation
7. Retrieval
8. Decay
9. Archival
10. Deletion

Provide detailed workflows.

---

### 5. Retrieval Architecture

Design:

* Hybrid retrieval
* Vector search
* Knowledge graph search
* BM25 retrieval
* Reranking layer
* Context assembly engine

Include sequence diagrams.

---

### 6. Knowledge Graph Layer

Design:

* Entity extraction
* Relationship extraction
* Graph updates
* Contradiction resolution

Example:

User → Works At → Company

User → Interested In → AI

User → Building → AI Agent

---

### 7. Memory Scoring Engine

Create algorithms for:

* Importance score
* Recency score
* Frequency score
* Emotional significance score
* Retrieval score

Provide formulas.

---

### 8. Memory Consolidation Engine

Explain:

* Duplicate merging
* Fact updates
* Profile evolution
* Memory summarization

---

### 9. Forgetting and Decay Engine

Design:

* Decay schedules
* Retention policies
* Archival rules
* User-controlled deletion

---

### 10. Multi-Agent Memory Architecture

Support:

* Personal agent memory
* Team memory
* Organization memory
* Shared memory spaces

Include access-control mechanisms.

---

### 11. Data Model

Generate schemas for:

* Memory
* User
* Session
* Entity
* Relationship
* Knowledge Graph
* Retrieval Index

Use SQL-style definitions.

---

### 12. API Design

Create REST and MCP-compatible APIs:

Examples:

POST /memory

GET /memory/search

POST /memory/retrieve

DELETE /memory

POST /memory/consolidate

Provide request and response examples.

---

### 13. Infrastructure Architecture

Recommend production stack:

Backend:

* FastAPI
* Python

Storage:

* PostgreSQL
* Neo4j
* Qdrant

Messaging:

* Kafka

Caching:

* Redis

Observability:

* OpenTelemetry
* Prometheus
* Grafana

Deployment:

* Kubernetes

Explain reasoning for each choice.

---

### 14. Security Architecture

Cover:

* Authentication
* Authorization
* RBAC
* Encryption at rest
* Encryption in transit
* Secret management
* Audit logging

---

### 15. Scalability Design

Explain:

* Horizontal scaling
* Sharding
* Partitioning
* Replication
* Retrieval optimization

Target:

10M users

100M memories

Sub-200ms retrieval

---

### 16. Failure Recovery

Design:

* Backup strategy
* Disaster recovery
* Replication
* Data restoration

---

### 17. Observability

Include:

* Metrics
* Tracing
* Logging
* Dashboards
* Alerting

---

### 18. Sequence Diagrams

Generate diagrams for:

1. Memory ingestion
2. Memory retrieval
3. Memory consolidation
4. Memory decay
5. Multi-agent access

Use Mermaid format.

---

### 19. Technology Decisions

Create ADRs (Architecture Decision Records) for:

* PostgreSQL
* Neo4j
* Qdrant
* Kafka
* FastAPI
* Kubernetes

---

### 20. Future Roadmap

Design future capabilities:

* Agent self-reflection
* Autonomous memory management
* Temporal reasoning
* Federated memory
* Cross-agent memory sharing
* Memory marketplaces
* Personal AI operating systems

---

Output Requirements

The document should:

* Be implementation-ready
* Include diagrams
* Include schemas
* Include APIs
* Include workflows
* Include tradeoff analysis
* Include scaling calculations
* Be written at Staff/Principal Engineer level
* Target a production deployment used by enterprise AI agents
