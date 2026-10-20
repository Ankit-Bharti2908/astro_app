# Testing and evaluation

| Layer | Command | Needs | What it shows |
|---|---|---|---|
| Unit and integration tests | `pytest -q` | nothing | The pipeline and rules behave as designed (42 tests, ~5 s) |
| Memory accuracy | `python -m eval.run_eval` | API key | The real model stores the right facts from 30 labelled messages |
| Scenario journeys | `python -m eval.run_scenarios` | API key, running API | The brain holds up over whole conversations with real models |

Tests use a scripted LLM (`tests/fake_llm.py`) and the in-memory brain, which implement the same interfaces as
LiteLLM and Neo4j, so every graph path runs deterministically.

## The eight required scenarios

| # | Scenario | Test | Asserts |
|---|---|---|---|
| 1 | New user | `test_new_user_gets_generic_answer_without_memories` | A reply, and no memories in context |
| 2 | Create a memory | `test_create_long_term_memory_and_profile` | `career_change` stored with `target_year: 2027`; profile and sign set |
| 3 | Retrieve a memory | `test_retrieve_memory_in_career_answer` | A career question puts the goal in `context_used` |
| 4 | Follow-up | `test_followup_uses_short_term_context_only` | Route `followup`, no retrieval, nothing written |
| 5 | New session | `test_new_session_recalls_goal_from_brain` | A fresh session still recalls the goal |
| 6 | Irrelevant memory | `test_irrelevant_memory_not_in_career_context` | A health memory stays out of a career answer |
| 7 | Correction | `test_correction_supersedes_old_memory` | Old memory superseded and linked; new one active |
| 8 | Missing information | `test_missing_profile_answers_generically` | General answer that asks only for what's missing |

Other tests cover: learning before the reply, refusing bad birth details, not storing hypotheticals, temporary chats,
Neo4j and LLM outages, expiry, remember and forget (including "a question can never delete a memory"), the tool-loop
cap, token budgets, languages, chat summaries, user isolation, and the HTTP endpoints.

## Memory accuracy

The real extractor (gpt-4o-mini) runs over 30 labelled messages, 15 that should be stored and 15 that shouldn't,
and the storage threshold is swept:

| Threshold | Precision | Recall |
|---|---|---|
| 0.3 – 0.6 | 100% | 100% |
| 0.7 – 0.9 | 100% | 87% |

0.6 is the highest threshold that keeps every fact that should be stored. Label accuracy: certainty 100%, subject
100%, type 93%. Most "missed" facts were stored under a different but reasonable slot name (`music_preference`
instead of `classical_music`), which points to a known weakness: slot names aren't stable. Results are in
[`eval/results/latest.json`](../eval/results/latest.json).

## Scenario journeys

Ten short conversations in [`eval/scenarios.yaml`](../eval/scenarios.yaml). After each message the runner checks
the API's report of the turn (route, language, which memories reached the prompt) and the brain's contents. Where
only the reply's wording can show a defect, an LLM reviewer also checks one statement about the reply.

**Latest run: 6 pass, 4 known issues, 0 regressions** (68 checks, gpt-4o-mini, Neo4j).
Full report: [`eval/results/scenarios.md`](../eval/results/scenarios.md).

| Known issue | Cause |
|---|---|
| B01: "I might not switch after all" is ignored | A hedged statement scores exactly at the threshold, and reconcile won't let a hedged statement replace a firm one |
| B04: one plan stored twice | The extractor names the same slot differently on each turn, and reconcile matches by name |
| B07: small talk denies stored memories | The small-talk route sees no memories but still answers questions about them |
| B08: a diagnosis is repeated back | It isn't stored, but it is still in the chat transcript the reply reads |

## Is the brain improving answers?

The principle: **keep the questions fixed and change only the brain.** A user with no history, or a temporary chat,
gives the "brain off" side for free.

| Question | Measure |
|---|---|
| Are the right facts stored? | Precision and recall on the labelled set (above) |
| Does the right memory reach the prompt? | Recall over expected `memory:*` keys in `context_used`, which needs no judge |
| Does irrelevant memory leak in? | The same check inverted; this matters because leaks fail silently |
| Is the answer more personal? | Same questions with the brain on and off, judged blind, reported as win / tie / loss |
| Does it contradict itself? | After "actually this year, not next", any later mention of the old year is a defect |
| Does memory survive a restart? | Restart the container, then recall in a new session |
| What does it cost? | `context_tokens` per reply, plus latency per node |

Two cautions: an LLM judge grading its own system is generous, so a few dozen human-labelled turns are worth more
than many judged ones. And answers can improve because the model improved, not the brain, which is why every
comparison is paired on the same question.
