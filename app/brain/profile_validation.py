"""Admissibility of the five profile slots (§11, invalid input).

Birth details reach the User node two ways — the typed `/users` API and free text the
extractor turns into a PROFILE write — and neither is trustworthy. The prompt asks the
model to refuse an unusable value and mostly it does, but a prompt is a request, not a
guarantee: observed failures include an impossible clock time being "repaired" into a
plausible one. The chart is computed from these three fields, so a wrong value is worse
than a missing one — it yields a confident reading of a moment that never happened.
These checks are deterministic, run on both paths, and reject rather than repair.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

MIN_BIRTH_YEAR = 1900
# A chart needs a latitude and longitude, so a "place" that cannot be located on Earth is
# unusable rather than merely odd. Geocoding is the production answer (§17); this catches
# what is obvious without a network call.
NON_EARTH_PLACES = frozenset(
    {"mars", "moon", "jupiter", "venus", "saturn", "mercury", "neptune", "uranus", "pluto",
     "sun", "earth", "space", "heaven", "nowhere", "unknown", "n/a", "none", "somewhere"}
)
_TIME_RE = re.compile(r"^(\d{1,2})\s*[:.\s]\s*(\d{1,2})\s*(am|pm)?$", re.IGNORECASE)
_LANGUAGES = {"en", "hi", "hinglish"}
_LANGUAGE_ALIASES = {"english": "en", "hindi": "hi"}


def normalize_tob(value: Any) -> str:
    """"2:30 PM" / "14.30" → "14:30". Raises ValueError on anything that is not a clock time."""
    text = str(value).strip()
    m = _TIME_RE.match(text)
    if not m:
        raise ValueError(f"{text!r} is not a time of birth (expected HH:MM, 24-hour or with am/pm)")
    hour, minute, meridiem = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if minute > 59:
        raise ValueError(f"{text!r} has an impossible minute")
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError(f"{text!r} has an impossible hour for a 12-hour clock")
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    elif hour > 23:
        raise ValueError(f"{text!r} has an impossible hour")
    return f"{hour:02d}:{minute:02d}"


def normalize_birth_place(value: Any) -> str:
    text = " ".join(str(value).split())
    if len(text) < 2 or not any(ch.isalpha() for ch in text):
        raise ValueError(f"{text!r} is not a place name")
    if text.lower().strip(".") in NON_EARTH_PLACES:
        raise ValueError(f"{text!r} is not a locatable place on Earth")
    return text


def normalize_dob(value: Any, *, today: date | None = None) -> date:
    if isinstance(value, date):
        parsed = value
    else:
        try:
            parsed = date.fromisoformat(str(value).strip()[:10])
        except ValueError as exc:
            raise ValueError(f"{value!r} is not a date (expected YYYY-MM-DD)") from exc
    if parsed.year < MIN_BIRTH_YEAR:
        raise ValueError(f"{parsed.isoformat()} is before {MIN_BIRTH_YEAR}")
    if today is not None and parsed > today:
        raise ValueError(f"{parsed.isoformat()} is in the future")
    return parsed


def normalize_language(value: Any) -> str:
    text = str(value).strip().lower()
    text = _LANGUAGE_ALIASES.get(text, text)
    if text not in _LANGUAGES:
        raise ValueError(f"{value!r} is not one of {sorted(_LANGUAGES)}")
    return text


def normalize_name(value: Any) -> str:
    text = " ".join(str(value).split())
    if not 1 <= len(text) <= 120 or not any(ch.isalpha() for ch in text):
        raise ValueError(f"{value!r} is not a name")
    return text


NORMALIZERS = {
    "name": normalize_name,
    "dob": normalize_dob,
    "tob": normalize_tob,
    "birth_place": normalize_birth_place,
    "preferred_language": normalize_language,
}


def validate_profile_patch(
    patch: dict[str, Any], *, today: date | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    """Split a profile patch into what may be stored and what may not.

    Returns `(clean, rejected)`, where `rejected` maps slot → why, so a caller can report
    the reason instead of dropping the value silently. `None` clears a slot and is kept.
    """
    clean: dict[str, Any] = {}
    rejected: dict[str, str] = {}
    for key, value in patch.items():
        if key not in NORMALIZERS:
            continue
        if value is None:
            clean[key] = None
            continue
        try:
            clean[key] = normalize_dob(value, today=today) if key == "dob" else NORMALIZERS[key](value)
        except ValueError as exc:
            rejected[key] = str(exc)
    return clean, rejected
