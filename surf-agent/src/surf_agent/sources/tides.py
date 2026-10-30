"""NOAA CO-OPS tide predictions (hi/lo)."""

from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ..models import TideEvent
from .http import Request

BASE = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"


def tide_request(station: str, begin: date) -> Request:
    # `range` hours from begin_date; 48 h so a pre-dawn run always sees the morning's tides.
    url = (
        f"{BASE}?product=predictions&station={station}&begin_date={begin:%Y%m%d}&range=48"
        "&datum=MLLW&time_zone=lst_ldt&units=english&interval=hilo&format=json&application=surf-check"
    )
    return Request("tides", url)


def parse_tides(body: str, tz: ZoneInfo) -> list[TideEvent]:
    data = json.loads(body)
    if "error" in data:
        raise ValueError(data["error"].get("message", "tide API error"))
    return [
        TideEvent(
            time=datetime.strptime(p["t"], "%Y-%m-%d %H:%M").replace(tzinfo=tz),
            kind=p["type"],
            height_ft=float(p["v"]),
        )
        for p in data.get("predictions", [])
    ]
