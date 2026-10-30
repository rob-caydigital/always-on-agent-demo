"""OCEARCH tagged-animal tracker via its Mapotic backing endpoints (unofficial, no key).

See SPEC-shark-threat §4. Neither endpoint filters by place or date, so everything is filtered locally.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from .http import Request

BASE = "https://www.mapotic.com/api/v1/maps/3413"


class FeedSchemaError(ValueError):
    """The feed no longer looks like it did when this parser was written."""


@dataclass(frozen=True)
class FeedShark:
    id: int
    name: str
    species: str
    stage_of_life: str | None
    length: str | None
    last_move: datetime | None  # UTC
    lat: float
    lon: float


def feed_request(cache_s: int) -> Request:
    return Request("sharks", f"{BASE}/pois.geojson/", min_cache_s=cache_s)


def track_request(shark_id: int, cache_s: int) -> Request:
    return Request(f"shark_track_{shark_id}", f"{BASE}/pois/{shark_id}/motion/", min_cache_s=cache_s)


def _utc(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None  # handles the "Z" suffix on Python 3.11+


def parse_feed(body: str) -> tuple[int, list[FeedShark]]:
    """-> (number of animals in the feed, the sharks among them)."""
    data = json.loads(body)
    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list):
        raise FeedSchemaError("no 'features' list")
    sharks: list[FeedShark] = []
    for f in features:
        try:
            p = f["properties"]
            category = p["category_name"]["en"]
            if category != "Sharks":
                continue
            lon, lat = f["geometry"]["coordinates"][:2]
            sharks.append(
                FeedShark(
                    id=int(p["id"]),
                    name=p.get("name") or str(p["id"]),
                    species=p["species"],
                    stage_of_life=p.get("stage_of_life"),
                    length=p.get("length"),
                    last_move=_utc(p.get("last_move_datetime")),
                    lat=float(lat),
                    lon=float(lon),
                )
            )
        except (KeyError, TypeError, ValueError) as e:
            raise FeedSchemaError(f"feature {f.get('id', '?') if isinstance(f, dict) else '?'}: {e!r}") from e
    if features and not sharks:
        raise FeedSchemaError("no features in category 'Sharks'")
    return len(features), sharks


def parse_track(body: str) -> list[tuple[datetime, float, float]]:
    """-> [(time UTC, lat, lon)], duplicates included (the feed repeats pings under new import ids)."""
    data = json.loads(body)
    if not isinstance(data, list):
        raise FeedSchemaError("track is not a list")
    out = []
    for m in data:
        try:
            t = _utc(m["dt_move"])
            if t is None:
                continue
            lon, lat = m["point"]["coordinates"][:2]
            out.append((t, float(lat), float(lon)))
        except (KeyError, TypeError, ValueError) as e:
            raise FeedSchemaError(f"track ping: {e!r}") from e
    return out
