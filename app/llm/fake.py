"""FakeLLM: deterministic, scriptable provider used by the test-suite and `LLM_BACKEND=fake`.

Default behaviour per task is rule-based so the §13 walkthrough runs end-to-end with no API key.
Tests can override any call with `script(task, response)` (FIFO per task) or force failures.
"""

from __future__ import annotations

import calendar
import json
import re
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.graphs.rules import detect_language, explicit_language_request, guess_life_areas
from app.llm.provider import (
    BaseLLMProvider,
    LLMMessage,
    LLMResponse,
    LLMUnavailable,
    ToolSpec,
)

Scripted = LLMResponse | str | dict | list | Callable[[list[LLMMessage], list[ToolSpec] | None], LLMResponse]


@dataclass
class RecordedCall:
    task: str
    messages: list[LLMMessage]
    tools: list[ToolSpec] | None
    response: LLMResponse | None = None
    error: str | None = None


@dataclass
class FakeLLM(BaseLLMProvider):
    calls: list[RecordedCall] = field(default_factory=list)
    fail_tasks: set[str] = field(default_factory=set)
    fail_all: bool = False
    _scripts: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))

    # ---- test API -------------------------------------------------------
    def script(self, task: str, *responses: Scripted) -> None:
        for r in responses:
            self._scripts[task].append(r)

    def calls_for(self, task: str) -> list[RecordedCall]:
        return [c for c in self.calls if c.task == task]

    async def healthcheck(self) -> dict[str, Any]:
        return {"backend": "fake", "ok": not self.fail_all, "calls": len(self.calls)}

    # ---- provider -------------------------------------------------------
    async def complete(
        self,
        task: str,
        messages: list[LLMMessage],
        *,
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        record = RecordedCall(task, messages, tools)
        self.calls.append(record)
        if self.fail_all or task in self.fail_tasks:
            record.error = "scripted failure"
            raise LLMUnavailable(f"fake provider failure for task={task}")
        if self._scripts[task]:
            scripted = self._scripts[task].popleft()
            response = self._materialize(scripted, messages, tools)
        else:
            response = self._default(task, messages, tools)
        response.model = response.model or "fake"
        record.response = response
        return response

    @staticmethod
    def _materialize(scripted: Scripted, messages, tools) -> LLMResponse:
        if isinstance(scripted, LLMResponse):
            return scripted
        if callable(scripted):
            return scripted(messages, tools)
        if isinstance(scripted, (dict, list)):
            return LLMResponse(content=json.dumps(scripted, default=str))
        return LLMResponse(content=str(scripted))

    # ---- default behaviours --------------------------------------------
    def _default(self, task: str, messages: list[LLMMessage], tools) -> LLMResponse:
        system = "\n".join(m.content for m in messages if m.role == "system")
        user = _last_user(messages)
        handler = {
            "analyze": self._analyze,
            "extract": self._extract,
            "reconcile": self._reconcile,
            "summarize": self._summarize,
            "answer": self._answer,
            "followup": self._followup,
            "memory_recall": self._recall,
            "smalltalk": self._smalltalk,
            "memory_cmd": self._extract,
        }.get(task, self._answer)
        return handler(system, user, messages)

    def _analyze(self, system: str, user: str, messages) -> LLMResponse:
        m = re.search(r"^Message: (.*)\Z", user, flags=re.S | re.M)
        message = m.group(1).strip() if m else user
        areas = guess_life_areas(message) or ["general"]
        payload = {
            "route": "answer",
            "life_areas": areas,
            "language": detect_language(message),
            "explicit_language": explicit_language_request(message),
            "reason": "fake",
        }
        return LLMResponse(content=json.dumps(payload))

    def _extract(self, system: str, user: str, messages) -> LLMResponse:
        today = _current_date(system) or date.today()
        quoted = re.search(r'"""(.*?)"""', user, flags=re.S)
        message = quoted.group(1) if quoted else user
        ctx = re.search(r"^assistant: (.*)$", user, flags=re.M)
        context = ctx.group(1) if ctx else ""
        return LLMResponse(content=json.dumps({"candidates": rule_extract(message, today, context=context)}, default=str))

    def _reconcile(self, system: str, user: str, messages) -> LLMResponse:
        existing = _json_block(user, "EXISTING MEMORIES") or []
        candidate = _json_block(user, "NEW CANDIDATE") or {}
        decision = {"action": "ADD", "target_memory_id": None, "merged_attributes": None, "reason": "fake"}
        for mem in existing:
            if mem.get("key") != candidate.get("key"):
                continue
            same_value = (mem.get("value") or "").lower() == (candidate.get("value") or "").lower()
            same_attrs = (mem.get("attributes") or {}) == (candidate.get("attributes") or {})
            if same_value and same_attrs:
                decision = {"action": "NOOP", "target_memory_id": mem["id"], "merged_attributes": None, "reason": "duplicate"}
            elif same_value and not (mem.get("attributes") or {}):
                decision = {
                    "action": "UPDATE",
                    "target_memory_id": mem["id"],
                    "merged_attributes": {**(mem.get("attributes") or {}), **(candidate.get("attributes") or {})},
                    "reason": "adds detail",
                }
            else:
                decision = {"action": "SUPERSEDE", "target_memory_id": mem["id"], "merged_attributes": None, "reason": "newer statement"}
            break
        return LLMResponse(content=json.dumps(decision))

    def _summarize(self, system: str, user: str, messages) -> LLMResponse:
        turns = re.findall(r"^(?:user|assistant): (.+)$", user, flags=re.M)
        gist = "; ".join(t[:60] for t in turns)[:400]
        prev = re.search(r"PREVIOUS SUMMARY:\n(.+?)\n\n", user, flags=re.S)
        prefix = (prev.group(1).strip() + " ") if prev and prev.group(1).strip() not in ("", "(none)") else ""
        return LLMResponse(content=f"{prefix}Summary: {gist}".strip())

    def _answer(self, system: str, user: str, messages) -> LLMResponse:
        lang = _reply_language(system)
        name = _profile_field(system, "Name")
        sign = _profile_field(system, "Sun sign")
        memory_lines = _memory_lines(system)
        tool_facts = [m.content for m in messages if m.role == "tool"]
        parts = [_greeting(lang, name)]
        if sign:
            parts.append(f"As a {sign}, the stars favour steady, deliberate moves.")
        if memory_lines:
            parts.append("I'm keeping in mind: " + "; ".join(_memory_value(line) for line in memory_lines) + ".")
        if tool_facts:
            parts.append("From what I just looked up: " + " | ".join(t[:120] for t in tool_facts))
        if not sign and not memory_lines:
            parts.append("Share your birth date and place and I can be far more specific.")
        parts.append(f"About your question — {user.strip()[:80]} — focus on patience and timing.")
        return LLMResponse(content=" ".join(parts))

    def _followup(self, system: str, user: str, messages) -> LLMResponse:
        prev_ai = [m.content for m in messages if m.role == "assistant" and m.content]
        basis = prev_ai[-1][:120] if prev_ai else "my earlier reading"
        return LLMResponse(content=f"Here's why: it follows from {basis}")

    def _recall(self, system: str, user: str, messages) -> LLMResponse:
        lines = _memory_lines(system) or _memory_lines(user)
        if not lines:
            return LLMResponse(content="I don't have anything stored about that yet.")
        return LLMResponse(content="Here's what I remember: " + "; ".join(_memory_value(line) for line in lines) + ".")

    def _smalltalk(self, system: str, user: str, messages) -> LLMResponse:
        lang = _reply_language(system)
        return LLMResponse(content=f"{_greeting(lang, _profile_field(system, 'Name'))} How can I help you today?")


# ---------------------------------------------------------------------------
# helpers shared with tests
# ---------------------------------------------------------------------------

def _last_user(messages: list[LLMMessage]) -> str:
    for m in reversed(messages):
        if m.role == "user":
            return m.content
    return ""


def _current_date(system: str) -> date | None:
    m = re.search(r"Current date: (\d{4}-\d{2}-\d{2})", system)
    return date.fromisoformat(m.group(1)) if m else None


def _reply_language(system: str) -> str:
    m = re.search(r"Reply language: (\w+)", system)
    return m.group(1) if m else "en"


def _profile_field(system: str, label: str) -> str | None:
    m = re.search(rf"{label}: ([^\n|]+)", system)
    value = m.group(1).strip() if m else None
    return None if value in (None, "unknown", "-") else value


def _memory_lines(text: str) -> list[str]:
    return re.findall(r"^- \[[a-z_]+\] .+$", text, flags=re.M)


def _memory_value(line: str) -> str:
    m = re.match(r"- \[[a-z_]+\] [A-Za-z ]+: (.+?) \(", line)
    return m.group(1) if m else line


def _greeting(lang: str, name: str | None) -> str:
    who = f" {name}" if name else ""
    return {"hi": f"नमस्ते{who}!", "hinglish": f"Namaste{who}!"}.get(lang, f"Hello{who}!")


def _json_block(text: str, label: str) -> Any:
    m = re.search(rf"{label}[^\n]*:\n(.+?)(?:\n\n|\Z)", text, flags=re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})


