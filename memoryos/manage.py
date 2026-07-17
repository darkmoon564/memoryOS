"""Small operational commands for a MemoryOS deployment."""

import argparse

from memoryos.db.postgres import get_postgres_conn
from memoryos.security import generate_api_key, hash_api_key


def create_api_key(workspace_id: str, description: str | None) -> str:
    """Create a workspace key and persist only its lookup hash."""
    api_key = generate_api_key()
    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_keys (key_hash, workspace_id, description) VALUES (%s, %s, %s)",
                (hash_api_key(api_key), workspace_id, description),
            )
        conn.commit()
    finally:
        conn.close()
    return api_key


def main() -> None:
    parser = argparse.ArgumentParser(description="MemoryOS operational commands")
    subcommands = parser.add_subparsers(dest="command", required=True)
    create = subcommands.add_parser("create-api-key", help="Create a workspace API key")
    create.add_argument("workspace_id")
    create.add_argument("--description", default=None)
    args = parser.parse_args()

    if args.command == "create-api-key":
        api_key = create_api_key(args.workspace_id, args.description)
        print("Store this key now; it cannot be shown again:")
        print(api_key)


if __name__ == "__main__":
    main()
