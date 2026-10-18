# API — sample requests and responses

Every response below is a real capture against a running instance (`BRAIN_BACKEND=neo4j`, OpenAI models), trimmed
only where noted. The same sequence is replayable with [`demo.http`](../demo.http).

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/chat` | A chat turn |
| `POST` | `/users` · `GET` `PATCH` `/users/{user_id}` | Profile (optional — chat fills it in too) |
| `GET` | `/users/{user_id}/memories` | Memories, ranked by retrieval score |
| `DELETE` | `/users/{user_id}/memories/{memory_id}` | Forget (soft delete) |
| `GET` | `/users/{user_id}/memory-runs` | What the memory graph did, per turn |
| `GET` | `/health` | Liveness + brain/LLM status |

---

## 1. First message — profile and goal in one sentence

```http
POST /chat
Content-Type: application/json

{"user_id": "rahul", "session_id": "s1",
 "message": "My name is Rahul, born 15 August 1995 in Delhi. Planning to switch jobs next year."}
```

```json
{
  "response": "It's great to connect with you, Rahul! Since you're planning to switch jobs next year, this can be an exciting time… If you share your time of birth, I can provide more personalized insights…",
  "user_id": "rahul",
  "session_id": "s1",
  "route": "answer",
  "language": "en",
  "context_used": ["user_profile", "memory:career_change"],
  "context_tokens": {"meta": 22, "profile": 43, "index": 11, "memories": 22, "summary": 0, "turns": 25},
  "degraded": [],
  "tools_used": [],
  "memory_cmd_result": null
}
```

The profile and the goal were both written **before** this reply was composed — which is why the very first answer
already greets him by name, knows the sign, and asks only for the one detail still missing.

```http
GET /users/rahul
```

```json
{"user_id": "rahul", "name": "Rahul", "dob": "1995-08-15", "tob": null,
 "birth_place": "Delhi", "preferred_language": null, "zodiac": "Leo",
 "created_at": "2026-10-18T11:02:06Z", "updated_at": "2026-10-18T11:02:06Z"}
```

```http
GET /users/rahul/memories
```

```json
[{"id": "ea0b56801facbcc8", "type": "goal", "key": "career_change", "value": "switch jobs",
  "attributes": {"target_year": 2027}, "life_area": "general", "cardinality": "multi",
  "certainty": "explicit", "source": "stated",
  "confidence": 0.9, "importance": 0.9, "retrieval_score": 0.81,
  "mention_count": 1, "status": "active", "valid_until": null, "last_used_at": null,
  "session_id": "s1", "prompt_version": "v5", "supersedes": null, "superseded_by": null,
  "created_at": "2026-10-18T11:02:06Z", "updated_at": "2026-10-18T11:02:06Z"}]
```

*"next year"* was resolved against the current date into `target_year: 2027` at extraction time, so the memory stays
true when read in a later year.

## 2. Using the memory

```http
POST /chat   {"user_id": "rahul", "session_id": "s1", "message": "What should I focus on for my career?"}
```

```json
{"route": "answer", "language": "en",
 "context_used": ["user_profile", "memory_index", "memory:career_change", "recent_turns"],
 "context_tokens": {"meta": 22, "profile": 43, "index": 13, "memories": 22, "summary": 0, "turns": 152},
 "degraded": []}
```

## 3. Follow-up — short-term context only

```http
POST /chat   {"user_id": "rahul", "session_id": "s1", "message": "Why do you say that?"}
```

```json
{"route": "followup", "language": "en",
 "context_used": ["user_profile", "memory_index", "recent_turns"],
 "context_tokens": {"meta": 22, "profile": 43, "index": 13, "memories": 0, "summary": 0, "turns": 380}}
```

`memories: 0` is the assertion: a follow-up is answered from the transcript, with no retrieval.

## 4. New session — long-term memory survives

```http
POST /chat   {"user_id": "rahul", "session_id": "s2", "message": "What do you remember about my career goals?"}
```

```json
{"response": "I remember that your goal is to switch jobs, with a target year of 2027…",
 "route": "memory_recall", "language": "en",
 "context_used": ["user_profile", "memory_index", "memory:career_change"]}
