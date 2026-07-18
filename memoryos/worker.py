"""Dedicated durable graph-projection worker."""

import argparse
import signal
import time

import memoryos.config as config
from memoryos.db.neo4j import close_neo4j_conn, get_neo4j_conn
from memoryos.db.postgres import close_postgres_pool, init_postgres_pool
from memoryos.migrations import status as migration_status
from memoryos.services.background import drain_graph_work
from memoryos.config import logger


running = True


def _stop(_signum, _frame) -> None:
    global running
    running = False


def verify_dependencies() -> None:
    init_postgres_pool()
    if not config.allow_in_memory_fallback():
        pending, changed = migration_status()
        if pending or changed:
            raise RuntimeError(f"Worker cannot start with pending/changed migrations: pending={pending}, changed={changed}")
    get_neo4j_conn()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the MemoryOS durable graph-projection worker")
    parser.add_argument("--interval", type=float, default=5.0, help="Seconds to wait between empty polls")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--once", action="store_true", help="Drain one batch then exit")
    args = parser.parse_args()
    if args.interval <= 0 or args.batch_size <= 0:
        parser.error("--interval and --batch-size must be positive")

    verify_dependencies()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        while running:
            completed = drain_graph_work(args.batch_size)
            if args.once:
                break
            if not completed:
                time.sleep(args.interval)
    finally:
        close_postgres_pool()
        close_neo4j_conn()


if __name__ == "__main__":
    main()
