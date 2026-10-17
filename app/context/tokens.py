"""Cheap, dependency-free token estimation (≈4 chars/token) used for prompt budgets."""

from __future__ import annotations

CHARS_PER_TOKEN = 4


def count_tokens(text: str | None) -> int:
    if not text:
        return 0
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def truncate_to_budget(text: str, budget_tokens: int) -> str:
    if count_tokens(text) <= budget_tokens:
        return text
    limit = max(0, budget_tokens * CHARS_PER_TOKEN - 1)
    return text[:limit].rstrip() + "…"
