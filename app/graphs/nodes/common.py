"""Helpers shared by response-graph nodes."""

from __future__ import annotations

from datetime import UTC, datetime

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, RemoveMessage, ToolMessage

from app.brain.models import UserProfile
from app.context.builder import _text, current_turn_start
from app.graphs.state import ChatState
from app.prompts.templates import CANNED_LLM_UNAVAILABLE, pick


def parse_now(state: ChatState, clock) -> datetime:
    raw = state.get("now")
    if raw:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return clock()


def profile_from_state(state: ChatState) -> UserProfile | None:
    raw = state.get("profile")
    return UserProfile.model_validate(raw) if raw else None


def last_user_text(state: ChatState) -> str:
    for m in reversed(state.get("messages", [])):
        if isinstance(m, HumanMessage):
            return _text(m.content)
    return ""


def previous_assistant_text(state: ChatState) -> str:
    msgs = state.get("messages", [])
    start = current_turn_start(msgs)
    for m in reversed(msgs[:start]):
        if isinstance(m, AIMessage) and not m.tool_calls:
            return _text(m.content)
    return ""


def intra_turn_cleanup(messages: list[AnyMessage]) -> list[RemoveMessage]:
    """RemoveMessage for this turn's tool-call/tool-result traffic (keeps history compact)."""
    start = current_turn_start(messages)
    removals = []
    for m in messages[start + 1 :]:
        if (isinstance(m, AIMessage) and m.tool_calls) or isinstance(m, ToolMessage):
            if m.id:
                removals.append(RemoveMessage(id=m.id))
    return removals


def current_turn_tool_artifacts(messages: list[AnyMessage]) -> tuple[list[str], list[str], list[str]]:
    """(context_used, memory_ids, degraded) collected from this turn's ToolMessages."""
    start = current_turn_start(messages)
    used: list[str] = []
    ids: list[str] = []
    degraded: list[str] = []
    for m in messages[start:]:
        if isinstance(m, ToolMessage):
            art = getattr(m, "artifact", None) or {}
            used += list(art.get("context_used", []))
            ids += list(art.get("memory_ids", []))
            degraded += list(art.get("degraded", []))
            if m.status == "error":
                used.append(f"tool_error:{m.name}")
    return used, ids, degraded


def merge_unique(*lists: list[str]) -> list[str]:
    out: list[str] = []
    for lst in lists:
        for x in lst:
            if x not in out:
                out.append(x)
    return out


def final_reply(state: ChatState, text: str, *, context_used, context_tokens, memory_ids=None, degraded=None) -> dict:
    cleanup = intra_turn_cleanup(state.get("messages", []))
    return {
        "messages": [*cleanup, AIMessage(content=text)],
        "response": text,
        "context_used": context_used,
        "context_tokens": context_tokens,
        "memory_ids_used": memory_ids or [],
        "degraded": merge_unique(state.get("degraded", []), degraded or []),
    }


def llm_failure_handler(state: ChatState) -> dict:
    """error_handler for LLM nodes: canned reply, degraded flag, never a 500 (§11)."""
    language = state.get("language", "en")
    text = pick(CANNED_LLM_UNAVAILABLE, language)
    return final_reply(
        state,
        text,
        context_used=state.get("context_used", []),
        context_tokens=state.get("context_tokens", {}),
        memory_ids=[],
        degraded=["llm_unavailable"],
    )
