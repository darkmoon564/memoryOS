import os
import sys
import time
import subprocess
import requests

# Ensure package is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from memoryos.db.postgres import get_postgres_conn
from memoryos.security import hash_api_key

def test_api_endpoints():
    print("=" * 60)
    print("  Testing MemoryOS REST API Endpoints Local Startup")
    print("=" * 60)

    # Seed API key for the test
    print("Seeding test API key...")
    try:
        conn = get_postgres_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM api_keys WHERE key_hash = %s", (hash_api_key("test_api_server_key"),))
            cur.execute("INSERT INTO api_keys (key_hash, workspace_id, description) VALUES (%s, %s, %s)", (hash_api_key("test_api_server_key"), "api_test", "Test Key for API server checks"))
        conn.commit()
        conn.close()
    except Exception as seed_err:
        print(f"[ERROR] Failed to seed API key: {seed_err}")
        sys.exit(1)

    # 1. Start the uvicorn server as a subprocess
    cmd = [
        sys.executable,
        "-m", "uvicorn",
        "memoryos.main:app",
        "--host", "127.0.0.1",
        "--port", "8088"
    ]

    print("Starting FastAPI Uvicorn Server in OFFLINE_MODE...")
    env = os.environ.copy()
    env["OFFLINE_MODE"] = "true"
    process = subprocess.Popen(
        cmd,
        cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
        env=env,
        text=True
    )

    # Wait for the server to bind
    print("Waiting 3 seconds for database initialization...")
    time.sleep(3.0)

    if process.poll() is not None:
        print(f"[ERROR] Server failed to start. Code: {process.returncode}")
        # Clean up
        try:
            conn = get_postgres_conn()
            with conn.cursor() as cur:
                cur.execute("DELETE FROM api_keys WHERE key_hash = %s", (hash_api_key("test_api_server_key"),))
            conn.commit()
            conn.close()
        except:
            pass
        sys.exit(1)

    print("[PASS] Uvicorn server started successfully in background.")

    # Server Base URL
    base_url = "http://127.0.0.1:8088"
    user_id = "api_test_user"
    headers = {"Authorization": "Bearer test_api_server_key"}

    try:
        # 2. Test Ingestion endpoint
        print("\nSending Memory Ingest request to /v1/memories...")
        ingest_payload = {
            "user_id": user_id,
            "content": "Dave prefers dark mode and codes primarily in Python.",
            "workspace_id": "api_test"
        }
        res = requests.post(f"{base_url}/v1/memories", json=ingest_payload, headers=headers, timeout=30.0)
        print(f"Status Code: {res.status_code}")
        print(f"Response: {res.json()}")
        assert res.status_code == 200, "Ingestion should return HTTP 200"

        # 3. Test Retrieval endpoint
        print("\nSending Memory Retrieval request to /v1/memories/retrieve...")
        retrieve_payload = {
            "user_id": user_id,
            "query": "What preferences does Dave have?",
            "limit": 3,
            "workspace_id": "api_test"
        }
        res = requests.post(f"{base_url}/v1/memories/retrieve", json=retrieve_payload, headers=headers, timeout=30.0)
        print(f"Status Code: {res.status_code}")
        print(f"Response: {res.json()}")
        assert res.status_code == 200, "Retrieval should return HTTP 200"

        # Check if Dave's preference is in results
        results = res.json().get("results", [])
        assert len(results) > 0, "Should return at least one retrieved memory"
        print(f"[PASS] Successfully retrieved memory: '{results[0]['content']}'")

        # 4. Test Consolidation endpoint
        print("\nSending Consolidation request to /v1/memories/consolidate...")
        res = requests.post(f"{base_url}/v1/memories/consolidate?user_id={user_id}&workspace_id=api_test", headers=headers, timeout=30.0)
        print(f"Status Code: {res.status_code}")
        print(f"Response: {res.json()}")
        assert res.status_code == 200, "Consolidation should return HTTP 200"

        # 5. Test List Tools endpoint
        print("\nSending MCP List Tools request to /tools...")
        res = requests.get(f"{base_url}/tools", headers=headers, timeout=30.0)
        print(f"Status Code: {res.status_code}")
        print(f"Response: {res.json()}")
        assert res.status_code == 200, "Tools listing should return HTTP 200"

        print("\n" + "=" * 60)
        print("  ALL API ROUTE INTEGRATIONS VERIFIED SUCCESSFULLY!")
        print("=" * 60)

    except Exception as e:
        print(f"\n[ERROR] API testing failed: {e}")
        raise e
    finally:
        print("\nStopping background Uvicorn Server...")
        process.terminate()
        process.wait()
        print("Server stopped.")

        print("Cleaning up test API key...")
        try:
            conn = get_postgres_conn()
            with conn.cursor() as cur:
                cur.execute("DELETE FROM api_keys WHERE key_hash = %s", (hash_api_key("test_api_server_key"),))
            conn.commit()
            conn.close()
            print("Cleanup successful.")
        except Exception as cleanup_err:
            print(f"[WARNING] Failed to clean up API key: {cleanup_err}")

if __name__ == "__main__":
    test_api_endpoints()
