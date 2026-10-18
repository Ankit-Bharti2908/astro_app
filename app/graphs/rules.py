"""Deterministic rules shared by `analyze` (rules-first routing, §4) and heuristics."""

from __future__ import annotations

import re

from app.brain.models import LIFE_AREAS

ROUTES = ("answer", "followup", "memory_recall", "memory_cmd", "smalltalk")

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_HINGLISH_TOKENS = {
    "kab", "hogi", "hoga", "meri", "mera", "mere", "kya", "hai", "hain", "nahi", "nahin", "kaise",
    "kyun", "kyon", "agle", "saal", "naukri", "shaadi", "mujhe", "main", "hoon", "kar", "karna",
    "karu", "karoon", "chahiye", "batao", "bata", "mujhko", "kuch", "bahut", "abhi", "kal", "aaj",
    "paisa", "paise", "ghar", "pyaar", "dost", "zindagi", "bhai", "didi", "yaar", "accha", "theek",
    "sahi", "galat", "kaam", "padhai", "sehat", "rishta", "rishtey", "mann", "dil", "waqt", "samay",
}

LIFE_AREA_KEYWORDS: dict[str, tuple[str, ...]] = {
    "career": ("job", "jobs", "career", "work", "promotion", "interview", "boss", "office", "naukri", "kaam", "salary hike", "startup", "business", "profession", "resign", "employer", "pm "),
    "relationships": ("marriage", "marry", "married", "shaadi", "love", "partner", "girlfriend", "boyfriend", "relationship", "pyaar", "wife", "husband", "dating", "breakup", "rishta", "soulmate", "wedding"),
    "health": ("health", "sehat", "sick", "illness", "diet", "exercise", "fitness", "sleep", "stress", "doctor", "hospital", "disease", "medical", "anxiety", "diabetes"),
    "finance": ("money", "paisa", "paise", "finance", "financial", "invest", "investment", "savings", "loan", "debt", "wealth", "stock", "stocks", "property", "income"),
    "family": ("family", "parents", "mother", "father", "mom", "dad", "kids", "children", "child", "parivar", "brother", "sister", "son", "daughter", "in-laws"),
    "education": ("study", "studies", "exam", "exams", "college", "degree", "university", "padhai", "course", "learning", "school", "phd", "masters"),
    "spirituality": ("meditation", "puja", "spiritual", "spirituality", "temple", "god", "karma", "prayer", "mantra", "yoga", "dharma", "faith", "religion"),
}

_FOLLOWUP_PATTERNS = re.compile(
    r"^(why|why so|why\?|why do you say that|how so|explain( that| this| more)?|elaborate|tell me more|"
    r"what do you mean|can you explain|say more|more details|and then|go on|really\??|are you sure|"
    r"what about (that|this|it)|kyun|kyon|aisa kyun|matlab)\b",
    re.I,
)
_MEMORY_CMD_PATTERNS = re.compile(
    r"^(please\s+)?(remember|note|save|store|don'?t forget|do not forget|forget|delete|remove|erase|"
    r"yaad rakh\w*|bhool ja\w*|yaad rakho)\b",
    re.I,
)
_MEMORY_RECALL_PATTERNS = re.compile(
    r"(what do you (remember|know) about( me| my)?|do you remember|what have you (stored|saved|remembered)|"
    r"what('s| is) in (your|my) memory|kya yaad hai|tumhe kya yaad|what did i tell you)",
    re.I,
)
_SMALLTALK_PATTERNS = re.compile(
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
    areas = [area for area in LIFE_AREAS if any(kw in text for kw in LIFE_AREA_KEYWORDS[area])]
    return areas


# Unanchored, and deliberately generous: this gate only ever *blocks* a memory_cmd, so a
# false positive costs nothing while a false negative would swallow a real command.
_MEMORY_VERB = re.compile(
    r"\b(remember|note|save|store|forget|delete|remove|erase|"
    r"yaad\s+rakh\w*|yaad\s+rakho|bhool\s+ja\w*)\b",
    re.I,
)


def honour_route(route: str, message: str, *, has_history: bool) -> str:
    """Accept the classifier's route only where the message can actually support it.

    A route is not merely a reply style. `memory_cmd` writes and then acknowledges; it cannot
    answer anything, so a message misrouted there loses the question the user actually asked.
    That makes it the one route a classifier must not be trusted with alone: the prompt asks
    for an explicit remember/forget instruction, and this is what guarantees one is present.
    A plain statement of fact belongs on `answer` — persisting it is the memory graph's job,
    not the reply's (§4, §5).
    """
    if route == "memory_cmd" and not _MEMORY_VERB.search(message):
        return "answer"
    if route == "followup" and not has_history:
        return "answer"
    return route


def rule_route(message: str, *, has_history: bool) -> str | None:
    """Return a route when a deterministic rule applies, else None (→ LLM)."""
    text = message.strip()
    words = text.split()
    if _MEMORY_CMD_PATTERNS.match(text):
        return "memory_cmd"
    if _MEMORY_RECALL_PATTERNS.search(text):
        return "memory_recall"
    if _SMALLTALK_PATTERNS.match(text):
        return "smalltalk"
    if has_history and len(words) <= 8 and _FOLLOWUP_PATTERNS.match(text):
        return "followup"
    return None
