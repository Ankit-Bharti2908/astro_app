"""LiteLLM Router-backed provider: task-based routing, retries and provider fallback (§9)."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import litellm
from litellm import Router

from app.config import Settings
from app.llm.provider import (
    STRUCTURED_TASKS,
    TASK_TIERS,
    BaseLLMProvider,
    LLMMessage,
    LLMResponse,
    LLMUnavailable,
    ToolCall,
    ToolSpec,
)

log = logging.getLogger(__name__)

litellm.suppress_debug_info = True
litellm.drop_params = True  # silently drop params a provider does not support
litellm.telemetry = False

FALLBACK_GROUP = "fallback"


def build_model_list(settings: Settings) -> list[dict[str, Any]]:
    model_list = [
        {"model_name": "strong", "litellm_params": {"model": settings.strong_model}},
        {"model_name": "small", "litellm_params": {"model": settings.small_model}},
    ]
    if settings.fallback_model:
        model_list.append({"model_name": FALLBACK_GROUP, "litellm_params": {"model": settings.fallback_model}})
    return model_list


def to_openai_messages(messages: list[LLMMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "assistant":
            item: dict[str, Any] = {"role": "assistant", "content": m.content or None}
            if m.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.args)},
                    }
                    for tc in m.tool_calls
                ]
                if not m.content:
                    item["content"] = None
            out.append(item)
        elif m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id or "", "content": m.content, **({"name": m.name} if m.name else {})})
        else:
            out.append({"role": m.role, "content": m.content})
    return out


def to_openai_tools(tools: list[ToolSpec] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
        for t in tools
    ]


def parse_response(resp: Any) -> LLMResponse:
    choice = resp.choices[0]
    message = choice.message
    tool_calls: list[ToolCall] = []
    for tc in getattr(message, "tool_calls", None) or []:
        raw_args = tc.function.arguments or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except json.JSONDecodeError:
            args = {"_raw": raw_args}
        tool_calls.append(ToolCall(id=tc.id or f"call_{len(tool_calls)}", name=tc.function.name, args=args))
    usage = getattr(resp, "usage", None)
    usage_dict = {}
    if usage is not None:
        usage_dict = {
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        }
    return LLMResponse(
        content=message.content or "",
        tool_calls=tool_calls,
        model=getattr(resp, "model", "") or "",
        usage=usage_dict,
        finish_reason=getattr(choice, "finish_reason", None),
    )


class LiteLLMProvider(BaseLLMProvider):
    def __init__(
        self,
        settings: Settings,
        *,
        model_list: list[dict[str, Any]] | None = None,
        router_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.settings = settings
        self.model_list = model_list or build_model_list(settings)
        groups = {d["model_name"] for d in self.model_list}
        fallbacks = (
            [{"strong": [FALLBACK_GROUP]}, {"small": [FALLBACK_GROUP]}] if FALLBACK_GROUP in groups else []
        )
        kwargs: dict[str, Any] = dict(
            model_list=self.model_list,
            fallbacks=fallbacks,
            num_retries=settings.llm_num_retries,
            timeout=settings.llm_timeout_s,
            set_verbose=False,
            disable_cooldowns=True,
        )
        kwargs.update(router_kwargs or {})
        self.router = Router(**kwargs)

    def _max_tokens(self, task: str) -> int:
        tier = TASK_TIERS.get(task, "strong")
        return self.settings.llm_max_tokens_answer if tier == "strong" else self.settings.llm_max_tokens_small

    def _temperature(self, task: str) -> float:
        if task in STRUCTURED_TASKS:
            return self.settings.llm_temperature_structured
        return self.settings.llm_temperature_reply

    async def complete(
        self,
        task: str,
        messages: list[LLMMessage],
        *,
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        group = TASK_TIERS.get(task, "strong")
        kwargs: dict[str, Any] = {
            "model": group,
            "messages": to_openai_messages(messages),
            "max_tokens": max_tokens or self._max_tokens(task),
            "temperature": self._temperature(task),
            "metadata": {"task": task},
        }
        oa_tools = to_openai_tools(tools)
        if oa_tools:
            kwargs["tools"] = oa_tools
            kwargs["tool_choice"] = "auto"
        try:
            resp = await self.router.acompletion(**kwargs)
        except Exception as exc:  # LiteLLM raises OpenAI-style exceptions; all end the chain here
            log.warning("LLM call failed task=%s group=%s error=%s: %s", task, group, type(exc).__name__, exc)
            raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc
        parsed = parse_response(resp)
        log.info("LLM ok task=%s model=%s usage=%s tool_calls=%d", task, parsed.model, parsed.usage, len(parsed.tool_calls))
        return parsed

    async def healthcheck(self) -> dict[str, Any]:
        keys = {
            "anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
            "openai": bool(os.getenv("OPENAI_API_KEY")),
            "gemini": bool(os.getenv("GEMINI_API_KEY")),
        }
        return {
            "backend": "litellm",
            "ok": True,
            "models": {d["model_name"]: d["litellm_params"]["model"] for d in self.model_list},
            "provider_keys_present": keys,
        }
