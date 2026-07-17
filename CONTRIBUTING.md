# Contributing to MemoryOS

MemoryOS is a durable-memory system. Changes must preserve tenant isolation,
data recoverability, replayability, and a clear failure mode when dependencies
are unavailable.

## Before opening a pull request

1. Do not commit `.env`, credentials, database volumes, or generated model data.
2. Keep schema changes forward-only. Never add destructive statements to
   `schema.sql`; use a versioned migration for upgrades.
3. Add or update a regression test for changed behavior.
4. Run `python tests/run_all.py` against PostgreSQL and Neo4j, then include the
   result in the pull request.
5. Explain any change to persistence, authentication, retrieval ranking, or
   event replay in the pull request description.

## Design boundary

Do not silently replace a configured durable dependency with process-local
state. In-memory adapters are strictly for explicitly enabled tests.
