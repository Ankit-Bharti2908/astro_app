"""Neo4j implementation of the BrainRepository (§6 schema)."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase
from neo4j.exceptions import DriverError, Neo4jError

from app.brain.errors import BrainUnavailable
from app.brain.models import (
    LIFE_AREAS,
    RELATIONSHIP_BY_TYPE,
    Memory,
    SessionRecord,
    UserProfile,
)
from app.brain.repository import apply_profile_patch, search_terms

log = logging.getLogger(__name__)

SCHEMA_STATEMENTS = [
    "DROP CONSTRAINT session_id IF EXISTS",  # pre-0.1 schema keyed sessions globally; now per user
    "CREATE CONSTRAINT user_id IF NOT EXISTS FOR (u:User) REQUIRE u.user_id IS UNIQUE",
    "CREATE CONSTRAINT memory_id IF NOT EXISTS FOR (m:Memory) REQUIRE m.id IS UNIQUE",
    "CREATE CONSTRAINT session_key IF NOT EXISTS FOR (s:Session) REQUIRE s.key IS UNIQUE",
    "CREATE INDEX session_lookup IF NOT EXISTS FOR (s:Session) ON (s.user_id, s.session_id)",
    "CREATE INDEX memory_lookup IF NOT EXISTS FOR (m:Memory) ON (m.life_area, m.status)",
]

_MEMORY_DT_FIELDS = ("created_at", "updated_at", "last_used_at", "last_confirmed_at")


def _iso(v: datetime | date | None) -> str | None:
    return v.isoformat() if v is not None else None


def _parse_dt(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    if hasattr(v, "to_native"):
        return v.to_native()
    return datetime.fromisoformat(str(v))


def _parse_date(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if hasattr(v, "to_native"):
        return v.to_native()
    return date.fromisoformat(str(v)[:10])


def memory_to_props(m: Memory) -> dict[str, Any]:
    props = m.model_dump(exclude={"attributes", "supersedes", "superseded_by"})
    props["attributes"] = json.dumps(m.attributes, sort_keys=True, default=str)
    for f in _MEMORY_DT_FIELDS:
        props[f] = _iso(getattr(m, f))
    props["valid_until"] = _iso(m.valid_until)
    return props


def node_to_memory(node: Any, supersedes: str | None = None, superseded_by: str | None = None) -> Memory:
    data = dict(node)
    raw_attrs = data.get("attributes") or "{}"
    data["attributes"] = json.loads(raw_attrs) if isinstance(raw_attrs, str) else dict(raw_attrs)
    for f in _MEMORY_DT_FIELDS:
        data[f] = _parse_dt(data.get(f))
    data["valid_until"] = _parse_date(data.get("valid_until"))
    data["supersedes"] = supersedes
    data["superseded_by"] = superseded_by
    return Memory(**data)


def node_to_profile(node: Any) -> UserProfile:
    data = dict(node)
    return UserProfile(
        user_id=data["user_id"],
        name=data.get("name"),
        dob=_parse_date(data.get("dob")),
        tob=data.get("tob"),
        birth_place=data.get("birth_place"),
        preferred_language=data.get("preferred_language"),
        zodiac=data.get("zodiac"),
        created_at=_parse_dt(data.get("created_at")),
        updated_at=_parse_dt(data.get("updated_at")),
    )


def node_to_session(node: Any, user_id: str) -> SessionRecord:
    data = dict(node)
    return SessionRecord(
        session_id=data["session_id"],
        user_id=data.get("user_id") or user_id,
        started_at=_parse_dt(data.get("started_at")) or datetime.min,
        ended_at=_parse_dt(data.get("ended_at")),
        summary=data.get("summary"),
        turn_count=int(data.get("turn_count") or 0),
    )


class Neo4jBrainRepository:
    def __init__(
        self,
        uri: str,
        user: str | None,
        password: str | None,
        database: str = "neo4j",
        *,
        connect_timeout_s: float = 3.0,
        max_retry_time_s: float = 5.0,
    ) -> None:
        auth = (user, password) if user else None
        # Fail fast so an outage becomes `degraded: ["brain_unavailable"]` instead of a 30 s+ stall (§11)
        self._driver: AsyncDriver = AsyncGraphDatabase.driver(
            uri,
            auth=auth,
            notifications_min_severity="OFF",
            connection_timeout=connect_timeout_s,
            connection_acquisition_timeout=connect_timeout_s * 2,
            max_transaction_retry_time=max_retry_time_s,
        )
        self._database = database

    @classmethod
    def from_settings(cls, settings) -> Neo4jBrainRepository:
        return cls(
            settings.neo4j_uri,
            settings.neo4j_user,
            settings.neo4j_password,
            settings.neo4j_database,
            connect_timeout_s=settings.neo4j_connect_timeout_s,
            max_retry_time_s=settings.neo4j_max_retry_time_s,
        )

    # plumbing ------------------------------------------------------------
    async def _run(self, query: str, **params: Any) -> list[Any]:
        try:
            result = await self._driver.execute_query(query, parameters_=params, database_=self._database)
            return list(result.records)
        except (Neo4jError, DriverError, OSError, TimeoutError) as exc:
            raise BrainUnavailable(str(exc)) from exc

    async def _write_many(self, statements: list[tuple[str, dict[str, Any]]]) -> list[Any]:
        async def work(tx):
            last = []
            for q, p in statements:
                res = await tx.run(q, p)
                last = [r async for r in res]
            return last

        try:
            async with self._driver.session(database=self._database) as session:
                return await session.execute_write(work)
        except (Neo4jError, DriverError, OSError, TimeoutError) as exc:
            raise BrainUnavailable(str(exc)) from exc

    async def ping(self) -> bool:
        try:
            await self._run("RETURN 1 AS ok")
            return True
        except BrainUnavailable:
            return False

    async def ensure_schema(self) -> None:
        for stmt in SCHEMA_STATEMENTS:
            await self._run(stmt)
        await self._run("UNWIND $names AS n MERGE (:LifeArea {name: n})", names=list(LIFE_AREAS) + ["general"])

    async def close(self) -> None:
        await self._driver.close()

    async def clear_all(self) -> None:  # test helper
        await self._run("MATCH (n) DETACH DELETE n")

    # users ---------------------------------------------------------------
    async def _write_user(self, profile: UserProfile) -> UserProfile:
        props = {
            "name": profile.name,
            "dob": _iso(profile.dob),
            "tob": profile.tob,
            "birth_place": profile.birth_place,
            "preferred_language": profile.preferred_language,
            "zodiac": profile.zodiac,
            "updated_at": _iso(profile.updated_at),
        }
        records = await self._write_many(
            [
                (
                    """
                    MERGE (u:User {user_id: $user_id})
                    ON CREATE SET u.created_at = $created_at
                    SET u += $props
                    WITH u
                    OPTIONAL MATCH (u)-[rz:HAS_ZODIAC]->() DELETE rz
                    WITH u
                    OPTIONAL MATCH (u)-[rl:PREFERS_LANGUAGE]->() DELETE rl
                    WITH u
                    FOREACH (_ IN CASE WHEN $zodiac IS NULL THEN [] ELSE [1] END |
                      MERGE (z:ZodiacSign {name: $zodiac}) MERGE (u)-[:HAS_ZODIAC]->(z))
                    FOREACH (_ IN CASE WHEN $language IS NULL THEN [] ELSE [1] END |
                      MERGE (l:Language {code: $language}) MERGE (u)-[:PREFERS_LANGUAGE]->(l))
                    RETURN u
                    """,
                    {
                        "user_id": profile.user_id,
                        "created_at": _iso(profile.created_at or profile.updated_at),
                        "props": props,
                        "zodiac": profile.zodiac,
                        "language": profile.preferred_language,
                    },
                )
            ]
        )
        return node_to_profile(records[0]["u"])

    async def create_user(self, profile: UserProfile, *, now: datetime) -> UserProfile:
        created = apply_profile_patch(profile, profile.model_dump(), now)
        created.created_at = now
        return await self._write_user(created)

    async def get_user(self, user_id: str) -> UserProfile | None:
        # Sessions/memories MERGE a bare User node; only a profile write sets updated_at → treat bare nodes as absent
        records = await self._run(
            "MATCH (u:User {user_id: $user_id}) WHERE u.updated_at IS NOT NULL RETURN u", user_id=user_id
        )
        return node_to_profile(records[0]["u"]) if records else None

    async def update_user(self, user_id: str, patch: dict[str, Any], *, now: datetime) -> UserProfile:
        base = await self.get_user(user_id) or UserProfile(user_id=user_id, created_at=now)
        updated = apply_profile_patch(base, patch, now)
        return await self._write_user(updated)

    # memories ------------------------------------------------------------
    def _add_memory_statements(self, user_id: str, m: Memory) -> list[tuple[str, dict[str, Any]]]:
        rel = RELATIONSHIP_BY_TYPE[m.type]  # whitelist → safe to interpolate
        props = memory_to_props(m)
        stmts = [
            (
                f"""
                MERGE (u:User {{user_id: $user_id}})
                ON CREATE SET u.created_at = $now
                MERGE (m:Memory {{id: $id}})
                ON CREATE SET m += $props
                MERGE (u)-[:{rel}]->(m)
                WITH u, m
                MERGE (la:LifeArea {{name: $life_area}})
                MERGE (m)-[:ABOUT]->(la)
                RETURN m
                """,
                {"user_id": user_id, "id": m.id, "props": props, "life_area": m.life_area, "now": props["created_at"]},
            )
        ]
        if m.session_id:
            stmts.append(
                (
                    """
                    MATCH (u:User {user_id: $user_id}), (m:Memory {id: $id})
                    MERGE (s:Session {key: $key})
                    ON CREATE SET s.started_at = $now, s.turn_count = 0, s.user_id = $user_id, s.session_id = $session_id
                    MERGE (u)-[:HAD_SESSION]->(s)
                    MERGE (m)-[:EXTRACTED_FROM]->(s)
                    RETURN m
                    """,
                    {"user_id": user_id, "id": m.id, "session_id": m.session_id, "key": f"{user_id}:{m.session_id}", "now": props["created_at"]},
                )
            )
        return stmts

    async def add_memory(self, user_id: str, memory: Memory) -> Memory:
        await self._write_many(self._add_memory_statements(user_id, memory))
        return await self.get_memory(user_id, memory.id) or memory

    _MEMORY_QUERY = """
        MATCH (u:User {user_id: $user_id})-[]->(m:Memory)
        WHERE m.status IN $statuses
          AND ($life_areas IS NULL OR m.life_area IN $life_areas)
          AND ($keys IS NULL OR m.key IN $keys)
          AND ($memory_id IS NULL OR m.id = $memory_id)
        OPTIONAL MATCH (m)-[:SUPERSEDES]->(old:Memory)
        OPTIONAL MATCH (newer:Memory)-[:SUPERSEDES]->(m)
        WITH m, head(collect(DISTINCT old.id)) AS supersedes, head(collect(DISTINCT newer.id)) AS superseded_by
        RETURN m, supersedes, superseded_by
        ORDER BY m.created_at
    """

    async def list_memories(
        self,
        user_id: str,
        *,
        life_areas: Sequence[str] | None = None,
        statuses: Sequence[str] = ("active",),
        keys: Sequence[str] | None = None,
    ) -> list[Memory]:
        records = await self._run(
            self._MEMORY_QUERY,
            user_id=user_id,
            statuses=list(statuses),
            life_areas=list(life_areas) if life_areas is not None else None,
            keys=list(keys) if keys is not None else None,
            memory_id=None,
        )
        return [node_to_memory(r["m"], r["supersedes"], r["superseded_by"]) for r in records]

    async def get_memory(self, user_id: str, memory_id: str) -> Memory | None:
        records = await self._run(
            self._MEMORY_QUERY,
            user_id=user_id,
            statuses=["active", "superseded", "expired", "deleted"],
            life_areas=None,
            keys=None,
            memory_id=memory_id,
        )
        if not records:
            return None
        r = records[0]
        return node_to_memory(r["m"], r["supersedes"], r["superseded_by"])

    async def update_memory(self, user_id: str, memory_id: str, patch: dict[str, Any]) -> Memory | None:
        current = await self.get_memory(user_id, memory_id)
        if current is None:
            return None
        data = current.model_dump()
        data.update(patch)
        updated = Memory(**data)
        props = memory_to_props(updated)
        await self._run(
            "MATCH (u:User {user_id: $user_id})-[]->(m:Memory {id: $id}) SET m += $props RETURN m",
            user_id=user_id,
            id=memory_id,
            props=props,
        )
        return await self.get_memory(user_id, memory_id)

    async def supersede_memory(self, user_id: str, old_id: str, new: Memory) -> Memory:
        stmts = self._add_memory_statements(user_id, new)
        stmts.append(
            (
                """
                MATCH (u:User {user_id: $user_id})-[]->(old:Memory {id: $old_id})
                MATCH (new:Memory {id: $new_id})
                SET old.status = 'superseded', old.updated_at = $now
                MERGE (new)-[:SUPERSEDES]->(old)
                RETURN new
                """,
                {"user_id": user_id, "old_id": old_id, "new_id": new.id, "now": _iso(new.updated_at)},
            )
        )
        await self._write_many(stmts)
        return await self.get_memory(user_id, new.id) or new

    async def mark_deleted(self, user_id: str, memory_id: str, *, now: datetime) -> bool:
        records = await self._run(
            """
            MATCH (u:User {user_id: $user_id})-[]->(m:Memory {id: $id})
            WHERE m.status <> 'deleted'
            SET m.status = 'deleted', m.updated_at = $now
            RETURN count(m) AS n
            """,
            user_id=user_id,
            id=memory_id,
            now=_iso(now),
        )
        return bool(records and records[0]["n"] > 0)

    async def touch_used(self, user_id: str, memory_ids: Sequence[str], *, now: datetime) -> None:
        if not memory_ids:
            return
        await self._run(
            """
            MATCH (u:User {user_id: $user_id})-[]->(m:Memory)
            WHERE m.id IN $ids
            SET m.last_used_at = $now
            """,
            user_id=user_id,
            ids=list(memory_ids),
            now=_iso(now),
        )

    async def memory_index(self, user_id: str) -> dict[str, dict[str, int]]:
        records = await self._run(
            """
            MATCH (u:User {user_id: $user_id})-[]->(m:Memory {status: 'active'})
            RETURN m.life_area AS area, m.type AS type, count(*) AS n
            """,
            user_id=user_id,
        )
        index: dict[str, dict[str, int]] = {}
        for r in records:
            index.setdefault(r["area"], {})[r["type"]] = int(r["n"])
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
        records = await self._run(
            """
            MERGE (u:User {user_id: $user_id})
            ON CREATE SET u.created_at = $now
            MERGE (s:Session {key: $key})
            ON CREATE SET s.started_at = $now, s.turn_count = 0, s.user_id = $user_id, s.session_id = $session_id
            MERGE (u)-[:HAD_SESSION]->(s)
            SET s.turn_count = coalesce(s.turn_count, 0) + $inc,
                s.ended_at = $now,
                s.summary = coalesce($summary, s.summary)
            RETURN s
            """,
            user_id=user_id,
            session_id=session_id,
            key=f"{user_id}:{session_id}",
            now=_iso(now),
            inc=turn_increment,
            summary=summary,
        )
        return node_to_session(records[0]["s"], user_id)

    async def get_session(self, user_id: str, session_id: str) -> SessionRecord | None:
        records = await self._run(
            "MATCH (u:User {user_id: $user_id})-[:HAD_SESSION]->(s:Session {session_id: $session_id}) RETURN s",
            user_id=user_id,
            session_id=session_id,
        )
        return node_to_session(records[0]["s"], user_id) if records else None

    async def search_sessions(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 3,
        exclude_session_id: str | None = None,
    ) -> list[SessionRecord]:
        terms = search_terms(query)
        records = await self._run(
            """
            MATCH (u:User {user_id: $user_id})-[:HAD_SESSION]->(s:Session)
            WHERE s.summary IS NOT NULL AND s.session_id <> coalesce($exclude, '')
            WITH s, size([t IN $terms WHERE toLower(s.summary) CONTAINS t]) AS hits
            WHERE hits > 0 OR size($terms) = 0
            RETURN s ORDER BY hits DESC, s.ended_at DESC LIMIT $limit
            """,
            user_id=user_id,
            terms=terms,
            exclude=exclude_session_id,
            limit=limit,
        )
        return [node_to_session(r["s"], user_id) for r in records]
