-- Enable extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- This file is intentionally forward-safe: applying it must never delete
-- memory data. Use a versioned migration tool for upgrades. A development-only
-- reset script may be added separately. It must never be used in deployment.

-- Core User Table
CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(64) PRIMARY KEY,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Active Sessions for STM management
CREATE TABLE IF NOT EXISTS sessions (
    id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) REFERENCES users(id) ON DELETE CASCADE,
    started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP WITH TIME ZONE
);

-- Episodes (temporal blocks of interaction)
CREATE TABLE IF NOT EXISTS episodes (
    id UUID PRIMARY KEY,
    user_id VARCHAR(64) REFERENCES users(id) ON DELETE CASCADE,
    session_id VARCHAR(64) REFERENCES sessions(id) ON DELETE SET NULL,
    workspace_id VARCHAR(64) NOT NULL DEFAULT 'default',
    summary TEXT,
    embedding vector(384), -- 384 dimensions for all-MiniLM-L6-v2
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_interaction_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Ephemeral Conversation Logs (raw inputs)
CREATE TABLE IF NOT EXISTS conversation_logs (
    id UUID PRIMARY KEY,
    user_id VARCHAR(64) REFERENCES users(id) ON DELETE CASCADE,
    session_id VARCHAR(64) REFERENCES sessions(id) ON DELETE SET NULL,
    workspace_id VARCHAR(64) NOT NULL DEFAULT 'default',
    episode_id UUID REFERENCES episodes(id) ON DELETE SET NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Central Memory Store (Metadata + pgvector Embeddings of facts)
CREATE TABLE IF NOT EXISTS memories (
    id UUID PRIMARY KEY,
    user_id VARCHAR(64) REFERENCES users(id) ON DELETE CASCADE,
    session_id VARCHAR(64) REFERENCES sessions(id) ON DELETE SET NULL,
    workspace_id VARCHAR(64) NOT NULL DEFAULT 'default',
    content TEXT NOT NULL,
    embedding vector(384) NOT NULL, -- 384 dimensions for all-MiniLM-L6-v2
    memory_type VARCHAR(32) NOT NULL DEFAULT 'EPISODIC', -- EPISODIC, FACTUAL, PREFERENCE
    importance_score NUMERIC(3, 2) NOT NULL DEFAULT 0.50,
    frequency_count INT NOT NULL DEFAULT 1,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    fingerprint VARCHAR(64),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_accessed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Indices for vector searches and text fuzzy matching
CREATE INDEX IF NOT EXISTS idx_memories_embedding ON memories USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_memories_trgm ON memories USING gin (content gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_memories_lookup ON memories(user_id, workspace_id, is_active);
CREATE INDEX IF NOT EXISTS idx_memories_fingerprint ON memories(user_id, workspace_id, fingerprint, is_active);

CREATE INDEX IF NOT EXISTS idx_episodes_embedding ON episodes USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_episodes_lookup ON episodes(user_id, workspace_id);
CREATE INDEX IF NOT EXISTS idx_conv_logs_lookup ON conversation_logs(user_id, workspace_id, episode_id);

-- Workflows (Procedural Memory)
CREATE TABLE IF NOT EXISTS workflows (
    id UUID PRIMARY KEY,
    user_id VARCHAR(64) REFERENCES users(id) ON DELETE CASCADE,
    workspace_id VARCHAR(64) NOT NULL DEFAULT 'default',
    name VARCHAR(256) NOT NULL,
    description TEXT,
    steps JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_workflows_lookup ON workflows(user_id, workspace_id);

-- Immutable Event Store
CREATE TABLE IF NOT EXISTS event_store (
    id UUID PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    workspace_id VARCHAR(64) NOT NULL DEFAULT 'default',
    event_type VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_event_store_lookup ON event_store(user_id, workspace_id);

-- Durable graph projection work. Rows are committed with the corresponding
-- memory/event and can be retried after a process restart.
CREATE TABLE IF NOT EXISTS graph_projection_outbox (
    id UUID PRIMARY KEY,
    memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    user_id VARCHAR(64) NOT NULL,
    workspace_id VARCHAR(64) NOT NULL,
    content TEXT NOT NULL,
    graph_payload JSONB NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    attempts INT NOT NULL DEFAULT 0,
    error_message TEXT,
    next_attempt_at TIMESTAMP WITH TIME ZONE,
    locked_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE
);
CREATE INDEX IF NOT EXISTS idx_graph_projection_outbox_pending
    ON graph_projection_outbox(status, next_attempt_at, created_at);

-- Workspace API Keys
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash CHAR(64) PRIMARY KEY,
    workspace_id VARCHAR(64) NOT NULL,
    description VARCHAR(256),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Dead Letter Queue (failed_jobs)
CREATE TABLE IF NOT EXISTS dead_letter_queue (
    id UUID PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    workspace_id VARCHAR(64) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL,
    error_message TEXT NOT NULL,
    failed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Async Background Jobs (for Replay/Rebuild operations)
CREATE TABLE IF NOT EXISTS background_jobs (
    id UUID PRIMARY KEY,
    job_type VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    workspace_id VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'QUEUED',
    total_events INT DEFAULT 0,
    processed_events INT DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_background_jobs_lookup ON background_jobs(user_id, workspace_id);
