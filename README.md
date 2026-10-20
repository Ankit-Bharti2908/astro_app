# MyNaksh Shared Brain

Personalised astrology chat with **short-term session memory** (LangGraph checkpointer), a **persistent graph-based
Shared Brain** (Neo4j) and **LLM-driven responses** routed through LiteLLM.

Stack: Python 3.11+ · FastAPI · LangGraph · Neo4j · LiteLLM · Pydantic.

| Doc | What is in it |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | The design: graphs, context layers, brain schema, memory strategy |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Key decisions and trade-offs — what each one cost |
| [`docs/API.md`](docs/API.md) | Sample requests and responses, captured from a live run |
| [`docs/TESTING.md`](docs/TESTING.md) | The required scenarios → tests, and the evaluation plan |

This README covers running, configuring and operating it.

```
POST /chat ──► response graph ──► learn ──► memory graph ──► Neo4j      (the write, in-turn)
                                    │        extract → score → filter → reconcile‖ → write
                                    └──► reply, composed from what was just written
```

## Quick start

### 1. Tests only — no keys, no database

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q                                           # scripted LLM + in-memory brain
```

### 2. Full stack with Docker (Neo4j + API)

```bash
cp .env.example .env              # add OPENAI_API_KEY and/or ANTHROPIC_API_KEY
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
| `NEO4J_CONNECT_TIMEOUT_S` / `NEO4J_MAX_RETRY_TIME_S` | `3` / `5` | Fail fast so an outage degrades instead of stalling |
| `STRONG_MODEL` | `anthropic/claude-opus-5-5` | LiteLLM model for `answer`, `memory_recall`, `followup` |
| `SMALL_MODEL` | `anthropic/claude-haiku-5-5` | LiteLLM model for `analyze`, `extract`, `reconcile`, summarisation, smalltalk |
| `FALLBACK_MODEL` | *(empty)* | Optional fallback provider/model, e.g. `openai/gpt-4o-mini`; the Router falls back to it when the primary fails |
| `LLM_TIMEOUT_S` / `LLM_NUM_RETRIES` | `30` / `1` | Per-call timeout and Router retries before fallback |
| `LLM_TEMPERATURE_STRUCTURED` / `LLM_TEMPERATURE_REPLY` | `0.0` / `0.4` | Sampling temperature for decision tasks (`analyze`, `extract`, `reconcile`, `summarize`) and for user-facing replies. Decisions are pinned to 0 so routing and conflict resolution are reproducible |
| `CHECKPOINTER` / `CHECKPOINTER_URL` | `memory` | `memory` (`InMemorySaver`) or `postgres` (`pip install -e ".[postgres]"`) |
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
| `GET` | `/users/{user_id}/memory-runs?session_id=&since=` | What the memory graph did, per turn |
| `GET` | `/health` | Liveness + brain/LLM status |
| `GET` | `/ui/` | The orchestration console (§ below) |

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
  "tools_used": [{"name": "recall_memories", "args": {"life_area": "career", "query": "job switch"}, "result": "…"}],
  "degraded": []
}
```

`ephemeral: true` reads memory but writes nothing. Every other turn writes *before* it replies, so the write is
already durable when the response returns and the reply is composed on top of it. `degraded` reports
`brain_unavailable` / `llm_unavailable` instead of failing the request.

## Orchestration console

`http://localhost:8000/ui/` (also served at `/`) is a single static page — no build step, no dependencies — that
chats against `/chat` and shows what each turn actually did:

- **Response graph** — the route the turn took (`answer` / `followup` / `memory_recall` / `memory_cmd` /
  `smalltalk`), the detected language and any `degraded` flag.
- **Context assembled** — a bar per layer against its token budget, plus the `context_used` keys, so it is visible
  that a follow-up carries no memories and a career question carries `memory:career_change`.
- **Agent tools** — each `recall_memories` / `get_astro_profile` / `search_past_sessions` call with its arguments
  and the result the model got back, or a note that the pre-loaded context was enough.
- **Memory graph** — the write pipeline for that turn: extracted candidates → what the filter kept or dropped
  and why → the reconcile action per candidate (`ADD` / `UPDATE` / `SUPERSEDE` / `NOOP` / `PROFILE` / `SKIP`) with
  its reason → what was written. It polls `/users/{id}/memory-runs`, which is complete by the time the reply lands.
