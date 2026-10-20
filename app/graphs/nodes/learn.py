"""learn: run the memory graph before the reply, so the reply sees what was stored or refused.

See DECISIONS.md, decision 2, for why this is not a background task.
"""

from __future__ import annotations

import logging

from app.brain.errors import BrainUnavailable
from app.brain.models import PROFILE_KEYS
from app.graphs.deps import Deps
from app.graphs.nodes.common import last_user_text, parse_now, previous_assistant_text
from app.graphs.state import ChatState

log = logging.getLogger(__name__)

# `memory_cmd` does its own writing inline and `smalltalk` carries nothing durable.
LEARN_ROUTES = frozenset({"answer", "followup", "memory_recall"})


def make_learn(deps: Deps):
    async def learn(state: ChatState) -> dict:
        if deps.learn is None or state.get("ephemeral") or state.get("route") not in LEARN_ROUTES:
            return {}  # an ephemeral turn reads the brain and changes nothing

        now = parse_now(state, deps.clock)
        result = (
            await deps.learn(
                {
                    "user_id": state["user_id"],
                    "session_id": state.get("session_id"),
                    "message": last_user_text(state),
                    "now": now.isoformat(),
                    "language": state.get("language", "en"),
                    "ephemeral": False,
                    "context": previous_assistant_text(state),
                }
            )
            or {}
        )

        # A refused profile slot is the reply's business: "unknown" and "you gave me something
        # unusable" are different states, and only the second one has something to say back.
        refused = {
            w["key"]: w["reason"]
            for w in result.get("written", [])
            if w.get("action") == "SKIP"
            and w.get("key") in PROFILE_KEYS
            and w.get("reason")
            and w.get("reason") != "brain_unavailable"
        }
        # Ids written this turn, so the reply can use them without also counting them as
        # "recalled": a memory is not reinforced by the very message that created it.
        out: dict = {
            "refused_details": refused,
            "written_this_turn": [w["memory_id"] for w in result.get("written", []) if w.get("memory_id")],
        }

        try:  # the write may have just changed the profile this reply is about to speak from
            profile = await deps.repo.get_user(state["user_id"])
        except BrainUnavailable:
            return out
        if profile is not None:
            out["profile"] = profile.model_dump(mode="json")
        return out

    return learn
