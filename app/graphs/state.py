"""Graph state schemas."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class ChatState(TypedDict, total=False):
    # short-term memory (persisted by the checkpointer, thread_id = session_id)
    messages: Annotated[list[AnyMessage], add_messages]
    summary: str

    # per-turn input
    user_id: str
    session_id: str
    ephemeral: bool
    now: str  # ISO-8601 UTC
    timezone: str

    # per-turn working state (reset by load_session)
    route: str
    life_areas: list[str]
    language: str
    explicit_language: str | None
    profile: dict[str, Any] | None
    memory_index: dict[str, dict[str, int]]
    prefetched_memories: list[dict[str, Any]]
    context_used: list[str]
    context_tokens: dict[str, int]
    memory_ids_used: list[str]
    tool_trace: list[dict[str, Any]]  # this turn's tool calls, kept for /chat's response (§4)
    degraded: list[str]
    tool_rounds: int
    response: str
    memory_cmd_result: dict[str, Any]


class MemoryState(TypedDict, total=False):
    user_id: str
    session_id: str
    message: str
    now: str
    language: str
    ephemeral: bool
    context: str  # previous assistant reply, reference-resolution only

    candidates: list[dict[str, Any]]
    accepted: list[dict[str, Any]]
    dropped: list[dict[str, Any]]
    decisions: Annotated[list[dict[str, Any]], operator.add]
    written: list[dict[str, Any]]
    degraded: list[str]
    skipped_reason: str


class ReconcileInput(TypedDict, total=False):
    user_id: str
    session_id: str
    now: str
    candidate: dict[str, Any]
