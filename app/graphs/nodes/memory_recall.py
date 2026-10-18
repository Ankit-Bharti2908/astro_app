"""memory_recall: facts come from Neo4j; the LLM only phrases them (§4)."""

from __future__ import annotations

import logging

from app.brain.errors import BrainUnavailable
from app.brain.models import recall_areas
from app.brain.scoring import rank_memories
from app.context.builder import build_context
from app.graphs.deps import Deps
from app.graphs.nodes.common import final_reply, parse_now, profile_from_state
from app.graphs.state import ChatState
from app.llm.provider import LLMMessage
from app.prompts.templates import RECALL_BRAIN_DOWN, RECALL_EMPTY, SYSTEM_RECALL, pick

log = logging.getLogger(__name__)


def make_memory_recall(deps: Deps):
    async def memory_recall(state: ChatState) -> dict:
        now = parse_now(state, deps.clock)
        language = state.get("language", "en")
        areas = state.get("life_areas") or None
        topic = ", ".join(areas) if areas else "you"

        if "brain_unavailable" in state.get("degraded", []):
            return final_reply(state, pick(RECALL_BRAIN_DOWN, language), context_used=[], context_tokens={})
        try:
            memories = rank_memories(
                await deps.repo.list_memories(state["user_id"], life_areas=recall_areas(areas)), now, top_k=deps.settings.top_k_memories * 2
            )
        except BrainUnavailable:
            return final_reply(state, pick(RECALL_BRAIN_DOWN, language), context_used=[], context_tokens={}, degraded=["brain_unavailable"])

        if not memories:
            return final_reply(state, pick(RECALL_EMPTY, language, topic=topic), context_used=["memory_index"] if state.get("memory_index") else [], context_tokens={})

        ctx = build_context(
            settings=deps.settings,
            now=now,
            tz=state.get("timezone") or deps.settings.default_timezone,
            language=language,
            profile=profile_from_state(state),
            memory_index=state.get("memory_index", {}),
            memories=memories,
            summary=state.get("summary"),
            messages=state.get("messages", []),
            extra_system=SYSTEM_RECALL,
        )
        response = await deps.llm.complete("memory_recall", [LLMMessage("system", ctx.system_prompt), *ctx.messages])
        text = response.content.strip() or "\n".join(m.as_line() for m in memories)
        if not state.get("ephemeral"):
            try:
                await deps.repo.touch_used(state["user_id"], ctx.memory_ids, now=now)
            except BrainUnavailable:
                pass
        return final_reply(state, text, context_used=ctx.context_used, context_tokens=ctx.context_tokens, memory_ids=ctx.memory_ids)

    return memory_recall
