# MyNaksh Shared Brain

Personalised astrology chat with **short-term session memory** (LangGraph checkpointer), a **persistent graph-based
Shared Brain** (Neo4j) and **LLM-driven responses** routed through LiteLLM.

Stack: Python 3.11+ · FastAPI · LangGraph · Neo4j · LiteLLM · Pydantic.
The design is documented in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); this README covers running, configuring
and testing the implementation of that design.

```
POST /chat ──► response graph (reads brain) ──► reply
                   │ BackgroundTasks, after the reply
                   └─► memory graph (extract → score → filter → reconcile‖ → write) ──► Neo4j
```

## Quick start

### 1. No keys, no database (deterministic fake LLM + in-memory brain)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
LLM_BACKEND=fake uvicorn app.main:app --reload      # http://localhost:8000/docs
```

### 2. Full stack with Docker (Neo4j + API)

```bash
cp .env.example .env              # add ANTHROPIC_API_KEY (and/or OPENAI_API_KEY) — or set LLM_BACKEND=fake
docker compose up --build         # Neo4j browser: http://localhost:7474 (neo4j / please-change-me)
```

Then replay the walkthrough from the architecture doc with [`demo.http`](demo.http) (VS Code REST Client / JetBrains).

### 3. Local API against your own Neo4j and a real model

```bash
export BRAIN_BACKEND=neo4j NEO4J_URI=bolt://localhost:7687 NEO4J_PASSWORD=...
export ANTHROPIC_API_KEY=...      # LiteLLM reads provider keys directly
uvicorn app.main:app --reload
```

## Configuration

Everything is an environment variable (or `.env`); see [`app/config.py`](app/config.py) and [`.env.example`](.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `BRAIN_BACKEND` | `memory` | `neo4j` or `memory` (in-process, for tests/dev) |
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` / `NEO4J_DATABASE` | `bolt://localhost:7687` / `neo4j` / `neo4j` / `neo4j` | Neo4j connection; empty `NEO4J_USER` disables auth |
| `LLM_BACKEND` | `litellm` | `litellm` (real providers) or `fake` (scripted, offline) |
| `STRONG_MODEL` | `anthropic/claude-opus-5-5` | LiteLLM model for `answer`, `memory_recall`, `followup` |
| `SMALL_MODEL` | `anthropic/claude-haiku-5-5` | LiteLLM model for `analyze`, `extract`, `reconcile`, summarisation, smalltalk |
| `FALLBACK_MODEL` | *(empty)* | Optional fallback provider/model, e.g. `openai/gpt-4o-mini`; the Router falls back to it when the primary fails |
| `LLM_TIMEOUT_S` / `LLM_TOTAL_TIMEOUT_S` / `LLM_NUM_RETRIES` | `30` / `60` / `1` | Per-attempt timeout, hard ceiling per call (across retries and fallback), Router retries before fallback |
| `NEO4J_CONNECT_TIMEOUT_S` / `NEO4J_MAX_RETRY_TIME_S` | `3` / `5` | Fail fast so an outage degrades instead of stalling |
| `BUDGET_*` | 30/150/100/600/300/1200 | Token budgets per prompt layer (§3) |
| `COMPACT_THRESHOLD_TOKENS` / `KEEP_LAST_MESSAGES` | `1200` / `6` | Session compaction (§8) |
| `MAX_TOOL_ROUNDS` / `RECURSION_LIMIT` | `2` / `25` | Agent loop bounds (§4, §11) |
| `CONFIDENCE_THRESHOLD` / `TOP_K_MEMORIES` | `0.6` / `5` | Memory storage threshold and L3 prefetch size (§7) |
| `DEFAULT_TIMEZONE` | `Asia/Kolkata` | Used for L0 and relative-date resolution |

Any model LiteLLM supports works (`anthropic/…`, `openai/…`, `gemini/…`, `ollama/…`, …). Keys are read by LiteLLM
from the provider's usual variable (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, …).

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/users` | Create a user; zodiac derived from DOB |
| `GET` / `PATCH` | `/users/{user_id}` | Read / update profile (zodiac recomputed) |
| `POST` | `/chat` | Chat turn |
| `GET` | `/users/{user_id}/memories?life_area=&status=active\|superseded\|deleted\|all` | Memories ranked by retrieval score |
| `DELETE` | `/users/{user_id}/memories/{memory_id}` | Forget (soft delete, removed from retrieval) |
| `GET` | `/health` | Liveness + brain/LLM status |

```bash
curl -s localhost:8000/chat -H 'content-type: application/json' -d '{
  "user_id": "user-123", "session_id": "session-456",
  "message": "My name is Rahul, born 15 Aug 1995 in Delhi. Planning to switch jobs next year."}'
