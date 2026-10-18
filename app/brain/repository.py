"""BrainRepository protocol: the only contract the graphs depend on."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from app.brain.models import Memory, SessionRecord, UserProfile
from app.brain.profile_validation import validate_profile_patch


@runtime_checkable
class BrainRepository(Protocol):
    # lifecycle
    async def ping(self) -> bool: ...
    async def ensure_schema(self) -> None: ...
    async def close(self) -> None: ...

    # users
    async def create_user(self, profile: UserProfile, *, now: datetime) -> UserProfile: ...
    async def get_user(self, user_id: str) -> UserProfile | None: ...
    async def update_user(self, user_id: str, patch: dict[str, Any], *, now: datetime) -> UserProfile: ...

    # memories
    async def list_memories(
        self,
        user_id: str,
        *,
        life_areas: Sequence[str] | None = None,
        statuses: Sequence[str] = ("active",),
        keys: Sequence[str] | None = None,
    ) -> list[Memory]: ...
    async def get_memory(self, user_id: str, memory_id: str) -> Memory | None: ...
    async def add_memory(self, user_id: str, memory: Memory) -> Memory: ...
    async def update_memory(self, user_id: str, memory_id: str, patch: dict[str, Any]) -> Memory | None: ...
    async def supersede_memory(self, user_id: str, old_id: str, new: Memory) -> Memory: ...
    async def mark_deleted(self, user_id: str, memory_id: str, *, now: datetime) -> bool: ...
    async def touch_used(self, user_id: str, memory_ids: Sequence[str], *, now: datetime) -> None: ...
    async def memory_index(self, user_id: str) -> dict[str, dict[str, int]]: ...

    # sessions
    async def upsert_session(
        self,
        user_id: str,
        session_id: str,
        *,
        now: datetime,
        summary: str | None = None,
        turn_increment: int = 0,
    ) -> SessionRecord: ...
    async def get_session(self, user_id: str, session_id: str) -> SessionRecord | None: ...
    async def search_sessions(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 3,
        exclude_session_id: str | None = None,
    ) -> list[SessionRecord]: ...


def apply_profile_patch(profile: UserProfile, patch: dict[str, Any], now: datetime) -> UserProfile:
    """Shared helper: apply a patch and recompute derived attributes (zodiac)."""
    from app.profile.zodiac import sun_sign

    data = profile.model_dump()
    # Nothing unparseable may reach the User node: the chart is computed from it (§11).
    clean, _rejected = validate_profile_patch(patch, today=now.date())
    data.update(clean)
    updated = UserProfile(**data)
    updated.zodiac = sun_sign(updated.dob) if updated.dob else None
    updated.updated_at = now
    if updated.created_at is None:
        updated.created_at = now
    return updated


def search_terms(query: str) -> list[str]:
    stop = {"the", "a", "an", "my", "me", "i", "about", "what", "do", "you", "remember", "of", "in", "for", "to", "is", "are", "and"}
    terms = [t.strip("?.,!").lower() for t in query.split()]
    return [t for t in terms if len(t) > 2 and t not in stop]
