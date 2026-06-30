-- Enable extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Drop tables if they exist (for clean setup)
DROP TABLE IF EXISTS memories CASCADE;
DROP TABLE IF EXISTS sessions CASCADE;
DROP TABLE IF EXISTS users CASCADE;

-- Core User Table
CREATE TABLE users (
    id VARCHAR(64) PRIMARY KEY,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Active Sessions for STM management
CREATE TABLE sessions (
    id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) REFERENCES users(id) ON DELETE CASCADE,
    started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP WITH TIME ZONE
);

-- Central Memory Store (Metadata + pgvector Embeddings)
CREATE TABLE memories (
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