```

```json
{
  "response": "…", "user_id": "user-123", "session_id": "session-456",
  "route": "answer", "language": "en",
  "context_used": ["user_profile", "memory_index", "memory:career_change", "recent_turns"],
  "context_tokens": {"meta": 23, "profile": 35, "index": 13, "memories": 26, "summary": 0, "turns": 93},
  "degraded": []
}
```

`ephemeral: true` reads memory but writes nothing. `sync_memory: true` (debug aid) runs the memory graph before
responding so the next request is guaranteed to see the write. `degraded` reports `brain_unavailable` /
`llm_unavailable` instead of failing the request.

## Project layout

```
app/
├── main.py                 # app factory, dependency container, lifespan
├── config.py               # Settings
├── api/                    # schemas.py, routes.py
├── graphs/
│   ├── state.py            # ChatState, MemoryState
│   ├── response_graph.py   # load_session → analyze → {answer_agent ⇄ fetch_context | followup | memory_recall | memory_cmd | smalltalk} → maybe_compact
│   ├── memory_graph.py     # extract → score → filter → Send(reconcile)* → write
│   ├── rules.py            # rules-first routing, language heuristics, life-area keywords
│   └── nodes/              # one module per node (+ memory_ops.py: shared pipeline primitives)
├── tools/brain_tools.py    # recall_memories · get_astro_profile · search_past_sessions (read-only ToolNode tools)
├── brain/                  # models.py · scoring.py · repository.py (protocol) · neo4j_repo.py · memory_repo.py
├── llm/                    # provider.py (protocol + structured-output retry) · litellm_provider.py · fake.py
├── context/builder.py      # L0–L3 + ST layers with token budgets
├── profile/zodiac.py       # sun-sign stub
└── prompts/templates.py    # versioned prompts (PROMPT_VERSION recorded on every memory)
tests/                      # see below
eval/                       # labelled set + storage-accuracy / threshold-sweep script
docs/ARCHITECTURE.md        # the design this implements
docker-compose.yml · Dockerfile · demo.http
```

## Testing

```bash
pytest -q                   # no network, no keys, no database needed: ~10 s
ruff check app tests eval
```

| Layer | Where | What it proves |
|---|---|---|
| Rules & scoring | `test_rules.py`, `test_scoring.py`, `test_zodiac.py` | Routing shortcuts, language detection, confidence/importance/decay/expiry maths |
| Context layers | `test_context_builder.py` | Every layer stays inside its budget, stable layers come first, `context_used`/`context_tokens` are right |
| LLM contract | `test_llm_provider.py` | JSON extraction, structured-output retry with the validation error, give-up after one retry, failure surfacing |
| Memory pipeline | `test_memory_pipeline.py` | Filter rules, rules-first reconcile (ADD/NOOP/UPDATE/SUPERSEDE/PROFILE/SKIP), parallel `Send` fan-out, idempotent re-runs |
| §15 scenarios 1–12 + walkthrough | `test_chat_scenarios.py` | New user, create/retrieve/correct memories, follow-up without brain calls, cross-session recall, irrelevant memories excluded, brain down, LLM down, Hinglish, bounded tool loop, expiry, remember/forget, ephemeral, compaction + past-session search, language precedence |
| HTTP API | `test_api_users.py` | CRUD, validation (422), isolation by `user_id`, soft delete, health |
| Repository contract | `test_repo_contract.py` | Same contract run against the in-memory repo **and real Neo4j** (`NEO4J_TEST_URI`) |
| **Every LLM API path** | `test_litellm_provider.py` | Real LiteLLM Router against a local mock provider that speaks both the **OpenAI** and **Anthropic** wire formats: task→tier routing, request shape (system, tools, `max_tokens`, auth headers), tool-call parsing both ways, structured retry through the router, fallback on 5xx / 429 / 529 / timeout, all-down → `LLMUnavailable`, and the full HTTP → graph → Router → mock stack including a mid-conversation outage |

With a provider key set, [`demo.http`](demo.http) is the live check: it replays the §13 walkthrough against real models.

Against a real Neo4j (e.g. `docker compose up neo4j`):

```bash
NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_USER=neo4j NEO4J_TEST_PASSWORD=please-change-me pytest -q
```

### Memory-accuracy eval (§16)

```bash
python -m eval.run_eval --backend fake      # offline, deterministic
python -m eval.run_eval --backend litellm   # with the real extraction model
```

Reports storage-decision precision/recall at several confidence thresholds (validating the 0.6 default), label
accuracy for `certainty` / `subject` / `type`, and missed/unexpected candidates. Results land in `eval/results/`.

## How a turn works

1. **`load_session`** loads L0 (date, timezone, language), L1 (profile card) and L2 (memory index: one line per life
   area). The checkpointer (thread = `user_id:session_id`) has already restored the session messages and summary.
   Neo4j failure → `degraded: ["brain_unavailable"]`, continue with short-term context.
2. **`analyze`** routes with rules first (follow-ups, remember/forget, "what do you remember", greetings); everything
   else goes to the small model with structured output (route, life areas, language). Reply language precedence:
   explicit request → language of the message → profile preference.
3. **`answer_agent`** prefetches the top-k memories for the classified life areas (importance × confidence ×
   recency), asks the strong model, and executes at most 2 rounds of read-only tool calls via the `fetch_context`
   ToolNode. Memories that were used get `last_used_at` reinforced. Tool traffic is dropped from the persisted history.
4. **`followup`** / **`memory_recall`** / **`memory_cmd`** / **`smalltalk`** as in §4; `memory_cmd` writes
   synchronously (remember = extract with `user_asked_to_remember`, forget = `status: deleted`).
5. **`maybe_compact`** incrementally summarises older turns when the session exceeds the budget, removes them with
   `RemoveMessage`, persists the summary on the `Session` node (searchable by `search_past_sessions`).
6. After the reply, the **memory graph** runs in the background for `answer` / `memory_recall` turns (never for
   follow-ups, smalltalk, memory commands or ephemeral chats): the small model extracts `MemoryCandidate`s (features
   only, canonical English, relative dates resolved), rules score and filter them, one `reconcile` runs per candidate
   via `Send`, and `write` applies ADD / UPDATE / SUPERSEDE / NOOP(reinforce) / PROFILE with `SUPERSEDES` and
   `EXTRACTED_FROM` links.

LLM nodes carry `RetryPolicy(max_attempts=2)` and a node-level `error_handler` that returns a canned reply with
`degraded: ["llm_unavailable"]`; structured-output failures retry once with the validation error and then skip.

## Implementation notes (where this goes beyond or refines the doc)

- **Checkpointer thread id is `user_id:session_id`** (the doc says `session_id`): a session id alone could let one
  user resume another's session. Likewise Neo4j `Session` nodes are keyed per user (`key = user_id:session_id`);
  ids are restricted to `[A-Za-z0-9_.@-]` so the separator cannot be forged. Turns are serialised per session
  (an `asyncio.Lock` per thread) because the checkpointer is last-writer-wins under concurrent requests.
- **"Forget" never guesses**: an explicit target is required ("forget my job plans"); "forget everything" wipes all
  active memories; "forget that" asks which memory. Ephemeral turns never write, including forget.
- **Extraction gets the previous assistant turn as *reference-only* context** so corrections such as "actually this
  year, not next" resolve against what was being discussed. The prompt forbids extracting facts from it; assistant
  text is still never a source of user facts.
- **Profile facts** (`name`, `dob`, `tob`, `birth_place`, `preferred_language`) are written to the `User` node (zodiac
  recomputed) rather than stored as `Memory` nodes; other `profile_fact`s are single-valued memories.
- **Never-store** is a label the extractor emits (`sensitive_category`) plus a rule-based backstop for health
  diagnoses and financial details, applied in the deterministic filter.
- **Structured output** is provider-neutral: schema in the prompt, parse, validate, retry once with the error. This
  avoids provider-specific forced-tool JSON modes (which newer Anthropic models reject).
- **Token counting** is a 4-chars-per-token estimate; budgets are enforced on that estimate so no tokenizer download
  is needed. Swap `app/context/tokens.py` for a provider tokenizer if exact accounting matters.
- `prompt_version` is recorded on every memory (§17) as a dedicated property.

## Production notes

Not in this demo, needed before real traffic: **authentication** (every endpoint trusts the `user_id` in the path or
body; put the service behind a gateway that injects a verified user id, or add an API-key/JWT dependency on the router);
a Redis/Postgres checkpointer with a TTL instead of `InMemorySaver`; a queue (Celery/Arq/SQS) for the memory graph
instead of `BackgroundTasks`; a nightly job that marks memories `expired` (retrieval already excludes them at read time);
LangSmith/Langfuse tracing around the graphs; provider keys via secrets, not the image. See §17 of the architecture doc.
