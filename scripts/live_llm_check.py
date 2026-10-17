"""Exercise every LLM task (analyze, extract, reconcile, summarize, answer+tools, memory_recall, followup,
smalltalk) once against the configured real provider and print a report.

    python -m scripts.live_llm_check          # uses .env / environment (needs provider API keys)
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, date, datetime

from app.brain.memory_repo import InMemoryBrainRepository
from app.brain.models import Memory, UserProfile
from app.config import Settings
from app.context.builder import build_context
from app.graphs.nodes.analyze import AnalyzeResult
from app.graphs.nodes.memory_ops import (
    extract_candidates,
    reconcile_candidate,
    score_candidates,
)
from app.llm.provider import LLMMessage, LLMProvider, ToolSpec
from app.prompts.templates import (
    ANALYZE_SYSTEM,
    ANALYZE_USER,
    SUMMARIZE_SYSTEM,
    SUMMARIZE_USER,
    SYSTEM_RECALL,
)

NOW = datetime.now(UTC)
TOOLS = [ToolSpec("recall_memories", "Fetch the user's memories for a life area", {"type": "object", "properties": {"life_area": {"type": "string"}, "query": {"type": "string"}}, "required": ["life_area", "query"]})]


async def _timed(name, coro, check):
    t = time.perf_counter()
    try:
        out = await coro
        ok, detail = check(out)
    except Exception as exc:  # noqa: BLE001
        ok, detail, out = False, f"{type(exc).__name__}: {exc}", None
    return name, {"ok": ok, "ms": round((time.perf_counter() - t) * 1000), "detail": detail}


async def run_all(llm: LLMProvider) -> dict[str, dict]:
    settings = Settings()
    repo = InMemoryBrainRepository()
    profile = UserProfile(user_id="live", name="Rahul", dob=date(1995, 8, 15), birth_place="Delhi", zodiac="Leo")
    goal = Memory(id="g1", type="goal", key="career_change", value="switch jobs", attributes={"target_year": NOW.year + 1}, life_area="career", importance=0.9, created_at=NOW, updated_at=NOW)
    await repo.add_memory("live", goal)

    results = dict(
        await asyncio.gather(
            _timed(
                "analyze (small, structured)",
                llm.structured("analyze", [LLMMessage("system", ANALYZE_SYSTEM), LLMMessage("user", ANALYZE_USER.format(previous_reply="", message="Meri shaadi kab hogi?"))], AnalyzeResult),
                lambda r: (r.language == "hinglish" and "relationships" in r.life_areas, r.model_dump()),
            ),
            _timed(
                "extract (small, structured, relative dates)",
                extract_candidates(llm, message="My name is Rahul, born 15 Aug 1995 in Delhi. Planning to switch jobs next year.", now=NOW, tz=settings.default_timezone),
                lambda cs: (any(c.key == "career_change" and c.attributes.get("target_year") == NOW.year + 1 for c in cs) and any(c.key == "name" for c in cs), [c.model_dump(mode="json") for c in cs]),
            ),
            _timed(
                "reconcile (small, structured, multi-valued)",
                reconcile_candidate(llm, repo, user_id="live", candidate=score_candidates([__import__("app.brain.models", fromlist=["MemoryCandidate"]).MemoryCandidate(type="goal", key="career_change", value="switch jobs", attributes={"target_year": NOW.year}, life_area="career")])[0]),
                lambda d: (d["action"] in {"SUPERSEDE", "UPDATE", "ADD"}, d),
            ),
            _timed(
                "summarize (small)",
                llm.complete("summarize", [LLMMessage("system", SUMMARIZE_SYSTEM), LLMMessage("user", SUMMARIZE_USER.format(previous="(none)", turns="user: I want to switch jobs next year\nassistant: Focus on upskilling; Jupiter favours growth in 2027."))]),
                lambda r: (len(r.content) > 20, r.content[:200]),
            ),
        )
    )

    ctx = build_context(settings=settings, now=NOW, tz=settings.default_timezone, language="en", profile=profile, memory_index={"career": {"goal": 1}}, memories=[goal], summary=None, messages=[__import__("langchain_core.messages", fromlist=["HumanMessage"]).HumanMessage(content="What should I focus on for my career?")])
    results.update(
        dict(
            await asyncio.gather(
                _timed("answer (strong, tools offered)", llm.complete("answer", [LLMMessage("system", ctx.system_prompt), *ctx.messages], tools=TOOLS), lambda r: (bool(r.content or r.tool_calls), {"content": r.content[:200], "tool_calls": [tc.name for tc in r.tool_calls], "model": r.model, "usage": r.usage})),
                _timed("memory_recall (strong)", llm.complete("memory_recall", [LLMMessage("system", ctx.system_prompt + "\n\n" + SYSTEM_RECALL), LLMMessage("user", "What do you remember about my career goals?")]), lambda r: ("switch" in r.content.lower() or "job" in r.content.lower(), r.content[:200])),
                _timed("followup (strong)", llm.complete("followup", [LLMMessage("system", ctx.system_prompt), LLMMessage("user", "What should I focus on for my career?"), LLMMessage("assistant", "Focus on upskilling and networking this year."), LLMMessage("user", "Why do you say that?")]), lambda r: (len(r.content) > 10, r.content[:200])),
                _timed("smalltalk (small)", llm.complete("smalltalk", [LLMMessage("system", ctx.system_prompt), LLMMessage("user", "Namaste!")]), lambda r: (len(r.content) > 0, r.content[:120])),
            )
        )
    )
    return results


def main() -> int:
    from app.llm.litellm_provider import LiteLLMProvider

    settings = Settings()
    llm = LiteLLMProvider(settings)
    print(f"strong={settings.strong_model} small={settings.small_model} fallback={settings.fallback_model or '-'}")
    results = asyncio.run(run_all(llm))
    for name, r in results.items():
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {name:45s} {r['ms']:6d} ms  {json.dumps(r['detail'], default=str)[:160]}")
    return 0 if all(r["ok"] for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
