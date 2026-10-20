"""Dependency container handed to node factories."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from langchain_core.tools import BaseTool

from app.brain.repository import BrainRepository
from app.config import Settings
from app.llm.provider import LLMProvider


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Deps:
    settings: Settings
    repo: BrainRepository
    llm: LLMProvider
    clock: Callable[[], datetime] = utc_now
    tools: list[BaseTool] = field(default_factory=list)
    # Bound once the memory graph exists; the `learn` node runs it inside the turn.
    learn: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = None
