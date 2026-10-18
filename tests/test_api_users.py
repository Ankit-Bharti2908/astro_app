from datetime import timedelta

from app.brain.models import Memory
from tests.conftest import FIXED_NOW, chat


def test_user_crud_and_validation(client):
    r = client.post("/users", json={"user_id": "u1", "name": "Rahul", "dob": "1995-08-15", "birth_place": "Delhi"})
    assert r.status_code == 201 and r.json()["zodiac"] == "Leo" and r.json()["created_at"].startswith("2026-10-17")
    assert client.post("/users", json={"user_id": "u1"}).status_code == 409
    assert client.post("/users", json={"user_id": ""}).status_code == 422
    assert client.post("/users", json={"user_id": "u2", "dob": "not-a-date"}).status_code == 422
    assert client.get("/users/nope").status_code == 404
    r = client.patch("/users/u1", json={"dob": "1996-01-05", "preferred_language": "hinglish"})
    assert r.status_code == 200 and r.json()["zodiac"] == "Capricorn" and r.json()["preferred_language"] == "hinglish"
    assert r.json()["name"] == "Rahul"  # untouched fields preserved
    assert client.patch("/users/nope", json={"name": "x"}).status_code == 404
    assert client.patch("/users/u1", json={"preferred_language": "fr"}).status_code == 422


async def test_memory_listing_filters_and_delete(client, repo):
    for i, area in enumerate(["career", "health", "career"]):
        await repo.add_memory("u1", Memory(id=f"m{i}", type="goal", key=f"k{i}", value=f"v{i}", life_area=area, created_at=FIXED_NOW - timedelta(days=i), updated_at=FIXED_NOW - timedelta(days=i), importance=0.9, confidence=0.9 - 0.1 * i))
    r = client.get("/users/u1/memories")
    assert r.status_code == 200 and [m["id"] for m in r.json()] == ["m0", "m1", "m2"]  # ranked by retrieval score
    assert all("retrieval_score" in m for m in r.json())
    assert [m["id"] for m in client.get("/users/u1/memories", params={"life_area": "health"}).json()] == ["m1"]
    assert client.get("/users/u1/memories", params={"life_area": "astrology"}).status_code == 422
    assert client.get("/users/u1/memories", params={"status": "bogus"}).status_code == 422
    assert client.get("/users/other/memories").json() == []  # isolation by user_id
    assert client.delete("/users/u1/memories/m1").status_code == 204
    assert client.delete("/users/u1/memories/m1").status_code == 404  # already deleted
    assert client.delete("/users/other/memories/m0").status_code == 404  # cannot delete another user's memory
    assert [m["id"] for m in client.get("/users/u1/memories").json()] == ["m0", "m2"]
    assert [m["id"] for m in client.get("/users/u1/memories", params={"status": "deleted"}).json()] == ["m1"]
    assert len(client.get("/users/u1/memories", params={"status": "all"}).json()) == 3


def test_chat_validation(client):
    assert client.post("/chat", json={"user_id": "u", "session_id": "s", "message": ""}).status_code == 422
    # ids cannot contain the composite-key separator (or whitespace); timezone must be a real IANA zone
    assert client.post("/chat", json={"user_id": "org:uid", "session_id": "s", "message": "hi"}).status_code == 422
    assert client.post("/chat", json={"user_id": "u", "session_id": "uid:s1", "message": "hi"}).status_code == 422
    assert client.post("/chat", json={"user_id": "u", "session_id": "s", "message": "hi", "timezone": "Mars/Olympus"}).status_code == 422
    assert client.post("/chat", json={"user_id": "a.b@c-d_e", "session_id": "s", "message": "hi", "timezone": "Europe/London"}).status_code == 200
    assert client.post("/users", json={"user_id": "bad:id"}).status_code == 422
    assert client.get("/users/bad:id").status_code == 422
    assert client.get("/users/bad:id/memories").status_code == 422
    assert client.post("/chat", json={"user_id": "u", "message": "hi"}).status_code == 422
    r = client.post("/chat", json={"user_id": "u", "session_id": "s", "message": "x" * 4001})
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "message"]


def test_health_and_response_shape(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["brain"] == {"backend": "memory", "ok": True} and h["llm"]["backend"] == "fake"
    assert h["prompt_version"] == "v1"
    d = chat(client, "What should I focus on for my career?")
    assert set(d) >= {"response", "user_id", "session_id", "route", "language", "context_used", "context_tokens", "degraded"}
    assert set(d["context_tokens"]) >= {"profile", "index", "memories", "summary", "turns"}
