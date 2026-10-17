import pytest

from app.graphs.rules import (
    detect_language,
    explicit_language_request,
    guess_life_areas,
    rule_route,
)


@pytest.mark.parametrize(
    "message,has_history,expected",
    [
        ("Why do you say that?", True, "followup"),
        ("Why do you say that?", False, None),  # nothing to follow up on → LLM decides
        ("explain that", True, "followup"),
        ("Remember that I love classical music", False, "memory_cmd"),
        ("Please forget about my job plans", False, "memory_cmd"),
        ("What do you remember about my career goals?", False, "memory_recall"),
        ("Do you remember what I told you?", False, "memory_recall"),
        ("hi", False, "smalltalk"),
        ("Namaste!", False, "smalltalk"),
        ("thanks a lot", True, "smalltalk"),
        ("What should I focus on for my career?", False, None),
        ("Meri shaadi kab hogi?", False, None),
    ],
)
def test_rule_route(message, has_history, expected):
    assert rule_route(message, has_history=has_history) == expected


@pytest.mark.parametrize(
    "message,expected",
    [
        ("What should I focus on for my career?", "en"),
        ("Meri shaadi kab hogi?", "hinglish"),
        ("Mujhe agle saal naukri badalni hai", "hinglish"),
        ("मेरी शादी कब होगी?", "hi"),
        ("Please reply in Hindi from now on", "hi"),
        ("Answer in Hinglish please", "hinglish"),
        ("kab?", "hinglish"),
    ],
)
def test_detect_language(message, expected):
    assert detect_language(message) == expected


def test_explicit_language_request():
    assert explicit_language_request("Reply in Hindi: what about my career?") == "hi"
    assert explicit_language_request("What about my career?") is None


def test_guess_life_areas():
    assert guess_life_areas("What should I focus on for my career?") == ["career"]
    assert guess_life_areas("Meri shaadi kab hogi?") == ["relationships"]
    assert set(guess_life_areas("money and my parents")) == {"finance", "family"}
    assert guess_life_areas("tell me a joke") == []
