"""Durable daily quota tracking for the shared configured LLM provider."""
import os
from datetime import date

from memoryos.db.postgres import get_postgres_conn


class LLMRateLimitExceeded(RuntimeError):
    pass


def daily_limit() -> int:
    return max(1, int(os.getenv("LLM_DAILY_REQUEST_LIMIT", "50")))


def get_daily_usage() -> dict[str, int]:
    today = date.today()
    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT request_count FROM llm_daily_usage WHERE usage_date = %s", (today,))
            row = cur.fetchone()
        used = int(row[0]) if row else 0
        return {"used": used, "limit": daily_limit(), "remaining": max(0, daily_limit() - used)}
    finally:
        conn.close()


def consume_llm_request() -> dict[str, int]:
    """Reserve one provider call atomically, failing before the quota is exceeded."""
    today = date.today()
    limit = daily_limit()
    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO llm_daily_usage (usage_date, request_count)
                VALUES (%s, 1)
                ON CONFLICT (usage_date) DO UPDATE
                SET request_count = llm_daily_usage.request_count + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE llm_daily_usage.request_count < %s
                RETURNING request_count
                """,
                (today, limit),
            )
            row = cur.fetchone()
        conn.commit()
        if not row:
            raise LLMRateLimitExceeded(f"Daily LLM limit of {limit} requests has been reached.")
        used = int(row[0])
        return {"used": used, "limit": limit, "remaining": max(0, limit - used)}
    finally:
        conn.close()
