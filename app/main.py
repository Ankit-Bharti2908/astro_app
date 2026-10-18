"""FastAPI application factory and dependency wiring."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
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
    from app.llm.litellm_provider import LiteLLMProvider

    return LiteLLMProvider(settings)


def build_checkpointer(settings: Settings) -> BaseCheckpointSaver:
    if settings.checkpointer == "postgres":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # optional dependency

        return AsyncPostgresSaver.from_conn_string(settings.checkpointer_url)  # type: ignore[return-value]
    return InMemorySaver()


MAX_MEMORY_RUNS = 50  # the UI's memory-graph trace is a rolling window, not a log


@dataclass
class Container:
    settings: Settings
    repo: BrainRepository
    llm: LLMProvider
    deps: Deps
    checkpointer: BaseCheckpointSaver
    response_graph: Any
    memory_graph: Any
    memory_runs: list[dict[str, Any]]

    async def run_memory_graph(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Never raises: a memory-graph failure degrades the turn, it does not fail the reply."""
        try:
            result = await self.memory_graph.ainvoke(payload)
            self.memory_runs.append({"payload": payload, "result": result})
            return result
        except Exception as exc:  # noqa: BLE001
            log.exception("memory graph failed for user=%s: %s", payload.get("user_id"), exc)
            self.memory_runs.append({"payload": payload, "error": str(exc)})
            return None
        finally:
            del self.memory_runs[:-MAX_MEMORY_RUNS]


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
    container = Container(
        settings=settings,
        repo=repo,
        llm=llm,
        deps=deps,
        checkpointer=checkpointer,
        response_graph=build_response_graph(deps, checkpointer),
        memory_graph=build_memory_graph(deps),
        memory_runs=[],
    )
    deps.learn = container.run_memory_graph  # the `learn` node runs the memory graph in-turn
    return container


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

    # the orchestration console: chat on the left, what the graphs did on the right
    ui_dir = Path(__file__).parent / "static"
    app.mount("/ui", StaticFiles(directory=ui_dir, html=True), name="ui")

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/ui/")

    return app


app = create_app()
