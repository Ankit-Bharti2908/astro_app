"""HTTP endpoints (§12)."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from langchain_core.messages import AIMessage, HumanMessage

from app.api.schemas import (
    ID_PATTERN,
    ChatRequest,
    ChatResponse,
    HealthOut,
    MemoryOut,
    UserCreate,
    UserOut,
    UserPatch,
)
from app.brain.errors import BrainUnavailable
from app.brain.models import LIFE_AREAS, UserProfile
from app.brain.scoring import rank_memories, retrieval_score
from app.prompts.templates import PROMPT_VERSION

log = logging.getLogger(__name__)
router = APIRouter()

MEMORY_ROUTES = {"answer", "memory_recall"}  # §5: skipped for followup, smalltalk, memory_cmd, ephemeral
UserId = Annotated[str, Path(min_length=1, max_length=128, pattern=ID_PATTERN)]


def _container(request: Request):
    return request.app.state.container


def _brain_503(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail={"error": "brain_unavailable", "message": str(exc)})


# ---- users ----------------------------------------------------------------------

@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(body: UserCreate, request: Request):
    c = _container(request)
    try:
        if await c.repo.get_user(body.user_id):
            raise HTTPException(status.HTTP_409_CONFLICT, detail="user already exists")
        profile = await c.repo.create_user(UserProfile(**body.model_dump()), now=c.deps.clock())
    except BrainUnavailable as exc:
        raise _brain_503(exc) from exc
    return UserOut(**profile.model_dump())


@router.get("/users/{user_id}", response_model=UserOut)
async def get_user(user_id: UserId, request: Request):
    c = _container(request)
    try:
        profile = await c.repo.get_user(user_id)
    except BrainUnavailable as exc:
        raise _brain_503(exc) from exc
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user not found")
    return UserOut(**profile.model_dump())


@router.patch("/users/{user_id}", response_model=UserOut)
async def patch_user(user_id: UserId, body: UserPatch, request: Request):
    c = _container(request)
    patch = body.model_dump(exclude_unset=True)
    try:
        if await c.repo.get_user(user_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user not found")
        profile = await c.repo.update_user(user_id, patch, now=c.deps.clock())
    except BrainUnavailable as exc:
        raise _brain_503(exc) from exc
    return UserOut(**profile.model_dump())


# ---- memories -------------------------------------------------------------------

@router.get("/users/{user_id}/memories", response_model=list[MemoryOut])
async def list_memories(
    user_id: UserId,
    request: Request,
    life_area: str | None = Query(default=None),
    status_filter: str = Query(default="active", alias="status", pattern="^(active|superseded|expired|deleted|all)$"),
):
    c = _container(request)
    if life_area is not None and life_area not in LIFE_AREAS and life_area != "general":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"life_area must be one of {list(LIFE_AREAS)}")
    statuses = ("active", "superseded", "expired", "deleted") if status_filter == "all" else (status_filter,)
    try:
        memories = await c.repo.list_memories(user_id, life_areas=[life_area] if life_area else None, statuses=statuses)
    except BrainUnavailable as exc:
        raise _brain_503(exc) from exc
    now = c.deps.clock()
    ordered = rank_memories(memories, now, include_expired=True) if status_filter == "active" else memories
    return [MemoryOut(**m.model_dump(), retrieval_score=round(retrieval_score(m, now), 4)) for m in ordered]


@router.delete("/users/{user_id}/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(user_id: UserId, memory_id: str, request: Request):
    c = _container(request)
    try:
        ok = await c.repo.mark_deleted(user_id, memory_id, now=c.deps.clock())
    except BrainUnavailable as exc:
        raise _brain_503(exc) from exc
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="memory not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- chat -----------------------------------------------------------------------

@router.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, background: BackgroundTasks, request: Request):
    c = _container(request)
    now = c.deps.clock()
    tz = body.timezone or c.settings.default_timezone
    thread_id = f"{body.user_id}:{body.session_id}"
    inputs: dict[str, Any] = {
        "messages": [HumanMessage(content=body.message)],
        "user_id": body.user_id,
        "session_id": body.session_id,
        "ephemeral": body.ephemeral,
        "now": now.isoformat(),
        "timezone": tz,
    }
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": c.settings.recursion_limit}
    async with c.lock_for(thread_id):  # one turn at a time per session (checkpoint is last-writer-wins)
        state = await c.response_graph.ainvoke(inputs, config=config)

    route = state.get("route") or "answer"
    if route in MEMORY_ROUTES and not body.ephemeral:
        payload = {
            "user_id": body.user_id,
            "session_id": body.session_id,
            "message": body.message,
            "now": now.isoformat(),
            "language": state.get("language", "en"),
            "ephemeral": body.ephemeral,
            "timezone": tz,
            "context": _previous_assistant_reply(state),
        }
        if body.sync_memory:
            await c.run_memory_graph(payload)
        else:
            background.add_task(c.run_memory_graph, payload)

    return ChatResponse(
        response=state.get("response", ""),
        user_id=body.user_id,
        session_id=body.session_id,
        route=route,
        language=state.get("language", "en"),
        context_used=list(state.get("context_used", [])),
        context_tokens=dict(state.get("context_tokens", {})),
        degraded=list(state.get("degraded", [])),
        memory_cmd_result=state.get("memory_cmd_result") or None,
    )


def _previous_assistant_reply(state: dict[str, Any]) -> str:
    """The assistant turn *before* this turn's reply (reference resolution for extraction)."""
    ai = [m for m in state.get("messages", []) if isinstance(m, AIMessage) and not m.tool_calls]
    if len(ai) >= 2:
        content = ai[-2].content
        return content if isinstance(content, str) else str(content)
    return state.get("summary") or ""


# ---- health ---------------------------------------------------------------------

@router.get("/health", response_model=HealthOut)
async def health(request: Request):
    c = _container(request)
    brain_ok = await c.repo.ping()
    llm = await c.llm.healthcheck()
    return HealthOut(
        status="ok" if brain_ok and llm.get("ok", False) else "degraded",
        brain={"backend": c.settings.brain_backend, "ok": brain_ok},
        llm=llm,
        prompt_version=PROMPT_VERSION,
    )
