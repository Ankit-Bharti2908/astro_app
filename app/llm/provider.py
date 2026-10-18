"""LLMProvider protocol, message/tool types and the shared structured-output logic (§9)."""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

# task → model tier (§9)
TASK_TIERS: dict[str, str] = {
    "analyze": "small",
    "extract": "small",
    "reconcile": "small",
    "summarize": "small",
    "memory_cmd": "small",
    "smalltalk": "small",
    "answer": "strong",
    "memory_recall": "strong",
    "followup": "strong",
}

# Tasks that return a decision or a JSON record rather than prose. These are sampled at
# `llm_temperature_structured` (0 by default) so routing and reconciliation are reproducible:
# at the provider default of 1.0 the same correction would sometimes ADD and sometimes
# SUPERSEDE, and the same opening message would sometimes misroute.
STRUCTURED_TASKS: frozenset[str] = frozenset({"analyze", "extract", "reconcile", "summarize"})

Role = Literal["system", "user", "assistant", "tool"]


class LLMError(Exception):
    """Base class for provider errors."""


class LLMUnavailable(LLMError):
    """Primary and fallback providers both failed (or no provider is configured)."""


class StructuredOutputError(LLMError):
    """The model did not return valid JSON for the schema after a retry."""


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class LLMMessage:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str | None = None


T = TypeVar("T", bound=BaseModel)


class LLMProvider(Protocol):
    async def complete(
        self,
        task: str,
        messages: list[LLMMessage],
        *,
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...

    async def structured(self, task: str, messages: list[LLMMessage], schema: type[T]) -> T: ...

    async def healthcheck(self) -> dict[str, Any]: ...


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of a model reply (tolerates fences and prose)."""
    text = text.strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON object found in model output")


def json_instructions(schema: type[BaseModel]) -> str:
    return (
        "Respond with a single JSON object only — no prose, no markdown fences. "
        "It must validate against this JSON schema:\n"
        + json.dumps(schema.model_json_schema(), separators=(",", ":"))
    )


def with_json_instructions(messages: list[LLMMessage], schema: type[BaseModel]) -> list[LLMMessage]:
    instr = json_instructions(schema)
    out = [LLMMessage(m.role, m.content, list(m.tool_calls), m.tool_call_id, m.name) for m in messages]
    for m in out:
        if m.role == "system":
            m.content = f"{m.content}\n\n{instr}"
            return out
    return [LLMMessage("system", instr), *out]


class BaseLLMProvider(ABC):
    """Shared behaviour: structured output = JSON-in-prompt + validation + one retry."""

    structured_retries: int = 1

    @abstractmethod
    async def complete(
        self,
        task: str,
        messages: list[LLMMessage],
        *,
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...

    async def healthcheck(self) -> dict[str, Any]:
        return {"backend": type(self).__name__, "ok": True}

    async def structured(self, task: str, messages: list[LLMMessage], schema: type[T]) -> T:
        prompt = with_json_instructions(messages, schema)
        last_error: Exception | None = None
        for attempt in range(self.structured_retries + 1):
            response = await self.complete(task, prompt, max_tokens=None)
            try:
                data = extract_json(response.content)
                return schema.model_validate(data)
            except (ValueError, ValidationError) as exc:
                last_error = exc
                log.warning("structured output invalid for task=%s attempt=%d: %s", task, attempt, exc)
                # §9: retry once with the validation error in the prompt
                prompt = [
                    *prompt,
                    LLMMessage("assistant", response.content),
                    LLMMessage(
                        "user",
                        "Your previous reply was not valid for the schema. Error:\n"
                        f"{exc}\n\nReturn only the corrected JSON object.",
                    ),
                ]
        raise StructuredOutputError(str(last_error))
