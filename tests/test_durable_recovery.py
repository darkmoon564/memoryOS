"""Integration checks for durable graph recovery and workspace isolation.

Run against the Postgres and Neo4j services configured for the application.
The suite deliberately skips when the explicit in-memory test adapter is in
use: mock mode cannot prove persistence or cross-process recovery.
"""

import json
import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import HTTPException
from psycopg2.extras import RealDictCursor

from memoryos.api.memories import verify_workspace_key
from memoryos.db.postgres import get_postgres_conn
from memoryos.security import hash_api_key
from memoryos.services import background


WORKSPACE = f"durability_{uuid.uuid4().hex[:12]}"
USER_ID = f"user_{uuid.uuid4().hex[:12]}"
ZERO_VECTOR = "[" + ",".join("0" for _ in range(384)) + "]"


def is_durable_database() -> bool:
    conn = get_postgres_conn()
    try:
        return not hasattr(conn, "sqlite_conn")
    finally:
        conn.close()


def insert_memory_and_projection(status: str = "PENDING", attempts: int = 0) -> tuple[str, str]:
    memory_id = str(uuid.uuid4())
    projection_id = str(uuid.uuid4())
    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (USER_ID,))
            cur.execute(
                """
                INSERT INTO memories (id, user_id, workspace_id, content, embedding, memory_type, importance_score)
                VALUES (%s, %s, %s, %s, %s::vector, 'FACTUAL', 0.5)
                """,
                (memory_id, USER_ID, WORKSPACE, "durability probe", ZERO_VECTOR),
            )
            cur.execute(
                """
                INSERT INTO graph_projection_outbox
                    (id, memory_id, user_id, workspace_id, content, graph_payload, status, attempts)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (projection_id, memory_id, USER_ID, WORKSPACE, "durability probe", json.dumps({"entities": [], "relationships": []}), status, attempts),
            )
        conn.commit()
        return memory_id, projection_id
    finally:
        conn.close()


def fetch_projection(projection_id: str) -> dict:
    conn = get_postgres_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT status, attempts, error_message FROM graph_projection_outbox WHERE id = %s", (projection_id,))
            return dict(cur.fetchone())
    finally:
        conn.close()


def test_workspace_key_isolation() -> None:
    good_key = f"key_{uuid.uuid4().hex}"
    other_key = f"key_{uuid.uuid4().hex}"
    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO api_keys (key_hash, workspace_id, description) VALUES (%s, %s, %s)", (hash_api_key(good_key), WORKSPACE, "durability test"))
            cur.execute("INSERT INTO api_keys (key_hash, workspace_id, description) VALUES (%s, %s, %s)", (hash_api_key(other_key), f"{WORKSPACE}_other", "durability test"))
        conn.commit()
    finally:
        conn.close()

    verify_workspace_key(WORKSPACE, f"Bearer {good_key}")
    try:
        verify_workspace_key(WORKSPACE, f"Bearer {other_key}")
        raise AssertionError("A key from another workspace was accepted")
    except HTTPException as exc:
        assert exc.status_code == 403


def test_pending_projection_recovers_after_new_connection() -> None:
    _, projection_id = insert_memory_and_projection()
    # drain_graph_projections opens fresh connections, modeling recovery after
    # a request process commits but exits before it can run graph work.
    assert background.drain_graph_projections(limit=10) >= 1
    projection = fetch_projection(projection_id)
    assert projection["status"] == "COMPLETED", projection
    assert projection["attempts"] == 1, projection


def test_projection_retries_then_moves_to_dlq() -> None:
    _, projection_id = insert_memory_and_projection()
    original = background._execute_graph_inserts
    background._execute_graph_inserts = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("simulated graph outage"))
    try:
        assert background.process_graph_projection(projection_id) is False
        assert fetch_projection(projection_id)["status"] == "RETRY"

        conn = get_postgres_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE graph_projection_outbox SET attempts = 9, status = 'RETRY', next_attempt_at = NULL WHERE id = %s", (projection_id,))
            conn.commit()
        finally:
            conn.close()

        assert background.process_graph_projection(projection_id) is False
        assert fetch_projection(projection_id)["status"] == "FAILED"
        conn = get_postgres_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM dead_letter_queue WHERE workspace_id = %s AND event_type = 'GRAPH_PROJECTION_FAILED'", (WORKSPACE,))
                assert cur.fetchone()[0] >= 1
        finally:
            conn.close()
    finally:
        background._execute_graph_inserts = original


def cleanup() -> None:
    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM dead_letter_queue WHERE workspace_id = %s", (WORKSPACE,))
            cur.execute("DELETE FROM api_keys WHERE workspace_id IN (%s, %s)", (WORKSPACE, f"{WORKSPACE}_other"))
            cur.execute("DELETE FROM memories WHERE user_id = %s AND workspace_id = %s", (USER_ID, WORKSPACE))
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    if not is_durable_database():
        print("[SKIPPED] Durable recovery integration checks require PostgreSQL and Neo4j.")
        return
    try:
        test_workspace_key_isolation()
        test_pending_projection_recovers_after_new_connection()
        test_projection_retries_then_moves_to_dlq()
        print("[PASS] durable recovery and tenant-isolation checks")
    finally:
        cleanup()


if __name__ == "__main__":
    main()
