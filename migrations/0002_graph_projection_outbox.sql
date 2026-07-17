-- Durable graph projection queue for existing MemoryOS databases.
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
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE
);
CREATE INDEX IF NOT EXISTS idx_graph_projection_outbox_pending
    ON graph_projection_outbox(status, created_at);
