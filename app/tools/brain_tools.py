"""Read-only tools the answer agent may request; executed by the `fetch_context` ToolNode."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import InjectedState

from app.brain.errors import BrainUnavailable
from app.brain.models import LIFE_AREAS, UserProfile, recall_areas
from app.brain.repository import BrainRepository
from app.brain.scoring import rank_memories
from app.config import Settings
from app.llm.provider import ToolSpec
from app.profile.zodiac import chart_stub


def make_tools(repo: BrainRepository, settings: Settings, clock: Callable[[], datetime]) -> list[BaseTool]:
    @tool(response_format="content_and_artifact")
    async def recall_memories(
        life_area: str,
        query: str,
        state: Annotated[dict, InjectedState],
    ) -> tuple[str, dict[str, Any]]:
        """Fetch the user's active long-term memories for one life area
        (career, relationships, health, finance, family, education, spirituality, or "all").
        Use it when the memory index shows something relevant that is not already loaded."""
        user_id = state["user_id"]
        areas = None if life_area in ("all", "", "*") else [life_area]
        if areas and areas[0] not in LIFE_AREAS and areas[0] != "general":
            return f"Unknown life area '{life_area}'. Use one of {', '.join(LIFE_AREAS)} or 'all'.", {}
        try:
            memories = rank_memories(
                await repo.list_memories(user_id, life_areas=recall_areas(areas)), clock(), top_k=settings.top_k_memories
            )
        except BrainUnavailable:
            return "The long-term memory store is unavailable right now.", {"degraded": ["brain_unavailable"]}
        if not memories:
            return f"No stored memories for life area '{life_area}'.", {"context_used": [f"tool:recall_memories:{life_area}"]}
        text = "\n".join(m.as_line() for m in memories)
        return text, {
            "memory_ids": [m.id for m in memories],
            "context_used": [f"memory:{m.key}" for m in memories],
        }

    @tool(response_format="content_and_artifact")
    async def get_astro_profile(state: Annotated[dict, InjectedState]) -> tuple[str, dict[str, Any]]:
        """Return the user's sun sign and stubbed chart attributes (element, modality, ruling planet)
        derived from their birth details. Use when the answer needs chart specifics."""
        raw = state.get("profile")
        if not raw:
            return "No birth details on file; ask the user for date, time and place of birth.", {"context_used": ["tool:get_astro_profile"]}
        profile = UserProfile.model_validate(raw)
        chart = chart_stub(profile.dob, profile.tob, profile.birth_place)
        return json.dumps(chart), {"context_used": ["astro_profile"]}

    @tool(response_format="content_and_artifact")
    async def search_past_sessions(query: str, state: Annotated[dict, InjectedState]) -> tuple[str, dict[str, Any]]:
        """Search summaries of the user's earlier chat sessions for a topic. Use when the user refers
        to something discussed in a previous conversation."""
        try:
            sessions = await repo.search_sessions(
                state["user_id"], query, limit=3, exclude_session_id=state.get("session_id")
            )
        except BrainUnavailable:
            return "The long-term memory store is unavailable right now.", {"degraded": ["brain_unavailable"]}
        if not sessions:
            return "No earlier sessions match that topic.", {"context_used": ["tool:search_past_sessions"]}
        text = "\n".join(f"- [{s.session_id} · {s.ended_at or s.started_at:%Y-%m-%d}] {s.summary}" for s in sessions)
        return text, {"context_used": [f"past_session:{s.session_id}" for s in sessions]}

    return [recall_memories, get_astro_profile, search_past_sessions]


def tool_specs(tools: list[BaseTool]) -> list[ToolSpec]:
    specs = []
    for t in tools:
        schema_model = getattr(t, "tool_call_schema", None) or t.args_schema
        schema = schema_model.model_json_schema() if schema_model is not None else {"properties": {}}
        params = {
            "type": "object",
            "properties": schema.get("properties", {}),
            "required": schema.get("required", []),
        }
        specs.append(ToolSpec(name=t.name, description=t.description or "", parameters=params))
    return specs
