"""FastAPI application factory and dependency wiring."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fastapi import FastAPI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from app.api.routes import router
from app.brain.repository import BrainRepository
from app.config import Settings, get_settings
from app.graphs.deps import Deps, utc_now
from app.graphs.memory_graph import build_memory_graph
from app.graphs.response_graph import build_response_graph
from app.llm.provider import LLMProvider
from app.tools.brain_tools import make_tools

log = logging.getLogger(__name__)


def build_repo(settings: Settings) -> BrainRepository:
    if settings.brain_backend == "neo4j":
        from app.brain.neo4j_repo import Neo4jBrainRepository

        return Neo4jBrainRepository.from_settings(settings)
    from app.brain.memory_repo import InMemoryBrainRepository

    return InMemoryBrainRepository()


def build_llm(settings: Settings) -> LLMProvider:
    if settings.llm_backend == "fake":
        from app.llm.fake import FakeLLM

        return FakeLLM()
    from app.llm.litellm_provider import LiteLLMProvider

    return LiteLLMProvider(settings)


def build_checkpointer(settings: Settings) -> BaseCheckpointSaver:
    # Demo scope: in-process saver. Production swaps in a Redis/Postgres saver (§17) via create_app(checkpointer=...).
    return InMemorySaver()


@dataclass
class Container:
    settings: Settings
    repo: BrainRepository
    llm: LLMProvider
    deps: Deps
    checkpointer: BaseCheckpointSaver
    response_graph: Any
    memory_graph: Any
    memory_runs: deque[dict[str, Any]]  # recent background runs (bounded) for debugging/tests
    _locks: defaultdict[str, asyncio.Lock] = field(default_factory=lambda: defaultdict(asyncio.Lock))

    def lock_for(self, thread_id: str) -> asyncio.Lock:
        """Serialize turns per session so concurrent requests cannot fork the checkpoint."""
        return self._locks[thread_id]

    async def run_memory_graph(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Background task: never raises (the user already has their reply)."""
        try:
            result = await self.memory_graph.ainvoke(payload)
            self.memory_runs.append({"payload": payload, "result": result})
            return result
        except Exception as exc:  # noqa: BLE001
            log.exception("memory graph failed for user=%s: %s", payload.get("user_id"), exc)
            self.memory_runs.append({"payload": payload, "error": str(exc)})
            return None


def build_container(
    settings: Settings | None = None,
    *,
    repo: BrainRepository | None = None,
    llm: LLMProvider | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Container:
    settings = settings or get_settings()
    repo = repo or build_repo(settings)
    llm = llm or build_llm(settings)
    checkpointer = checkpointer or build_checkpointer(settings)
    deps = Deps(settings=settings, repo=repo, llm=llm, clock=clock or utc_now)
    deps.tools = make_tools(repo, settings, deps.clock)
    return Container(
        settings=settings,
        repo=repo,
        llm=llm,
        deps=deps,
        checkpointer=checkpointer,
        response_graph=build_response_graph(deps, checkpointer),
        memory_graph=build_memory_graph(deps),
        memory_runs=deque(maxlen=200),
    )


def create_app(
    settings: Settings | None = None,
    *,
    repo: BrainRepository | None = None,
    llm: LLMProvider | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    container = build_container(settings, repo=repo, llm=llm, checkpointer=checkpointer, clock=clock)
    logging.basicConfig(level=getattr(logging, container.settings.log_level.upper(), logging.INFO))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await container.repo.ensure_schema()
        except Exception as exc:  # noqa: BLE001 — degrade, don't crash (§11)
            log.warning("could not ensure brain schema at startup: %s", exc)
        yield
        await container.repo.close()

    app = FastAPI(title="MyNaksh Shared Brain", version="0.1.0", lifespan=lifespan)
    app.state.container = container
    app.include_router(router)
    return app


app = create_app()
