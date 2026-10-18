"""FakeLLM: deterministic, scriptable LLMProvider used by the test-suite.

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

from app.llm.provider import (
    BaseLLMProvider,
    LLMMessage,
    LLMResponse,
    LLMUnavailable,
    ToolSpec,
)

Scripted = LLMResponse | str | dict | list | Callable[[list[LLMMessage], list[ToolSpec] | None], LLMResponse]

# ---------------------------------------------------------------------------
# A stand-in for the classifier. These word lists used to live in `app/graphs/rules.py` and
# decide real routes; they were removed because a word list cannot judge what a sentence
# means. They are kept here, where their job is only to make a scripted "model" behave
# plausibly enough that the code under test is exercised. Nothing in `app/` imports them.
# ---------------------------------------------------------------------------

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_HINGLISH_TOKENS = {
    "kab", "hogi", "hoga", "meri", "mera", "mere", "kya", "hai", "hain", "nahi", "nahin", "kaise",
    "kyun", "kyon", "agle", "saal", "naukri", "shaadi", "mujhe", "main", "hoon", "kar", "karna",
    "karu", "karoon", "chahiye", "batao", "bata", "mujhko", "kuch", "bahut", "abhi", "kal", "aaj",
    "paisa", "paise", "ghar", "pyaar", "dost", "zindagi", "bhai", "didi", "yaar", "accha", "theek",
    "sahi", "galat", "kaam", "padhai", "sehat", "rishta", "rishtey", "mann", "dil", "waqt", "samay",
}
_LIFE_AREA_WORDS: dict[str, tuple[str, ...]] = {
    "career": ("job", "jobs", "career", "work", "promotion", "interview", "boss", "office", "naukri", "kaam", "salary hike", "startup", "business", "profession", "resign", "employer", "pm "),
    "relationships": ("marriage", "marry", "married", "shaadi", "love", "partner", "girlfriend", "boyfriend", "relationship", "pyaar", "wife", "husband", "dating", "breakup", "rishta", "soulmate", "wedding"),
    "health": ("health", "sehat", "sick", "illness", "diet", "exercise", "fitness", "sleep", "stress", "doctor", "hospital", "disease", "medical", "anxiety", "diabetes"),
    "finance": ("money", "paisa", "paise", "finance", "financial", "invest", "investment", "savings", "loan", "debt", "wealth", "stock", "stocks", "property", "income"),
    "family": ("family", "parents", "mother", "father", "mom", "dad", "kids", "children", "child", "parivar", "brother", "sister", "son", "daughter", "in-laws"),
    "education": ("study", "studies", "exam", "exams", "college", "degree", "university", "padhai", "course", "learning", "school", "phd", "masters"),
    "spirituality": ("meditation", "puja", "spiritual", "spirituality", "temple", "god", "karma", "prayer", "mantra", "yoga", "dharma", "faith", "religion"),
}
_FOLLOWUP = re.compile(
    r"^(why|why so|why\?|why do you say that|how so|explain( that| this| more)?|elaborate|tell me more|"
    r"what do you mean|can you explain|say more|more details|and then|go on|really\??|are you sure|"
    r"what about (that|this|it)|kyun|kyon|aisa kyun|matlab)\b",
    re.I,
)
_RECALL = re.compile(
    r"(what do you (remember|know) about( me| my)?|do you remember|what have you (stored|saved|remembered)|"
    r"what('s| is) in (your|my) memory|kya yaad hai|tumhe kya yaad|what did i tell you)",
    re.I,
)
_SMALLTALK = re.compile(
    r"^(hi|hello|hey|hii+|namaste|namaskar|good (morning|evening|night|afternoon)|thanks|thank you|"
    r"thanks a lot|ok|okay|cool|great|nice|bye|goodbye|how are you\??|shukriya|dhanyavaad)[\s!.]*$",
    re.I,
)
_EXPLICIT_LANGUAGE = re.compile(
    r"(reply|answer|respond|talk|speak|baat karo)\s+(to me\s+)?in\s+(hindi|english|hinglish)", re.I
)


def explicit_language_request(message: str) -> str | None:
    m = _EXPLICIT_LANGUAGE.search(message)
    if not m:
        return None
    return {"hindi": "hi", "english": "en", "hinglish": "hinglish"}[m.group(3).lower()]


def detect_language(message: str) -> str:
    explicit = explicit_language_request(message)
    if explicit:
        return explicit
    if _DEVANAGARI.search(message):
        return "hi"
    tokens = re.findall(r"[a-zA-Z]+", message.lower())
    hits = sum(1 for t in tokens if t in _HINGLISH_TOKENS)
    if hits >= 2 or (hits >= 1 and len(tokens) <= 4):
        return "hinglish"
    return "en"


def guess_life_areas(message: str) -> list[str]:
    text = f" {message.lower()} "
    return [area for area, words in _LIFE_AREA_WORDS.items() if any(w in text for w in words)]


_MEMORY_VERB_PREFIX = re.compile(r"^(please\s+)?(remember|note|save|store|forget|delete|remove|erase|yaad rakh\w*|bhool ja\w*)\s*(that|about|the|ki)?\s*", re.I)
_FORGET_VERB = re.compile(r"^(please\s+)?(forget|delete|remove|erase|bhool ja\w*)\b", re.I)


def _route_of(message: str, *, has_history: bool) -> str:
    """The route a competent classifier would pick — the real one is an LLM (§4).

    `app/` no longer routes by keyword at all, so the stand-in has to make the whole judgement
    or nothing below it is exercised. The memory-command arm is kept crude on purpose: an
    imperative that runs into a second clause or a question is not an instruction about
    memory, which is exactly the distinction the deleted word lists could not draw.
    """
    text = message.strip()
    if _MEMORY_VERB_PREFIX.match(text) and "?" not in text and "," not in text:
        return "memory_cmd"
    if _RECALL.search(text):
        return "memory_recall"
    if _SMALLTALK.match(text):
        return "smalltalk"
    if has_history and len(text.split()) <= 8 and _FOLLOWUP.match(text):
        return "followup"
    return "answer"


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
            "memory_cmd_select": self._memory_cmd_select,
        }.get(task, self._answer)
        return handler(system, user, messages)

    def _analyze(self, system: str, user: str, messages) -> LLMResponse:
        m = re.search(r"<message>\s*(.*?)\s*</message>", user, flags=re.S)
        message = m.group(1).strip() if m else user
        prev = re.search(r"<previous_assistant_reply>\s*(.*?)\s*</previous_assistant_reply>", user, flags=re.S)
        has_history = bool(prev and prev.group(1).strip() not in ("", "(none)"))
        areas = guess_life_areas(message) or ["general"]
        payload = {
            "route": _route_of(message, has_history=has_history),
            "life_areas": areas,
            "language": detect_language(message),
            "explicit_language": explicit_language_request(message),
            "reason": "fake",
        }
        return LLMResponse(content=json.dumps(payload))

    def _memory_cmd_select(self, system: str, user: str, messages) -> LLMResponse:
        """Stand-in for the model that reads a memory instruction (§7.6).

        Deliberately conservative in the same direction the prompt asks for: a memory is a
        target only when the instruction names the whole of it, so an instruction that merely
        shares a word with a stored memory matches nothing. The real model matches on meaning;
        what both must agree on is that a partial overlap is not a reference.
        """
        m = re.search(r"<message>\s*(.*?)\s*</message>", user, flags=re.S)
        message = (m.group(1) if m else user).strip()
        memories = _xml_json(user, "stored_memories") or []
        body = _MEMORY_VERB_PREFIX.sub("", message).strip(" .!?,").lower()
        plan = {
            "command": "forget" if _FORGET_VERB.match(message) else "remember",
            "names_a_target": bool(body) and body not in {"that", "it", "this", "them"},
            "memory_ids": [],
            "forget_all": body in {"everything", "all", "all my memories", "sab kuch", "sab"},
            "reason": "fake",
        }
        if plan["command"] == "forget" and plan["names_a_target"] and not plan["forget_all"]:
            plan["memory_ids"] = [
                mem["id"]
                for mem in memories
                if all(tok in body for tok in str(mem.get("key", "")).split("_") if len(tok) > 2)
            ]
        return LLMResponse(content=json.dumps(plan))

    def _extract(self, system: str, user: str, messages) -> LLMResponse:
        today = _current_date(system) or date.today()
        quoted = re.search(r"<user_message>\s*(.*?)\s*</user_message>", user, flags=re.S)
        message = quoted.group(1) if quoted else user
        ctx = re.search(r"^assistant: (.*)$", user, flags=re.M)
        context = ctx.group(1) if ctx else ""
        return LLMResponse(content=json.dumps({"candidates": rule_extract(message, today, context=context)}, default=str))

    def _reconcile(self, system: str, user: str, messages) -> LLMResponse:
        existing = _xml_json(user, "existing_memories") or []
        candidate = _xml_json(user, "new_candidate") or {}
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
        prev = re.search(r"<previous_summary>\s*(.*?)\s*</previous_summary>", user, flags=re.S)
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


def _xml_json(text: str, tag: str) -> Any:
    """Read the JSON payload of an XML block in a prompt, e.g. <existing_memories>[...]."""
    m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, flags=re.S)
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
        year, month, day = int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1))
        try:
            value = date(year, month, day).isoformat()
        except ValueError:
            # The real extractor hands over impossible dates rather than refusing them
            # ("65 may 2200" -> "2200-05-65"); the stand-in has to do the same or the
            # validation path below it is never exercised.
            value = f"{year:04d}-{month:02d}-{day:02d}"
        add(type="profile_fact", key="dob", value=value)
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
