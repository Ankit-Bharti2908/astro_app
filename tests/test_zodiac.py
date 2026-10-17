from datetime import date

from app.profile.zodiac import chart_stub, sun_sign


def test_sun_sign_boundaries():
    assert sun_sign(date(1995, 8, 15)) == "Leo"
    assert sun_sign(date(1995, 8, 22)) == "Leo"
    assert sun_sign(date(1995, 8, 23)) == "Virgo"
    assert sun_sign(date(2000, 12, 25)) == "Capricorn"
    assert sun_sign(date(2000, 1, 1)) == "Capricorn"
    assert sun_sign(date(2000, 1, 20)) == "Aquarius"
    assert sun_sign(date(2000, 3, 20)) == "Pisces"
    assert sun_sign(date(2000, 3, 21)) == "Aries"


def test_chart_stub():
    chart = chart_stub(date(1995, 8, 15), "10:30", "Delhi")
    assert chart["available"] and chart["sun_sign"] == "Leo" and chart["element"] == "fire"
    assert chart_stub(None)["available"] is False