- **Shared brain** — every memory for the user, superseded ones greyed out, with confidence, importance and
  retrieval score.

Click any earlier reply to re-inspect that turn. The preset buttons replay the walkthrough from the architecture doc.

## Project layout

```
app/
├── main.py                 # app factory, dependency container, lifespan
├── config.py               # Settings
├── api/                    # schemas.py, routes.py
├── graphs/
│   ├── state.py            # ChatState, MemoryState
│   ├── response_graph.py   # load_session → analyze → learn → {answer_agent ⇄ fetch_context | followup | memory_recall | memory_cmd | smalltalk} → maybe_compact
│   ├── memory_graph.py     # extract → score → filter → Send(reconcile)* → write
│   └── nodes/              # one module per node (+ memory_ops.py: shared pipeline primitives)
├── tools/brain_tools.py    # recall_memories · get_astro_profile · search_past_sessions (read-only ToolNode tools)
├── brain/                  # models.py · scoring.py · repository.py (protocol) · neo4j_repo.py · memory_repo.py
├── llm/                    # provider.py (protocol + structured-output retry) · litellm_provider.py
├── context/builder.py      # L0–L3 + ST layers with token budgets
├── profile/zodiac.py       # sun-sign stub
├── prompts/templates.py    # versioned prompts (PROMPT_VERSION recorded on every memory)
└── static/index.html       # the orchestration console served at /ui
tests/                      # see below
eval/                       # labelled set · storage-accuracy sweep · scenarios.yaml + runner
docs/                       # ARCHITECTURE · DECISIONS · API · TESTING
docker-compose.yml · Dockerfile · demo.http
```

## Testing

```bash
pytest -q                   # 42 tests, no network, no keys, no database needed: ~3 s
ruff check app tests eval
python -m eval.run_eval          # memory-accuracy eval (needs a provider key)
python -m eval.run_scenarios     # 10 multi-turn journeys against a running API, LLM-reviewed
```

| Where | What it covers |
|---|---|
| `test_chat_scenarios.py` | The full turn: new user, create/retrieve/correct memories, follow-up without retrieval, cross-session recall, irrelevant memories excluded, brain down, LLM down, Hinglish, bounded tool loop, expiry, remember/forget, ephemeral, compaction, write-before-reply |
| `test_basics.py` | Building blocks: routing rules and language detection, confidence/importance/decay maths, context layers inside their budgets, profile validation, structured-output retry, HTTP CRUD + isolation |

The eight required scenarios mapped to the tests that prove them, and how I would evaluate whether the brain is
actually improving answers: [`docs/TESTING.md`](docs/TESTING.md).

With a provider key set, [`demo.http`](demo.http) is the live check: it replays the §13 walkthrough against real
models.

## How a turn works

1. **`load_session`** loads L0 (date, timezone, language), L1 (profile card, naming the slots still missing) and L2
   (memory index: one line per life area). The checkpointer (thread = `user_id:session_id`) has already restored the
   session messages and summary. Neo4j failure → `degraded: ["brain_unavailable"]`, continue on short-term context.
2. **`analyze`** asks the small model for route, life areas and language, with structured output. Every turn asks:
   the keyword short-circuit that used to catch greetings and follow-ups is gone, because the same word lists routed
   "Remove obstacles from my career" into a delete. Language precedence: explicit request → message → profile
   preference.
3. **`learn`** runs the **memory graph** *before* any reply node, for `answer` / `followup` / `memory_recall`
   (never for smalltalk, memory commands or ephemeral chats): the small model extracts `MemoryCandidate`s (features
   only, canonical English, relative dates resolved), rules score and filter them, one `reconcile` runs per candidate
   via `Send`, and `write` applies ADD / UPDATE / SUPERSEDE / NOOP(reinforce) / PROFILE with `SUPERSEDES` and
   `EXTRACTED_FROM` links. The reply is then composed from the refreshed profile, and any slot the validator refused
   is named in its prompt so it can say *which part* it could not use instead of restating it as fact (§11).
   Costs ~1.4 s/turn; it buys a reply that cannot contradict the store, and a next turn that cannot race the write.
4. **`answer_agent`** prefetches the top-k memories for the classified life areas (importance × confidence ×
   recency), asks the strong model, and executes at most 2 rounds of read-only tool calls via the `fetch_context`
   ToolNode. Memories used get `last_used_at` reinforced — except ones this same turn wrote. Tool traffic is dropped
   from the persisted history.
