"""Sun-sign stub derived from the date of birth (tropical zodiac boundaries)."""

from __future__ import annotations

from datetime import date

# (month, day) the sign *starts*, in calendar order starting with Capricorn.
_BOUNDARIES: list[tuple[tuple[int, int], str]] = [
    ((1, 20), "Aquarius"),
    ((2, 19), "Pisces"),
    ((3, 21), "Aries"),
    ((4, 20), "Taurus"),
    ((5, 21), "Gemini"),
    ((6, 21), "Cancer"),
    ((7, 23), "Leo"),
    ((8, 23), "Virgo"),
    ((9, 23), "Libra"),
    ((10, 23), "Scorpio"),
    ((11, 22), "Sagittarius"),
    ((12, 22), "Capricorn"),
]

_ATTRIBUTES: dict[str, dict[str, str]] = {
    "Aries": {"element": "fire", "modality": "cardinal", "ruler": "Mars"},
    "Taurus": {"element": "earth", "modality": "fixed", "ruler": "Venus"},
    "Gemini": {"element": "air", "modality": "mutable", "ruler": "Mercury"},
    "Cancer": {"element": "water", "modality": "cardinal", "ruler": "Moon"},
    "Leo": {"element": "fire", "modality": "fixed", "ruler": "Sun"},
    "Virgo": {"element": "earth", "modality": "mutable", "ruler": "Mercury"},
    "Libra": {"element": "air", "modality": "cardinal", "ruler": "Venus"},
    "Scorpio": {"element": "water", "modality": "fixed", "ruler": "Mars"},
    "Sagittarius": {"element": "fire", "modality": "mutable", "ruler": "Jupiter"},
    "Capricorn": {"element": "earth", "modality": "cardinal", "ruler": "Saturn"},
    "Aquarius": {"element": "air", "modality": "fixed", "ruler": "Saturn"},
    "Pisces": {"element": "water", "modality": "mutable", "ruler": "Jupiter"},
}


def sun_sign(dob: date) -> str:
    sign = "Capricorn"
    for (month, day), name in _BOUNDARIES:
        if (dob.month, dob.day) >= (month, day):
            sign = name
    return sign


def chart_stub(dob: date | None, tob: str | None = None, birth_place: str | None = None) -> dict:
    """Stubbed chart attributes; a real ephemeris would replace this."""
    if dob is None:
        return {"available": False, "reason": "date of birth unknown"}
    sign = sun_sign(dob)
    return {
        "available": True,
        "sun_sign": sign,
        **_ATTRIBUTES[sign],
        "time_of_birth": tob,
        "birth_place": birth_place,
        "note": "stub: sun sign only; houses and planetary positions not computed",
    }
