"""§15 scenarios 1–12 plus the §13 walkthrough, all with FakeLLM + in-memory brain + MemorySaver."""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.brain.models import Memory
from app.llm.provider import LLMResponse, ToolCall
from app.main import create_app
from tests.conftest import FIXED_NOW, FailingRepo, chat, make_settings


def memories(client: TestClient, user="rahul", **params) -> list[dict]:
    r = client.get(f"/users/{user}/memories", params=params)
    assert r.status_code == 200
    return r.json()


# 1 ------------------------------------------------------------------------------
def test_new_user_gets_generic_answer_without_memories(client):
    d = chat(client, "What should I focus on for my career?", user="newbie")
    assert d["route"] == "answer" and d["degraded"] == []
    assert not [c for c in d["context_used"] if c.startswith("memory:")]
    assert "user_profile" not in d["context_used"]
    assert d["response"]


# 2 ------------------------------------------------------------------------------
def test_create_long_term_memory_and_profile(client, repo):
    d = chat(client, "My name is Rahul, born 15 Aug 1995 in Delhi. Planning to switch jobs next year.")
    assert d["route"] == "answer"
    mems = memories(client)
    assert len(mems) == 1
    goal = mems[0]
    assert goal["key"] == "career_change" and goal["type"] == "goal"
    assert goal["attributes"]["target_year"] == 2027 and goal["confidence"] == 0.9 and goal["importance"] == 0.9
    assert goal["session_id"] == "s1" and goal["status"] == "active"
    user = client.get("/users/rahul").json()
    assert user["name"] == "Rahul" and user["dob"] == "1995-08-15" and user["birth_place"] == "Delhi" and user["zodiac"] == "Leo"


# 3 ------------------------------------------------------------------------------
def test_retrieve_memory_in_career_answer(client, fake_llm, repo):
    chat(client, "Planning to switch jobs next year.")
    d = chat(client, "What should I focus on for my career?")
    assert "memory:career_change" in d["context_used"]
    assert "switch jobs" in d["response"]
    assert d["context_tokens"]["memories"] > 0
    # the strong model saw the memory line in its system prompt
    answer_call = fake_llm.calls_for("answer")[-1]
    assert "[career] Goal: switch jobs (target year 2027" in answer_call.messages[0].content
    # reinforcement: last_used_at updated
    mem = (repo.memories["rahul"] and list(repo.memories["rahul"].values())[0])
    assert mem.last_used_at == FIXED_NOW


# 4 ------------------------------------------------------------------------------
def test_followup_uses_short_term_context_only(client, fake_llm, repo, app):
    chat(client, "Planning to switch jobs next year.")
    chat(client, "What should I focus on for my career?")
    repo.calls.clear()
    runs_before = len(app.state.container.memory_runs)
    d = chat(client, "Why do you say that?")
    assert d["route"] == "followup"
    assert "recent_turns" in d["context_used"] and not [c for c in d["context_used"] if c.startswith("memory:")]
    # no retrieval calls: only the always-on profile/index load and session bookkeeping
    assert set(repo.calls) <= {"get_user", "memory_index", "upsert_session"}
    assert len(app.state.container.memory_runs) == runs_before  # memory graph skipped
    assert fake_llm.calls_for("followup") and not fake_llm.calls_for("extract")[runs_before + 1 :]


# 5 ------------------------------------------------------------------------------
def test_new_session_recalls_goal_from_brain(client):
    chat(client, "Planning to switch jobs next year.", session="s1")
    d = chat(client, "What do you remember about my career goals?", session="s2")
    assert d["route"] == "memory_recall"
    assert "memory:career_change" in d["context_used"]
    assert "switch jobs" in d["response"]
    assert d["context_tokens"]["turns"] < 40  # fresh checkpointer thread: only the current turn


