-- Preserve when a fact happened separately from when the service received it.
-- A separate provenance relation keeps duplicate observations traceable without
-- multiplying canonical memory rows.
ALTER TABLE memories ADD COLUMN IF NOT EXISTS occurred_at TIMESTAMP WITH TIME ZONE;
UPDATE memories SET occurred_at = created_at WHERE occurred_at IS NULL;
ALTER TABLE memories ALTER COLUMN occurred_at SET DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE memories ALTER COLUMN occurred_at SET NOT NULL;

ALTER TABLE conversation_logs ADD COLUMN IF NOT EXISTS source_event_id VARCHAR(256);
ALTER TABLE conversation_logs ADD COLUMN IF NOT EXISTS occurred_at TIMESTAMP WITH TIME ZONE;
UPDATE conversation_logs SET occurred_at = created_at WHERE occurred_at IS NULL;
ALTER TABLE conversation_logs ALTER COLUMN occurred_at SET DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE conversation_logs ALTER COLUMN occurred_at SET NOT NULL;

CREATE TABLE IF NOT EXISTS memory_sources (
    memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    source_event_id VARCHAR(256) NOT NULL,
    occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (memory_id, source_event_id)
);

CREATE INDEX IF NOT EXISTS idx_memories_occurred_at
    ON memories(user_id, workspace_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_sources_event
    ON memory_sources(source_event_id);
CREATE INDEX IF NOT EXISTS idx_conv_logs_occurred_at
    ON conversation_logs(user_id, workspace_id, occurred_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_conv_logs_source_event
    ON conversation_logs(user_id, workspace_id, source_event_id)
    WHERE source_event_id IS NOT NULL;
