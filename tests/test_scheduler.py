from __future__ import annotations

from datetime import datetime

from ghost_daemon.scheduler import _parse_schedule, _should_fire_now


# ── _parse_schedule ───────────────────────────────────────────────────────────

def test_parse_daily():
    result = _parse_schedule("09:00")
    assert result == ("daily", 9, 0)


def test_parse_weekday():
    result = _parse_schedule("weekday 09:00")
    assert result == ("weekday", 9, 0)


def test_parse_specific_weekday():
    assert _parse_schedule("friday 17:00") == ("friday", 17, 0)
    assert _parse_schedule("monday 08:30") == ("monday", 8, 30)


def test_parse_empty_returns_none():
    assert _parse_schedule("") is None
    assert _parse_schedule("   ") is None


def test_parse_bad_time_returns_none():
    assert _parse_schedule("09-00") is None


def test_parse_case_insensitive():
    assert _parse_schedule("FRIDAY 17:00") == ("friday", 17, 0)
    assert _parse_schedule("Weekday 09:00") == ("weekday", 9, 0)


# ── _should_fire_now ──────────────────────────────────────────────────────────

def _dt(weekday: int, hour: int, minute: int) -> datetime:
    # 2026-10-26 is a Monday (weekday=0)
    base = datetime(2026, 10, 26)  # Monday
    from datetime import timedelta
    return (base + timedelta(days=weekday)).replace(hour=hour, minute=minute)


def test_daily_fires_at_exact_time():
    parsed = _parse_schedule("09:00")
    assert _should_fire_now(parsed, _dt(0, 9, 0)) is True


def test_daily_does_not_fire_at_wrong_time():
    parsed = _parse_schedule("09:00")
    assert _should_fire_now(parsed, _dt(0, 9, 1)) is False
    assert _should_fire_now(parsed, _dt(0, 10, 0)) is False


def test_weekday_fires_monday():
    parsed = _parse_schedule("weekday 09:00")
    assert _should_fire_now(parsed, _dt(0, 9, 0)) is True  # Monday


def test_weekday_fires_friday():
    parsed = _parse_schedule("weekday 09:00")
    assert _should_fire_now(parsed, _dt(4, 9, 0)) is True  # Friday


def test_weekday_does_not_fire_saturday():
    parsed = _parse_schedule("weekday 09:00")
    assert _should_fire_now(parsed, _dt(5, 9, 0)) is False  # Saturday


def test_weekday_does_not_fire_sunday():
    parsed = _parse_schedule("weekday 09:00")
    assert _should_fire_now(parsed, _dt(6, 9, 0)) is False  # Sunday


def test_specific_day_fires_on_right_day():
    parsed = _parse_schedule("friday 17:00")
    assert _should_fire_now(parsed, _dt(4, 17, 0)) is True   # Friday
    assert _should_fire_now(parsed, _dt(0, 17, 0)) is False  # Monday


def test_specific_day_wrong_time():
    parsed = _parse_schedule("friday 17:00")
    assert _should_fire_now(parsed, _dt(4, 16, 0)) is False


def test_minute_precision():
    parsed = _parse_schedule("08:30")
    assert _should_fire_now(parsed, _dt(0, 8, 30)) is True
    assert _should_fire_now(parsed, _dt(0, 8, 0)) is False
    assert _should_fire_now(parsed, _dt(0, 8, 31)) is False
