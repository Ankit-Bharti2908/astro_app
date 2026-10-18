"""learn: the turn's memory write, run before the reply instead of after it (§4, §5).

The memory graph used to run as a BackgroundTask once the reply had already been sent, which
left the reply and the store disagreeing about the same sentence in two ways:

- The store refused "65 may 2200" and the reply, which never saw that verdict, restated it as
  established fact. A refusal computed after the answer cannot inform the answer.
- The write landed ~2 s after the response returned, so a user who typed again inside that
  window got a turn whose profile had not caught up — observed, with the model telling them
  "ye details file mein save nahi hui hain".

Both are the same mistake: two paths deciding independently what the user just said. Learning
first and answering second gives one verdict, and the reply is built on top of it — the write
is durable before the response leaves, and whatever was refused is named in the prompt.
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
            return {}  # §12: an ephemeral turn reads the brain and changes nothing

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
        # "recalled": a memory is not reinforced by the very message that created it (§7.4).
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
