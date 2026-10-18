"""analyze: rules first, small-model structured fallback; also decides reply language (§4, §10)."""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from app.brain.models import LIFE_AREAS
from app.graphs.deps import Deps
from app.graphs.nodes.common import last_user_text, previous_assistant_text
from app.graphs.rules import (
    detect_language,
    explicit_language_request,
    guess_life_areas,
    honour_route,
    rule_route,
)
from app.graphs.state import ChatState
from app.llm.provider import LLMError, LLMMessage
from app.prompts.templates import ANALYZE_SYSTEM, ANALYZE_USER

log = logging.getLogger(__name__)


class AnalyzeResult(BaseModel):
    route: Literal["answer", "followup", "memory_recall", "memory_cmd", "smalltalk"] = "answer"
    life_areas: list[str] = Field(default_factory=list)
    language: Literal["en", "hi", "hinglish"] = "en"
    explicit_language: Literal["en", "hi", "hinglish"] | None = None
    reason: str | None = None


def make_analyze(deps: Deps):
    async def analyze(state: ChatState) -> dict:
        message = last_user_text(state)
        messages = state.get("messages", [])
        has_history = len(messages) > 1 or bool(state.get("summary"))
        profile = state.get("profile") or {}
        explicit = explicit_language_request(message)

        route = rule_route(message, has_history=has_history)
        if route is not None:
            language = explicit or detect_language(message)
            if route == "smalltalk" and len(message.split()) <= 2 and profile.get("preferred_language"):
                language = profile["preferred_language"]
            return {
                "route": route,
                "life_areas": guess_life_areas(message),
                "language": language,
                "explicit_language": explicit,
            }

        try:
            result = await deps.llm.structured(
                "analyze",
                [
                    LLMMessage("system", ANALYZE_SYSTEM),
                    LLMMessage("user", ANALYZE_USER.format(previous_reply=previous_assistant_text(state)[:300], message=message)),
                ],
                AnalyzeResult,
            )
            areas = [a for a in result.life_areas if a in LIFE_AREAS] or guess_life_areas(message)
            language = explicit or result.explicit_language or result.language
            route = honour_route(result.route, message, has_history=has_history)
            return {"route": route, "life_areas": areas, "language": language, "explicit_language": explicit or result.explicit_language}
        except LLMError as exc:
            log.warning("analyze LLM failed, defaulting to answer route: %s", exc)
            return {
                "route": "answer",
                "life_areas": guess_life_areas(message),
                "language": explicit or detect_language(message),
                "explicit_language": explicit,
            }

    return analyze


def route_from_state(state: ChatState) -> str:
    return state.get("route") or "answer"
