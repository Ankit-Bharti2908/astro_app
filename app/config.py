"""Application settings (environment driven, with sane local defaults)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Brain
    brain_backend: Literal["neo4j", "memory"] = "memory"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "neo4j"
    neo4j_database: str = "neo4j"

    # LLM
    strong_model: str = "anthropic/claude-opus-5-5"
    small_model: str = "anthropic/claude-haiku-5-5"
    fallback_model: str = ""
    llm_timeout_s: float = 30.0
    llm_num_retries: int = 1
    llm_json_mode: bool = False  # ask providers for native JSON mode when they support it
    llm_max_tokens_answer: int = 1024
    llm_max_tokens_small: int = 800
    # Routing, extraction and reconciliation are decisions, not prose: sample them at 0 so the
    # same message always classifies the same way. Replies keep a little warmth.
    llm_temperature_structured: float = 0.0
    llm_temperature_reply: float = 0.4

    # Short-term memory
    checkpointer: Literal["memory", "postgres"] = "memory"
    checkpointer_url: str = ""

    # Context budgets (tokens) — §3
    budget_meta: int = 30
    budget_profile: int = 150
    budget_index: int = 100
    budget_memories: int = 600
    budget_summary: int = 300
    budget_turns: int = 1200

    # Compaction — §8
    compact_threshold_tokens: int = 1200
    keep_last_messages: int = 6

    # Agent loop — §4
    max_tool_rounds: int = 2
    recursion_limit: int = 25

    # Memory strategy — §7
    confidence_threshold: float = 0.6
    top_k_memories: int = 5

    default_timezone: str = "Asia/Kolkata"
    log_level: str = "INFO"
    prompt_version: str = Field(default="v1")


def get_settings(**overrides) -> Settings:
    return Settings(**overrides)
