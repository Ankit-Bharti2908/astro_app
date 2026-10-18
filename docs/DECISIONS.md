# Key design decisions

Each entry: what was chosen, what it rules out, and what it cost. Mechanics are in
[ARCHITECTURE.md](ARCHITECTURE.md).

---

### 1. Two graphs, not one service

A read path (response) and a write path (memory) with separate state. The agent that talks to the user holds
**read-only** tools, so nothing it invents can reach long-term memory.

*Costs:* two graphs to keep in step, and a `Deps` wiring step so the response graph can invoke the memory graph.
*Alternative:* one linear pipeline — simpler, but then "the model may write" is a prompt rule rather than a
structural fact.

### 2. The memory write runs inside the turn, before the reply

Originally a `BackgroundTask` after the response. That made the reply and the store two independent judges of the
same sentence, and they disagreed in both directions:

- the store refused `"65 may 2200"`; the reply, never told, answered *"Aapka janm 65 May 2200 ko…"*
- the write landed ~2 s after the response, so a fast second turn read a stale profile and said the details
  weren't saved

Learning first produces one verdict. The reply is composed from the post-write profile, and refused values are named
in the prompt.

*Costs:* **+1.38 s per turn** (4.06 s → 5.44 s, measured). Recoverable later by overlapping reconcile+write with
answer generation — only extraction truly has to precede the answer.
*Alternative:* keep it in the background and add a per-user write lock plus a read-your-writes cache. More moving
parts for the same guarantee.

### 3. Neo4j, not a vector store

The things that make this a *brain* rather than a cache are edges: `SUPERSEDES` (what a fact replaced),
`EXTRACTED_FROM` (where it came from), `ABOUT` (the retrieval axis). Retrieval is a traversal of one life area, not
a scan of everything the user ever said.

*Costs:* no semantic similarity — a question phrased far from the stored wording can miss.
*Alternative / next step:* hybrid — graph traversal for structure, embeddings for recall within a life area.

### 4. Rules produce the scores; the LLM only labels features

The model answers *"was this explicit or hedged? about the user or someone else?"*. `confidence` and `importance`
are then computed. Asking a model for "confidence: 0.85" gives numbers that are poorly calibrated and unstable
between identical runs — and the storage threshold depends on them.

*Costs:* the weights are hand-set, so they need the labelled set to stay honest (`python -m eval.run_eval`).

### 5. Retrieval narrows by life area — and always includes `general`

`general` is where extraction files whatever it could not confidently place. An exact-match read would turn every
misfiling into permanent amnesia: the brain answers "I have nothing on that" while the fact sits one bucket away.
Narrowing is an optimisation, so it may only drop memories that are *definitely* about something else.

### 6. Unusable birth details are refused, never repaired

A chart comes from `dob` / `tob` / `birth_place`, so a plausible-but-wrong value is worse than a blank: it produces a
confident reading of a moment that never happened. One validator, `apply_profile_patch`, guards both write paths
(the typed API and the memory graph).

The extraction prompt already asks for this. It is in code because **a prompt is a request, not a guarantee** —
observed: an impossible clock time silently "repaired" into a plausible one.

*Related:* the refusal reason is user-facing, so it is plain English. An early version leaked
`expected YYYY-MM-DD` into the prompt; the model taught the user the format and invented an example birthday — which
the next turn read back as the user's own. The prompt now bans example values outright.

### 7. No keyword rules anywhere a judgement is being made

A leading verb picked the route, so *"Remove obstacles from my career, what should I do?"* was read as an
instruction about memory; a word-overlap match then picked the target, and `career` appeared in both. The reply:
*"Done — I've forgotten: switch jobs."*

The repair was not a better word list. The first attempt was one — require a memory-referring object, ban question
marks — and that is just a longer list with the same blind spot. So every word list that stood for a judgement went:
routing by leading verb, follow-up and greeting patterns, recall-question patterns, the Hinglish vocabulary, the
life-area keyword tables, and the health/finance term lists under `never_store`. `app/graphs/rules.py` is deleted.
The rules they encoded were not wrong; their form was, and they now live in the prompts, where something can weigh
them against the sentence in front of it.

What stays in code is everything that is not a judgement about meaning:

| Kept | Why it is not a judgement |
|---|---|
| A `followup` needs a previous reply | A fact about the turn, not the sentence |
| No Devanagari ⇒ not written in Devanagari | A fact about the bytes |
| A forget may touch only ids it was shown | A bound |
| `validate_profile_patch` | Does this parse as a date, a time, a place |
| `extract_json`, key slugification, id patterns | Parsing and normalisation |

*Costs, measured rather than assumed:*
- One small-model call on every turn that used to short-circuit — greetings and follow-ups included.
- With the classifier down there is no routing at all; everything falls back to `answer`. Nothing user-facing
  changes, since a degraded turn answers with the same canned reply either way.
- The model is less reliable than a regex at the one sub-task that was genuinely mechanical: it labelled romanised
  Hindi `hi` in about one case in four, against a prompt that spells the rule out. That is why the Devanagari check
  came back — and only that one. Guessing Hinglish from a vocabulary list stays deleted.
- Of five phrasings that used to delete a memory, four no longer reach `memory_cmd` and the fifth reaches it and is
  refused by the target step. Which route a borderline sentence takes still varies run to run; the second layer is
  what makes that safe rather than alarming.

*Related:* decision 6 is the same split stated the other way round. There, code owed a check the prompt could not
guarantee. Here, code owed bounds and had tried to supply meaning instead.

### 8. Profile slots are decided by key, never by the extractor's guessed type

The same `birth_place` arrived as `life_event` on one turn and `profile_fact` on the next — one became a `User`
field, the other a `Memory` node, and the Memory branch never meets the validator. That is how an invalid `tob`
reached the graph. The slot now decides.

### 9. Decay is computed at read time

`score = importance × confidence × exp(-λ × days_since_used)`, evaluated on read. No scheduler, no cron, nothing to
drift.

*Costs:* nothing is ever archived, so the store only grows. Production adds a nightly job to mark rows `expired` —
retrieval already excludes them.

### 10. English-only prompts, canonical-English memories

The reply language is an instruction resolved against the meta layer, not a translated prompt: one prompt to
maintain, no drift between variants. Memories are stored in English, so a fact stated in Hindi answers an English
question later.

*Costs:* a translation step in extraction, and canned fallbacks stay English — they fire exactly when no model is
reachable to translate them.

### 11. Everything degrades

Neo4j down → reply from short-term context with `degraded: ["brain_unavailable"]`. LLM down → fallback provider,
then a canned reply. A memory-graph failure never fails the turn. The client is always told, in `degraded`.

*Costs:* a degraded reply can be worse than an error for a caller that would rather retry — hence the explicit flag
rather than a silent fallback.
