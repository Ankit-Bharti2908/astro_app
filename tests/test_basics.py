"""Unit checks for the deterministic pieces the chat scenarios lean on:
routing/language rules, memory scoring, context budgets, the structured-output retry and the HTTP API.
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from app.brain.models import Memory, MemoryCandidate, ScoredCandidate, UserProfile
from app.brain.profile_validation import validate_profile_patch
from app.brain.repository import apply_profile_patch
from app.brain.scoring import initial_confidence, initial_importance, is_expired, rank_memories
from app.context.builder import build_context, profile_layer
from app.graphs.nodes.analyze import honour_route
from app.llm.provider import LLMMessage, StructuredOutputError, extract_json
from app.profile.zodiac import sun_sign
from app.prompts.templates import PROMPT_VERSION
from tests.conftest import FIXED_NOW, chat, make_settings
from tests.fake_llm import FakeLLM

NOW = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)


def mem(i: int, **kw) -> Memory:
    base = dict(id=f"m{i}", type="goal", key=f"goal_{i}", value=f"goal number {i}", life_area="career",
                importance=0.9, confidence=0.9, created_at=NOW, updated_at=NOW)
    base.update(kw)
    return Memory(**base)


def test_a_followup_needs_something_to_follow_up_on():
    """The only check left on the classifier's route, and the only one that is not about
    meaning: `followup` answers from the previous reply, so with no previous reply there is
    nothing to answer from. Everything else — greetings, recall questions, memory commands,
    life areas, Hinglish — was a word list and is now the prompt's job (§4)."""
    assert honour_route("followup", has_history=False) == "answer"
    assert honour_route("followup", has_history=True) == "followup"
    for route in ("answer", "memory_recall", "memory_cmd", "smalltalk"):
        assert honour_route(route, has_history=False) == route


def test_every_turn_asks_the_classifier(client, fake_llm):
    """There is no keyword short-circuit any more: a bare greeting is classified by the model
    exactly like anything else. The rules-first path existed to save this call, and it is what
    turned "Remove obstacles from my career" into a deletion."""
    for message in ("hi", "Why do you say that?", "Remember that I love chess", "What do you remember about me?"):
        before = len(fake_llm.calls_for("analyze"))
        chat(client, message)
        assert len(fake_llm.calls_for("analyze")) == before + 1, message


def test_scoring_and_ranking():
    def cand(**kw):
        return MemoryCandidate(**{"type": "goal", "key": "k", "value": "v", "life_area": "career", **kw})

    assert initial_confidence(cand(certainty="explicit")) == 0.9
    assert initial_confidence(cand(certainty="hypothetical")) == 0.3
    assert initial_confidence(cand(certainty="explicit", subject="other")) == 0.45
    assert initial_importance(cand(type="profile_fact")) > initial_importance(cand(type="interest"))

    strong, weak = mem(0), mem(1, type="interest", importance=0.5, confidence=0.6, updated_at=NOW - timedelta(days=60))
    expired, superseded = mem(2, valid_until=date(2026, 1, 1)), mem(3, status="superseded")
    assert [m.id for m in rank_memories([weak, expired, superseded, strong], NOW)] == ["m0", "m1"]
    assert is_expired(expired, NOW.date()) and not is_expired(strong, NOW.date())
    assert sun_sign(date(1995, 8, 15)) == "Leo" and sun_sign(date(2000, 1, 1)) == "Capricorn"


