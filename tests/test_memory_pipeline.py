from datetime import timedelta

from app.brain.memory_repo import InMemoryBrainRepository
from app.brain.models import MemoryCandidate, ScoredCandidate
from app.graphs.deps import Deps
from app.graphs.memory_graph import build_memory_graph
from app.graphs.nodes.memory_ops import (
    apply_decision,
    build_memory,
    filter_candidates,
    reconcile_candidate,
    score_candidates,
)
from app.llm.fake import FakeLLM
from app.llm.provider import LLMMessage
from app.tools.brain_tools import make_tools
from tests.conftest import FIXED_NOW, make_settings


def scored(**kw) -> ScoredCandidate:
    base = dict(type="goal", key="career_change", value="switch jobs", life_area="career", attributes={"target_year": 2027})
    base.update(kw)
    return score_candidates([MemoryCandidate(**base)])[0]


def test_filter_rules():
    settings = make_settings()
    cands = [
        scored(),
        scored(key="hyp", certainty="hypothetical"),
        scored(key="bro", subject="other"),
        scored(key="bro_asked", subject="other", user_asked_to_remember=True),
        scored(key="health_diabetes", type="life_event", life_area="health", value="diabetes"),
        scored(key="income", type="profile_fact", value="₹50k", sensitive_category="financial_detail"),
        scored(key="hedged", certainty="hedged"),
    ]
    accepted, dropped = filter_candidates(cands, settings)
    assert {c.key for c in accepted} == {"career_change", "bro_asked", "hedged"}
    reasons = {c.key: c.drop_reason for c in dropped}
    assert reasons["hyp"].startswith("below_threshold")
    assert reasons["bro"] == "subject_other"
    assert reasons["health_diabetes"] == "never_store:health_detail"
    assert reasons["income"] == "never_store:financial_detail"
    accepted, dropped = filter_candidates([scored()], settings, ephemeral=True)
    assert not accepted and dropped[0].drop_reason == "ephemeral"


async def test_reconcile_rules_and_apply():
    repo, fake = InMemoryBrainRepository(), FakeLLM()
    user = "u1"
    c1 = scored()
    assert (await reconcile_candidate(fake, repo, user_id=user, candidate=c1))["action"] == "ADD"
    r = await apply_decision(repo, user_id=user, session_id="s1", candidate=c1, decision={"action": "ADD"}, now=FIXED_NOW)
    first_id = r["memory_id"]
    # duplicate → NOOP reinforce
    d = await reconcile_candidate(fake, repo, user_id=user, candidate=c1)
    assert d == {"action": "NOOP", "target_memory_id": first_id, "reason": "duplicate → reinforce"}
    await apply_decision(repo, user_id=user, session_id="s2", candidate=c1, decision=d, now=FIXED_NOW + timedelta(days=1))
    m = await repo.get_memory(user, first_id)
    assert m.mention_count == 2 and m.confidence == 1.0
    # profile slot → PROFILE
    pf = scored(type="profile_fact", key="dob", value="1995-08-15", attributes={})
    d = await reconcile_candidate(fake, repo, user_id=user, candidate=pf)
    assert d["action"] == "PROFILE"
    r = await apply_decision(repo, user_id=user, session_id="s1", candidate=pf, decision=d, now=FIXED_NOW)
    assert r["profile"]["zodiac"] == "Leo"
    # single-valued key → deterministic SUPERSEDE
    job1 = scored(type="profile_fact", key="current_job", value="analyst", attributes={})
    await apply_decision(repo, user_id=user, session_id="s1", candidate=job1, decision={"action": "ADD"}, now=FIXED_NOW)
    job2 = scored(type="profile_fact", key="current_job", value="product manager", attributes={})
    d = await reconcile_candidate(fake, repo, user_id=user, candidate=job2)
    assert d["action"] == "SUPERSEDE" and len(fake.calls_for("reconcile")) == 0
    r = await apply_decision(repo, user_id=user, session_id="s3", candidate=job2, decision=d, now=FIXED_NOW)
    old = await repo.get_memory(user, d["target_memory_id"])
    new = await repo.get_memory(user, r["memory_id"])
    assert old.status == "superseded" and old.superseded_by == new.id and new.supersedes == old.id
    # multi-valued ambiguity → LLM; scripted UPDATE merges attributes
    c2 = scored(attributes={"target_year": 2027, "target_role": "PM"})
    fake.script("reconcile", {"action": "UPDATE", "target_memory_id": first_id, "merged_attributes": {"target_year": 2027, "target_role": "PM"}})
    d = await reconcile_candidate(fake, repo, user_id=user, candidate=c2)
    assert d["action"] == "UPDATE"
    await apply_decision(repo, user_id=user, session_id="s3", candidate=c2, decision=d, now=FIXED_NOW)
    assert (await repo.get_memory(user, first_id)).attributes["target_role"] == "PM"
    # weaker certainty may not supersede a firmer memory
    weak = scored(attributes={"target_year": 2030}, certainty="hedged")
    fake.script("reconcile", {"action": "SUPERSEDE", "target_memory_id": first_id})
    assert (await reconcile_candidate(fake, repo, user_id=user, candidate=weak))["action"] == "ADD"
    # LLM failure → SKIP (no write)
    fake.script("reconcile", "garbage", "still garbage")
    d = await reconcile_candidate(fake, repo, user_id=user, candidate=scored(attributes={"target_year": 2031}))
    assert d["action"] == "SKIP" and d["reason"].startswith("reconcile_failed")
    r = await apply_decision(repo, user_id=user, session_id="s3", candidate=weak, decision=d, now=FIXED_NOW)
    assert r["action"] == "SKIP"


