"""Tracked, forward-only SQL migrations for MemoryOS deployments."""

import argparse
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from memoryos.db.postgres import get_postgres_conn


ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "migrations"
BASE_SCHEMA = ROOT / "schema.sql"
MIGRATION_LOCK_ID = 584_315_911


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    sql: str
    checksum: str


def discover_migrations() -> list[Migration]:
    migrations = []
    for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        sql = path.read_text(encoding="utf-8")
        migrations.append(Migration(path.stem.split("_", 1)[0], path, sql, hashlib.sha256(sql.encode("utf-8")).hexdigest()))
    return migrations


def _ensure_tracking_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR(64) PRIMARY KEY,
                checksum CHAR(64) NOT NULL,
                applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def _applied_migrations(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT version, checksum FROM schema_migrations")
        return {row[0]: row[1] for row in cur.fetchall()}


@contextmanager
def _migration_lock(conn):
    """Serialize upgrades across replicas without affecting test adapters."""
    if hasattr(conn, "sqlite_conn"):
        yield
        return
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
    yield


def upgrade() -> list[str]:
    """Apply every unapplied migration and reject edited migration history."""
    conn = get_postgres_conn()
    try:
        with _migration_lock(conn):
            _ensure_tracking_table(conn)
            applied = _applied_migrations(conn)
            completed: list[str] = []
            for migration in discover_migrations():
                existing_checksum = applied.get(migration.version)
                if existing_checksum:
                    if existing_checksum != migration.checksum:
                        raise RuntimeError(f"Migration {migration.path.name} was changed after it was applied.")
                    continue
                with conn.cursor() as cur:
                    cur.execute(migration.sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                        (migration.version, migration.checksum),
                    )
                completed.append(migration.path.name)
            # The transaction-scoped advisory lock is released only after this
            # commit, so another replica cannot observe partial migration state.
            conn.commit()
        return completed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def bootstrap() -> list[str]:
    """Create a new database from the safe base schema, then record migrations."""
    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(BASE_SCHEMA.read_text(encoding="utf-8"))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return upgrade()


def status() -> tuple[list[str], list[str]]:
    conn = get_postgres_conn()
    try:
        _ensure_tracking_table(conn)
        applied = _applied_migrations(conn)
        pending = [migration.path.name for migration in discover_migrations() if migration.version not in applied]
        changed = [migration.path.name for migration in discover_migrations() if migration.version in applied and applied[migration.version] != migration.checksum]
        conn.commit()
        return pending, changed
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="MemoryOS schema migration commands")
    parser.add_argument("command", choices=("bootstrap", "upgrade", "status"))
    parser.add_argument("--check", action="store_true", help="Fail when migrations are pending or changed (status only)")
    args = parser.parse_args()
    if args.command == "bootstrap":
        print("Applied:", ", ".join(bootstrap()) or "none")
    elif args.command == "upgrade":
        print("Applied:", ", ".join(upgrade()) or "none")
    else:
        pending, changed = status()
        print("Pending:", ", ".join(pending) or "none")
        print("Changed:", ", ".join(changed) or "none")
        if changed or (args.check and pending):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