def test_context_layers_stay_within_budget_and_report_usage():
    settings = make_settings(budget_memories=40)
    profile = UserProfile(user_id="u", name="Rahul", dob=date(1995, 8, 15), birth_place="Delhi", zodiac="Leo")
    ctx = build_context(
        settings=settings, now=NOW, tz="Asia/Kolkata", language="hinglish", profile=profile,
        memory_index={"career": {"goal": 1}}, memories=[mem(i) for i in range(20)], summary="We discussed careers.",
        messages=[HumanMessage(content="earlier", id="h0"), AIMessage(content="reply", id="a0"), HumanMessage(content="now?", id="h1")],
    )
    sp = ctx.system_prompt
    assert sp.index("## User profile") < sp.index("## Memory index") < sp.index("## Known memories") < sp.index("Current date: 2026-10-17")
    assert "Reply language: hinglish" in sp and "Sun sign: Leo" in sp
    assert ctx.context_tokens["memories"] <= settings.budget_memories
    assert 0 < len(ctx.memory_ids) < 20  # budget trimmed the tail
    assert ctx.context_used[:3] == ["user_profile", "memory_index", "memory:goal_0"]


async def test_structured_output_retries_once_then_gives_up():
    class Out(BaseModel):
        route: str

    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    fake = FakeLLM()
    fake.script("analyze", "not json at all", {"route": "answer"})
    assert (await fake.structured("analyze", [LLMMessage("user", "x")], Out)).route == "answer"
    assert "not valid for the schema" in fake.calls_for("analyze")[1].messages[-1].content

    fake.script("analyze", "garbage", {"route": 5})
    with pytest.raises(StructuredOutputError):
        await fake.structured("analyze", [LLMMessage("user", "x")], Out)


def test_user_api_crud_and_validation(client):
    r = client.post("/users", json={"user_id": "u1", "name": "Rahul", "dob": "1995-08-15", "birth_place": "Delhi"})
    assert r.status_code == 201 and r.json()["zodiac"] == "Leo"
    assert client.post("/users", json={"user_id": "u1"}).status_code == 409
    assert client.post("/users", json={"user_id": "u2", "dob": "not-a-date"}).status_code == 422
    assert client.get("/users/nope").status_code == 404
    r = client.patch("/users/u1", json={"dob": "1996-01-05", "preferred_language": "hinglish"})
    assert r.json()["zodiac"] == "Capricorn" and r.json()["name"] == "Rahul"  # untouched fields preserved
    assert client.patch("/users/u1", json={"preferred_language": "fr"}).status_code == 422
    assert client.post("/chat", json={"user_id": "u", "session_id": "s", "message": ""}).status_code == 422


async def test_memory_api_lists_ranked_and_soft_deletes(client, repo):
    for i, area in enumerate(["career", "health", "career"]):
        await repo.add_memory("u1", Memory(id=f"m{i}", type="goal", key=f"k{i}", value=f"v{i}", life_area=area,
                                           created_at=FIXED_NOW - timedelta(days=i), updated_at=FIXED_NOW - timedelta(days=i),
                                           importance=0.9, confidence=0.9 - 0.1 * i))
    assert [m["id"] for m in client.get("/users/u1/memories").json()] == ["m0", "m1", "m2"]  # by retrieval score
    assert [m["id"] for m in client.get("/users/u1/memories", params={"life_area": "health"}).json()] == ["m1"]
    assert client.get("/users/other/memories").json() == []  # isolation by user_id
    assert client.delete("/users/u1/memories/m1").status_code == 204
    assert client.delete("/users/u1/memories/m1").status_code == 404  # already deleted
    assert [m["id"] for m in client.get("/users/u1/memories").json()] == ["m0", "m2"]


