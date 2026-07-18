import math
from datetime import datetime, timezone
from psycopg2.extras import RealDictCursor
from memoryos.db.postgres import get_postgres_conn

def calculate_importance(content: str) -> float:
    """Simple local heuristic to score semantic importance [0.1 to 1.0]."""
    content_lower = content.lower()
    score = 0.50
    boost_terms = ["prefers", "always", "never", "must", "hates", "loves", "important", "allergic", "favorite"]
    for term in boost_terms:
        if term in content_lower:
            score += 0.15
    score += min(len(content) / 500.0, 0.15)
    return min(score, 1.0)

RECENCY_HALF_LIFE_DAYS = 180.0


def _as_utc_datetime(value, fallback: datetime) -> datetime:
    """Coerce database/API timestamp values to a timezone-aware datetime."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            value = fallback
    if not isinstance(value, datetime):
        value = fallback
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def calculate_recency_strength(
    last_accessed_at,
    *,
    now: datetime | None = None,
) -> float:
    """Return the exponential recency component shared by retrieval and decay."""
    now = now or datetime.now(timezone.utc)
    last_access = _as_utc_datetime(last_accessed_at, now)
    age_days = max(0.0, (now - last_access).total_seconds() / 86400.0)
    return math.exp(-math.log(2) * age_days / RECENCY_HALF_LIFE_DAYS)


def calculate_decay_strength(
    last_accessed_at,
    importance_score: float = 0.50,
    frequency_count: int = 1,
    *,
    now: datetime | None = None,
) -> float:
    """Return a conservative retrieval-strength multiplier in the range [0, 1].

    Long-term memories are deprioritized as they become stale, but are not
    automatically made unreachable. Importance and repeated retrieval retain
    a durable signal while recency gradually fades with a 180-day half-life.
    """
    now = now or datetime.now(timezone.utc)
    recency = calculate_recency_strength(last_accessed_at, now=now)
    importance = min(1.0, max(0.0, float(importance_score)))
    frequency = max(0, int(frequency_count))
    frequency_support = min(1.0, math.log1p(frequency) / math.log1p(10))

    return min(1.0, max(0.0, (0.30 * recency) + (0.45 * importance) + (0.25 * frequency_support)))


def _execute_decay_logic(workspace_id: str | None = None) -> int:
    """Count active memories evaluated by the ranking-only decay sweep.

    Decay is calculated at retrieval time from the current access timestamp,
    so a scheduled sweep must never deactivate ordinary long-term memories.
    """
    conn = get_postgres_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if workspace_id is None:
            cur.execute("SELECT count(*) AS count FROM memories WHERE is_active = TRUE")
        else:
            cur.execute(
                "SELECT count(*) AS count FROM memories WHERE workspace_id = %s AND is_active = TRUE",
                (workspace_id,),
            )
        row = cur.fetchone()
    conn.close()
    return int(row["count"] if isinstance(row, dict) else row[0])
