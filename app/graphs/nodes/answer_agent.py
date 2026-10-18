"""answer_agent: deterministic L3 prefetch + strong model + bounded read-only tool loop (§4)."""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage

from app.brain.errors import BrainUnavailable
from app.brain.models import Memory, recall_areas
from app.brain.scoring import rank_memories
from app.context.builder import build_context
from app.graphs.deps import Deps
from app.graphs.nodes.common import (
    current_turn_tool_artifacts,
    current_turn_tool_trace,
    final_reply,
    merge_unique,
    parse_now,
    profile_from_state,
)
from app.graphs.state import ChatState
from app.prompts.templates import SYSTEM_TOOLS_HINT
from app.tools.brain_tools import tool_specs

log = logging.getLogger(__name__)


def make_answer_agent(deps: Deps):
    specs = tool_specs(deps.tools)

    async def answer_agent(state: ChatState) -> dict:
        now = parse_now(state, deps.clock)
        brain_down = "brain_unavailable" in state.get("degraded", [])
        rounds = state.get("tool_rounds", 0)
        degraded: list[str] = []

        # 1. deterministic prefetch (first round only; reused afterwards)
        if rounds == 0:
            prefetched: list[Memory] = []
            if not brain_down:
                try:
                    areas = state.get("life_areas") or None
                    all_mems = await deps.repo.list_memories(state["user_id"], life_areas=recall_areas(areas))
                    prefetched = rank_memories(all_mems, now, top_k=deps.settings.top_k_memories)
                except BrainUnavailable as exc:
                    log.warning("prefetch failed: %s", exc)
                    degraded.append("brain_unavailable")
                    brain_down = True
            prefetched_raw = [m.model_dump(mode="json") for m in prefetched]
        else:
            prefetched_raw = state.get("prefetched_memories", [])
            prefetched = [Memory.model_validate(m) for m in prefetched_raw]

        tool_used, tool_ids, tool_degraded = current_turn_tool_artifacts(state.get("messages", []))
        tool_trace = current_turn_tool_trace(state.get("messages", []))
        degraded += tool_degraded

        # 2. bounded context
        allow_tools = rounds < deps.settings.max_tool_rounds and not brain_down and bool(specs)
        ctx = build_context(
            settings=deps.settings,
            now=now,
            tz=state.get("timezone") or deps.settings.default_timezone,
            language=state.get("language", "en"),
            profile=profile_from_state(state),
            memory_index=state.get("memory_index", {}),
            memories=prefetched,
            summary=state.get("summary"),
            messages=state.get("messages", []),
            extra_system=SYSTEM_TOOLS_HINT if allow_tools else "",
        )
        context_used = merge_unique(ctx.context_used, tool_used)
        memory_ids = merge_unique(ctx.memory_ids, tool_ids)

        # 3. strong model (LLMUnavailable propagates → RetryPolicy → error_handler)
        response = await deps.llm.complete(
            "answer",
            [*_system(ctx.system_prompt), *ctx.messages],
            tools=specs if allow_tools else None,
            max_tokens=deps.settings.llm_max_tokens_answer,
        )

        if response.tool_calls and allow_tools:
            ai = AIMessage(
                content=response.content or "",
                tool_calls=[{"id": tc.id, "name": tc.name, "args": tc.args, "type": "tool_call"} for tc in response.tool_calls],
            )
            return {
                "messages": [ai],
                "tool_rounds": rounds + 1,
                "prefetched_memories": prefetched_raw,
                "context_used": context_used,
                "context_tokens": ctx.context_tokens,
                "memory_ids_used": memory_ids,
                "tool_trace": tool_trace,
                "degraded": merge_unique(state.get("degraded", []), degraded),
            }

        text = response.content.strip() or "I'm here — could you tell me a little more about what you'd like guidance on?"
        if memory_ids and not brain_down and not state.get("ephemeral"):  # a temporary chat mutates nothing
            try:
                await deps.repo.touch_used(state["user_id"], memory_ids, now=now)  # §7.4 reinforcement
            except BrainUnavailable:
                degraded.append("brain_unavailable")
        return final_reply(
            state, text, context_used=context_used, context_tokens=ctx.context_tokens,
            memory_ids=memory_ids, tool_trace=tool_trace, degraded=degraded,
        )

    return answer_agent


def _system(prompt: str):
    from app.llm.provider import LLMMessage

    return [LLMMessage("system", prompt)]


def route_after_answer(state: ChatState) -> str:
    msgs = state.get("messages", [])
    if msgs and isinstance(msgs[-1], AIMessage) and msgs[-1].tool_calls:
        return "fetch_context"
    return "maybe_compact"
