"""Exercises the real LiteLLM Router path against a local mock provider (OpenAI + Anthropic wire formats):
routing by task tier, request shape, tool-call parsing, structured retry, provider fallback, timeouts, failure."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from app.llm.litellm_provider import (
    LiteLLMProvider,
    build_model_list,
    parse_response,
    to_openai_messages,
    to_openai_tools,
)
from app.llm.provider import LLMMessage, LLMUnavailable, StructuredOutputError, ToolCall, ToolSpec
from tests.conftest import make_settings
from tests.mock_llm_server import MockLLMServer

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture(scope="module")
def server():
    s = MockLLMServer().start()
    yield s
    s.stop()


@pytest.fixture(autouse=True)
def _reset(server):
    server.reset()


def deployments(server: MockLLMServer, *, provider: str = "openai", with_fallback: bool = True, strong="mock-strong", small="mock-small"):
    def params(model):
        if provider == "openai":
            return {"model": f"openai/{model}", "api_base": f"{server.url}/v1", "api_key": "test-key"}
        return {"model": f"anthropic/{model}", "api_base": f"{server.url}/v1/messages", "api_key": "test-key"}

    out = [{"model_name": "strong", "litellm_params": params(strong)}, {"model_name": "small", "litellm_params": params(small)}]
    if with_fallback:
        out.append({"model_name": "fallback", "litellm_params": params("mock-fallback")})
    return out


def provider(server, **kw) -> LiteLLMProvider:
    settings = make_settings(llm_backend="litellm", llm_timeout_s=kw.pop("timeout", 5.0), llm_num_retries=kw.pop("retries", 0))
    return LiteLLMProvider(settings, model_list=deployments(server, **kw))


TOOLS = [ToolSpec("recall_memories", "Fetch memories", {"type": "object", "properties": {"life_area": {"type": "string"}, "query": {"type": "string"}}, "required": ["life_area", "query"]})]
CONVO = [LLMMessage("system", "You are a test."), LLMMessage("user", "What about my career?")]


# ---- pure conversion units ----------------------------------------------------------

def test_message_and_tool_conversion():
    msgs = to_openai_messages(
        [
            LLMMessage("system", "sys"),
            LLMMessage("user", "hi"),
            LLMMessage("assistant", "", tool_calls=[ToolCall("c1", "recall_memories", {"life_area": "career", "query": "x"})]),
            LLMMessage("tool", "result", tool_call_id="c1", name="recall_memories"),
            LLMMessage("assistant", "done"),
        ]
    )
    assert msgs[0] == {"role": "system", "content": "sys"}
    assert msgs[2]["content"] is None and msgs[2]["tool_calls"][0]["function"] == {"name": "recall_memories", "arguments": json.dumps({"life_area": "career", "query": "x"})}
    assert msgs[3] == {"role": "tool", "tool_call_id": "c1", "content": "result", "name": "recall_memories"}
    assert msgs[4] == {"role": "assistant", "content": "done"}
    tools = to_openai_tools(TOOLS)
    assert tools[0]["type"] == "function" and tools[0]["function"]["parameters"]["required"] == ["life_area", "query"]
    assert to_openai_tools(None) is None


def test_build_model_list_from_settings():
    s = make_settings(strong_model="anthropic/claude-opus-5-5", small_model="anthropic/claude-haiku-5-5", fallback_model="")
    assert [d["model_name"] for d in build_model_list(s)] == ["strong", "small"]
    s = make_settings(fallback_model="openai/gpt-4o-mini")
    assert build_model_list(s)[-1] == {"model_name": "fallback", "litellm_params": {"model": "openai/gpt-4o-mini"}}


def test_parse_response_tolerates_bad_tool_json():
    from types import SimpleNamespace as NS

    resp = NS(model="m", usage=NS(prompt_tokens=1, completion_tokens=2), choices=[NS(finish_reason="tool_calls", message=NS(content=None, tool_calls=[NS(id=None, function=NS(name="f", arguments="{not json"))]))])
    parsed = parse_response(resp)
    assert parsed.tool_calls[0].args == {"_raw": "{not json"} and parsed.tool_calls[0].id == "call_0" and parsed.content == ""
    assert parsed.usage == {"prompt_tokens": 1, "completion_tokens": 2}


# ---- OpenAI wire format ---------------------------------------------------------------

async def test_openai_roundtrip_routes_by_tier_and_parses(server):
    p = provider(server)
    r = await p.complete("answer", CONVO)
    assert r.content.startswith("OK: What about my career?") and r.model == "mock-strong" and r.usage["prompt_tokens"] == 11
    r = await p.complete("extract", CONVO)
    assert r.model == "mock-small"
    assert [req["body"]["model"] for req in server.requests] == ["mock-strong", "mock-small"]
    body = server.requests[0]["body"]
    assert body["messages"][0] == {"role": "system", "content": "You are a test."} and body["max_tokens"] == 1024
    assert server.requests[1]["body"]["max_tokens"] == 800
    assert server.requests[0]["headers"]["authorization"] == "Bearer test-key"


async def test_openai_tool_call_request_and_response(server):
    p = provider(server)
    server.script("mock-strong", {"tool_calls": [{"name": "recall_memories", "args": {"life_area": "career", "query": "goals"}}]})
    r = await p.complete("answer", CONVO, tools=TOOLS)
    assert r.tool_calls == [ToolCall("call_0", "recall_memories", {"life_area": "career", "query": "goals"})] and r.content == ""
    body = server.requests[-1]["body"]
    assert body["tools"][0]["function"]["name"] == "recall_memories" and body["tool_choice"] == "auto"
    # second round: tool result goes back in OpenAI format
    follow = [*CONVO, LLMMessage("assistant", tool_calls=r.tool_calls), LLMMessage("tool", "- [career] Goal: switch jobs", tool_call_id="call_0", name="recall_memories")]
    r2 = await p.complete("answer", follow, tools=TOOLS)
    assert r2.content.startswith("OK:")
    sent = server.requests[-1]["body"]["messages"]
    assert sent[-1]["role"] == "tool" and sent[-2]["tool_calls"][0]["id"] == "call_0"


class Decision(BaseModel):
    action: str
    reason: str | None = None


async def test_structured_output_retries_through_router(server):
    p = provider(server)
    server.script("mock-small", {"content": "Sure! here you go: not json"}, {"content": '```json\n{"action": "ADD", "reason": "new"}\n```'})
    out = await p.structured("reconcile", CONVO, Decision)
    assert out == Decision(action="ADD", reason="new")
    reqs = server.requests_for("mock-small")
    assert len(reqs) == 2
    assert "JSON schema" in reqs[0]["body"]["messages"][0]["content"]
    assert reqs[1]["body"]["messages"][-1]["content"].startswith("Your previous reply was not valid")
    server.script("mock-small", {"content": "nope"}, {"content": "{}"})
    with pytest.raises(StructuredOutputError):
        await p.structured("reconcile", CONVO, Decision)


async def test_fallback_provider_on_server_error(server):
    p = provider(server)
    server.always["mock-strong"] = {"status": 500}
    r = await p.complete("answer", CONVO)
    assert r.model == "mock-fallback" and r.content.startswith("OK:")
    models = [req["body"]["model"] for req in server.requests]
    assert models[0] == "mock-strong" and models[-1] == "mock-fallback"


async def test_fallback_provider_on_timeout(server):
    p = provider(server, timeout=0.6)
    server.always["mock-strong"] = {"delay": 2.5}
    r = await p.complete("answer", CONVO)
    assert r.model == "mock-fallback"


async def test_all_providers_down_raises_unavailable(server):
    p = provider(server)
    server.always["mock-strong"] = {"status": 503}
    server.always["mock-fallback"] = {"status": 503}
    with pytest.raises(LLMUnavailable) as exc:
        await p.complete("answer", CONVO)
    assert "503" in str(exc.value) or "ServiceUnavailable" in str(exc.value) or "mock failure" in str(exc.value)


async def test_no_fallback_configured_surfaces_failure(server):
    p = provider(server, with_fallback=False)
    server.always["mock-strong"] = {"status": 429}
    with pytest.raises(LLMUnavailable):
        await p.complete("answer", CONVO)
    assert all(req["body"]["model"] == "mock-strong" for req in server.requests)


async def test_malformed_provider_response_is_unavailable(server):
    p = provider(server, with_fallback=False)
    server.script("mock-strong", {"raw": {"id": "x", "object": "chat.completion", "created": 0, "model": "mock-strong", "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1}}})
    with pytest.raises(LLMUnavailable):
        await p.complete("answer", CONVO)


async def test_structured_retry_never_replays_empty_assistant_content(server):
    p = provider(server)
    server.script("mock-small", {"content": ""}, {"content": '{"action": "ADD"}'})
    assert (await p.structured("reconcile", CONVO, Decision)).action == "ADD"
    retry = server.requests_for("mock-small")[1]["body"]["messages"]
    assert retry[-2]["role"] == "assistant" and retry[-2]["content"] == "(empty reply)"


async def test_total_timeout_bounds_the_whole_retry_chain(server):
    settings = make_settings(llm_backend="litellm", llm_timeout_s=5.0, llm_num_retries=3, llm_total_timeout_s=0.8)
    p = LiteLLMProvider(settings, model_list=deployments(server))
    server.always["mock-strong"] = {"delay": 3.0}
    server.always["mock-fallback"] = {"delay": 3.0}
    import time

    t = time.perf_counter()
    with pytest.raises(LLMUnavailable):
        await p.complete("answer", CONVO)
    assert time.perf_counter() - t < 2.5


async def test_auth_error_is_unavailable_not_crash(server):
    p = provider(server, with_fallback=False)
    server.always["mock-strong"] = {"status": 401}
    with pytest.raises(LLMUnavailable):
        await p.complete("answer", CONVO)


# ---- Anthropic wire format ------------------------------------------------------------

async def test_anthropic_roundtrip_request_shape(server):
    p = provider(server, provider="anthropic", strong="mock-claude-strong", small="mock-claude-small")
    r = await p.complete("answer", CONVO)
    assert r.content.startswith("OK: What about my career?") and r.model.endswith("mock-claude-strong")
    req = server.requests[-1]
    assert req["path"] == "/v1/messages" and req["headers"]["x-api-key"] == "test-key" and "anthropic-version" in req["headers"]
    body = req["body"]
    assert "You are a test." in json.dumps(body["system"]) and body["messages"][0]["role"] == "user" and body["max_tokens"] == 1024


async def test_anthropic_tool_use_blocks(server):
    p = provider(server, provider="anthropic", strong="mock-claude-strong", small="mock-claude-small")
    server.script("mock-claude-strong", {"tool_calls": [{"name": "recall_memories", "args": {"life_area": "career", "query": "goals"}}]})
    r = await p.complete("answer", CONVO, tools=TOOLS)
    assert r.tool_calls[0].name == "recall_memories" and r.tool_calls[0].args == {"life_area": "career", "query": "goals"}
    body = server.requests[-1]["body"]
    assert body["tools"][0]["name"] == "recall_memories" and body["tools"][0]["input_schema"]["required"] == ["life_area", "query"]
    follow = [*CONVO, LLMMessage("assistant", tool_calls=r.tool_calls), LLMMessage("tool", "- [career] Goal: switch jobs", tool_call_id=r.tool_calls[0].id, name="recall_memories")]
    r2 = await p.complete("answer", follow, tools=TOOLS)
    assert r2.content.startswith("OK:")
    sent = server.requests[-1]["body"]["messages"]
    assert sent[-1]["role"] == "user" and sent[-1]["content"][0]["type"] == "tool_result"
    assert sent[-2]["role"] == "assistant" and any(b.get("type") == "tool_use" for b in sent[-2]["content"])


async def test_anthropic_structured_and_fallback(server):
    p = provider(server, provider="anthropic", strong="mock-claude-strong", small="mock-claude-small")
    server.script("mock-claude-small", {"content": '{"action": "NOOP"}'})
    assert (await p.structured("reconcile", CONVO, Decision)).action == "NOOP"
    server.always["mock-claude-strong"] = {"status": 529}
    r = await p.complete("answer", CONVO)
    assert r.model.endswith("mock-fallback")


async def test_healthcheck_reports_models_without_calling(server, monkeypatch):
    p = provider(server)
    h = await p.healthcheck()
    assert h["backend"] == "litellm" and h["models"]["strong"] == "openai/mock-strong" and "provider_keys_present" in h
    assert h["ok"] is True and server.requests == []  # api_base/api_key configured on the deployments
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    bare = LiteLLMProvider(make_settings(llm_backend="litellm"))
    assert (await bare.healthcheck())["ok"] is False  # default models, no keys anywhere → degraded


# ---- full stack: HTTP API → response graph → LiteLLM Router → mock provider -------------

def test_full_stack_chat_through_router(server):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import FIXED_NOW

    p = provider(server)
    app = create_app(make_settings(llm_backend="litellm"), llm=p, clock=lambda: FIXED_NOW)
    # analyze (small) → JSON; answer (strong) → text; extract (small) → candidates; reconcile unused (first write)
    server.script("mock-small", {"content": '{"route": "answer", "life_areas": ["career"], "language": "en"}'})
    server.script("mock-strong", {"content": "Leo energy favours a bold career move in 2027."})
    server.script("mock-small", {"content": json.dumps({"candidates": [{"type": "goal", "key": "career_change", "value": "switch jobs", "attributes": {"target_year": 2027}, "life_area": "career", "certainty": "explicit", "subject": "self", "temporality": "future", "user_asked_to_remember": False, "valid_until": None, "sensitive_category": "none"}]})})
    with TestClient(app) as client:
        r = client.post("/chat", json={"user_id": "u", "session_id": "s", "message": "Planning to switch jobs next year."})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["route"] == "answer" and d["response"].startswith("Leo energy") and d["degraded"] == []
        mems = client.get("/users/u/memories").json()
        assert mems[0]["key"] == "career_change" and mems[0]["attributes"] == {"target_year": 2027}
        models = [req["body"]["model"] for req in server.requests]
        assert models == ["mock-small", "mock-strong", "mock-small"]  # analyze → answer → extract
        # provider outage mid-conversation → canned reply, degraded flag, HTTP 200
        server.always["mock-strong"] = {"status": 500}
        server.always["mock-fallback"] = {"status": 500}
        server.always["mock-small"] = {"status": 500}
        d = client.post("/chat", json={"user_id": "u", "session_id": "s", "message": "What should I focus on for my career?"}).json()
        assert d["degraded"] == ["llm_unavailable"] and "trouble reaching" in d["response"]
        assert client.get("/health").json()["llm"]["backend"] == "litellm"
