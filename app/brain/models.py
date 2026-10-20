"""Domain models shared by the graphs, repositories and API."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

LIFE_AREAS: tuple[str, ...] = (
    "career",
    "relationships",
    "health",
    "finance",
    "family",
    "education",
    "spirituality",
)
GENERAL_AREA = "general"


def recall_areas(life_areas: Sequence[str] | None) -> list[str] | None:
    """Add the `general` area to a narrowed read, so a misfiled memory is never invisible.

    The REST filter stays exact: there the caller asked for one specific area.
    """
    if not life_areas:
        return None
    return list(dict.fromkeys([*life_areas, GENERAL_AREA]))

MemoryType = Literal["goal", "preference", "interest", "life_event", "profile_fact"]
Certainty = Literal["explicit", "hedged", "hypothetical"]
Subject = Literal["self", "other"]
Temporality = Literal["current", "future", "past"]
Cardinality = Literal["single", "multi"]
MemoryStatus = Literal["active", "superseded", "expired", "deleted"]

# Profile facts that live on the User node instead of a Memory node.
PROFILE_KEYS: frozenset[str] = frozenset({"name", "dob", "tob", "birth_place", "preferred_language"})
# Keys that can only hold one current value (deterministic SUPERSEDE on change).
SINGLE_VALUE_KEYS: frozenset[str] = frozenset(
    {"name", "dob", "tob", "birth_place", "preferred_language", "current_job", "current_city",
     "relationship_status", "employer", "job_title"}
)

RELATIONSHIP_BY_TYPE: dict[str, str] = {
    "goal": "HAS_GOAL",
    "interest": "INTERESTED_IN",
    "preference": "PREFERS",
    "life_event": "EXPERIENCED",
    "profile_fact": "HAS_FACT",
}


class MemoryCandidate(BaseModel):
    """What the extraction LLM emits: observable features, no numeric scores."""

    type: MemoryType
    key: str = Field(description="canonical slot, e.g. career_change")
    value: str = Field(description="canonical English value")
    attributes: dict[str, Any] = Field(default_factory=dict)
    life_area: str = GENERAL_AREA
    certainty: Certainty = "explicit"
    subject: Subject = "self"
    temporality: Temporality = "current"
    user_asked_to_remember: bool = False
    valid_until: date | None = None
    sensitive_category: Literal[
        "none", "health_detail", "financial_detail", "third_party_sensitive"
    ] = "none"

    def cardinality(self) -> Cardinality:
        if self.type == "profile_fact" or self.key in SINGLE_VALUE_KEYS:
            return "single"
        return "multi"


class ScoredCandidate(MemoryCandidate):
    confidence: float = 0.0
    importance: float = 0.0
    drop_reason: str | None = None


class Memory(BaseModel):
    id: str
    type: MemoryType
    key: str
    value: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    life_area: str = GENERAL_AREA
    cardinality: Cardinality = "multi"
    certainty: Certainty = "explicit"
    source: Literal["stated", "confirmed"] = "stated"
    confidence: float = 0.9
    importance: float = 0.5
    mention_count: int = 1
    last_used_at: datetime | None = None
    last_confirmed_at: datetime | None = None
    valid_until: date | None = None
    status: MemoryStatus = "active"
    created_at: datetime
    updated_at: datetime
    session_id: str | None = None
    prompt_version: str | None = None  # provenance: extraction prompt version
    supersedes: str | None = None  # id of the memory this one replaced
    superseded_by: str | None = None

    def as_line(self) -> str:
        """Compact one-line serialization used in prompts."""
        extras = []
        for k, v in sorted(self.attributes.items()):
            extras.append(f"{k.replace('_', ' ')} {v}")
        if self.valid_until:
            extras.append(f"until {self.valid_until.isoformat()}")
        extras.append(f"conf {self.confidence:.1f}")
        label = self.type.replace("_", " ").capitalize()
        return f"- [{self.life_area}] {label}: {self.value} ({', '.join(extras)})"


class UserProfile(BaseModel):
    user_id: str
    name: str | None = None
    dob: date | None = None
    tob: str | None = None
    birth_place: str | None = None
    preferred_language: str | None = None
    zodiac: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SessionRecord(BaseModel):
    session_id: str
    user_id: str
    started_at: datetime
    ended_at: datetime | None = None
    summary: str | None = None
    turn_count: int = 0
