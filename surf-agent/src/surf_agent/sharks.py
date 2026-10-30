"""Shark threat rating (SPEC-shark-threat §5). Pure functions: no I/O, no LLM."""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime

from .config import SharkConfig
from .models import SharkPing, SharkRating, SharkReport

log = logging.getLogger(__name__)

EARTH_RADIUS_MI = 3958.7613  # mean Earth radius (6371.0088 km) in statute miles

_SCIENTIFIC = re.compile(r"\(([^)]+)\)")


def haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(math.sqrt(a))


def common_name(species: str) -> str:
    """'White Shark (Carcharodon carcharias)' -> 'White Shark'."""
    return _SCIENTIFIC.sub("", species).strip() or species


def match_species(species: str, cfg: SharkConfig) -> tuple[str | None, float]:
    """Scientific name in parentheses first, then common-name aliases, else the unknown weight."""
    by_key = {k.lower(): k for k in cfg.species_weights}
    m = _SCIENTIFIC.search(species)
    if m and m.group(1).strip().lower() in by_key:
        key = by_key[m.group(1).strip().lower()]
        return key, cfg.species_weights[key].weight
    name = common_name(species).lower()
    for key, sw in cfg.species_weights.items():
        if name == key.lower() or name in (a.lower() for a in sw.aliases):
            return key, sw.weight
    log.warning("shark_species_unmapped:%s", species)
    return None, cfg.unknown_species_weight


def stage_factor(stage: str | None, cfg: SharkConfig) -> float:
    return cfg.stage_factors.get(stage, 1.0) if stage else 1.0


def recency_factor(age_h: float, cfg: SharkConfig) -> float | None:
    """Upper-inclusive buckets. None = outside the lookback window (or a ping from the future)."""
    if age_h < 0:
        return None
    for b in cfg.recency_factors:
        if age_h <= b.max_hours:
            return b.factor
    return None


def distance_factor(distance_mi: float, cfg: SharkConfig) -> float | None:
    """Upper-inclusive buckets. None = outside the radius."""
    for b in cfg.distance_factors:
        if distance_mi <= b.max_mi:
            return b.factor
    return None


def qualifying_pings(
    points: list[tuple[datetime, float, float]], lat: float, lon: float, run_at: datetime, cfg: SharkConfig
) -> list[SharkPing]:
    """Dedupe (time, lat, lon) points and keep those inside the radius and the lookback window."""
    out: list[SharkPing] = []
    for t, plat, plon in sorted(set(points)):
        age_h = (run_at - t).total_seconds() / 3600
        dist = haversine_mi(lat, lon, plat, plon)
        if recency_factor(age_h, cfg) is not None and distance_factor(dist, cfg) is not None:
            out.append(SharkPing(time=t, lat=plat, lon=plon, distance_mi=round(dist, 2), age_h=round(age_h, 2)))
    return out


def ping_threat(p: SharkPing, weight: float, stage: float, cfg: SharkConfig) -> float:
    return weight * stage * (recency_factor(p.age_h, cfg) or 0) * (distance_factor(p.distance_mi, cfg) or 0)


def rate_sharks(report: SharkReport | None, cfg: SharkConfig | None) -> list[SharkRating]:
    if report is None or cfg is None or report.status != "checked":
        return []
    out: list[SharkRating] = []
    for s in report.nearby:
        if not s.pings:
            continue
        key, weight = match_species(s.species, cfg)
        stage = stage_factor(s.stage_of_life, cfg)
        worst = max(s.pings, key=lambda p: ping_threat(p, weight, stage, cfg))
        out.append(
            SharkRating(
                id=s.id,
                name=s.name,
                species=s.species,
                species_key=key,
                species_weight=weight,
                stage_factor=stage,
                recency_factor=recency_factor(worst.age_h, cfg) or 0,
                distance_factor=distance_factor(worst.distance_mi, cfg) or 0,
                threat=round(ping_threat(worst, weight, stage, cfg), 2),
                worst_ping=worst,
            )
        )
    return sorted(out, key=lambda r: r.threat, reverse=True)


def veto_rating(ratings: list[SharkRating], cfg: SharkConfig | None) -> SharkRating | None:
    """The highest-threat shark at or above the threshold, if any."""
    if cfg is None:
        return None
    return next((r for r in ratings if r.threat > 0 and r.threat >= cfg.veto_threshold), None)
