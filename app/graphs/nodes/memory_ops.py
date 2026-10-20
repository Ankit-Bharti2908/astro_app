"""Memory pipeline primitives shared by the memory graph and the synchronous `memory_cmd` node."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from app.brain.models import (
    LIFE_AREAS,
    PROFILE_KEYS,
    Memory,
    MemoryCandidate,
    ScoredCandidate,
)
from app.brain.profile_validation import validate_profile_patch
from app.brain.repository import BrainRepository
from app.brain.scoring import initial_confidence, initial_importance, reinforce
from app.config import Settings
from app.llm.provider import LLMMessage, LLMProvider
from app.prompts.templates import (
    EXTRACT_REMEMBER_HINT,
    EXTRACT_SYSTEM,
    EXTRACT_USER,
    PROMPT_VERSION,
    RECONCILE_SYSTEM,
    RECONCILE_USER,
)

log = logging.getLogger(__name__)

Action = Literal["ADD", "UPDATE", "SUPERSEDE", "NOOP", "PROFILE", "SKIP"]


class ExtractionResult(BaseModel):
    candidates: list[MemoryCandidate] = Field(default_factory=list)


class ReconcileDecision(BaseModel):
    action: Literal["ADD", "UPDATE", "SUPERSEDE", "NOOP"]
    target_memory_id: str | None = None
    merged_attributes: dict[str, Any] | None = None
    reason: str | None = None


# ---- extract ------------------------------------------------------------------

async def extract_candidates(
    llm: LLMProvider,
    *,
    message: str,
    now: datetime,
    tz: str,
    user_asked: bool = False,
    context: str = "",
) -> list[MemoryCandidate]:
    try:
        local = now.astimezone(ZoneInfo(tz))
    except Exception:
        local = now
    system = EXTRACT_SYSTEM.format(
        today=local.date().isoformat(),
        weekday=local.strftime("%A"),
        timezone=tz,
        next_year=local.year + 1,
        this_year=local.year,
    )
    hint = EXTRACT_REMEMBER_HINT if user_asked else ""
    result = await llm.structured(
        "extract",
        [LLMMessage("system", system), LLMMessage("user", EXTRACT_USER.format(message=message, remember_hint=hint, context=(context or "(none)")[:600]))],
        ExtractionResult,
    )
    out = []
    for c in result.candidates:
        if user_asked:
            c.user_asked_to_remember = True
        if c.life_area not in LIFE_AREAS:
            c.life_area = "general"
        c.key = re.sub(r"[^a-z0-9_]+", "_", c.key.strip().lower()).strip("_") or "fact"
        out.append(c)
    return out


# ---- score / filter -------------------------------------------------------------

def score_candidates(candidates: list[MemoryCandidate]) -> list[ScoredCandidate]:
    return [
        ScoredCandidate(**c.model_dump(), confidence=initial_confidence(c), importance=initial_importance(c))
        for c in candidates
    ]


def never_store_reason(c: ScoredCandidate) -> str | None:
    """Never store sensitive categories (the extractor's label), or anyone else's health or money.

    There is deliberately no keyword backstop: a term list catches `diabetes` but not "my sugar is high".
    """
    if c.sensitive_category != "none":
        return f"never_store:{c.sensitive_category}"
    if c.subject == "other" and c.life_area in ("health", "finance"):
        return "never_store:third_party_sensitive"
    return None


def filter_candidates(
    scored: list[ScoredCandidate], settings: Settings, *, ephemeral: bool = False
) -> tuple[list[ScoredCandidate], list[ScoredCandidate]]:
    accepted: list[ScoredCandidate] = []
    dropped: list[ScoredCandidate] = []
    for c in scored:
        reason = None
        if ephemeral:
            reason = "ephemeral"
        elif c.subject == "other" and not c.user_asked_to_remember:
            reason = "subject_other"
        elif c.confidence < settings.confidence_threshold:
            reason = f"below_threshold:{c.confidence:.2f}"
        else:
            reason = never_store_reason(c)
        if reason:
            c.drop_reason = reason
            dropped.append(c)
        else:
            accepted.append(c)
    return accepted, dropped


# ---- reconcile ----------------------------------------------------------------

def memory_id_for(user_id: str, c: MemoryCandidate, session_id: str | None) -> str:
    raw = f"{user_id}|{c.key}|{c.value.lower()}|{json.dumps(c.attributes, sort_keys=True, default=str)}|{session_id}"
    return hashlib.sha1(raw.encode()).hexdigest()[:24]


def build_memory(user_id: str, c: ScoredCandidate, *, session_id: str | None, now: datetime) -> Memory:
    return Memory(
        id=memory_id_for(user_id, c, session_id),
        type=c.type,
        key=c.key,
        value=c.value,
        attributes=dict(c.attributes),
        life_area=c.life_area,
        cardinality=c.cardinality(),
        certainty=c.certainty,
        source="confirmed" if c.user_asked_to_remember else "stated",
        confidence=c.confidence,
        importance=c.importance,
        mention_count=1,
        last_used_at=None,
        last_confirmed_at=now if c.user_asked_to_remember else None,
        valid_until=c.valid_until,
        status="active",
        created_at=now,
        updated_at=now,
        session_id=session_id,
        prompt_version=PROMPT_VERSION,
    )


_CERTAINTY_RANK = {"hypothetical": 0, "hedged": 1, "explicit": 2}

# Types a later statement can retire; a preference or interest rarely cancels another one.
_CONFLICTABLE_TYPES = ("goal", "life_event")
_MAX_RELATED_FOR_RECONCILE = 5


def _existing_json(mems: list[Memory]) -> str:
    return json.dumps(
        [
            {
                "id": m.id, "type": m.type, "key": m.key, "value": m.value,
                "attributes": m.attributes, "life_area": m.life_area,
                "certainty": m.certainty, "created_at": m.created_at.date().isoformat(),
            }
            for m in mems
        ],
        default=str,
    )


def _candidate_json(c: ScoredCandidate) -> str:
    return json.dumps(
        {"type": c.type, "key": c.key, "value": c.value, "attributes": c.attributes, "certainty": c.certainty,
         "life_area": c.life_area, "temporality": c.temporality, "valid_until": c.valid_until},
        default=str,
    )


async def reconcile_candidate(
    llm: LLMProvider, repo: BrainRepository, *, user_id: str, candidate: ScoredCandidate
) -> dict[str, Any]:
    """Rules first; the LLM arbitrates multi-valued ambiguity and cross-key conflicts."""
    # Birth-detail slots are decided by key, never by the type the extractor guessed.
    if candidate.key in PROFILE_KEYS:
        return {"action": "PROFILE", "target_memory_id": None, "reason": "profile slot"}

    same_key = await repo.list_memories(user_id, keys=[candidate.key], statuses=("active",))

    def same(m: Memory) -> bool:
        return m.value.strip().lower() == candidate.value.strip().lower() and m.attributes == candidate.attributes

    # deterministic rules apply within the slot only
    for m in same_key:
        if same(m):
            return {"action": "NOOP", "target_memory_id": m.id, "reason": "duplicate → reinforce"}

    if same_key and candidate.cardinality() == "single":
        latest = max(same_key, key=lambda m: m.created_at)
        return {"action": "SUPERSEDE", "target_memory_id": latest.id, "reason": "single-valued slot changed"}

    # Corrections often use a different key ("start_own_business" vs "career_change"),
    # so also show recent goals and life events from the same life area.
    existing = list(same_key)
    if candidate.type in _CONFLICTABLE_TYPES and candidate.life_area != "general":
        related = await repo.list_memories(
            user_id, life_areas=[candidate.life_area], statuses=("active",)
        )
        seen = {m.id for m in existing}
        extra = [m for m in related if m.id not in seen and m.type in _CONFLICTABLE_TYPES]
        extra.sort(key=lambda m: m.created_at, reverse=True)
        existing += extra[:_MAX_RELATED_FOR_RECONCILE]

    if not existing:
        return {"action": "ADD", "target_memory_id": None, "reason": "no existing memory to reconcile against"}

    try:
        decision = await llm.structured(
            "reconcile",
            [
                LLMMessage("system", RECONCILE_SYSTEM),
                LLMMessage("user", RECONCILE_USER.format(existing=_existing_json(existing), candidate=_candidate_json(candidate))),
            ],
            ReconcileDecision,
        )
    except Exception as exc:  # structured failure after retry → skip
        log.warning("reconcile LLM failed for key=%s: %s", candidate.key, exc)
        return {"action": "SKIP", "target_memory_id": None, "reason": f"reconcile_failed:{type(exc).__name__}"}

    ids = {m.id for m in existing}
    action = decision.action
    target = decision.target_memory_id if decision.target_memory_id in ids else None
    if action in ("UPDATE", "SUPERSEDE", "NOOP") and target is None:
        target = max(existing, key=lambda m: m.created_at).id
    if action == "SUPERSEDE":
        old = next(m for m in existing if m.id == target)
        if _CERTAINTY_RANK[candidate.certainty] < _CERTAINTY_RANK[old.certainty]:
            action = "ADD"  # a weaker statement never overrides a firmer one (source priority)
            target = None
    return {
        "action": action,
        "target_memory_id": target,
        "merged_attributes": decision.merged_attributes,
        "reason": decision.reason,
    }


# ---- write ----------------------------------------------------------------------

def _profile_patch(c: ScoredCandidate, *, today: date) -> tuple[dict[str, Any], dict[str, str]]:
    """The candidate as a validated profile patch, plus why any slot was refused."""
    return validate_profile_patch({c.key: c.value}, today=today)


async def apply_decision(
    repo: BrainRepository,
    *,
    user_id: str,
    session_id: str | None,
    candidate: ScoredCandidate,
    decision: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    action: Action = decision["action"]
    target = decision.get("target_memory_id")
    result: dict[str, Any] = {"action": action, "key": candidate.key, "value": candidate.value, "reason": decision.get("reason")}

    if action == "PROFILE":
        patch, rejected = _profile_patch(candidate, today=now.date())
        if rejected:  # inadmissible value: refuse it loudly rather than storing a guess
            result.update(action="SKIP", reason=rejected[candidate.key])
            return result
        if patch:
            profile = await repo.update_user(user_id, patch, now=now)
            result["profile"] = profile.model_dump(mode="json")
        return result
    if action == "ADD":
        mem = await repo.add_memory(user_id, build_memory(user_id, candidate, session_id=session_id, now=now))
        result["memory_id"] = mem.id
        return result
    if action == "SUPERSEDE" and target:
        mem = await repo.supersede_memory(user_id, target, build_memory(user_id, candidate, session_id=session_id, now=now))
        result["memory_id"] = mem.id
        result["superseded"] = target
        return result
    if action == "UPDATE" and target:
        existing = await repo.get_memory(user_id, target)
        if existing:
            merged = {**existing.attributes, **(decision.get("merged_attributes") or candidate.attributes)}
            mem = await repo.update_memory(
                user_id, target,
                {"attributes": merged, "confidence": reinforce(existing.confidence), "mention_count": existing.mention_count + 1,
                 "updated_at": now, "valid_until": candidate.valid_until or existing.valid_until},
            )
            result["memory_id"] = target
        return result
    if action == "NOOP" and target:
        existing = await repo.get_memory(user_id, target)
        if existing:
            patch = {"confidence": reinforce(existing.confidence), "mention_count": existing.mention_count + 1, "updated_at": now}
            if candidate.user_asked_to_remember:
                patch.update({"confidence": 1.0, "last_confirmed_at": now, "source": "confirmed"})
            await repo.update_memory(user_id, target, patch)
            result["memory_id"] = target
        return result
    result["action"] = "SKIP"
    return result
