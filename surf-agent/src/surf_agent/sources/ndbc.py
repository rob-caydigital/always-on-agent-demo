"""NDBC realtime2 text files: two '#' header lines, newest row first, 'MM' = missing."""

from __future__ import annotations

from datetime import UTC, datetime

from ..models import BuoyObs, WindObs
from .http import Request
from .units import M_TO_FT, MS_TO_KT

BASE = "https://www.ndbc.noaa.gov/data/realtime2"


def realtime_request(name: str, station: str) -> Request:
    return Request(name, f"{BASE}/{station}.txt", kind="text")


def _rows(body: str):
    lines = body.splitlines()
    cols = lines[0].lstrip("#").split()
    for line in lines:
        if line.startswith("#") or not line.strip():
            continue
        yield dict(zip(cols, line.split()))


def _num(row: dict, key: str) -> float | None:
    v = row.get(key)
    return None if v in (None, "MM") else float(v)


def _time(row: dict) -> datetime:
    return datetime(
        int(row["YY"]), int(row["MM"]), int(row["DD"]), int(row["hh"]), int(row["mm"]), tzinfo=UTC
    )


def parse_buoy(body: str, station: str) -> BuoyObs | None:
    for row in _rows(body):
        if _num(row, "WVHT") is not None:
            wvht = _num(row, "WVHT")
            return BuoyObs(
                station=station,
                observed_at=_time(row),
                height_ft=wvht * M_TO_FT,
                period_s=_num(row, "DPD"),
                direction_deg=_num(row, "MWD"),
            )
    return None


def parse_wind(body: str, station: str) -> WindObs | None:
    for row in _rows(body):
        if _num(row, "WSPD") is not None:
            gst = _num(row, "GST")
            return WindObs(
                station=station,
                observed_at=_time(row),
                wind_kt=_num(row, "WSPD") * MS_TO_KT,
                gust_kt=gst * MS_TO_KT if gst is not None else None,
                direction_deg=_num(row, "WDIR"),
            )
    return None
