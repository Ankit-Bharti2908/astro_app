"""smalltalk: profile card only, no retrieval."""

from __future__ import annotations

from app.context.builder import build_context, current_turn_start
from app.graphs.deps import Deps
from app.graphs.nodes.common import final_reply, parse_now, profile_from_state
from app.graphs.state import ChatState
from app.llm.provider import LLMMessage
from app.prompts.templates import SYSTEM_SMALLTALK


def make_smalltalk(deps: Deps):
    async def smalltalk(state: ChatState) -> dict:
        msgs = state.get("messages", [])
        ctx = build_context(
            settings=deps.settings,
            now=parse_now(state, deps.clock),
            tz=state.get("timezone") or deps.settings.default_timezone,
            language=state.get("language", "en"),
            profile=profile_from_state(state),
            memory_index={},
            memories=None,
            summary=None,
            messages=msgs[current_turn_start(msgs):],
            extra_system=SYSTEM_SMALLTALK,
            include_index=False,
        )
        response = await deps.llm.complete("smalltalk", [LLMMessage("system", ctx.system_prompt), *ctx.messages])
        text = response.content.strip() or "Hello! How can I help you today?"
        return final_reply(state, text, context_used=ctx.context_used, context_tokens=ctx.context_tokens)

    return smalltalk
