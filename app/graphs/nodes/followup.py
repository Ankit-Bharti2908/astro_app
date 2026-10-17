"""followup: short-term context only — no Neo4j calls, no memory write (§4)."""

from __future__ import annotations

from app.context.builder import build_context
from app.graphs.deps import Deps
from app.graphs.nodes.common import final_reply, parse_now, profile_from_state
from app.graphs.state import ChatState
from app.llm.provider import LLMMessage
from app.prompts.templates import SYSTEM_FOLLOWUP


def make_followup(deps: Deps):
    async def followup(state: ChatState) -> dict:
        ctx = build_context(
            settings=deps.settings,
            now=parse_now(state, deps.clock),
            tz=state.get("timezone") or deps.settings.default_timezone,
            language=state.get("language", "en"),
            profile=profile_from_state(state),
            memory_index=state.get("memory_index", {}),
            memories=None,
            summary=state.get("summary"),
            messages=state.get("messages", []),
            extra_system=SYSTEM_FOLLOWUP,
        )
        response = await deps.llm.complete("followup", [LLMMessage("system", ctx.system_prompt), *ctx.messages])
        text = response.content.strip() or "Let me put it another way: the guidance follows from what we discussed just now."
        return final_reply(state, text, context_used=ctx.context_used, context_tokens=ctx.context_tokens)

    return followup
