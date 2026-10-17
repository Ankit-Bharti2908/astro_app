"""Live provider checks. Opt in with LIVE_LLM=1 and the provider keys for STRONG_MODEL / SMALL_MODEL.

    LIVE_LLM=1 ANTHROPIC_API_KEY=... pytest tests/live -m live -q
"""

from __future__ import annotations

import os

import pytest

from app.config import Settings

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.getenv("LIVE_LLM") != "1", reason="set LIVE_LLM=1 to hit real providers")]


@pytest.fixture(scope="module")
def live_provider():
    from app.llm.litellm_provider import LiteLLMProvider

    return LiteLLMProvider(Settings(llm_backend="litellm"))


async def test_live_every_task(live_provider):
    from scripts.live_llm_check import run_all

    results = await run_all(live_provider)
    failed = {k: v for k, v in results.items() if not v["ok"]}
    assert not failed, failed
