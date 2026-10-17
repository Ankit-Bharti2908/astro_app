"""Same contract for the in-memory repo and (when NEO4J_TEST_URI is set) the real Neo4j repo."""

from __future__ import annotations

from datetime import date, timedelta

from app.brain.models import Memory, UserProfile
from tests.conftest import FIXED_NOW, unique_user


def mem(uid: str, i: int, **kw) -> Memory:
    base = dict(id=f"{uid}-m{i}", type="goal", key=f"goal_{i}", value=f"value {i}", life_area="career", attributes={"n": i},
                importance=0.9, confidence=0.9, created_at=FIXED_NOW + timedelta(seconds=i), updated_at=FIXED_NOW + timedelta(seconds=i), session_id="s1")
    base.update(kw)
    return Memory(**base)


async def test_users(any_repo):
    repo, uid = any_repo, unique_user()
    assert await repo.ping() is True
    assert await repo.get_user(uid) is None
    created = await repo.create_user(UserProfile(user_id=uid, name="Rahul", dob=date(1995, 8, 15), birth_place="Delhi", preferred_language="hinglish"), now=FIXED_NOW)
    assert created.zodiac == "Leo" and created.created_at == FIXED_NOW
    fetched = await repo.get_user(uid)
    assert fetched.name == "Rahul" and fetched.dob == date(1995, 8, 15) and fetched.preferred_language == "hinglish"
    updated = await repo.update_user(uid, {"dob": date(1996, 1, 5), "preferred_language": "hi"}, now=FIXED_NOW + timedelta(days=1))
    assert updated.zodiac == "Capricorn" and updated.name == "Rahul" and updated.preferred_language == "hi"
    assert updated.updated_at == FIXED_NOW + timedelta(days=1)
    # update_user upserts unknown users (profile facts can arrive before signup)
    other = unique_user()
    assert (await repo.update_user(other, {"name": "Priya"}, now=FIXED_NOW)).name == "Priya"
    assert (await repo.get_user(other)).created_at == FIXED_NOW


async def test_memories_lifecycle(any_repo):
    repo, uid = any_repo, unique_user()
    m0 = await repo.add_memory(uid, mem(uid, 0))
    assert m0.attributes == {"n": 0} and m0.session_id == "s1" and m0.supersedes is None
    assert (await repo.add_memory(uid, mem(uid, 0, value="changed"))).value == "value 0"  # idempotent on id
    await repo.add_memory(uid, mem(uid, 1, life_area="health", type="interest", key="yoga"))
    await repo.add_memory(uid, mem(uid, 2, valid_until=date(2026, 11, 30)))
    assert [m.id for m in await repo.list_memories(uid)] == [f"{uid}-m0", f"{uid}-m1", f"{uid}-m2"]
    assert [m.id for m in await repo.list_memories(uid, life_areas=["health"])] == [f"{uid}-m1"]
    assert [m.id for m in await repo.list_memories(uid, keys=["goal_2"])] == [f"{uid}-m2"]
    assert (await repo.list_memories(uid, keys=["goal_2"]))[0].valid_until == date(2026, 11, 30)
    assert await repo.list_memories(unique_user()) == []  # isolation
    assert await repo.memory_index(uid) == {"career": {"goal": 2}, "health": {"interest": 1}}

    upd = await repo.update_memory(uid, f"{uid}-m0", {"confidence": 1.0, "mention_count": 2, "attributes": {"n": 0, "extra": True}})
    assert upd.confidence == 1.0 and upd.mention_count == 2 and upd.attributes == {"n": 0, "extra": True}
    assert await repo.update_memory(uid, "missing", {"confidence": 0.1}) is None

    later = FIXED_NOW + timedelta(days=2)
    await repo.touch_used(uid, [f"{uid}-m0", f"{uid}-m1"], now=later)
    assert (await repo.get_memory(uid, f"{uid}-m0")).last_used_at == later
    assert (await repo.get_memory(uid, f"{uid}-m2")).last_used_at is None

    new = await repo.supersede_memory(uid, f"{uid}-m0", mem(uid, 9, attributes={"n": 9}, session_id="s2"))
    old = await repo.get_memory(uid, f"{uid}-m0")
    assert old.status == "superseded" and old.superseded_by == new.id and new.supersedes == old.id
    assert new.status == "active" and f"{uid}-m0" not in [m.id for m in await repo.list_memories(uid)]
    assert f"{uid}-m0" in [m.id for m in await repo.list_memories(uid, statuses=("superseded",))]
    assert await repo.memory_index(uid) == {"career": {"goal": 2}, "health": {"interest": 1}}

    assert await repo.mark_deleted(uid, f"{uid}-m1", now=later) is True
    assert await repo.mark_deleted(uid, f"{uid}-m1", now=later) is False
    assert await repo.mark_deleted(unique_user(), f"{uid}-m2", now=later) is False  # scoped by user
    assert (await repo.get_memory(uid, f"{uid}-m1")).status == "deleted"
    assert await repo.memory_index(uid) == {"career": {"goal": 2}}


async def test_sessions(any_repo):
    repo, uid = any_repo, unique_user()
    s = await repo.upsert_session(uid, "s1", now=FIXED_NOW, turn_increment=1)
    assert s.turn_count == 1 and s.started_at == FIXED_NOW and s.summary is None
    s = await repo.upsert_session(uid, "s1", now=FIXED_NOW + timedelta(minutes=1), summary="We discussed a career switch and interviews.", turn_increment=1)
    assert s.turn_count == 2 and s.summary.startswith("We discussed") and s.ended_at == FIXED_NOW + timedelta(minutes=1)
    s = await repo.upsert_session(uid, "s1", now=FIXED_NOW + timedelta(minutes=2), turn_increment=1)
    assert s.summary.startswith("We discussed")  # summary kept when not provided
    await repo.upsert_session(uid, "s2", now=FIXED_NOW, summary="Talked about marriage timing.", turn_increment=1)
    await repo.upsert_session(uid, "s3", now=FIXED_NOW, turn_increment=1)  # no summary → never returned
    assert (await repo.get_session(uid, "s1")).turn_count == 3
    assert await repo.get_session(uid, "nope") is None
    hits = await repo.search_sessions(uid, "career interviews")
    assert [h.session_id for h in hits] == ["s1"]
    assert [h.session_id for h in await repo.search_sessions(uid, "marriage")] == ["s2"]
    assert [h.session_id for h in await repo.search_sessions(uid, "career", exclude_session_id="s1")] == []
    assert await repo.search_sessions(unique_user(), "career") == []
    # memory provenance links sessions too
    await repo.add_memory(uid, mem(uid, 5, session_id="s9"))
    assert (await repo.get_session(uid, "s9")) is not None
