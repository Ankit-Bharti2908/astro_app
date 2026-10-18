"""Request/response schemas (§12)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

Language = Literal["en", "hi", "hinglish"]
ID_PATTERN = r"^[A-Za-z0-9_.@-]+$"  # no ':' — it separates user_id and session_id in composite keys


class UserCreate(BaseModel):
    user_id: str = Field(min_length=1, max_length=128, pattern=ID_PATTERN)
    name: str | None = Field(default=None, max_length=120)
    dob: date | None = None
    tob: str | None = Field(default=None, max_length=16, description="time of birth, e.g. 14:30")
    birth_place: str | None = Field(default=None, max_length=120)
    preferred_language: Language | None = None


class UserPatch(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    dob: date | None = None
    tob: str | None = Field(default=None, max_length=16)
    birth_place: str | None = Field(default=None, max_length=120)
    preferred_language: Language | None = None


class UserOut(BaseModel):
    user_id: str
    name: str | None
    dob: date | None
    tob: str | None
    birth_place: str | None
    preferred_language: str | None
    zodiac: str | None
    created_at: datetime | None
    updated_at: datetime | None


class MemoryOut(BaseModel):
    id: str
    type: str
    key: str
    value: str
    attributes: dict[str, Any]
    life_area: str
    cardinality: str
    certainty: str
    source: str
    confidence: float
    importance: float
    retrieval_score: float
    mention_count: int
    status: str
    valid_until: date | None
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime
    session_id: str | None
    prompt_version: str | None
    supersedes: str | None
    superseded_by: str | None


class ChatRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128, pattern=ID_PATTERN)
    session_id: str = Field(min_length=1, max_length=128, pattern=ID_PATTERN)
    message: str = Field(min_length=1, max_length=4000)
    ephemeral: bool = False
    timezone: str | None = Field(default=None, max_length=64, description="IANA zone, e.g. Asia/Kolkata")
    sync_memory: bool = Field(default=False, description="debug aid: run the memory graph before responding")

    @field_validator("timezone")
    @classmethod
    def _valid_zone(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            ZoneInfo(v)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v


class ChatResponse(BaseModel):
    response: str
    user_id: str
    session_id: str
    route: str
    language: str
    context_used: list[str]
    context_tokens: dict[str, int]
    degraded: list[str]
    memory_cmd_result: dict[str, Any] | None = None


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    brain: dict[str, Any]
    llm: dict[str, Any]
    prompt_version: str
