"""HTTP route smoke test using FastAPI's in-process test client.

Keeping the client in the same process means the explicit SQLite test adapter
and seeded test key are shared. Production CI runs the identical HTTP routes
against its real Postgres and Neo4j services.
"""

import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.db.postgres import get_postgres_conn
from memoryos.main import app
from memoryos.security import hash_api_key


WORKSPACE_ID = "api_test"
API_KEY = "test_api_server_key"
USER_ID = "api_test_user"


def seed_api_key() -> None:
    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM api_keys WHERE key_hash = %s", (hash_api_key(API_KEY),))
            cur.execute(
                "INSERT INTO api_keys (key_hash, workspace_id, description) VALUES (%s, %s, %s)",
                (hash_api_key(API_KEY), WORKSPACE_ID, "HTTP route smoke-test key"),
            )
        conn.commit()
    finally:
        conn.close()


def cleanup() -> None:
    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE user_id = %s AND workspace_id = %s", (USER_ID, WORKSPACE_ID))
            cur.execute("DELETE FROM api_keys WHERE key_hash = %s", (hash_api_key(API_KEY),))
        conn.commit()
    finally:
        conn.close()


def test_api_endpoints() -> None:
    print("=" * 60)
    print("  Testing MemoryOS HTTP Routes")
    print("=" * 60)
    seed_api_key()
    headers = {"Authorization": f"Bearer {API_KEY}"}

    try:
        # TestClient waits for the application lifespan to complete, proving
        # readiness rather than assuming a fixed startup sleep is sufficient.
        with TestClient(app) as client:
            health = client.get("/health")
            assert health.status_code == 200, health.text

            ingest = client.post(
                "/v1/memories",
                json={
                    "user_id": USER_ID,
                    "content": "Dave prefers dark mode and codes primarily in Python.",
                    "workspace_id": WORKSPACE_ID,
                    "source_event_id": "http-smoke:turn-1",
                    "occurred_at": "2026-07-19T09:30:00Z",
                },
                headers=headers,
            )
            assert ingest.status_code == 200, ingest.text

            retrieval = client.post(
                "/v1/memories/retrieve",
                json={
                    "user_id": USER_ID,
                    "query": "What preferences does Dave have?",
                    "limit": 3,
                    "workspace_id": WORKSPACE_ID,
                },
                headers=headers,
            )
            assert retrieval.status_code == 200, retrieval.text
            results = retrieval.json().get("results", [])
            assert any("dark mode" in item["content"].lower() for item in results), results

            consolidation = client.post(
                f"/v1/memories/consolidate?user_id={USER_ID}&workspace_id={WORKSPACE_ID}",
                headers=headers,
            )
            assert consolidation.status_code == 200, consolidation.text

            tools_response = client.get("/tools", headers=headers)
            assert tools_response.status_code == 200, tools_response.text

        print("[PASS] HTTP routes and application readiness verified.")
    finally:
        cleanup()


if __name__ == "__main__":
    test_api_endpoints()
