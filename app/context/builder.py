"""Assembles the bounded prompt layers (§3) and converts LangChain messages for the provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

from app.brain.models import Memory, UserProfile
from app.config import Settings
from app.context.tokens import count_tokens, truncate_to_budget
from app.llm.provider import LLMMessage, ToolCall
from app.prompts.templates import SYSTEM_BASE

LANGUAGE_NAMES = {"en": "English", "hi": "Hindi (Devanagari)", "hinglish": "Hinglish (romanised Hindi mixed with English)"}


@dataclass
class ContextBundle:
    system_prompt: str
    messages: list[LLMMessage]
    context_used: list[str] = field(default_factory=list)
    context_tokens: dict[str, int] = field(default_factory=dict)
    memory_ids: list[str] = field(default_factory=list)


# ---- layers -----------------------------------------------------------------

def meta_layer(now: datetime, tz: str, language: str) -> str:  # L0
    try:
        local = now.astimezone(ZoneInfo(tz))
    except Exception:  # unknown tz → keep UTC
        local = now
    return (
        f"Current date: {local.date().isoformat()} ({local.strftime('%A')}). "
        f"Timezone: {tz}. Reply language: {language} ({LANGUAGE_NAMES.get(language, language)})."
    )


def profile_layer(profile: UserProfile | None, budget: int) -> str:  # L1
    if profile is None:
        return "## User profile\nUnknown user: no birth details on file."
    rows = [
        f"Name: {profile.name or 'unknown'}",
        f"DOB: {profile.dob.isoformat() if profile.dob else 'unknown'}",
        f"Time of birth: {profile.tob or 'unknown'}",
        f"Birth place: {profile.birth_place or 'unknown'}",
        f"Sun sign: {profile.zodiac or 'unknown'}",
        f"Preferred language: {profile.preferred_language or 'unknown'}",
    ]
    return truncate_to_budget("## User profile\n" + " | ".join(rows), budget)


def index_layer(index: dict[str, dict[str, int]], budget: int) -> str:  # L2
    if not index:
        return "## Memory index\n(no long-term memories yet)"
    parts = []
    for area in sorted(index):
        counts = index[area]
        inner = ", ".join(f"{n} {t.replace('_', ' ')}{'s' if n != 1 else ''}" for t, n in sorted(counts.items()))
        parts.append(f"{area}: {inner}")
    return truncate_to_budget("## Memory index (what I know exists)\n" + " · ".join(parts), budget)


def memories_layer(memories: list[Memory], budget: int) -> tuple[str, list[Memory]]:  # L3
    if not memories:
        return "", []
    header = "## Known memories (ranked)\n"
    lines: list[str] = []
    included: list[Memory] = []
    used = count_tokens(header)
    for m in memories:
        line = m.as_line()
        cost = count_tokens(line) + 1
        if used + cost > budget:
            break
        lines.append(line)
        included.append(m)
        used += cost
    if not lines:
        return "", []
    return header + "\n".join(lines), included


def summary_layer(summary: str | None, budget: int) -> str:
    if not summary:
        return ""
    return "## Earlier in this conversation\n" + truncate_to_budget(summary, budget)


def to_llm_message(m: AnyMessage) -> LLMMessage | None:
    if isinstance(m, HumanMessage):
        return LLMMessage("user", _text(m.content))
    if isinstance(m, AIMessage):
        calls = [ToolCall(id=tc["id"] or "", name=tc["name"], args=dict(tc.get("args") or {})) for tc in (m.tool_calls or [])]
        return LLMMessage("assistant", _text(m.content), tool_calls=calls)
    if isinstance(m, ToolMessage):
        return LLMMessage("tool", _text(m.content), tool_call_id=m.tool_call_id, name=m.name)
    return None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            (c.get("text", "") if isinstance(c, dict) else str(c)) for c in content
        ).strip()
    return str(content)


def current_turn_start(messages: list[AnyMessage]) -> int:
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return i
    return max(0, len(messages) - 1)


def turns_layer(messages: list[AnyMessage], budget: int) -> tuple[list[LLMMessage], int]:
    """Current turn always; then earlier messages newest-first while within budget."""
    start = current_turn_start(messages)
    current = [x for x in (to_llm_message(m) for m in messages[start:]) if x]
    used = sum(count_tokens(m.content) + 4 for m in current)
    earlier: list[LLMMessage] = []
    for m in reversed(messages[:start]):
        llm_m = to_llm_message(m)
        if llm_m is None or llm_m.role == "tool" or llm_m.tool_calls:
            continue  # intra-turn tool traffic from past turns is not replayed
        cost = count_tokens(llm_m.content) + 4
        if used + cost > budget:
            break
        earlier.append(llm_m)
        used += cost
    earlier.reverse()
    return earlier + current, used


# ---- assembly -----------------------------------------------------------------

def build_context(
    *,
    settings: Settings,
    now: datetime,
    tz: str,
    language: str,
    profile: UserProfile | None,
    memory_index: dict[str, dict[str, int]],
    memories: list[Memory] | None,
    summary: str | None,
    messages: list[AnyMessage],
    extra_system: str = "",
    include_index: bool = True,
) -> ContextBundle:
    meta = meta_layer(now, tz, language)
    l1 = profile_layer(profile, settings.budget_profile)
    l2 = index_layer(memory_index, settings.budget_index) if include_index else ""
    l3, included = memories_layer(memories or [], settings.budget_memories)
    st_summary = summary_layer(summary, settings.budget_summary)
    turns, turns_tokens = turns_layer(messages, settings.budget_turns)

    # stable layers first (prefix caching), volatile L0 last
    blocks = [SYSTEM_BASE]
    if extra_system:
        blocks.append(extra_system)
    blocks.append(l1)
    if l2:
        blocks.append(l2)
    if l3:
        blocks.append(l3)
    if st_summary:
        blocks.append(st_summary)
    blocks.append(meta)
    system_prompt = "\n\n".join(blocks)

    context_used: list[str] = []
    if profile is not None:
        context_used.append("user_profile")
    if l2 and memory_index:
        context_used.append("memory_index")
    context_used += [f"memory:{m.key}" for m in included]
    if st_summary:
        context_used.append("session_summary")
    if len(turns) > 1:
        context_used.append("recent_turns")

    tokens = {
        "meta": count_tokens(meta),
        "profile": count_tokens(l1),
        "index": count_tokens(l2),
        "memories": count_tokens(l3),
        "summary": count_tokens(st_summary),
        "turns": turns_tokens,
    }
    return ContextBundle(system_prompt, turns, context_used, tokens, [m.id for m in included])
