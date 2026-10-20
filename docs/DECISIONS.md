# Design decisions

Each entry gives the choice, why it was made, and what it costs. How each one works is in
[ARCHITECTURE](ARCHITECTURE.md).

**1. Two graphs: one for replying, one for remembering.** The reply path can only read the brain; only the memory
path writes, behind rule-based filters. So nothing the answering model invents can become a stored fact, and that
is enforced by structure rather than by a prompt. *Cost:* two graphs to keep in step. *Alternative:* one linear
pipeline, simpler but with the write rule living only in a prompt.

**2. The memory write happens before the reply.** The reply is written from the updated profile, so it can't
contradict what was just stored, and a fast second message always sees the first one's facts. *Cost:* about 1.4 s
per turn (4.1 s → 5.4 s, measured). *Alternative:* write in the background and add a per-user lock plus a
read-your-writes cache, which is more machinery for the same guarantee.

**3. Neo4j rather than a vector store.** What makes this a brain rather than a cache is relationships: what a fact
replaced, which chat it came from, which life area it belongs to. Retrieval walks one life area instead of scanning
everything. *Cost:* no semantic similarity, so a question worded far from the stored fact can miss it. *Next step:*
embeddings for ranking within a life area.

**4. Rules compute the scores; the model only labels.** The model answers "was this firm or hedged? about the user or
someone else?", and fixed rules turn that into confidence and importance. Scores a model writes for itself are poorly
calibrated and change between identical runs, and the storage threshold depends on them. *Cost:* the weights are
hand-set, so the labelled eval checks them.

**5. Retrieval always includes the `general` area.** The extractor files anything it can't place under `general`.
Without this, one misfiled fact would be invisible forever. *Cost:* a few extra memories in some prompts.

**6. Impossible birth details are refused, not repaired.** A chart built from a wrong date is a confident reading of
a moment that never happened, which is worse than no chart. One validator guards both ways in (the `/users` API
and chat), because a prompt asking the model not to guess is a request, not a guarantee. *Cost:* the user is asked
again.

**7. No keyword rules where a judgement about meaning is needed.** Routing and "which memory does this forget mean?"
were word lists, and word lists misread sentences (see lessons below). Both are now model decisions; code keeps only
facts and limits, such as "a follow-up needs a previous reply" and "a forget may touch only IDs it was shown".
*Cost:* one small-model call on every turn, and routes for borderline messages can vary between runs.

**8. Birth-detail slots are chosen by key, not by the type the model guessed.** The same `birth_place` once arrived
as a life event on one turn and a profile fact on the next, and only one of those paths was validated. The key now
decides. *Cost:* none.

**9. Decay is computed when memories are read.** There's no scheduler, so nothing can drift or fail silently.
*Cost:* nothing is archived, so storage only grows. Production adds a nightly archive job.

**10. English prompts and English memories.** The reply language is an instruction, not a translated prompt, so
there is one prompt to maintain, and a fact said in Hindi answers an English question later. *Cost:* a translation
step at extraction, and the canned fallback replies are English only.

**11. Degrade instead of failing.** If Neo4j is down the reply uses the current chat; if the LLM is down, a fallback
model and then a canned reply. The response always says so in `degraded`. *Cost:* a caller that would rather retry
has to check the flag.

## Lessons from live testing

These came from running the system against real models. Each one changed the design.

| What happened | What changed |
|---|---|
| The store rejected "65 May 2200", but the reply, written before the write, said *"Aapka janm 65 May 2200 ko…"* | Decision 2: learn before replying, and tell the reply what was refused |
| A user who typed again within ~2 s was told their details weren't saved | Decision 2 |
| An error message containing `YYYY-MM-DD` led the model to show an example birthday, which the next turn stored as real | Prompts now ban example values |
| The model "repaired" an impossible birth time into a plausible one | Decision 6: a code validator, not just a prompt rule |
| *"Remove obstacles from my career, what should I do?"* replied *"Done — I've forgotten: switch jobs."* | Decision 7: all keyword routing removed; forget targets chosen by meaning |
| `birth_place` arrived with two different types on consecutive turns, and one path skipped validation | Decision 8 |
| A career goal was filed under `general` and a career question couldn't find it | Decision 5 |
| The model labelled romanised Hindi as Devanagari about one time in four | One byte-level check kept in code |
| At default temperature the same correction was added on some runs and replaced on others | Decision tasks run at temperature 0 |
| The LLM reviewer marked correct replies wrong twice, adding criteria of its own | Narrower reviewer instructions; reviewer results are treated as a weak signal |
