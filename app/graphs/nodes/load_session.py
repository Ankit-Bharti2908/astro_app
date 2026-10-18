"""load_session: L0 meta, L1 profile, L2 index; resets per-turn state (§4)."""

from __future__ import annotations

import logging

from app.brain.errors import BrainUnavailable
from app.graphs.deps import Deps
from app.graphs.state import ChatState

log = logging.getLogger(__name__)


def make_load_session(deps: Deps):
    async def load_session(state: ChatState) -> dict:
        degraded: list[str] = []
        profile = None
        index: dict = {}
        try:
            profile = await deps.repo.get_user(state["user_id"])
            index = await deps.repo.memory_index(state["user_id"])
        except BrainUnavailable as exc:
            log.warning("brain unavailable in load_session: %s", exc)
            degraded.append("brain_unavailable")
        return {
            "profile": profile.model_dump(mode="json") if profile else None,
            "memory_index": index,
            "refused_details": {},
            "written_this_turn": [],
            "degraded": degraded,
            "timezone": state.get("timezone") or deps.settings.default_timezone,
            "route": "",
            "life_areas": [],
            "language": "en",
            "explicit_language": None,
            "prefetched_memories": [],
            "context_used": [],
            "context_tokens": {},
            "memory_ids_used": [],
            "tool_trace": [],
            "tool_rounds": 0,
            "response": "",
            "memory_cmd_result": {},
        }

    return load_session
