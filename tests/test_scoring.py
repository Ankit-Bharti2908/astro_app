from datetime import UTC, date, datetime, timedelta

from app.brain.models import Memory, MemoryCandidate
from app.brain.scoring import (
    initial_confidence,
    initial_importance,
    is_expired,
    rank_memories,
    recency_factor,
    reinforce,
    retrieval_score,
)

NOW = datetime(2026, 10, 17, tzinfo=UTC)


def cand(**kw) -> MemoryCandidate:
    base = dict(type="goal", key="k", value="v", life_area="career")
    base.update(kw)
    return MemoryCandidate(**base)


def mem(**kw) -> Memory:
    base = dict(id="m", type="goal", key="k", value="v", life_area="career", importance=0.9, confidence=0.9, created_at=NOW, updated_at=NOW)
    base.update(kw)
    return Memory(**base)


def test_initial_confidence_table():
    assert initial_confidence(cand(certainty="explicit")) == 0.9
    assert initial_confidence(cand(certainty="hedged")) == 0.6
    assert initial_confidence(cand(certainty="hypothetical")) == 0.3
    assert initial_confidence(cand(certainty="explicit", subject="other")) == 0.45
    assert initial_confidence(cand(certainty="hypothetical", user_asked_to_remember=True)) == 1.0


def test_initial_importance_table():
    assert initial_importance(cand(type="profile_fact")) == 1.0
    assert initial_importance(cand(type="goal")) == 0.9
    assert initial_importance(cand(type="life_event")) == 0.8
    assert initial_importance(cand(type="preference")) == 0.6
    assert initial_importance(cand(type="interest")) == 0.5


def test_reinforce_caps_at_one():
    assert reinforce(0.9) == 1.0
    assert reinforce(0.6) == 0.7
    assert reinforce(1.0) == 1.0


def test_recency_decay_by_type():
    old = NOW - timedelta(days=100)
    assert recency_factor(mem(type="profile_fact", updated_at=old), NOW) == 1.0
    goal = recency_factor(mem(type="goal", updated_at=old), NOW)
    pref = recency_factor(mem(type="preference", updated_at=old), NOW)
    interest = recency_factor(mem(type="interest", updated_at=old), NOW)
    assert 1 > goal > pref > interest > 0
    # using a memory resets the clock
    assert recency_factor(mem(type="interest", updated_at=old, last_used_at=NOW), NOW) == 1.0


def test_retrieval_score_and_ranking():
    strong = mem(id="a", importance=0.9, confidence=0.9)
    weak = mem(id="b", type="interest", importance=0.5, confidence=0.6, updated_at=NOW - timedelta(days=60))
    expired = mem(id="c", valid_until=date(2026, 1, 1))
    superseded = mem(id="d", status="superseded")
    assert retrieval_score(strong, NOW) > retrieval_score(weak, NOW)
    ranked = rank_memories([weak, expired, superseded, strong], NOW)
    assert [m.id for m in ranked] == ["a", "b"]
    assert [m.id for m in rank_memories([weak, strong], NOW, top_k=1)] == ["a"]
    assert is_expired(expired, NOW.date()) and not is_expired(strong, NOW.date())
    assert "c" in [m.id for m in rank_memories([expired], NOW, include_expired=True)]