# 6 ------------------------------------------------------------------------------
async def test_irrelevant_memory_not_in_career_context(client, repo):
    await repo.add_memory("rahul", Memory(id="h1", type="preference", key="morning_yoga", value="morning yoga", life_area="health", importance=0.6, created_at=FIXED_NOW, updated_at=FIXED_NOW))
    chat(client, "Planning to switch jobs next year.")
    d = chat(client, "What should I focus on for my career?")
    assert "memory:career_change" in d["context_used"] and "memory:morning_yoga" not in d["context_used"]
    assert "memory_index" in d["context_used"]  # L2 still tells the model the health memory exists


# 7 ------------------------------------------------------------------------------
def test_correction_supersedes_old_memory(client):
    chat(client, "Planning to switch jobs next year.")
    chat(client, "What should I focus on for my career?")
    d = chat(client, "Actually I'll switch this year, not next.")
    assert d["route"] == "answer"
    all_mems = memories(client, status="all")
    old = next(m for m in all_mems if m["status"] == "superseded")
    new = next(m for m in all_mems if m["status"] == "active")
    assert old["attributes"]["target_year"] == 2027 and new["attributes"]["target_year"] == 2026
    assert new["supersedes"] == old["id"] and old["superseded_by"] == new["id"]
    assert [m["key"] for m in memories(client)] == ["career_change"]


# 8 ------------------------------------------------------------------------------
def test_missing_profile_answers_generically(client, fake_llm):
    d = chat(client, "When will I get married?", user="ghost")
    assert d["route"] == "answer" and d["degraded"] == []
    assert "Unknown user" in fake_llm.calls_for("answer")[-1].messages[0].content
    assert "birth date" in d["response"]


# 9 ------------------------------------------------------------------------------
def test_hypothetical_is_not_stored(client, app):
    chat(client, "If I ever moved to Canada, what would change for me?")
    assert memories(client) == []
    run = app.state.container.memory_runs[-1]["result"]
    assert run["dropped"] and run["dropped"][0]["drop_reason"].startswith("below_threshold")
    assert "written" not in run


# 10 -----------------------------------------------------------------------------
def test_brain_down_degrades_gracefully(settings, fake_llm, clock):
    app = create_app(settings, repo=FailingRepo(), llm=fake_llm, clock=clock)
    with TestClient(app) as client:
        d = chat(client, "Planning to switch jobs next year.")
        assert d["degraded"] == ["brain_unavailable"] and d["response"]
        assert d["route"] == "answer" and not [c for c in d["context_used"] if c.startswith("memory:")]
        run = app.state.container.memory_runs[-1]["result"]
        assert run["written"] and all(w["action"] == "SKIP" for w in run["written"])
        d = chat(client, "What do you remember about my career?")
        assert d["degraded"] == ["brain_unavailable"] and "unavailable" in d["response"]
        d = chat(client, "Remember that I love chess")
        assert d["degraded"] == ["brain_unavailable"]
        h = client.get("/health").json()
        assert h["status"] == "degraded" and h["brain"]["ok"] is False
        assert client.get("/users/rahul").status_code == 503


# 11 -----------------------------------------------------------------------------
def test_hinglish_detected_and_memory_stored_in_english(client):
    d = chat(client, "Mujhe agle saal naukri badalni hai")
    assert d["language"] == "hinglish" and d["route"] == "answer"
    assert d["response"].startswith("Namaste")
    mems = memories(client)
    assert mems[0]["key"] == "career_change" and mems[0]["value"] == "switch jobs" and mems[0]["attributes"]["target_year"] == 2027
    d = chat(client, "Meri shaadi kab hogi?")
    assert d["language"] == "hinglish" and "memory:career_change" not in d["context_used"]  # relationships ≠ career
    assert memories(client, life_area="relationships") == []


