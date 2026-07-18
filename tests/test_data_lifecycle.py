"""Durable regression checks for tenant-scoped purge and decay.

This test requires the real PostgreSQL and Neo4j services.  The in-memory
adapters intentionally do not claim to model durable multi-tenant storage.
"""

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.api.memories import apply_decay, clear_all_memories
from memoryos.db.neo4j import get_neo4j_conn
from memoryos.db.postgres import get_postgres_conn
from memoryos.security import hash_api_key
from memoryos.services import background
from memoryos.services.background import _delete_user_graph_data, background_graph_ingest


WORKSPACE = f"lifecycle_{uuid.uuid4().hex[:12]}"
OTHER_WORKSPACE = f"lifecycle_other_{uuid.uuid4().hex[:12]}"
USER_TO_ERASE = f"erase_{uuid.uuid4().hex[:12]}"
USER_TO_KEEP = f"keep_{uuid.uuid4().hex[:12]}"
USER_TO_RETRY = f"retry_{uuid.uuid4().hex[:12]}"
API_KEY = f"lifecycle_key_{uuid.uuid4().hex}"
OTHER_API_KEY = f"lifecycle_other_key_{uuid.uuid4().hex}"
UNIT_VECTOR = "[1," + ",".join("0" for _ in range(383)) + "]"


def is_durable_database() -> bool:
    conn = get_postgres_conn()
    try:
        return not hasattr(conn, "sqlite_conn")
    finally:
        conn.close()


def insert_memory(user_id: str, workspace_id: str, content: str, *, stale: bool = False) -> str:
    memory_id = str(uuid.uuid4())
    last_accessed_at = datetime.now(timezone.utc) - timedelta(days=365) if stale else datetime.now(timezone.utc)
    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))
            cur.execute(
                """
                INSERT INTO memories
                    (id, user_id, workspace_id, content, embedding, memory_type, importance_score,
                     frequency_count, last_accessed_at)
                VALUES (%s, %s, %s, %s, %s::vector, 'FACTUAL', 0.10, 1, %s)
                """,
                (memory_id, user_id, workspace_id, content, UNIT_VECTOR, last_accessed_at),
            )
        conn.commit()
        return memory_id
    finally:
        conn.close()


def seed_user_records(user_id: str, marker: str) -> str:
    memory_id = insert_memory(user_id, WORKSPACE, f"{marker} raw memory")
    episode_id = str(uuid.uuid4())
    workflow_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    dlq_id = str(uuid.uuid4())
    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO episodes (id, user_id, workspace_id, summary, embedding) VALUES (%s, %s, %s, %s, %s::vector)",
                (episode_id, user_id, WORKSPACE, f"{marker} episode", UNIT_VECTOR),
            )
            cur.execute(
                "INSERT INTO conversation_logs (id, user_id, workspace_id, episode_id, content) VALUES (%s, %s, %s, %s, %s)",
                (str(uuid.uuid4()), user_id, WORKSPACE, episode_id, f"{marker} conversation"),
            )
            cur.execute(
                "INSERT INTO workflows (id, user_id, workspace_id, name, description, steps) VALUES (%s, %s, %s, %s, %s, %s)",
                (workflow_id, user_id, WORKSPACE, f"{marker} workflow", marker, json.dumps(["step"])),
            )
            cur.execute(
                "INSERT INTO event_store (id, user_id, workspace_id, event_type, payload) VALUES (%s, %s, %s, %s, %s)",
                (event_id, user_id, WORKSPACE, "MEMORY_INGESTED", json.dumps({"raw_text": f"{marker} event content"})),
            )
            cur.execute(
                "INSERT INTO background_jobs (id, job_type, user_id, workspace_id) VALUES (%s, %s, %s, %s)",
                (job_id, "TEST", user_id, WORKSPACE),
            )
            cur.execute(
                "INSERT INTO dead_letter_queue (id, user_id, workspace_id, event_type, payload, error_message) VALUES (%s, %s, %s, %s, %s, %s)",
                (dlq_id, user_id, WORKSPACE, "TEST", json.dumps({"content": marker}), "test record"),
            )
        conn.commit()
    finally:
        conn.close()

    assert background_graph_ingest(
        memory_id,
        f"{marker} graph source",
        user_id,
        WORKSPACE,
        precomputed_graph={
            "entities": [
                {"name": f"{marker} person", "type": "Person"},
                {"name": f"{marker} organization", "type": "Organization"},
            ],
            "relationships": [
                {"source": f"{marker} person", "target": f"{marker} organization", "type": "WORKS_AT"},
            ],
        },
    )
    return memory_id


def count_records(table: str, user_id: str, workspace_id: str) -> int:
    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table} WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            return cur.fetchone()[0]
    finally:
        conn.close()


def graph_node_count(user_id: str, workspace_id: str) -> int:
    neo4j = get_neo4j_conn()
    assert neo4j and not getattr(neo4j, "is_mock", False)
    rows = neo4j.query(
        "MATCH (n {user_id: $user_id, workspace_id: $workspace_id}) RETURN count(n) AS count",
        {"user_id": user_id, "workspace_id": workspace_id},
    )
    return rows[0]["count"]