def test_health_and_chat_response_shape(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["brain"]["ok"] and h["prompt_version"] == PROMPT_VERSION
    d = chat(client, "What should I focus on for my career?")
    assert set(d) >= {"response", "route", "language", "context_used", "context_tokens", "degraded"}


def test_console_page_and_memory_run_trace(client):
    """The /ui console is served, and the trace it polls reports what the memory graph did."""
    assert client.get("/ui/").status_code == 200
    chat(client, "Planning to switch jobs next year.")

    runs = client.get("/users/rahul/memory-runs", params={"session_id": "s1"}).json()
    assert len(runs) == 1 and runs[0]["message"].startswith("Planning to switch")
    assert [w["action"] for w in runs[0]["written"]] == ["ADD"]
    assert [c["key"] for c in runs[0]["accepted"]] == ["career_change"]
    assert client.get("/users/rahul/memory-runs", params={"since": runs[0]["seq"]}).json() == []
    assert client.get("/users/other/memory-runs").json() == []  # isolation by user_id


def test_ids_cannot_forge_the_composite_thread_key(client):
    """thread_id is f"{user_id}:{session_id}", so a ':' in either half could address another
    user's session. Both halves are charset-guarded; an omitted session starts a fresh UUID."""
    def post(**kw):
        return client.post("/chat", json={"message": "hi", **kw}).status_code

    assert post(user_id="u", session_id="victim:s1") == 422
    assert post(user_id="org:u", session_id="s1") == 422
    assert client.get("/users/bad:id").status_code == 422
    assert client.get("/users/bad:id/memories").status_code == 422
    assert post(user_id="a.b@c-d_e", session_id="s1") == 200

    body = client.post("/chat", json={"user_id": "rahul", "message": "hi"}).json()
    assert uuid.UUID(body["session_id"]).version == 4  # omitted → server mints one


def test_inadmissible_profile_values_are_refused_not_repaired(repo, clock):
    """A chart is computed from these, so an unusable value must not be stored as a plausible
    one. Validation sits at the shared write path, under both the API and the memory graph."""
    clean, rejected = validate_profile_patch(
        {"dob": "2000-01-01", "tob": "222:8 PM", "birth_place": "mars", "name": "Rahul"},
        today=FIXED_NOW.date(),
    )
    assert clean == {"dob": date(2000, 1, 1), "name": "Rahul"}
    assert set(rejected) == {"tob", "birth_place"}  # never 22:08, never "mars"
    assert validate_profile_patch({"tob": "2:30 pm", "birth_place": " new  delhi "})[0] == {
        "tob": "14:30", "birth_place": "new delhi",
    }
    assert "future" in validate_profile_patch({"dob": "2030-05-01"}, today=FIXED_NOW.date())[1]["dob"]

    profile = apply_profile_patch(UserProfile(user_id="u"), {"tob": "222:8 PM", "dob": "2000-01-01"}, FIXED_NOW)
    assert profile.tob is None and profile.zodiac == "Capricorn"  # the good half still lands


def test_profile_layer_names_only_the_gaps():
    """The model is told which slots are missing rather than left to scan six rows for
    "unknown" — re-asking for a detail already on file is what makes a brain feel amnesiac."""
    full = UserProfile(user_id="u", name="Rahul", dob=date(1995, 8, 15), tob="10:08", birth_place="Delhi")
    assert "Missing for a chart" not in profile_layer(full, 150)

    layer = profile_layer(full.model_copy(update={"tob": None}), 150)
    assert "Missing for a chart: time of birth" in layer
    assert "date of birth" not in layer.split("Missing for a chart:")[1]  # never re-ask for Delhi/the DOB
    assert "place of birth" not in layer.split("Missing for a chart:")[1]


async def test_a_profile_slot_is_decided_by_its_key_not_the_guessed_type(repo, settings, fake_llm):
    """Observed live: the same `birth_place` arrived as `life_event` on one turn and
    `profile_fact` on the next, so one became a Memory node and the other a User field. Only
    the User field is validated, so an impossible `tob` typed as `life_event` reached the
    graph intact. The slot decides, not the type."""
    from app.graphs.nodes.memory_ops import reconcile_candidate

    for guessed in ("life_event", "profile_fact", "preference"):
        candidate = ScoredCandidate(
            type=guessed, key="tob", value="12:00 PM", life_area="general",
            certainty="explicit", subject="self", temporality="current",
            confidence=0.9, importance=0.8,
        )
        decision = await reconcile_candidate(fake_llm, repo, user_id="u", candidate=candidate)
        assert decision["action"] == "PROFILE", guessed