def test_build_memory_is_deterministic():
    a = build_memory("u", scored(), session_id="s1", now=FIXED_NOW)
    b = build_memory("u", scored(), session_id="s1", now=FIXED_NOW)
    c = build_memory("u", scored(), session_id="s2", now=FIXED_NOW)
    assert a.id == b.id != c.id
    assert a.prompt_version == "v1" and a.attributes == {"target_year": 2027} and a.cardinality == "multi" and a.source == "stated"


async def test_memory_graph_fans_out_and_is_idempotent():
    settings, repo, fake = make_settings(), InMemoryBrainRepository(), FakeLLM()
    deps = Deps(settings=settings, repo=repo, llm=fake, clock=lambda: FIXED_NOW)
    deps.tools = make_tools(repo, settings, deps.clock)
    graph = build_memory_graph(deps)
    payload = {"user_id": "u1", "session_id": "s1", "now": FIXED_NOW.isoformat(), "language": "en", "ephemeral": False,
               "message": "I love classical music and I prefer morning meetings. Planning to switch jobs next year."}
    out = await graph.ainvoke(payload)
    actions = sorted((w["action"], w["key"]) for w in out["written"])
    assert actions == [("ADD", "career_change"), ("ADD", "classical_music"), ("ADD", "morning_meetings")]
    assert len(out["decisions"]) == 3  # one parallel reconcile per candidate (Send)
    out2 = await graph.ainvoke(payload)
    assert {w["action"] for w in out2["written"]} == {"NOOP"}
    mems = await repo.list_memories("u1")
    assert len(mems) == 3 and all(m.mention_count == 2 for m in mems)
    # extraction failure → nothing written, no exception
    fake.fail_tasks.add("extract")
    out3 = await graph.ainvoke({**payload, "message": "I prefer evening walks."})
    assert out3.get("skipped_reason") == "extraction_failed" and "written" not in out3
    assert len(await repo.list_memories("u1")) == 3
    # ephemeral → skipped
    fake.fail_tasks.clear()
    out4 = await graph.ainvoke({**payload, "ephemeral": True, "message": "I prefer evening walks."})
    assert out4.get("skipped_reason") == "ephemeral"


async def test_extract_prompt_contains_date_context_and_message():
    fake = FakeLLM()
    from app.graphs.nodes.memory_ops import extract_candidates

    await extract_candidates(fake, message="I love chess", now=FIXED_NOW, tz="Asia/Kolkata", context="You asked about hobbies")
    call = fake.calls_for("extract")[0]
    system = next(m.content for m in call.messages if m.role == "system")
    user = next(m.content for m in call.messages if m.role == "user")
    assert "Current date: 2026-10-17" in system and "Asia/Kolkata" in system and '"candidates"' in system
    assert 'assistant: You asked about hobbies' in user and '"""I love chess"""' in user
    assert isinstance(call.messages[0], LLMMessage)