def test_user_purge_is_scoped_and_complete() -> None:
    erase_memory_id = seed_user_records(USER_TO_ERASE, "erase")
    keep_memory_id = seed_user_records(USER_TO_KEEP, "keep")
    assert graph_node_count(USER_TO_ERASE, WORKSPACE) > 0
    assert graph_node_count(USER_TO_KEEP, WORKSPACE) > 0

    response = asyncio.run(
        clear_all_memories(USER_TO_ERASE, WORKSPACE, f"Bearer {API_KEY}")
    )
    assert response["status"] == "success", response
    assert response["graph_cleanup"] == "completed", response

    for table in ("memories", "episodes", "workflows", "conversation_logs", "event_store", "background_jobs", "dead_letter_queue"):
        assert count_records(table, USER_TO_ERASE, WORKSPACE) == 0, table
        assert count_records(table, USER_TO_KEEP, WORKSPACE) == 1, table

    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM graph_projection_outbox WHERE memory_id = %s", (erase_memory_id,))
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM graph_projection_outbox WHERE memory_id = %s", (keep_memory_id,))
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM graph_deletion_outbox WHERE user_id = %s AND workspace_id = %s", (USER_TO_ERASE, WORKSPACE))
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()

    assert graph_node_count(USER_TO_ERASE, WORKSPACE) == 0
    assert graph_node_count(USER_TO_KEEP, WORKSPACE) > 0


def test_decay_does_not_cross_workspace_boundaries() -> None:
    scoped_memory_id = insert_memory(USER_TO_ERASE, WORKSPACE, "scoped stale memory", stale=True)
    other_memory_id = insert_memory(USER_TO_KEEP, OTHER_WORKSPACE, "other stale memory", stale=True)

    response = asyncio.run(apply_decay(WORKSPACE, f"Bearer {API_KEY}"))
    assert response["status"] == "success", response
    assert response["archived_count"] == 0, response
    assert response["evaluated_count"] >= 1, response

    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT is_active FROM memories WHERE id = %s", (scoped_memory_id,))
            assert cur.fetchone()[0] is True
            cur.execute("SELECT is_active FROM memories WHERE id = %s", (other_memory_id,))
            assert cur.fetchone()[0] is True
    finally:
        conn.close()


def test_graph_erasure_recovers_after_outage() -> None:
    seed_user_records(USER_TO_RETRY, "retry")
    assert graph_node_count(USER_TO_RETRY, WORKSPACE) > 0

    original_delete = background._delete_user_graph_data
    background._delete_user_graph_data = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("simulated graph outage"))
    try:
        response = asyncio.run(clear_all_memories(USER_TO_RETRY, WORKSPACE, f"Bearer {API_KEY}"))
        assert response["status"] == "accepted", response
        assert response["graph_cleanup"] == "pending_retry", response
    finally:
        background._delete_user_graph_data = original_delete

    assert count_records("memories", USER_TO_RETRY, WORKSPACE) == 0
    assert graph_node_count(USER_TO_RETRY, WORKSPACE) > 0

    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status FROM graph_deletion_outbox WHERE user_id = %s AND workspace_id = %s",
                (USER_TO_RETRY, WORKSPACE),
            )
            deletion_id, status = cur.fetchone()
            assert status == "RETRY"
            cur.execute("UPDATE graph_deletion_outbox SET next_attempt_at = NULL WHERE id = %s", (deletion_id,))
        conn.commit()
    finally:
        conn.close()

    assert background.drain_graph_deletions(limit=10) >= 1
    assert graph_node_count(USER_TO_RETRY, WORKSPACE) == 0


def cleanup() -> None:
    for user_id, workspace_id in ((USER_TO_ERASE, WORKSPACE), (USER_TO_KEEP, WORKSPACE), (USER_TO_RETRY, WORKSPACE), (USER_TO_KEEP, OTHER_WORKSPACE)):
        try:
            _delete_user_graph_data(user_id, workspace_id, [])
        except Exception:
            pass

    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            for table in ("graph_deletion_outbox", "background_jobs", "dead_letter_queue", "event_store", "conversation_logs", "workflows", "episodes", "memories"):
                cur.execute(f"DELETE FROM {table} WHERE workspace_id IN (%s, %s)", (WORKSPACE, OTHER_WORKSPACE))
            cur.execute("DELETE FROM api_keys WHERE workspace_id IN (%s, %s)", (WORKSPACE, OTHER_WORKSPACE))
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    if not is_durable_database():
        print("[SKIPPED] Data lifecycle integration checks require PostgreSQL and Neo4j.")
        return
    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO api_keys (key_hash, workspace_id, description) VALUES (%s, %s, %s)", (hash_api_key(API_KEY), WORKSPACE, "data lifecycle test"))
            cur.execute("INSERT INTO api_keys (key_hash, workspace_id, description) VALUES (%s, %s, %s)", (hash_api_key(OTHER_API_KEY), OTHER_WORKSPACE, "data lifecycle test"))
        conn.commit()
    finally:
        conn.close()
    try:
        test_user_purge_is_scoped_and_complete()
        test_graph_erasure_recovers_after_outage()
        test_decay_does_not_cross_workspace_boundaries()
        print("[PASS] data lifecycle isolation and erasure checks")
    finally:
        cleanup()


if __name__ == "__main__":
    main()
