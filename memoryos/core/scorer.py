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

def _execute_decay_logic() -> int:
    """Shared decay logic used by both the API endpoint and the scheduler."""
    decayed_count = 0
    conn = get_postgres_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id, importance_score, frequency_count, last_accessed_at FROM memories WHERE is_active = TRUE")
        rows = cur.fetchall()
        
        now = datetime.now(timezone.utc)
        for row in rows:
            last_access = row['last_accessed_at']
            if isinstance(last_access, str):
                try:
                    last_access = datetime.fromisoformat(last_access.replace('Z', '+00:00'))
                except Exception:
                    last_access = now
            if last_access.tzinfo is None:
                last_access = last_access.replace(tzinfo=timezone.utc)
            
            delta_days = (now - last_access).days
            importance = float(row['importance_score'])
            frequency = float(row['frequency_count'])
            
            # 1. Recency decay curve (half-life of 14 days)
            recency_score = math.exp(-0.05 * delta_days)
            
            # 2. Frequency scaling score
            freq_score = math.log(frequency + 1) / math.log(20 + 1)
            
            # 3. Overall combined weight score
            combined_score = (0.40 * importance) + (0.35 * recency_score) + (0.25 * freq_score)
            
            if combined_score < 0.15:
                cur.execute("UPDATE memories SET is_active = FALSE WHERE id = %s", (row['id'],))
                decayed_count += 1
    conn.commit()
    conn.close()
    return decayed_count
