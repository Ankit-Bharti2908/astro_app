# Testing and evaluation

```bash
pytest -q                      # 45 tests · ~3 s · no API key, no network, no database
ruff check app tests eval
```

Tests run against a scripted `LLMProvider` (`tests/fake_llm.py`), the in-memory brain repository and
`InMemorySaver`, so the whole graph is exercised deterministically. Both substitutes implement the same protocols as
the real Neo4j repository and the LiteLLM provider.

With a provider key set, [`demo.http`](../demo.http) is the live check against real models.

---

## The eight required scenarios

| # | Scenario | Test | What it actually asserts |
|---|---|---|---|
| 1 | New user | `test_new_user_gets_generic_answer_without_memories` | A reply comes back and `context_used` carries no memories |
| 2 | Create a long-term memory | `test_create_long_term_memory_and_profile` | `career_change` stored with `target_year: 2027`, confidence 0.9; profile + zodiac set |
| 3 | Retrieve a memory | `test_retrieve_memory_in_career_answer` | A career question puts `memory:career_change` in `context_used` |
| 4 | Follow-up | `test_followup_uses_short_term_context_only` | Route `followup`, no retrieval, nothing written |
| 5 | New session, previous information | `test_new_session_recalls_goal_from_brain` | A fresh `session_id` (empty checkpointer) still recalls the goal from Neo4j |
| 6 | Irrelevant memory | `test_irrelevant_memory_not_in_career_context` | A health memory never appears in a career answer |
| 7 | Correcting existing information | `test_correction_supersedes_old_memory` | Old memory `superseded`, new one active, `SUPERSEDES` link present |
| 8 | Missing user information | `test_missing_profile_answers_generically` | Generic answer, no error, asks only for what is missing |

## Beyond the required eight

| Area | Tests |
|---|---|
| **Write/read ordering** | `test_the_write_is_durable_before_the_reply_returns` · `test_a_refused_birth_detail_is_told_to_the_reply` |
| **Invalid input** | `test_inadmissible_profile_values_are_refused_not_repaired` · `test_a_profile_slot_is_decided_by_its_key_not_the_guessed_type` |
| **What must not be stored** | `test_hypothetical_is_not_stored` · `test_ephemeral_turn_mutates_nothing` · `test_ephemeral_reads_but_never_writes` |
| **Degradation** | `test_brain_down_degrades_gracefully` · `test_llm_down_returns_canned_reply_with_degraded_flag` · `test_structured_output_retries_once_then_gives_up` |
| **Retrieval correctness** | `test_general_bucket_is_never_invisible_to_a_narrowed_recall` · `test_time_bound_memory_expires_from_retrieval` · `test_scoring_and_ranking` |
| **Memory commands** | `test_memory_cmd_remember_and_forget` · `test_untargeted_forget_never_wipes_the_brain` · `test_a_bare_fact_is_answered_not_merely_acknowledged` |
| **Agent loop** | `test_agent_tool_loop_is_bounded` · `test_tool_loop_honours_round_cap_even_if_model_keeps_asking` |
| **Context budgets** | `test_context_layers_stay_within_budget_and_report_usage` · `test_profile_layer_names_only_the_gaps` |
| **Multilingual** | `test_hinglish_detected_and_memory_stored_in_english` · `test_language_precedence_explicit_then_message_then_profile` |
| **Session handling** | `test_compaction_summarizes_and_persists_session` · `test_ids_cannot_forge_the_composite_thread_key` |
| **HTTP layer** | `test_user_api_crud_and_validation` · `test_memory_api_lists_ranked_and_soft_deletes` · `test_health_and_chat_response_shape` · `test_console_page_and_memory_run_trace` |

---

## Evaluation

### What ships: memory accuracy

```bash
python -m eval.run_eval        # needs a provider key; results land in eval/results/
```

Runs the real extraction model over ~30 labelled messages and reports precision/recall of the **store / don't-store**
decision swept across confidence thresholds — which is what validates the 0.6 default instead of asserting it — plus
per-label accuracy for `certainty` / `subject` / `type`, and the missed and unexpected candidates themselves.

### What I'd measure next, and how

This is the one dimension cheap enough to measure offline. The rest need a live pair of runs, and the design is the
same throughout: **hold the questions fixed and vary only the brain**, because without an A/B there is nothing to
attribute an improvement to. No special build is needed — `ephemeral: true` reads but never writes, and a fresh
`user_id` has no brain at all, so "brain off" is just a second run.

| Dimension | Measure | Method |
|---|---|---|
| **Context relevance** | Of the memories injected, how many belonged? | `context_used` is on every reply, so this is countable, not judged: label the expected keys per question, report precision@k |
| **Irrelevant context** | The same number read as a failure rate | The false-positive half. It matters separately because the failure is silent — a wrong memory does not error, it just makes the answer subtly wrong, and watching it stops recall from being bought with noise |
| **Personalization** | Is the answer actually about *this* user? | Same 15 questions, brain on vs off, shuffled and scored blind by an LLM judge on three axes: uses a stored fact, invents none, stays specific. Report win/tie/loss, not a mean |
| **Consistency** | Does it contradict itself across sessions? | Replay multi-session scripts, check each reply against the memories active at that turn. The correction case is the sharp one: after "actually this year, not next", any later mention of 2027 is a defect |
| **Persistence** | Does it survive the process? | Write, restart the container, assert recall in a new session. Scenario 5 covers the session boundary; this covers the storage boundary |
| **Cost / latency** | What is personalization costing? | `context_tokens` per layer is already returned; pair it with per-node latency. A 5% quality gain at triple the prompt should show up as the bad trade it is |

Two caveats I would report rather than bury. **An LLM judge scoring its own system is a generous judge** — the blind
shuffle is the minimum guard, and two dozen human-labelled turns are worth more than a thousand judged ones. And
**aggregate quality can rise while the brain gets worse**, because better answers also come from the model simply
being good; that is why every dimension above is a *paired* comparison on the same question, never an absolute score.
