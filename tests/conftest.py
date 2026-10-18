from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.brain.errors import BrainUnavailable
from app.brain.memory_repo import InMemoryBrainRepository
from app.config import Settings
from app.main import create_app
from tests.fake_llm import FakeLLM

FIXED_NOW = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)


def make_settings(**overrides) -> Settings:
    base = dict(brain_backend="memory", default_timezone="Asia/Kolkata")
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
