"""memory_cmd: explicit remember/forget executed synchronously in the turn."""

from __future__ import annotations

import json
import logging
from typing import Literal

from pydantic import BaseModel, Field

from app.brain.errors import BrainUnavailable
from app.brain.models import Memory
from app.graphs.deps import Deps
from app.graphs.nodes.common import final_reply, last_user_text, parse_now, previous_assistant_text
from app.graphs.nodes.memory_ops import (
    apply_decision,
    extract_candidates,
    filter_candidates,
    reconcile_candidate,
    score_candidates,
)
from app.graphs.state import ChatState
from app.llm.provider import LLMError, LLMMessage, LLMProvider
from app.prompts.templates import (
    MEMORY_BRAIN_DOWN,
    MEMORY_CMD_SYSTEM,
    MEMORY_CMD_USER,
    MEMORY_EPHEMERAL,
    MEMORY_FORGET_WHAT,
    MEMORY_FORGOTTEN,
    MEMORY_NOTHING_TO_FORGET,
    MEMORY_NOTHING_TO_SAVE,
    MEMORY_SAVED,
    pick,
)

log = logging.getLogger(__name__)


class MemoryCommand(BaseModel):
    """What the user asked to do to their stored memory, and to which memories."""

    command: Literal["remember", "forget"] = "remember"
    names_a_target: bool = False
    memory_ids: list[str] = Field(default_factory=list)
    forget_all: bool = False
    reason: str | None = None


def _memories_json(memories: list[Memory]) -> str:
    return json.dumps(
        [{"id": m.id, "key": m.key, "value": m.value, "life_area": m.life_area} for m in memories],
        default=str,
    )


async def read_command(
    llm: LLMProvider, *, message: str, memories: list[Memory], context: str
) -> MemoryCommand:
    """Ask the model which listed memories the instruction means. Matching is by meaning, not shared words."""
    return await llm.structured(
        "memory_cmd_select",
        [
            LLMMessage("system", MEMORY_CMD_SYSTEM),
            LLMMessage(
                "user",
                MEMORY_CMD_USER.format(memories=_memories_json(memories), message=message)
                + (f"\n\n<previous_assistant_reply>\n{context[:300]}\n</previous_assistant_reply>" if context else ""),
            ),
        ],
        MemoryCommand,
    )


def resolve_targets(plan: MemoryCommand, memories: list[Memory]) -> list[Memory]:
    """The memories a forget may touch: only ones we put in front of the model, never more."""
    if plan.forget_all:
        return list(memories)
    by_id = {m.id: m for m in memories}
    return [by_id[i] for i in dict.fromkeys(plan.memory_ids) if i in by_id]


def make_memory_cmd(deps: Deps):
    async def memory_cmd(state: ChatState) -> dict:
        message = last_user_text(state).strip()
        language = state.get("language", "en")
        now = parse_now(state, deps.clock)
        user_id = state["user_id"]
        ephemeral = bool(state.get("ephemeral"))

        if "brain_unavailable" in state.get("degraded", []):
            return final_reply(state, pick(MEMORY_BRAIN_DOWN, language), context_used=[], context_tokens={})

        try:
            memories = await deps.repo.list_memories(user_id)
        except BrainUnavailable:
            return final_reply(state, pick(MEMORY_BRAIN_DOWN, language), context_used=[], context_tokens={}, degraded=["brain_unavailable"])

        try:
            plan = await read_command(deps.llm, message=message, memories=memories, context=previous_assistant_text(state))
        except LLMError as exc:  # no verdict → do nothing, least of all delete
            log.warning("memory_cmd selection failed: %s", exc)
            out = final_reply(state, pick(MEMORY_FORGET_WHAT, language), context_used=[], context_tokens={}, degraded=["llm_unavailable"])
            out["memory_cmd_result"] = {"command": "unknown", "deleted": [], "keys": [], "reason": "selection_failed"}
            return out

        if ephemeral:  # a temporary chat may read the brain and change nothing
            out = final_reply(state, pick(MEMORY_EPHEMERAL, language), context_used=[], context_tokens={})
            out["memory_cmd_result"] = {"command": plan.command, "stored": [], "deleted": [], "reason": "ephemeral"}
            return out

        if plan.command == "forget":
            if not plan.names_a_target and not plan.forget_all:  # "forget that" — never guess
                out = final_reply(state, pick(MEMORY_FORGET_WHAT, language), context_used=[], context_tokens={})
                out["memory_cmd_result"] = {"command": "forget", "deleted": [], "keys": [], "reason": "no_target"}
                return out
            hits = resolve_targets(plan, memories)
            try:
                for m in hits:
                    await deps.repo.mark_deleted(user_id, m.id, now=now)
            except BrainUnavailable:
                return final_reply(state, pick(MEMORY_BRAIN_DOWN, language), context_used=[], context_tokens={}, degraded=["brain_unavailable"])
            text = pick(MEMORY_FORGOTTEN, language, items="; ".join(m.value for m in hits)) if hits else pick(MEMORY_NOTHING_TO_FORGET, language)
            out = final_reply(state, text, context_used=[f"memory:{m.key}" for m in hits], context_tokens={})
            out["memory_cmd_result"] = {"command": "forget", "deleted": [m.id for m in hits], "keys": [m.key for m in hits], "reason": plan.reason}
            return out

        # remember
        try:
            cands = await extract_candidates(
                deps.llm, message=message, now=now,
                tz=state.get("timezone") or deps.settings.default_timezone,
                user_asked=True, context=previous_assistant_text(state),
            )
        except LLMError as exc:
            log.warning("memory_cmd extraction failed: %s", exc)
            cands = []
            degraded = ["llm_unavailable"]
        else:
            degraded = []
        accepted, dropped = filter_candidates(score_candidates(cands), deps.settings)
        stored = []
        try:
            for c in accepted:
                decision = await reconcile_candidate(deps.llm, deps.repo, user_id=user_id, candidate=c)
                stored.append(await apply_decision(deps.repo, user_id=user_id, session_id=state.get("session_id"), candidate=c, decision=decision, now=now))
        except BrainUnavailable:
            return final_reply(state, pick(MEMORY_BRAIN_DOWN, language), context_used=[], context_tokens={}, degraded=["brain_unavailable"])
        effective = [s for s in stored if s["action"] != "SKIP"]
        text = pick(MEMORY_SAVED, language, items="; ".join(s["value"] for s in effective)) if effective else pick(MEMORY_NOTHING_TO_SAVE, language)
        out = final_reply(state, text, context_used=[f"memory:{s['key']}" for s in effective], context_tokens={}, degraded=degraded)
        out["memory_cmd_result"] = {"command": "remember", "stored": stored, "dropped": [d.drop_reason for d in dropped]}
        return out

    return memory_cmd
