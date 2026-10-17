from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.brain.errors import BrainUnavailable
from app.brain.memory_repo import InMemoryBrainRepository
from app.config import Settings
from app.llm.fake import FakeLLM
from app.main import create_app

FIXED_NOW = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)


def make_settings(**overrides) -> Settings:
    base = dict(brain_backend="memory", llm_backend="fake", default_timezone="Asia/Kolkata")
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def repo() -> InMemoryBrainRepository:
    return InMemoryBrainRepository()


@pytest.fixture
def clock():
    return lambda: FIXED_NOW


@pytest.fixture
def app(settings, repo, fake_llm, clock):
    return create_app(settings, repo=repo, llm=fake_llm, clock=clock)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def chat(client: TestClient, message: str, *, user: str = "rahul", session: str = "s1", **kwargs) -> dict:
    r = client.post("/chat", json={"user_id": user, "session_id": session, "message": message, **kwargs})
    assert r.status_code == 200, r.text
    return r.json()


class FailingRepo(InMemoryBrainRepository):
    """Every data method raises BrainUnavailable (scenario 10)."""

    async def ping(self) -> bool:
        return False


def _failing(name):
    async def method(self, *args, **kwargs):
        self.calls.append(name)
        raise BrainUnavailable(f"{name}: neo4j down")

    return method


for _name in (
    "create_user", "get_user", "update_user", "list_memories", "get_memory", "add_memory", "update_memory",
    "supersede_memory", "mark_deleted", "touch_used", "memory_index", "upsert_session", "get_session", "search_sessions",
):
    setattr(FailingRepo, _name, _failing(_name))


# ---- optional real Neo4j -------------------------------------------------------

NEO4J_TEST_URI = os.getenv("NEO4J_TEST_URI")


@pytest.fixture(params=["memory", pytest.param("neo4j", marks=pytest.mark.neo4j)])
async def any_repo(request):
    if request.param == "memory":
        yield InMemoryBrainRepository()
        return
    if not NEO4J_TEST_URI:
        pytest.skip("NEO4J_TEST_URI not set")
    from app.brain.neo4j_repo import Neo4jBrainRepository

    repo = Neo4jBrainRepository(NEO4J_TEST_URI, os.getenv("NEO4J_TEST_USER") or None, os.getenv("NEO4J_TEST_PASSWORD") or None)
    await repo.ensure_schema()
    yield repo
    await repo.close()


def unique_user() -> str:
    return f"u-{uuid.uuid4().hex[:8]}"