5. **`followup`** / **`memory_recall`** / **`memory_cmd`** / **`smalltalk`** as in §4; `memory_cmd` writes inline
   (remember = extract with `user_asked_to_remember`, forget = `status: deleted`). A forget's targets are named by
   the model from the memories themselves, and code bounds it to ids it was shown (§7.6).
6. **`maybe_compact`** incrementally summarises older turns when the session exceeds the budget, removes them with
   `RemoveMessage`, persists the summary on the `Session` node (searchable by `search_past_sessions`).

LLM nodes carry `RetryPolicy(max_attempts=2)` and a node-level `error_handler` that returns a canned reply with
`degraded: ["llm_unavailable"]`; structured-output failures retry once with the validation error and then skip.

## Implementation notes (where this goes beyond or refines the doc)

- **Checkpointer thread id is `user_id:session_id`** (the doc says `session_id`): a session id alone could let one
  user resume another's session. Likewise Neo4j `Session` nodes are keyed per user (`key = user_id:session_id`).
  Because `:` joins the halves, both are restricted to `[A-Za-z0-9_.@-]` — otherwise a `session_id` of
  `victim:s1` addresses another user's thread. Omit `session_id` and the server mints a UUID4 and returns it.
- **Inadmissible profile values are refused, not repaired.** A chart is computed from `dob` / `tob` /
  `birth_place`, so a wrong value is worse than a missing one. The extraction prompt asks the model to emit
  nothing it had to repair, and `apply_profile_patch` — the one write path shared by the API and the memory
  graph — validates again, since a prompt is a request and not a guarantee. A refusal shows in the
  memory-graph trace as `SKIP` with the reason, rather than disappearing.
- **Extraction gets the previous assistant turn as *reference-only* context** so corrections such as "actually this
  year, not next" resolve against what was being discussed. The prompt forbids extracting facts from it; assistant
  text is still never a source of user facts.
- **Profile facts** (`name`, `dob`, `tob`, `birth_place`, `preferred_language`) are written to the `User` node (zodiac
  recomputed) rather than stored as `Memory` nodes; other `profile_fact`s are single-valued memories.
- **Never-store** is a label the extractor emits (`sensitive_category`), applied in the deterministic filter. A
  keyword backstop under it was removed: a term list recognises only its own terms, so it caught `diabetes` and not
  "my sugar is high", while reading like a guarantee. The prompt defines the categories by substance; the labelled
  eval measures how well they are applied.
- **Structured output** is provider-neutral: schema in the prompt, parse, validate, retry once with the error. This
  avoids provider-specific forced-tool JSON modes (which newer Anthropic models reject).
- **Token counting** is a 4-chars-per-token estimate; budgets are enforced on that estimate so no tokenizer download
  is needed. Swap `app/context/tokens.py` for a provider tokenizer if exact accounting matters.
- **Prompts are XML-structured and versioned** (`app/prompts/templates.py`, `PROMPT_VERSION = "v6"`). Tagged sections
  (`<task>`, `<rules>`, `<examples>`, …) let one rule be revised without disturbing the rest. Prompt text is
  English-only; the reply language is an instruction resolved against the meta layer.
- **Routing lives in the prompt, not in code.** `ANALYZE_SYSTEM` draws every line: stating something (`answer`) vs
  asking what is stored (`memory_recall`), continuing a topic (`answer`) vs referring back to a reply (`followup`),
  and what counts as an instruction about memory. There is no keyword short-circuit — `app/graphs/rules.py` was
  deleted. Two things in `analyze.py` are not judgements and stay in code: a `followup` needs a previous reply to
  exist, and a message with no Devanagari in it is not written in Devanagari, whatever the model labelled it.
- `prompt_version` is recorded on every memory (§17) as a dedicated property.

## Production notes

Use the Postgres checkpointer with a TTL; add a nightly job that marks memories `expired` (retrieval already
excludes them at read time); wire LangSmith/Langfuse tracing around the graphs; geocode `birth_place` rather than
only shape-checking it; keep provider keys out of the image (`.env`/secrets). Moving the memory write off the turn
would buy back ~1.4 s, but it reintroduces the race it was moved in to fix, so it needs a per-user write lock or a
read-your-writes cache — not just a queue. See §17 of the architecture doc.
