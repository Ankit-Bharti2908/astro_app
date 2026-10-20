"""Rule-based confidence, importance, decay and retrieval scoring."""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import UTC, date, datetime

from app.brain.models import Memory, MemoryCandidate

BASE_CONFIDENCE: dict[str, float] = {"explicit": 0.9, "hedged": 0.6, "hypothetical": 0.3}
BASE_IMPORTANCE: dict[str, float] = {
    "profile_fact": 1.0,
    "goal": 0.9,
    "life_event": 0.8,
    "preference": 0.6,
    "interest": 0.5,
}
DECAY_LAMBDA: dict[str, float] = {
    "profile_fact": 0.0,
    "goal": 0.005,
    "life_event": 0.005,
    "preference": 0.01,
    "interest": 0.02,
}
REINFORCE_STEP = 0.1


def initial_confidence(c: MemoryCandidate) -> float:
    if c.user_asked_to_remember:
        return 1.0
    score = BASE_CONFIDENCE[c.certainty]
    if c.subject == "other":
        score *= 0.5
    return round(score, 3)


def initial_importance(c: MemoryCandidate) -> float:
    return BASE_IMPORTANCE[c.type]


def reinforce(confidence: float) -> float:
    return round(min(1.0, confidence + REINFORCE_STEP), 3)


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def days_since(reference: datetime, now: datetime) -> float:
    delta = _as_utc(now) - _as_utc(reference)
    return max(0.0, delta.total_seconds() / 86400.0)


def recency_factor(memory: Memory, now: datetime) -> float:
    lam = DECAY_LAMBDA.get(memory.type, 0.01)
    if lam == 0:
        return 1.0
    ref = memory.last_used_at or memory.updated_at
    return math.exp(-lam * days_since(ref, now))


def is_expired(memory: Memory, today: date) -> bool:
    return memory.valid_until is not None and memory.valid_until < today


def retrieval_score(memory: Memory, now: datetime) -> float:
    return memory.importance * memory.confidence * recency_factor(memory, now)


def rank_memories(
    memories: Iterable[Memory],
    now: datetime,
    *,
    top_k: int | None = None,
    include_expired: bool = False,
) -> list[Memory]:
    today = _as_utc(now).date()
    candidates = [
        m
        for m in memories
        if m.status == "active" and (include_expired or not is_expired(m, today))
    ]
    ranked = sorted(candidates, key=lambda m: retrieval_score(m, now), reverse=True)
    return ranked[:top_k] if top_k else ranked