# 12 -----------------------------------------------------------------------------
def test_agent_tool_loop_is_bounded(client, fake_llm, app, repo):
    chat(client, "Planning to switch jobs next year.")
    fake_llm.script(
        "answer",
        LLMResponse(tool_calls=[ToolCall("c1", "recall_memories", {"life_area": "career", "query": "goals"})]),
        LLMResponse(tool_calls=[ToolCall("c2", "search_past_sessions", {"query": "career"})]),
        LLMResponse(content="Final answer using the extra context."),
    )
    d = chat(client, "Looking at everything you know, what should my long-term plan be?")
    assert d["response"] == "Final answer using the extra context."
    assert "memory:career_change" in d["context_used"] and "tool:search_past_sessions" in d["context_used"]
    calls = fake_llm.calls_for("answer")[-3:]
    assert calls[0].tools and calls[1].tools and calls[2].tools is None  # round cap = 2
    assert calls[1].messages[-1].role == "tool" and "switch jobs" in calls[1].messages[-1].content
    # intra-turn tool traffic is not persisted in the session
    state = app.state.container.response_graph.get_state({"configurable": {"thread_id": "rahul:s1"}})
    msgs = state.values["messages"]
    assert not any(isinstance(m, ToolMessage) or (isinstance(m, AIMessage) and m.tool_calls) for m in msgs)
    assert [type(m).__name__ for m in msgs] == ["HumanMessage", "AIMessage", "HumanMessage", "AIMessage"]


def test_tool_loop_honours_round_cap_even_if_model_keeps_asking(client, fake_llm):
    fake_llm.script(
        "answer",
        LLMResponse(tool_calls=[ToolCall("c1", "get_astro_profile", {})]),
        LLMResponse(tool_calls=[ToolCall("c2", "get_astro_profile", {})]),
        LLMResponse(content="", tool_calls=[ToolCall("c3", "get_astro_profile", {})]),  # ignored: cap reached
    )
    d = chat(client, "What does my chart say about this year?", user="newbie")
    assert len(fake_llm.calls_for("answer")) == 3 and d["route"] == "answer"
    assert "tool:get_astro_profile" in d["context_used"]
    assert d["response"]  # fallback text, never an empty reply


# ---- beyond the 12: walkthrough extras ---------------------------------------

def test_time_bound_memory_expires_from_retrieval(client, repo):
    chat(client, "I have a PM interview next month.")
    mems = memories(client)
    assert mems[0]["key"] == "pm_interview" and mems[0]["valid_until"] == "2026-11-30"
    mem = list(repo.memories["rahul"].values())[0]
    mem.valid_until = date(2026, 10, 1)  # simulate time passing
    d = chat(client, "Anything I should prepare for at work?")
    assert "memory:pm_interview" not in d["context_used"]


def test_memory_cmd_remember_and_forget(client):
    d = chat(client, "Remember that I love classical music")
    assert d["route"] == "memory_cmd" and d["memory_cmd_result"]["stored"][0]["action"] == "ADD"
    assert "classical music" in d["response"]
    mem = memories(client)[0]
    assert mem["key"] == "classical_music" and mem["confidence"] == 1.0 and mem["source"] == "confirmed"
    d = chat(client, "Forget about classical music")
    assert d["memory_cmd_result"]["deleted"] == [mem["id"]]
    assert memories(client) == [] and memories(client, status="deleted")[0]["id"] == mem["id"]
    d = chat(client, "Forget about my pet iguana")
    assert d["memory_cmd_result"]["deleted"] == [] and "nothing to forget" in d["response"]


def test_ephemeral_reads_but_never_writes(client, app, repo):
    chat(client, "Planning to switch jobs next year.")
    d = chat(client, "I have a PM interview next month. What should I focus on?", ephemeral=True, session="tmp")
    assert "memory:career_change" in d["context_used"]  # reads
    assert [m["key"] for m in memories(client)] == ["career_change"]  # no write
    assert all(r["payload"]["session_id"] != "tmp" for r in app.state.container.memory_runs)
    assert repo.sessions.get(("rahul", "tmp")) is None
    d = chat(client, "Remember that I love chess", ephemeral=True, session="tmp")
    assert d["memory_cmd_result"]["stored"] == [] and "temporary" in d["response"]


