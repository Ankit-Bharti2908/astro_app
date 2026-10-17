"""Memory pipeline primitives shared by the memory graph and the synchronous `memory_cmd` node (§5, §7)."""

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
from app.brain.repository import BrainRepository
from app.brain.scoring import initial_confidence, initial_importance, reinforce
from app.config import Settings
from app.llm.provider import LLMMessage, LLMProvider
from app.prompts.templates import (
    EXTRACT_SYSTEM,
    EXTRACT_USER,
    PROMPT_VERSION,
    RECONCILE_SYSTEM,
    RECONCILE_USER,
)

log = logging.getLogger(__name__)

Action = Literal["ADD", "UPDATE", "SUPERSEDE", "NOOP", "PROFILE", "SKIP"]

_HEALTH_TERMS = re.compile(
    r"\b(diabetes|cancer|hiv|depression|anxiety disorder|bipolar|schizophrenia|thyroid|asthma|pcos|tumou?r|"
    r"blood pressure|medication|prescri|diagnos|surgery|pregnan)", re.I
)
_FINANCE_TERMS = re.compile(r"(₹|\$|rs\.?\s?\d|\b\d{2,}\s?(k|lakh|lakhs|crore|cr)\b|account number|\bsalary\b|\bdebt\b|\bloan\b.*\d)", re.I)


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
    hint = " (the user explicitly asked you to remember this)" if user_asked else ""
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
    if c.sensitive_category != "none":
        return f"never_store:{c.sensitive_category}"
    text = f"{c.key} {c.value} {json.dumps(c.attributes)}"
    if c.life_area == "health" and c.type in ("life_event", "profile_fact") and _HEALTH_TERMS.search(text):
        return "never_store:health_detail"
    if _FINANCE_TERMS.search(text) and c.type in ("profile_fact", "life_event"):
        return "never_store:financial_detail"
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


def _existing_json(mems: list[Memory]) -> str:
    return json.dumps(
        [
            {
                "id": m.id, "type": m.type, "key": m.key, "value": m.value,
                "attributes": m.attributes,
                "certainty": m.certainty, "created_at": m.created_at.date().isoformat(),
            }
            for m in mems
        ],
        default=str,
    )


def _candidate_json(c: ScoredCandidate) -> str:
    return json.dumps(
        {"type": c.type, "key": c.key, "value": c.value, "attributes": c.attributes, "certainty": c.certainty,
         "temporality": c.temporality, "valid_until": c.valid_until},
        default=str,
    )


async def reconcile_candidate(
    llm: LLMProvider, repo: BrainRepository, *, user_id: str, candidate: ScoredCandidate
) -> dict[str, Any]:
    """Rules first; the LLM only arbitrates multi-valued ambiguity (§7.5)."""
    if candidate.type == "profile_fact" and candidate.key in PROFILE_KEYS:
        return {"action": "PROFILE", "target_memory_id": None, "reason": "profile slot"}

    existing = await repo.list_memories(user_id, keys=[candidate.key], statuses=("active",))
    if not existing:
        return {"action": "ADD", "target_memory_id": None, "reason": "no existing memory with this key"}

    def same(m: Memory) -> bool:
        return m.value.strip().lower() == candidate.value.strip().lower() and m.attributes == candidate.attributes

    for m in existing:
        if same(m):
            return {"action": "NOOP", "target_memory_id": m.id, "reason": "duplicate → reinforce"}

    if candidate.cardinality() == "single":
        latest = max(existing, key=lambda m: m.created_at)
        return {"action": "SUPERSEDE", "target_memory_id": latest.id, "reason": "single-valued slot changed"}

    try:
        decision = await llm.structured(
            "reconcile",
            [
                LLMMessage("system", RECONCILE_SYSTEM),
                LLMMessage("user", RECONCILE_USER.format(existing=_existing_json(existing), candidate=_candidate_json(candidate))),
            ],
            ReconcileDecision,
        )
    except Exception as exc:  # structured failure after retry → skip (§9)
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

def _profile_patch(c: ScoredCandidate) -> dict[str, Any]:
    if c.key == "dob":
        try:
            return {"dob": date.fromisoformat(c.value[:10])}
        except ValueError:
            return {}
    if c.key == "preferred_language":
        v = c.value.strip().lower()
        v = {"hindi": "hi", "english": "en"}.get(v, v)
        return {"preferred_language": v}
    return {c.key: c.value}


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
        patch = _profile_patch(candidate)
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
