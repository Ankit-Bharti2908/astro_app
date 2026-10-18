"""memory_cmd: explicit remember/forget executed synchronously in the turn (§4)."""

from __future__ import annotations

import logging
import re

from app.brain.errors import BrainUnavailable
from app.brain.repository import search_terms
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
from app.llm.provider import LLMError
from app.prompts.templates import (
    MEMORY_BRAIN_DOWN,
    MEMORY_EPHEMERAL,
    MEMORY_FORGET_WHAT,
    MEMORY_FORGOTTEN,
    MEMORY_NOTHING_TO_FORGET,
    MEMORY_NOTHING_TO_SAVE,
    MEMORY_SAVED,
    pick,
)

log = logging.getLogger(__name__)

_FORGET = re.compile(r"^(please\s+)?(forget|delete|remove|erase|don'?t remember|do not remember|bhool ja\w*)\s*(that|about|the|ki)?\s*", re.I)
_REMEMBER = re.compile(r"^(please\s+)?(remember|note|save|store|don'?t forget|do not forget|yaad rakh\w*)\s*(that|ki)?\s*", re.I)
_WIPE_ALL = {"everything", "all", "all memories", "everything about me", "all my memories", "sab kuch", "sab"}


def make_memory_cmd(deps: Deps):
    async def memory_cmd(state: ChatState) -> dict:
        message = last_user_text(state).strip()
        language = state.get("language", "en")
        now = parse_now(state, deps.clock)
        user_id = state["user_id"]
        brain_down = "brain_unavailable" in state.get("degraded", [])

        if brain_down:
            return final_reply(state, pick(MEMORY_BRAIN_DOWN, language), context_used=[], context_tokens={})

        is_forget = bool(_FORGET.match(message))
        if state.get("ephemeral"):  # §12: ephemeral turns never write — neither remember nor forget
            out = final_reply(state, pick(MEMORY_EPHEMERAL, language), context_used=[], context_tokens={})
            out["memory_cmd_result"] = {"command": "forget" if is_forget else "remember", "stored": [], "deleted": [], "reason": "ephemeral"}
            return out

        if is_forget:
            target = _FORGET.sub("", message).strip(" .!?").lower()
            wipe_all = target in _WIPE_ALL
            terms = search_terms(target)
            if not terms and not wipe_all:  # "Forget that." / "Forget it" — never guess, never delete
                out = final_reply(state, pick(MEMORY_FORGET_WHAT, language), context_used=[], context_tokens={})
                out["memory_cmd_result"] = {"command": "forget", "deleted": [], "keys": [], "reason": "no_target"}
                return out
            try:
                memories = await deps.repo.list_memories(user_id)
                hits = memories if wipe_all else [
                    m for m in memories if any(t in f"{m.key} {m.value}".lower().replace("_", " ") for t in terms)
                ]
                for m in hits:
                    await deps.repo.mark_deleted(user_id, m.id, now=now)
            except BrainUnavailable:
                return final_reply(state, pick(MEMORY_BRAIN_DOWN, language), context_used=[], context_tokens={}, degraded=["brain_unavailable"])
            result = {"command": "forget", "deleted": [m.id for m in hits], "keys": [m.key for m in hits]}
            text = pick(MEMORY_FORGOTTEN, language, items="; ".join(m.value for m in hits)) if hits else pick(MEMORY_NOTHING_TO_FORGET, language)
            out = final_reply(state, text, context_used=[f"memory:{m.key}" for m in hits], context_tokens={})
            out["memory_cmd_result"] = result
            return out

        # remember
        body = _REMEMBER.sub("", message).strip() or message
        try:
            cands = await extract_candidates(deps.llm, message=body, now=now, tz=state.get("timezone") or deps.settings.default_timezone, user_asked=True, context=previous_assistant_text(state))
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
