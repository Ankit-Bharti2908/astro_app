"""Memory-graph nodes: extract → score → filter → (Send) reconcile → write."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from langgraph.graph import END
from langgraph.types import Send

from app.brain.errors import BrainUnavailable
from app.brain.models import ScoredCandidate
from app.graphs.deps import Deps
from app.graphs.nodes.memory_ops import (
    apply_decision,
    extract_candidates,
    filter_candidates,
    reconcile_candidate,
    score_candidates,
)
from app.graphs.state import MemoryState, ReconcileInput

log = logging.getLogger(__name__)


def _now(raw: str | None, clock) -> datetime:
    if raw:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return clock()


def make_extract(deps: Deps):
    async def extract(state: MemoryState) -> dict:
        if state.get("ephemeral"):
            return {"candidates": [], "skipped_reason": "ephemeral"}
        cands = await extract_candidates(
            deps.llm,
            message=state["message"],
            now=_now(state.get("now"), deps.clock),
            tz=deps.settings.default_timezone,
            context=state.get("context", ""),
        )
        return {"candidates": [c.model_dump(mode="json") for c in cands]}

    return extract


def extract_failure_handler(state: MemoryState) -> dict:
    return {"candidates": [], "skipped_reason": "extraction_failed", "degraded": ["llm_unavailable"]}


def make_score(deps: Deps):
    async def score(state: MemoryState) -> dict:
        from app.brain.models import MemoryCandidate

        scored = score_candidates([MemoryCandidate.model_validate(c) for c in state.get("candidates", [])])
        return {"candidates": [c.model_dump(mode="json") for c in scored]}

    return score


def make_filter(deps: Deps):
    async def filter_node(state: MemoryState) -> dict:
        scored = [ScoredCandidate.model_validate(c) for c in state.get("candidates", [])]
        accepted, dropped = filter_candidates(scored, deps.settings, ephemeral=bool(state.get("ephemeral")))
        return {
            "accepted": [c.model_dump(mode="json") for c in accepted],
            "dropped": [c.model_dump(mode="json") for c in dropped],
        }

    return filter_node


def dispatch_reconcile(state: MemoryState):
    accepted = state.get("accepted", [])
    if not accepted:
        return END
    return [
        Send("reconcile", {"user_id": state["user_id"], "session_id": state.get("session_id"), "now": state.get("now"), "candidate": c})
        for c in accepted
    ]


def make_reconcile(deps: Deps):
    async def reconcile(payload: ReconcileInput) -> dict:
        cand = ScoredCandidate.model_validate(payload["candidate"])
        try:
            decision = await reconcile_candidate(deps.llm, deps.repo, user_id=payload["user_id"], candidate=cand)
        except BrainUnavailable as exc:
            log.warning("reconcile: brain unavailable: %s", exc)
            return {"decisions": [{"candidate": payload["candidate"], "action": "SKIP", "reason": "brain_unavailable"}], "degraded": ["brain_unavailable"]}
        return {"decisions": [{"candidate": payload["candidate"], **decision}]}

    return reconcile


def make_write(deps: Deps):
    async def write(state: MemoryState) -> dict:
        now = _now(state.get("now"), deps.clock)
        written = []
        degraded = list(state.get("degraded", []))
        for d in state.get("decisions", []):
            cand = ScoredCandidate.model_validate(d["candidate"])
            if d["action"] == "SKIP":
                written.append({"action": "SKIP", "key": cand.key, "reason": d.get("reason")})
                continue
            try:
                written.append(
                    await apply_decision(
                        deps.repo, user_id=state["user_id"], session_id=state.get("session_id"),
                        candidate=cand, decision=d, now=now,
                    )
                )
            except BrainUnavailable as exc:
                log.warning("write skipped (brain unavailable): %s", exc)
                degraded.append("brain_unavailable")
                written.append({"action": "SKIP", "key": cand.key, "reason": "brain_unavailable"})
        log.info("memory graph wrote %s", [(w["action"], w["key"]) for w in written])
        return {"written": written, "degraded": degraded}

    return write
