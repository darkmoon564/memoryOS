-- Keep the old index for compatibility, and add the due-work index required
-- by lease reclamation and scheduled retry scans.
CREATE INDEX IF NOT EXISTS idx_graph_projection_outbox_due
    ON graph_projection_outbox(status, next_attempt_at, created_at);
