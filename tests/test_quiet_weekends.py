"""usage.quiet_hours.weekends: Saturday and Sunday are quiet all day."""
from datetime import datetime

import pytz

import sellbird.main as main_mod
from sellbird.config import QuietHoursConfig

TZ = pytz.timezone("America/New_York")


class _Cfg:
    def __init__(self, **qh):
        self.usage = type("U", (), {"quiet_hours": QuietHoursConfig(**qh)})()


def _at(monkeypatch, *args):
    moment = TZ.localize(datetime(*args))

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(main_mod, "datetime", _DT)


def test_off_by_default():
    assert QuietHoursConfig().weekends is False


def test_weekday_business_hours_are_not_quiet(monkeypatch):
    _at(monkeypatch, 2026, 11, 24, 11, 0)          # Tuesday 11:00
    assert not main_mod.in_quiet_hours(_Cfg(start="17:00", end="09:00", weekends=True))


def test_weekday_evening_is_quiet(monkeypatch):
    _at(monkeypatch, 2026, 11, 24, 18, 0)          # Tuesday 18:00
    assert main_mod.in_quiet_hours(_Cfg(start="17:00", end="09:00", weekends=True))


def test_saturday_noon_is_quiet_only_with_weekends(monkeypatch):
    _at(monkeypatch, 2026, 11, 28, 12, 0)          # Saturday 12:00
    assert main_mod.in_quiet_hours(_Cfg(start="17:00", end="09:00", weekends=True))
    assert not main_mod.in_quiet_hours(_Cfg(start="17:00", end="09:00"))


def test_friday_evening_sleeps_until_monday(monkeypatch):
    _at(monkeypatch, 2026, 11, 27, 18, 0)          # Friday 18:00
    secs = main_mod.seconds_until_quiet_hours_end(_Cfg(start="17:00", end="09:00", weekends=True))
    assert secs == (2 * 24 + 15) * 3600            # Monday 09:00