def test_llm_down_returns_canned_reply_with_degraded_flag(client, fake_llm, app):
    fake_llm.fail_all = True
    d = chat(client, "What should I focus on for my career?")
    assert d["degraded"] == ["llm_unavailable"] and d["route"] == "answer"
    assert "trouble reaching" in d["response"]
    assert len(fake_llm.calls_for("answer")) == 2  # RetryPolicy(max_attempts=2)
    run = app.state.container.memory_runs[-1]["result"]
    assert run["skipped_reason"] == "extraction_failed" and memories(client) == []
    d = chat(client, "Why?")
    assert d["route"] == "followup" and d["degraded"] == ["llm_unavailable"]
    fake_llm.fail_all = False
    d = chat(client, "Meri shaadi kab hogi?")
    assert d["degraded"] == [] and d["language"] == "hinglish"


def test_language_precedence_explicit_then_message_then_profile(client, repo):
    client.post("/users", json={"user_id": "rahul", "name": "Rahul", "preferred_language": "hi"})
    assert chat(client, "hi")["language"] == "hi"  # too short to detect → profile preference
    d = chat(client, "Reply in Hindi: what about my career?")
    assert d["language"] == "hi"
    assert client.get("/users/rahul").json()["preferred_language"] == "hi"
    assert chat(client, "What about my finances this year?")["language"] == "en"  # message language wins over profile
    assert chat(client, "मेरी शादी कब होगी?")["language"] == "hi"


def test_compaction_summarizes_and_persists_session(fake_llm, repo, clock):
    settings = make_settings(compact_threshold_tokens=60, keep_last_messages=2)
    app = create_app(settings, repo=repo, llm=fake_llm, clock=clock)
    with TestClient(app) as client:
        chat(client, "Planning to switch jobs next year, what should I focus on for my career?")
        chat(client, "And how should I prepare for interviews at work?")
        d = chat(client, "What about my finances in 2027?")
        state = app.state.container.response_graph.get_state({"configurable": {"thread_id": "rahul:s1"}}).values
        assert state["summary"].startswith("Summary:") or "Summary:" in state["summary"]
        assert len(state["messages"]) == 2 and isinstance(state["messages"][0], HumanMessage)
        assert fake_llm.calls_for("summarize")
        session = repo.sessions[("rahul", "s1")]
        assert session.turn_count == 3 and session.summary == state["summary"]
        d = chat(client, "Anything else?")
        assert "session_summary" in d["context_used"] and d["context_tokens"]["summary"] > 0
        # another session can find it through the search_past_sessions tool
        fake_llm.script("answer", LLMResponse(tool_calls=[ToolCall("c1", "search_past_sessions", {"query": "career interviews"})]), LLMResponse(content="Recalled."))
        d = chat(client, "What did we discuss about my career last time?", session="s2")
        assert "past_session:s1" in d["context_used"]
        # 3 memory-worthy turns + "Anything else?" (answer) + s2 turn → every answer turn scheduled the memory graph
        assert len(app.state.container.memory_runs) == 5


def test_sync_memory_flag_runs_memory_graph_inline(client, app):
    d = chat(client, "Planning to switch jobs next year.", sync_memory=True)
    assert d["route"] == "answer" and memories(client)[0]["key"] == "career_change"
    assert app.state.container.memory_runs[0]["result"]["written"][0]["action"] == "ADD"


def test_smalltalk_uses_profile_only(client, fake_llm, repo):
    client.post("/users", json={"user_id": "rahul", "name": "Rahul", "dob": "1995-08-15"})
    chat(client, "Planning to switch jobs next year.")
    repo.calls.clear()
    d = chat(client, "Namaste!")
    assert d["route"] == "smalltalk" and d["context_used"] == ["user_profile"]
    assert "Rahul" in d["response"] and "list_memories" not in repo.calls
    assert "Known memories" not in fake_llm.calls_for("smalltalk")[-1].messages[0].content


def test_recall_with_nothing_stored(client):
    d = chat(client, "What do you remember about my career?", user="newbie")
    assert d["route"] == "memory_recall" and "don't have anything stored" in d["response"]
    assert d["degraded"] == []
