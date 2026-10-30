"""Civil dawn / sunrise, computed locally (no API)."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import dawn, sunrise


def dawn_and_sunrise(lat: float, lon: float, day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    obs = Observer(latitude=lat, longitude=lon)
    return dawn(obs, day, tzinfo=tz), sunrise(obs, day, tzinfo=tz)
