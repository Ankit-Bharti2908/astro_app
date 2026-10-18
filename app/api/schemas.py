"""Request/response schemas (§12)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

Language = Literal["en", "hi", "hinglish"]
# ':' separates the two halves of the checkpointer thread key `user_id:session_id`, so an id
# containing one could address another user's thread. Barred from both halves (§11).
ID_PATTERN = r"^[A-Za-z0-9_.@-]+$"


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
    session_id: str = Field(
        default_factory=lambda: str(uuid4()),
        min_length=1,
        max_length=128,
        pattern=ID_PATTERN,
        description="omit to start a new session: the server mints a UUID4 and returns it",
    )
    message: str = Field(min_length=1, max_length=4000)
    ephemeral: bool = False
    timezone: str | None = Field(default=None, max_length=64)
    sync_memory: bool = Field(default=False, description="debug aid: run the memory graph before responding")


class ToolCallOut(BaseModel):
    """One agent tool call made while answering this turn (§4)."""

    name: str
    args: dict[str, Any]
    result: str


class ChatResponse(BaseModel):
    response: str
    user_id: str
    session_id: str
    route: str
    language: str
    context_used: list[str]
    context_tokens: dict[str, int]
    degraded: list[str]
    tools_used: list[ToolCallOut] = []
    memory_cmd_result: dict[str, Any] | None = None


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    brain: dict[str, Any]
    llm: dict[str, Any]
    prompt_version: str


class MemoryRunOut(BaseModel):
    """One background memory-graph run: what the pipeline made of a turn (§5, §7)."""

    seq: int
    user_id: str
    session_id: str
    message: str
    candidates: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    written: list[dict[str, Any]] = []
    degraded: list[str] = []
    skipped_reason: str | None = None
    error: str | None = None
