"""In-memory BrainRepository (tests, local dev without Neo4j)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from app.brain.models import Memory, SessionRecord, UserProfile
from app.brain.repository import apply_profile_patch, search_terms


class InMemoryBrainRepository:
    def __init__(self) -> None:
        self.users: dict[str, UserProfile] = {}
        self.memories: dict[str, dict[str, Memory]] = {}
        self.sessions: dict[tuple[str, str], SessionRecord] = {}
        self.calls: list[str] = []  # instrumentation for tests ("no brain calls" assertions)

    # lifecycle -----------------------------------------------------------
    async def ping(self) -> bool:
        return True

    async def ensure_schema(self) -> None:
        return None

    async def close(self) -> None:
        return None

    # users ---------------------------------------------------------------
    async def create_user(self, profile: UserProfile, *, now: datetime) -> UserProfile:
        self.calls.append("create_user")
        created = apply_profile_patch(profile, profile.model_dump(), now)
        created.created_at = now
        self.users[profile.user_id] = created
        return created.model_copy(deep=True)

    async def get_user(self, user_id: str) -> UserProfile | None:
        self.calls.append("get_user")
        u = self.users.get(user_id)
        return u.model_copy(deep=True) if u else None

    async def update_user(self, user_id: str, patch: dict[str, Any], *, now: datetime) -> UserProfile:
        self.calls.append("update_user")
        base = self.users.get(user_id) or UserProfile(user_id=user_id, created_at=now)
        updated = apply_profile_patch(base, patch, now)
        self.users[user_id] = updated
        return updated.model_copy(deep=True)

    # memories ------------------------------------------------------------
    def _bucket(self, user_id: str) -> dict[str, Memory]:
        return self.memories.setdefault(user_id, {})

    async def list_memories(
        self,
        user_id: str,
        *,
        life_areas: Sequence[str] | None = None,
        statuses: Sequence[str] = ("active",),
        keys: Sequence[str] | None = None,
    ) -> list[Memory]:
        self.calls.append("list_memories")
        out = []
        for m in self._bucket(user_id).values():
            if m.status not in statuses:
                continue
            if life_areas is not None and m.life_area not in life_areas:
                continue
            if keys is not None and m.key not in keys:
                continue
            out.append(m.model_copy(deep=True))
        out.sort(key=lambda m: m.created_at)
        return out

    async def get_memory(self, user_id: str, memory_id: str) -> Memory | None:
        self.calls.append("get_memory")
        m = self._bucket(user_id).get(memory_id)
        return m.model_copy(deep=True) if m else None

    async def add_memory(self, user_id: str, memory: Memory) -> Memory:
        self.calls.append("add_memory")
        bucket = self._bucket(user_id)
        if memory.id in bucket:  # idempotent MERGE semantics
            return bucket[memory.id].model_copy(deep=True)
        bucket[memory.id] = memory.model_copy(deep=True)
        if memory.session_id:
            await self.upsert_session(user_id, memory.session_id, now=memory.created_at)
        return memory.model_copy(deep=True)

    async def update_memory(self, user_id: str, memory_id: str, patch: dict[str, Any]) -> Memory | None:
        self.calls.append("update_memory")
        bucket = self._bucket(user_id)
        m = bucket.get(memory_id)
        if m is None:
            return None
        data = m.model_dump()
        data.update(patch)
        updated = Memory(**data)
        bucket[memory_id] = updated
        return updated.model_copy(deep=True)

    async def supersede_memory(self, user_id: str, old_id: str, new: Memory) -> Memory:
        self.calls.append("supersede_memory")
        bucket = self._bucket(user_id)
        old = bucket.get(old_id)
        new = new.model_copy(deep=True)
        new.supersedes = old_id
        bucket[new.id] = new
        if old is not None:
            old.status = "superseded"
            old.superseded_by = new.id
            old.updated_at = new.updated_at
        if new.session_id:
            await self.upsert_session(user_id, new.session_id, now=new.created_at)
        return new.model_copy(deep=True)

    async def mark_deleted(self, user_id: str, memory_id: str, *, now: datetime) -> bool:
        self.calls.append("mark_deleted")
        m = self._bucket(user_id).get(memory_id)
        if m is None or m.status == "deleted":
            return False
        m.status = "deleted"
        m.updated_at = now
        return True

    async def touch_used(self, user_id: str, memory_ids: Sequence[str], *, now: datetime) -> None:
        self.calls.append("touch_used")
        bucket = self._bucket(user_id)
        for mid in memory_ids:
            if mid in bucket:
                bucket[mid].last_used_at = now

    async def memory_index(self, user_id: str) -> dict[str, dict[str, int]]:
        self.calls.append("memory_index")
        index: dict[str, dict[str, int]] = {}
        for m in self._bucket(user_id).values():
            if m.status != "active":
                continue
            index.setdefault(m.life_area, {})
            index[m.life_area][m.type] = index[m.life_area].get(m.type, 0) + 1
        return index

    # sessions ------------------------------------------------------------
    async def upsert_session(
        self,
        user_id: str,
        session_id: str,
        *,
        now: datetime,
        summary: str | None = None,
        turn_increment: int = 0,
    ) -> SessionRecord:
        self.calls.append("upsert_session")
        key = (user_id, session_id)
        rec = self.sessions.get(key) or SessionRecord(session_id=session_id, user_id=user_id, started_at=now)
        rec.turn_count += turn_increment
        rec.ended_at = now
        if summary is not None:
            rec.summary = summary
        self.sessions[key] = rec
        return rec.model_copy(deep=True)

    async def get_session(self, user_id: str, session_id: str) -> SessionRecord | None:
        self.calls.append("get_session")
        rec = self.sessions.get((user_id, session_id))
        return rec.model_copy(deep=True) if rec else None

    async def search_sessions(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 3,
        exclude_session_id: str | None = None,
    ) -> list[SessionRecord]:
        self.calls.append("search_sessions")
        terms = search_terms(query)
        scored: list[tuple[int, SessionRecord]] = []
        for (uid, sid), rec in self.sessions.items():
            if uid != user_id or sid == exclude_session_id or not rec.summary:
                continue
            text = rec.summary.lower()
            hits = sum(1 for t in terms if t in text)
            if hits or not terms:
                scored.append((hits, rec))
        scored.sort(key=lambda p: (p[0], p[1].ended_at or p[1].started_at), reverse=True)
        return [r.model_copy(deep=True) for _, r in scored[:limit]]
