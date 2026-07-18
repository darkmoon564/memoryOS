"""Fast regression checks for retrieval ranking and non-destructive decay."""

import os
import sys
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.api.memories import (
    _apply_freshness_to_relevance,
    _normalized_rank_scores,
    _rank_reranker_candidates,
)
from memoryos.core.scorer import calculate_decay_strength, calculate_recency_strength
from memoryos.schemas.memory import MemoryRetrieve


def test_ranking_and_decay() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fresh = calculate_decay_strength(now, importance_score=0.5, frequency_count=1, now=now)
    stale_low_value = calculate_decay_strength(
        now - timedelta(days=365), importance_score=0.1, frequency_count=1, now=now
    )
    stale_important = calculate_decay_strength(
        now - timedelta(days=365), importance_score=0.9, frequency_count=1, now=now
    )

    assert 0.0 < stale_low_value < fresh < 1.0
    assert stale_low_value < stale_important
    assert abs(calculate_recency_strength(now - timedelta(days=180), now=now) - 0.5) < 1e-12
    assert _apply_freshness_to_relevance(1.0, stale_low_value) > _apply_freshness_to_relevance(0.94, fresh)

    rank_scores = _normalized_rank_scores(["first", "middle", "last"])
    assert rank_scores["first"] == 1.0
    assert 1.0 > rank_scores["middle"] > rank_scores["last"] > 0.0

    candidates = [("rrf_first", 0.1), ("rrf_second", 0.09), ("rrf_third", 0.08)]
    assert _rank_reranker_candidates(candidates, [0.2, 0.9, 0.9]) == ["rrf_second", "rrf_third", "rrf_first"]

    try:
        _rank_reranker_candidates(candidates, [0.5])
    except ValueError:
        pass
    else:
        raise AssertionError("Short reranker output must fail into the RRF fallback")

    try:
        MemoryRetrieve(user_id="user", query="   ")
    except ValidationError:
        pass
    else:
        raise AssertionError("Blank retrieval queries must be rejected before sparse search")


if __name__ == "__main__":
    test_ranking_and_decay()
    print("Ranking and decay checks passed.")
