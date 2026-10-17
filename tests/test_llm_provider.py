from datetime import date

import pytest
from pydantic import BaseModel

from app.llm.fake import FakeLLM, rule_extract
from app.llm.provider import (
    LLMMessage,
    LLMResponse,
    LLMUnavailable,
    StructuredOutputError,
    extract_json,
    with_json_instructions,
)


class Out(BaseModel):
    route: str
    n: int = 0


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here it is: {"a": {"b": 2}} hope that helps') == {"a": {"b": 2}}
    assert extract_json("[1, 2]") == [1, 2]
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_with_json_instructions_appends_schema_to_system():
    msgs = with_json_instructions([LLMMessage("system", "base"), LLMMessage("user", "hi")], Out)
    assert msgs[0].content.startswith("base") and '"route"' in msgs[0].content
    msgs = with_json_instructions([LLMMessage("user", "hi")], Out)
    assert msgs[0].role == "system" and msgs[1].role == "user"


async def test_structured_retries_once_with_validation_error():
    fake = FakeLLM()
    fake.script("analyze", "not json at all", {"route": "answer", "n": 3})
    out = await fake.structured("analyze", [LLMMessage("user", "x")], Out)
    assert out == Out(route="answer", n=3)
    calls = fake.calls_for("analyze")
    assert len(calls) == 2
    retry_prompt = calls[1].messages
    assert retry_prompt[-2].role == "assistant" and retry_prompt[-2].content == "not json at all"
    assert "not valid for the schema" in retry_prompt[-1].content


async def test_structured_gives_up_after_retry():
    fake = FakeLLM()
    fake.script("analyze", "garbage", {"route": 5})  # second is invalid type too
    with pytest.raises(StructuredOutputError):
        await fake.structured("analyze", [LLMMessage("user", "x")], Out)
    assert len(fake.calls_for("analyze")) == 2


async def test_fake_failures_and_scripts():
    fake = FakeLLM(fail_tasks={"answer"})
    with pytest.raises(LLMUnavailable):
        await fake.complete("answer", [LLMMessage("user", "x")])
    fake.script("summarize", LLMResponse(content="S"), lambda msgs, tools: LLMResponse(content=f"n={len(msgs)}"))
    assert (await fake.complete("summarize", [])).content == "S"
    assert (await fake.complete("summarize", [LLMMessage("user", "a")])).content == "n=1"
    assert (await fake.healthcheck())["backend"] == "fake"


def test_rule_extract_resolves_relative_dates_and_labels():
    today = date(2026, 10, 17)
    cands = rule_extract("My name is Rahul, born 15 Aug 1995 in Delhi. Planning to switch jobs next year.", today)
    by_key = {c["key"]: c for c in cands}
    assert by_key["name"]["value"] == "Rahul" and by_key["dob"]["value"] == "1995-08-15" and by_key["birth_place"]["value"] == "Delhi"
    assert by_key["career_change"]["attributes"] == {"target_year": 2027} and by_key["career_change"]["certainty"] == "explicit"
    pm = rule_extract("I have a PM interview next month.", today)[0]
    assert pm["key"] == "pm_interview" and pm["valid_until"] == "2026-11-30"
    hyp = rule_extract("If I ever moved to Canada, what would change?", today)[0]
    assert hyp["certainty"] == "hypothetical"
    other = rule_extract("My brother is planning to switch jobs next year", today)[0]
    assert other["subject"] == "other"
    assert rule_extract("What should I focus on for my career?", today) == []
    ctx = rule_extract("Actually I'll switch this year, not next.", today, context="You plan to switch jobs in 2027")[0]
    assert ctx["attributes"] == {"target_year": 2026}
