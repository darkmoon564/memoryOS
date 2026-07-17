-- Independent workers use leases and scheduled retries to safely recover work
-- after a worker or API process exits mid-projection.
ALTER TABLE graph_projection_outbox
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS locked_at TIMESTAMP WITH TIME ZONE;
CREATE INDEX IF NOT EXISTS idx_graph_projection_outbox_pending
    ON graph_projection_outbox(status, next_attempt_at, created_at);
