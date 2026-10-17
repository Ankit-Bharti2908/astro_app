from datetime import UTC, date, datetime

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.brain.models import Memory, UserProfile
from app.context.builder import build_context, index_layer, memories_layer, turns_layer
from app.context.tokens import count_tokens, truncate_to_budget
from app.prompts.templates import SYSTEM_BASE
from tests.conftest import make_settings

NOW = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)


def mem(i: int, area="career", **kw) -> Memory:
    base = dict(id=f"m{i}", type="goal", key=f"goal_{i}", value=f"goal number {i} " + "x" * 40, life_area=area, created_at=NOW, updated_at=NOW)
    base.update(kw)
    return Memory(**base)


def test_token_helpers():
    assert count_tokens("") == 0
    assert count_tokens("abcd" * 10) == 10
    assert truncate_to_budget("abcd" * 10, 5).endswith("…")
    assert truncate_to_budget("short", 100) == "short"


def test_index_layer_format():
    text = index_layer({"career": {"goal": 2}, "finance": {"goal": 1, "interest": 1}}, 100)
    assert "career: 2 goals" in text and "finance: 1 goal, 1 interest" in text and " · " in text
    assert "no long-term memories" in index_layer({}, 100)


def test_memories_layer_respects_budget():
    memories = [mem(i) for i in range(20)]
    text, included = memories_layer(memories, budget=100)
    assert 0 < len(included) < 20
    assert count_tokens(text) <= 100
    assert text.startswith("## Known memories")
    assert included[0].as_line() in text


def test_turns_layer_keeps_current_turn_and_trims_history():
    msgs = [HumanMessage(content="old question " * 30, id="h0"), AIMessage(content="old answer " * 30, id="a0")]
    msgs += [HumanMessage(content="current question", id="h1")]
    turns, used = turns_layer(msgs, budget=20)
    assert [m.role for m in turns] == ["user"] and turns[0].content == "current question"
    turns, _ = turns_layer(msgs, budget=5000)
    assert [m.role for m in turns] == ["user", "assistant", "user"]


def test_turns_layer_skips_past_tool_traffic_but_keeps_current_turn_tools():
    msgs = [
        HumanMessage(content="q1", id="h0"),
        AIMessage(content="", tool_calls=[{"id": "c0", "name": "recall_memories", "args": {}}], id="a0"),
        ToolMessage(content="old tool", tool_call_id="c0", id="t0"),
        AIMessage(content="a1", id="a1"),
        HumanMessage(content="q2", id="h1"),
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "recall_memories", "args": {"life_area": "career", "query": "x"}}], id="a2"),
        ToolMessage(content="new tool", tool_call_id="c1", id="t1"),
    ]
    turns, _ = turns_layer(msgs, budget=5000)
    roles = [m.role for m in turns]
    assert roles == ["user", "assistant", "user", "assistant", "tool"]
    assert turns[-1].content == "new tool" and turns[-2].tool_calls[0].name == "recall_memories"


def test_build_context_layers_and_usage():
    settings = make_settings()
    profile = UserProfile(user_id="u", name="Rahul", dob=date(1995, 8, 15), birth_place="Delhi", zodiac="Leo")
    ctx = build_context(
        settings=settings, now=NOW, tz="Asia/Kolkata", language="hinglish", profile=profile,
        memory_index={"career": {"goal": 1}}, memories=[mem(1)], summary="We discussed careers.",
        messages=[HumanMessage(content="earlier", id="h0"), AIMessage(content="reply", id="a0"), HumanMessage(content="now?", id="h1")],
    )
    sp = ctx.system_prompt
    assert sp.startswith(SYSTEM_BASE)
    assert sp.index("## User profile") < sp.index("## Memory index") < sp.index("## Known memories") < sp.index("## Earlier in this conversation") < sp.index("Current date: 2026-10-17")
    assert "Reply language: hinglish" in sp and "Name: Rahul" in sp and "Sun sign: Leo" in sp
    assert ctx.context_used == ["user_profile", "memory_index", "memory:goal_1", "session_summary", "recent_turns"]
    assert set(ctx.context_tokens) == {"meta", "profile", "index", "memories", "summary", "turns"}
    assert ctx.context_tokens["profile"] <= settings.budget_profile
    assert ctx.memory_ids == ["m1"]


def test_build_context_unknown_user():
    ctx = build_context(
        settings=make_settings(), now=NOW, tz="UTC", language="en", profile=None, memory_index={}, memories=None,
        summary=None, messages=[HumanMessage(content="hello", id="h")],
    )
    assert "Unknown user" in ctx.system_prompt
    assert ctx.context_used == []
    assert ctx.context_tokens["memories"] == 0
