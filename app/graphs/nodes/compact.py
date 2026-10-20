"""maybe_compact: incremental summarization of older turns + session persistence."""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from app.brain.errors import BrainUnavailable
from app.context.builder import _text
from app.context.tokens import count_tokens
from app.graphs.deps import Deps
from app.graphs.nodes.common import parse_now
from app.graphs.state import ChatState
from app.llm.provider import LLMError, LLMMessage
from app.prompts.templates import SUMMARIZE_SYSTEM, SUMMARIZE_USER

log = logging.getLogger(__name__)


def make_maybe_compact(deps: Deps):
    async def maybe_compact(state: ChatState) -> dict:
        messages = [m for m in state.get("messages", []) if isinstance(m, (HumanMessage, AIMessage)) and not getattr(m, "tool_calls", None)]
        total = sum(count_tokens(_text(m.content)) + 4 for m in messages)
        summary = state.get("summary") or ""
        update: dict = {}
        new_summary: str | None = None

        if total > deps.settings.compact_threshold_tokens and len(messages) > deps.settings.keep_last_messages:
            dropped = messages[: -deps.settings.keep_last_messages]
            turns = "\n".join(f"{'user' if isinstance(m, HumanMessage) else 'assistant'}: {_text(m.content)}" for m in dropped)
            try:
                resp = await deps.llm.complete(
                    "summarize",
                    [LLMMessage("system", SUMMARIZE_SYSTEM), LLMMessage("user", SUMMARIZE_USER.format(previous=summary or "(none)", turns=turns))],
                )
                new_summary = resp.content.strip() or summary
                update["summary"] = new_summary
                update["messages"] = [RemoveMessage(id=m.id) for m in dropped if m.id]
                log.info("compacted %d messages (%d tokens) into summary", len(dropped), total)
            except LLMError as exc:
                log.warning("compaction skipped, summarizer unavailable: %s", exc)

        if not state.get("ephemeral") and "brain_unavailable" not in state.get("degraded", []):
            try:
                await deps.repo.upsert_session(
                    state["user_id"], state["session_id"], now=parse_now(state, deps.clock),
                    summary=new_summary, turn_increment=1,
                )
            except BrainUnavailable as exc:
                log.warning("session persistence skipped: %s", exc)
        return update

    return maybe_compact
