"""Regression checks for event-time semantics, provenance, and replay idempotency."""

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

from pydantic import ValidationError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.api.memories import retrieve_context
from memoryos.core.episodes import process_conversation_log
from memoryos.core.event_store import restore_precomputed_clauses
from memoryos.core.temporal_parser import parse_temporal_window
from memoryos.db.postgres import get_postgres_conn
from memoryos.models.embeddings import MockEmbeddingModel
from memoryos.schemas.memory import MemoryIngest, MemoryRetrieve


def test_event_time_and_provenance() -> None:
    workspace_id = f"provenance_{uuid.uuid4().hex[:12]}"
    user_id = f"user_{uuid.uuid4().hex[:12]}"
    first_time = datetime(2024, 3, 10, 14, 0, tzinfo=timezone.utc)
    second_time = datetime(2024, 6, 12, 9, 30, tzinfo=timezone.utc)
    clause = {
        "content": "Aurora uses a purple notebook for research.",
        "embedding": [0.0] * 384,
        "memory_type": "FACTUAL",
        "importance": 0.8,
        "entities": [],
        "relationships": [],
    }

    try:
        MemoryIngest(user_id=user_id, content="invalid time", occurred_at=datetime(2024, 1, 1))
    except ValidationError:
        pass
    else:
        raise AssertionError("Naive occurrence times must be rejected")

    start, end = parse_temporal_window("What happened in 2024?")
    assert start and end and start.year == end.year == 2024

    restore_precomputed_clauses(
        user_id, "session_2024", workspace_id, [clause], occurred_at=first_time, source_event_id="turn-001"
    )
    # Retrying the same durable event must not change canonical state.
    restore_precomputed_clauses(
        user_id, "session_2024", workspace_id, [clause], occurred_at=first_time, source_event_id="turn-001"
    )
    # A distinct turn repeating the same fact increases frequency while retaining both sources.
    restore_precomputed_clauses(
        user_id, "session_2024", workspace_id, [clause], occurred_at=second_time, source_event_id="turn-002"
    )

    # Source turns must be retry-safe for episode transcript material too;
    # otherwise retrying an event would silently bias episode summaries.
    conn = get_postgres_conn()
    try:
        first_episode_id = process_conversation_log(
            conn,
            user_id,
            "session_2024",
            workspace_id,
            "Aurora recorded the notebook detail.",
            MockEmbeddingModel(),
            occurred_at=first_time,
            source_event_id="dialogue-001",
        )
        retry_episode_id = process_conversation_log(
            conn,
            user_id,
            "session_2024",
            workspace_id,
            "Aurora recorded the notebook detail.",
            MockEmbeddingModel(),
            occurred_at=first_time,
            source_event_id="dialogue-001",
        )
        conn.commit()
        assert retry_episode_id == first_episode_id
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM conversation_logs WHERE user_id = %s AND workspace_id = %s AND source_event_id = %s",
                (user_id, workspace_id, "dialogue-001"),
            )
            assert cur.fetchone()[0] == 1
    finally:
        conn.close()

    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, frequency_count, occurred_at FROM memories WHERE user_id = %s AND workspace_id = %s",
                (user_id, workspace_id),
            )
            memory_id, frequency_count, occurred_at = cur.fetchone()
            assert frequency_count == 2
            assert str(occurred_at).startswith("2024-06-12")

            cur.execute("SELECT source_event_id FROM memory_sources WHERE memory_id = %s ORDER BY source_event_id", (memory_id,))
            assert [row[0] for row in cur.fetchall()] == ["turn-001", "turn-002"]

            cur.execute("SELECT count(*) FROM graph_projection_outbox WHERE memory_id = %s", (memory_id,))
            assert cur.fetchone()[0] == 2
    finally:
        conn.close()

    # A superseded fact remains available when an explicit historical window
    # is requested, along with the source turns that support it.
    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE memories SET is_active = FALSE WHERE id = %s", (memory_id,))
        conn.commit()
    finally:
        conn.close()

    with patch("memoryos.api.memories.verify_workspace_key", return_value=None), patch(
        "memoryos.api.memories.get_neo4j_conn", return_value=None
    ):
        historical_response = asyncio.run(
            retrieve_context(MemoryRetrieve(user_id=user_id, workspace_id=workspace_id, query="What notebook did Aurora use in 2024?", limit=3))
        )
    item = next(result for result in historical_response.results if result.memory_id == str(memory_id))
    assert item.occurred_at.startswith("2024-06-12")
    assert item.source_event_ids == ["turn-001", "turn-002"]


if __name__ == "__main__":
    test_event_time_and_provenance()
    print("Event-time, provenance, and replay-idempotency checks passed.")