```

A new `session_id` means an empty checkpointer; the goal comes from Neo4j under `user_id`. On this route the facts
come straight from Cypher and the model only phrases them, so it cannot invent one.

## 5. Unusable birth details

```http
POST /chat   {"user_id": "u2", "session_id": "s1", "message": "65 may 2200, delhi me raat ke 12 PM pe"}
```

```json
{"response": "Mujhe aapka birth date aur time samajhne mein thoda confusion ho raha hai… Yeh details milne par main aapki astrology chart bana sakta hoon.",
 "route": "answer", "language": "hinglish", "context_used": []}
```

Nothing was stored: `GET /users/u2` → `404 user not found`. The reply says which part it could not use instead of
echoing it back as fact. Where the extractor *does* hand over an impossible value, the validator refuses it, the run
trace shows `SKIP` with the reason, and that reason is injected into the reply's prompt (§11).

## 6. What the memory graph did — `memory-runs`

The console at `/ui/` renders this; it is also the per-turn audit trail.

```http
GET /users/rahul/memory-runs?session_id=s1
```

```jsonc
{
  "seq": 18,
  "message": "My name is Rahul, born 15 August 1995 in Delhi. Planning to switch jobs next year.",
  "candidates": [ /* 4: name, dob, birth_place (profile_fact) + career_change (goal) */ ],
  "dropped": [],
  "decisions": [
    {"action": "PROFILE", "reason": "profile slot", "candidate": {"key": "name", "value": "Rahul", "…": "…"}},
    {"action": "PROFILE", "reason": "profile slot", "candidate": {"key": "dob", "value": "1995-08-15", "…": "…"}},
    {"action": "PROFILE", "reason": "profile slot", "candidate": {"key": "birth_place", "value": "Delhi", "…": "…"}},
    {"action": "ADD", "reason": "no existing memory to reconcile against",
     "candidate": {"key": "career_change", "value": "switch jobs", "attributes": {"target_year": 2027},
                   "certainty": "explicit", "subject": "self", "confidence": 0.9, "importance": 0.9}}
  ],
  "written": [
    {"action": "PROFILE", "key": "name",        "value": "Rahul"},
    {"action": "PROFILE", "key": "dob",         "value": "1995-08-15", "profile": {"zodiac": "Leo", "…": "…"}},
    {"action": "PROFILE", "key": "birth_place", "value": "Delhi"},
    {"action": "ADD",     "key": "career_change", "value": "switch jobs", "memory_id": "ea0b56801facbcc8"}
  ],
  "degraded": [], "skipped_reason": null, "error": null
}
```

Actions: `ADD` · `UPDATE` · `SUPERSEDE` · `NOOP` (duplicate, reinforced) · `PROFILE` (a User-node slot) ·
`SKIP` (refused, with the reason).

## 7. Other calls

```http
POST /users
{"user_id": "rahul", "name": "Rahul", "dob": "1995-08-15", "tob": "10:00",
 "birth_place": "Delhi", "preferred_language": "en"}          → 201, zodiac derived

PATCH /users/rahul      {"tob": "25:99"}                       → 200, but tob stays null (see below)
GET   /users/rahul/memories?status=all                         → includes superseded, with SUPERSEDES links
DELETE /users/rahul/memories/{id}                              → 204, soft delete (right-to-forget)
POST  /chat   {..., "ephemeral": true}                         → reads the brain, writes nothing
```

```http
GET /health
```

```json
{"status": "ok",
 "brain": {"backend": "neo4j", "ok": true},
 "llm": {"backend": "litellm", "ok": true, "models": {"strong": "…", "small": "…"}},
 "prompt_version": "v5"}
```

## Errors

| Status | When |
|---|---|
| `422` | Schema violation (bad id pattern, unparseable date, message over 4000 chars) |
| `404` | Unknown user or memory |
| `503` | Brain unreachable on a route that cannot degrade (the profile/memory CRUD endpoints) |

`/chat` does not fail on a dependency outage. It replies and reports the fact:
`"degraded": ["brain_unavailable"]` (answered from short-term context) or `["llm_unavailable"]` (canned reply).

**Known gap.** An inadmissible *value* on `POST`/`PATCH /users` (`"tob": "25:99"`, `"birth_place": "mars"`) is
dropped by the validator and the rest of the patch is applied, so the call returns `200` with that field unchanged
rather than reporting the rejection. Nothing wrong is ever stored — but a typed client is not told why its value
vanished. The chat path does not have this gap: there the refusal reaches the user in the reply.
