"""analyze: the small model classifies the turn — route, life areas, reply language (§4, §10).

This node used to try keyword rules first and only fall through to the model. Every one of
those rules was a word list standing in for a judgement about meaning, and they failed the way
word lists do: a leading "Remove" made "Remove obstacles from my career, what should I do?" an
instruction to delete, and the matching list below it then deleted a career memory. The
patterns for follow-ups, greetings, recall questions, Hinglish and life areas were the same
shape and wrong for the same reason — "my boss is a nightmare" has no listed career word, and
"I had a great workout" has one.

The rules themselves were not wrong; their form was. They now live in `ANALYZE_SYSTEM`, where
they are read by something that can weigh them against the sentence in front of it. What stays
here is the one thing that is not a judgement about the message: `followup` answers from a
previous reply, so it cannot be chosen when there is no previous reply.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

from app.brain.models import LIFE_AREAS
from app.graphs.deps import Deps
from app.graphs.nodes.common import last_user_text, previous_assistant_text
from app.graphs.state import ChatState
from app.llm.provider import LLMError, LLMMessage
from app.prompts.templates import ANALYZE_SYSTEM, ANALYZE_USER

log = logging.getLogger(__name__)

# A greeting carries no language of its own — "hi" is a word in several. Nothing in the
# message can settle it, so the user's stored preference does (§10).
GREETING_WORDS = 2

# The one survivor of the deleted word lists, and not a word list: whether a string contains
# characters from the Devanagari block is a fact about the bytes, in the same class as "does
# this date parse", not a judgement about what the sentence means. It is here because the
# model gets it wrong often enough to matter — `hi` for romanised Hindi in roughly one case in
# four, against a prompt that spells the rule out — and the cost is a user who writes
# "meri shaadi kab hogi" being answered in a script they may not read. The guess that WAS a
# judgement, deciding Hinglish from a vocabulary list, stays deleted (§10).
_DEVANAGARI = re.compile(r"[\u0900-\u097f]")


class AnalyzeResult(BaseModel):
    route: Literal["answer", "followup", "memory_recall", "memory_cmd", "smalltalk"] = "answer"
    life_areas: list[str] = Field(default_factory=list)
    language: Literal["en", "hi", "hinglish"] = "en"
    explicit_language: Literal["en", "hi", "hinglish"] | None = None
    reason: str | None = None


def honour_route(route: str, *, has_history: bool) -> str:
    """The one structural check: a follow-up needs something to follow up on."""
    return "answer" if route == "followup" and not has_history else route


def make_analyze(deps: Deps):
    async def analyze(state: ChatState) -> dict:
        message = last_user_text(state)
        messages = state.get("messages", [])
        has_history = len(messages) > 1 or bool(state.get("summary"))
        profile = state.get("profile") or {}
        preferred = profile.get("preferred_language")

        try:
            result = await deps.llm.structured(
                "analyze",
                [
                    LLMMessage("system", ANALYZE_SYSTEM),
                    LLMMessage("user", ANALYZE_USER.format(previous_reply=previous_assistant_text(state)[:300], message=message)),
                ],
                AnalyzeResult,
            )
        except LLMError as exc:
            log.warning("analyze LLM failed, defaulting to answer route: %s", exc)
            return {
                "route": "answer",
                "life_areas": [],
                "language": preferred or "en",
                "explicit_language": None,
            }

        route = honour_route(result.route, has_history=has_history)
        detected = result.language
        if detected == "hi" and not _DEVANAGARI.search(message):
            detected = "hinglish"  # Hindi it may be; Devanagari it demonstrably is not
        # An explicit "reply in Hindi" is a request about the REPLY, not a claim about the
        # script the request was typed in, so it outranks the correction above.
        language = result.explicit_language or detected
        if route == "smalltalk" and len(message.split()) <= GREETING_WORDS and preferred:
            language = preferred
        return {
            "route": route,
            "life_areas": [a for a in result.life_areas if a in LIFE_AREAS],
            "language": language,
            "explicit_language": result.explicit_language,
        }

    return analyze


def route_from_state(state: ChatState) -> str:
    return state.get("route") or "answer"