def _end_of_next_month(today: date) -> date:
    y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return date(y, m, calendar.monthrange(y, m)[1])


def rule_extract(message: str, today: date, context: str = "") -> list[dict[str, Any]]:
    """Tiny rule-based stand-in for the extraction LLM (English + a little Hinglish)."""
    text = message.strip()
    ctx_low = context.lower()
    low = text.lower()
    out: list[dict[str, Any]] = []
    asked = bool(re.match(r"^(please\s+)?(remember|note|save|store|yaad rakh\w*)\b", low))
    body = re.sub(r"^(please\s+)?(remember|note|save|store|yaad rakh\w*)\s+(that\s+)?", "", low) if asked else low
    hypothetical = bool(re.search(r"\b(if i ever|if i were|what if i|suppose i|hypothetically)\b", body))
    hedged = bool(re.search(r"\b(maybe|might|thinking of|thinking about|perhaps|probably|shayad)\b", body))
    certainty = "hypothetical" if hypothetical else ("hedged" if hedged else "explicit")
    other = bool(re.search(r"\bmy (brother|sister|friend|wife|husband|mother|father|mom|dad|son|daughter|cousin|colleague)\b", body))
    subject = "other" if other else "self"

    def add(**kw):
        cand = {
            "attributes": {}, "life_area": "general", "certainty": certainty, "subject": subject,
            "temporality": "current", "user_asked_to_remember": asked, "valid_until": None,
            "sensitive_category": "none",
        }
        cand.update(kw)
        out.append(cand)

    m = re.search(r"\bmy name is ([A-Za-z]+)", text, flags=re.I)
    if m:
        add(type="profile_fact", key="name", value=m.group(1).capitalize())
    m = re.search(r"\bborn (?:on )?(\d{1,2})(?:st|nd|rd|th)? ([A-Za-z]+),? (\d{4})(?: in ([A-Za-z][A-Za-z ]*?))?(?=[.,;]|$| and| planning)", text, flags=re.I)
    if m and m.group(2).lower() in _MONTHS:
        dob = date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
        add(type="profile_fact", key="dob", value=dob.isoformat())
        if m.group(4):
            add(type="profile_fact", key="birth_place", value=m.group(4).strip().title())
    lang = explicit_language_request(text)
    if lang:
        add(type="profile_fact", key="preferred_language", value=lang)

    career_ctx = bool(re.search(r"\b(job|jobs|career|naukri)\b", ctx_low))
    if re.search(r"(switch|switching|change|changing|chang) (my )?jobs?|job switch|naukri badal|new job|quit my job", body) or (
        career_ctx and re.search(r"\b(switch|switching|change|changing|move)\b.*\b(this|next) year\b", body)
    ):
        attrs: dict[str, Any] = {}
        if re.search(r"this year|is saal|not next", body):
            attrs["target_year"] = today.year
        elif re.search(r"next year|agle saal", body):
            attrs["target_year"] = today.year + 1
        else:
            y = re.search(r"\b(20\d{2})\b", body)
            if y:
                attrs["target_year"] = int(y.group(1))
        add(type="goal", key="career_change", value="switch jobs", attributes=attrs, life_area="career", temporality="future")

    m = re.search(r"(?:(?:have|got|have got) (?:an? )?)?([a-z]{2,15})?\s*interview (next month|next week|tomorrow|in \w+)", body)
    if m:
        role = (m.group(1) or "job").strip()
        if role in ("an", "a", "the", "my", "have", "got"):
            role = "job"
        when = m.group(2)
        valid_until = _end_of_next_month(today) if when == "next month" else (today.fromordinal(today.toordinal() + 7) if when == "next week" else today.fromordinal(today.toordinal() + 1))
        add(type="goal", key=f"{role}_interview", value=f"{role.upper() if len(role) <= 3 else role} interview {when}",
            attributes={"when": when}, life_area="career", temporality="future", valid_until=valid_until.isoformat())

    m = re.search(r"\bi (?:really )?(love|like|enjoy) ([a-z][a-z ]{2,40}?)(?=[.,;!]|$| and\b| but\b)", body)
    if m:
        topic = m.group(2).strip()
        add(type="interest", key=re.sub(r"\W+", "_", topic)[:40], value=topic, life_area=(guess_life_areas(topic) or ["general"])[0])
    m = re.search(r"\bi prefer ([a-z][a-z ]{2,40}?)(?=[.,;!]|$| and\b| over\b)", body)
    if m:
        pref = m.group(1).strip()
        add(type="preference", key=re.sub(r"\W+", "_", pref)[:40], value=pref)
    m = re.search(r"\b(?:i )?(moved|shifted|relocated) to ([a-z][a-z ]{2,30}?)(?=[.,;!]|$| last| in\b)", body)
    if m:
        add(type="life_event", key="relocation", value=f"moved to {m.group(2).strip().title()}", life_area="family", temporality="past")
    if re.search(r"\b(got married|just married|meri shaadi ho gayi)\b", body):
        add(type="life_event", key="marriage", value="got married", life_area="relationships", temporality="past")
    m = re.search(r"\b(?:i have|i was diagnosed with|i suffer from|i've got)\s+(diabetes|cancer|depression|anxiety|high blood pressure|thyroid|asthma|pcos)\b", body)
    if m:
        add(type="life_event", key=f"health_{m.group(1).replace(' ', '_')}", value=m.group(1), life_area="health", sensitive_category="health_detail")
    m = re.search(r"\b(?:my salary is|i earn|i owe|my debt is|i have a loan of)\s+([^\s.,;]+)", body)
    if m:
        add(type="profile_fact", key="income_detail", value=m.group(1), life_area="finance", sensitive_category="financial_detail")
    if asked and not out:
        key = re.sub(r"\W+", "_", body.strip(" ."))[:40] or "note"
        add(type="preference", key=key, value=body.strip(" ."))
    return out
