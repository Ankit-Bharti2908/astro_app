# API — sample requests and responses

All responses below are real captures from a running instance (Neo4j, OpenAI models), trimmed where marked `…`.
[`demo.http`](../demo.http) replays the same sequence.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/chat` | One chat turn |
| `POST` · `GET` · `PATCH` | `/users`, `/users/{user_id}` | Profile (optional: chat fills it in too) |
| `GET` | `/users/{user_id}/memories` | Memories, ranked |
| `DELETE` | `/users/{user_id}/memories/{memory_id}` | Forget one memory (soft delete) |
| `GET` | `/users/{user_id}/memory-runs` | What the memory graph did on each turn |
| `GET` | `/health` | Status of the brain and the LLM |

## 1. First message: profile and goal in one sentence

```http
POST /chat
{"user_id": "rahul", "session_id": "s1",
 "message": "My name is Rahul, born 15 August 1995 in Delhi. Planning to switch jobs next year."}
```

```json
{
  "response": "It's great to connect with you, Rahul! Since you're planning to switch jobs next year, this can be an exciting time… If you share your time of birth, I can provide more personalized insights…",
  "user_id": "rahul", "session_id": "s1", "route": "answer", "language": "en",
  "context_used": ["user_profile", "memory:career_change"],
  "context_tokens": {"meta": 22, "profile": 43, "index": 11, "memories": 22, "summary": 0, "turns": 25},
  "degraded": [], "tools_used": [], "memory_cmd_result": null
}
```

The profile and goal were stored before the reply was written, so the first answer already uses his name and asks
only for the one missing detail.

```http
GET /users/rahul
→ {"user_id": "rahul", "name": "Rahul", "dob": "1995-08-15", "tob": null, "birth_place": "Delhi",
   "preferred_language": null, "zodiac": "Leo", …}

GET /users/rahul/memories
→ [{"id": "ea0b56801facbcc8", "type": "goal", "key": "career_change", "value": "switch jobs",
    "attributes": {"target_year": 2027}, "certainty": "explicit", "confidence": 0.9, "importance": 0.9,
    "retrieval_score": 0.81, "status": "active", "session_id": "s1", …}]
```

"Next year" was stored as `target_year: 2027`, so the memory is still correct when read in a later year.

## 2. Using the memory

```http
POST /chat   {"user_id": "rahul", "session_id": "s1", "message": "What should I focus on for my career?"}
→ {"route": "answer",
   "context_used": ["user_profile", "memory_index", "memory:career_change", "recent_turns"],
   "context_tokens": {"meta": 22, "profile": 43, "index": 13, "memories": 22, "summary": 0, "turns": 152}, …}
```

## 3. Follow-up: current chat only

```http
POST /chat   {"user_id": "rahul", "session_id": "s1", "message": "Why do you say that?"}
→ {"route": "followup",
   "context_used": ["user_profile", "memory_index", "recent_turns"],
   "context_tokens": {"meta": 22, "profile": 43, "index": 13, "memories": 0, "summary": 0, "turns": 380}, …}
```

`memories: 0`: the follow-up is answered from the chat, with no retrieval.

## 4. New session: the memory survives

```http
POST /chat   {"user_id": "rahul", "session_id": "s2", "message": "What do you remember about my career goals?"}
→ {"response": "I remember that your goal is to switch jobs, with a target year of 2027…",
   "route": "memory_recall", "context_used": ["user_profile", "memory_index", "memory:career_change"], …}
```

The new session starts with an empty chat; the goal comes from Neo4j. On this route the facts come straight from
the graph, so the model can't invent one.

## 5. Impossible birth details

```http
POST /chat   {"user_id": "u2", "session_id": "s1", "message": "65 may 2200, delhi me raat ke 12 PM pe"}
→ {"response": "Mujhe aapka birth date aur time samajhne mein thoda confusion ho raha hai… Yeh details milne par main aapki astrology chart bana sakta hoon.",
   "route": "answer", "language": "hinglish", "context_used": []}
```

Nothing was stored (`GET /users/u2` → 404), and the reply asks again instead of repeating the date back.

## 6. What the memory graph did

```http
GET /users/rahul/memory-runs?session_id=s1
→ {"message": "My name is Rahul, born 15 August 1995 in Delhi. Planning to switch jobs next year.",
   "decisions": [
     {"action": "PROFILE", "candidate": {"key": "name", "value": "Rahul", …}},
     {"action": "PROFILE", "candidate": {"key": "dob", "value": "1995-08-15", …}},
     {"action": "PROFILE", "candidate": {"key": "birth_place", "value": "Delhi", …}},
     {"action": "ADD", "reason": "no existing memory to reconcile against",
      "candidate": {"key": "career_change", "attributes": {"target_year": 2027}, "certainty": "explicit", "confidence": 0.9, …}}],
   "written": [… the same four, with the new memory id …], "dropped": [], "error": null}
```

Actions: `ADD`, `UPDATE`, `SUPERSEDE` (replaces an older memory), `NOOP` (duplicate; confidence is raised),
`PROFILE` (a birth-detail slot), `SKIP` (refused, with the reason).

## 7. Other calls

```http
POST   /users  {"user_id": "rahul", "name": "Rahul", "dob": "1995-08-15", "tob": "10:00", "birth_place": "Delhi"}  → 201, sign derived
GET    /users/rahul/memories?status=all      → includes superseded memories and their links
DELETE /users/rahul/memories/{id}            → 204, soft delete
POST   /chat  {…, "ephemeral": true}         → reads the brain, writes nothing
GET    /health                               → {"status": "ok", "brain": {"backend": "neo4j", "ok": true}, "llm": {…}, …}
```

## Errors

| Status | When |
|---|---|
| 422 | Invalid request: bad ID characters, unparseable date, message over 4,000 characters |
| 404 | Unknown user or memory |
| 503 | Neo4j unreachable, on the profile and memory endpoints |

`/chat` never fails because a dependency is down. It replies and says so in `degraded`: `brain_unavailable`
(answered from the current chat) or `llm_unavailable` (canned reply).

**Known gap:** an impossible value sent to `POST`/`PATCH /users` (e.g. `"tob": "25:99"`) is dropped, but the call
still returns 200 without saying why. Nothing wrong is stored, but the client isn't told.
