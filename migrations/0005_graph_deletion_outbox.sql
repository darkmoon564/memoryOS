-- Keep graph erasure durable when relational user data is removed. Graph state
-- is derived, so workers may safely retry this idempotent cleanup after a
-- process or Neo4j outage.
CREATE TABLE IF NOT EXISTS graph_deletion_outbox (
    id UUID PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    workspace_id VARCHAR(64) NOT NULL,
    memory_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    attempts INT NOT NULL DEFAULT 0,
    error_message TEXT,
    next_attempt_at TIMESTAMP WITH TIME ZONE,
    locked_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE
);
CREATE INDEX IF NOT EXISTS idx_graph_deletion_outbox_due
    ON graph_deletion_outbox(status, next_attempt_at, created_at);
