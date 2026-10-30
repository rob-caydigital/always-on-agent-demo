"""api.weather.gov: gridpoint forecast, marine zone forecast, alerts, station observations."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from ..models import Alert, HourConditions, WindObs
from .http import Request
from .units import to_ft, to_kt

BASE = "https://api.weather.gov"

_DURATION = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")


def requests_for(noaa) -> list[Request]:
    return [
        Request("gridpoint", f"{BASE}/gridpoints/{noaa.nws_wfo}/{noaa.nws_grid_x},{noaa.nws_grid_y}"),
        Request("marine_forecast", f"{BASE}/zones/marine/{noaa.marine_zone}/forecast"),
        Request("alerts", f"{BASE}/alerts/active?zone={','.join(noaa.alert_zones)}"),
    ]


def station_request(station: str) -> Request:
    return Request("wind_fallback", f"{BASE}/stations/{station}/observations/latest")


def parse_interval(valid_time: str) -> tuple[datetime, datetime]:
    """'2026-10-30T14:00:00+00:00/PT9H' -> (start, end)."""
    start_s, dur_s = valid_time.split("/")
    start = datetime.fromisoformat(start_s)
    m = _DURATION.match(dur_s)
    if not m:
        raise ValueError(f"unsupported duration: {dur_s}")
    d, h, mi = (int(x) if x else 0 for x in m.groups())
    return start, start + timedelta(days=d, hours=h, minutes=mi)


def _value_at(field: dict, t: datetime):
    for v in field.get("values", []):
        start, end = parse_interval(v["validTime"])
        if start <= t < end:
            return v["value"]
    return None


def _weather_text(values) -> str | None:
    if not values:
        return None
    parts = []
    for w in values:
        if not w.get("weather"):
            continue
        cov = (w.get("coverage") or "").replace("_", " ")
        parts.append(f"{cov} {w['weather'].replace('_', ' ')}".strip())
    return ", ".join(parts) or None


def parse_gridpoint(body: str, hours: list[datetime]) -> tuple[list[HourConditions], datetime]:
    """Expand run-length intervals to the given local hours. Returns (hours, updateTime)."""
    p = json.loads(body)["properties"]

    def f(name):
        return p.get(name) or {}

    out = []
    for t in hours:
        height = to_ft(_value_at(f("waveHeight"), t), f("waveHeight").get("uom"))
        out.append(
            HourConditions(
                time=t,
                wave_height_ft=height,
                wave_period_s=_value_at(f("wavePeriod"), t),
                # primarySwellDirection is a constant placeholder in this grid; direction comes from CWF.
                swell_dir_deg=None,
                wind_kt=to_kt(_value_at(f("windSpeed"), t), f("windSpeed").get("uom")),
                gust_kt=to_kt(_value_at(f("windGust"), t), f("windGust").get("uom")),
                wind_dir_deg=_value_at(f("windDirection"), t),
                pop_pct=_value_at(f("probabilityOfPrecipitation"), t),
                thunder_pct=_value_at(f("probabilityOfThunder"), t),
                weather_text=_weather_text(_value_at(f("weather"), t)),
            )
        )
    return out, datetime.fromisoformat(p["updateTime"])


def pick_marine_period(body: str, target: date, run_date: date) -> tuple[str | None, str | None, datetime | None]:
    """Choose the CWF period covering the morning of `target`. Periods carry names, not times."""
    p = json.loads(body)["properties"]
    periods = p.get("periods") or []
    updated = datetime.fromisoformat(p["updated"]) if p.get("updated") else None
    if not periods:
        return None, None, updated
    names = [(x["name"], x["detailedForecast"]) for x in periods]
    if target == run_date:
        for name, text in names:
            n = name.lower()
            if "today" in n or "this morning" in n:
                return name, text, updated
        return names[0][0], names[0][1], updated
    day = target.strftime("%a").lower()  # e.g. "sat"
    full = target.strftime("%A").lower()
    for name, text in names:
        n = name.lower()
        if (n.startswith((day, full))) and "night" not in n:
            return name, text, updated
    return None, None, updated


def parse_alerts(body: str) -> list[Alert]:
    out = []
    for feat in json.loads(body).get("features", []):
        q = feat["properties"]
        out.append(
            Alert(
                event=q["event"],
                severity=q.get("severity"),
                headline=q.get("headline"),
                onset=q.get("onset") or q.get("effective"),
                ends=q.get("ends") or q.get("expires"),
            )
        )
    return out


def parse_station_obs(body: str, station: str) -> WindObs:
    p = json.loads(body)["properties"]

    def val(k):
        x = p.get(k) or {}
        return x.get("value"), x.get("unitCode")

    ws, ws_u = val("windSpeed")
    wg, wg_u = val("windGust")
    wd, _ = val("windDirection")
    return WindObs(
        station=station,
        observed_at=datetime.fromisoformat(p["timestamp"]),
        wind_kt=to_kt(ws, ws_u),
        gust_kt=to_kt(wg, wg_u),
        direction_deg=wd,
    )
