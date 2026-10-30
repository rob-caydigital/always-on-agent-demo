"""Deterministic surf scoring (SPEC §6). Pure functions: no I/O, no LLM."""

from __future__ import annotations

from .config import Preferences, ScoringConfig, Spot
from .models import (
    Alert,
    BlockScore,
    ConditionsSnapshot,
    HourConditions,
    HourScore,
    ScoreResult,
    SharkRating,
    SwellComponent,
    Veto,
)
from .sharks import common_name, rate_sharks, veto_rating

MAX_SCORE = 10.0


def angle_off(a: float, b: float) -> float:
    """Smallest angle between two compass directions, 0..180."""
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def _period_bucket(period_s: float, cfg: ScoringConfig):
    chosen = cfg.period_buckets[0]
    for b in cfg.period_buckets:
        if period_s >= b.min_s:
            chosen = b
    return chosen


def face_height_ft(height_ft: float, period_s: float, cfg: ScoringConfig) -> float:
    return height_ft * _period_bucket(period_s, cfg).face_multiplier


def period_quality(period_s: float, cfg: ScoringConfig) -> float:
    return _period_bucket(period_s, cfg).quality


def size_score(face_ft: float, prefs: Preferences) -> float:
    if face_ft < prefs.min_rideable_ft:
        return 0.0
    if prefs.max_ft is None:
        return MAX_SCORE
    taper_start = prefs.max_ft - 3
    if face_ft <= taper_start:
        return MAX_SCORE
    if face_ft >= prefs.max_ft:
        return 5.0
    return MAX_SCORE - 5.0 * (face_ft - taper_start) / 3


def in_swell_window(direction_deg: float, window: tuple[float, float]) -> bool:
    lo, hi = window
    d = direction_deg % 360
    return lo <= d <= hi if lo <= hi else (d >= lo or d <= hi)


def swell_direction_factor(direction_deg: float | None, spot: Spot, cfg: ScoringConfig) -> float:
    sd = cfg.swell_direction
    if direction_deg is None:
        return sd.unknown_factor
    if not in_swell_window(direction_deg, spot.swell_window_deg):
        return sd.outside_window_factor
    off = angle_off(direction_deg, spot.beach_facing_deg)
    for b in sd.buckets:
        if off <= b.max_off_deg:
            return b.factor
    return sd.outside_window_factor


def wind_class(wind_dir_deg: float, spot: Spot, cfg: ScoringConfig) -> str:
    off = angle_off(wind_dir_deg, spot.offshore_deg)
    for c in cfg.wind_classes:
        if off <= c.max_off_deg:
            return c.name
    return cfg.wind_classes[-1].name


def wind_factor(wind_kt: float | None, cls: str | None, cfg: ScoringConfig) -> float:
    if wind_kt is None:
        return cfg.wind_unknown_factor
    row = cfg.wind_factors[0]
    for r in cfg.wind_factors:
        if wind_kt >= r["min_kt"]:
            row = r
    if cls is None:
        # Direction unknown: only glassy conditions are direction-independent.
        return row["offshore"] if wind_kt < cfg.wind_factors[1]["min_kt"] else cfg.wind_unknown_factor
    return row[cls]


def alert_active(alert: Alert, start, end) -> bool:
    if alert.onset and alert.onset > end:
        return False
    return not (alert.ends and alert.ends < start)


def weather_factor(hour: HourConditions, active_alerts: list[Alert], cfg: ScoringConfig) -> float:
    f = 1.0
    if hour.pop_pct is not None:
        for b in cfg.weather.pop_buckets:
            if hour.pop_pct >= b.min_pct:
                f = b.factor
    if any("small craft" in a.event.lower() for a in active_alerts):
        f *= cfg.weather.small_craft_factor
    return f


def _components_for_hour(hour: HourConditions, snap: ConditionsSnapshot) -> list[SwellComponent]:
    if snap.swell_components:
        return snap.swell_components
    if hour.wave_height_ft is not None and hour.wave_period_s is not None:
        return [
            SwellComponent(
                direction_deg=hour.swell_dir_deg,
                height_ft=hour.wave_height_ft,
                period_s=hour.wave_period_s,
                source="gridpoint",
            )
        ]
    return []


def _has_thunder_words(text: str | None, cfg: ScoringConfig) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(w in t for w in cfg.vetoes.thunder_words)


def score_hour(
    hour: HourConditions,
    snap: ConditionsSnapshot,
    spot: Spot,
    prefs: Preferences,
    cfg: ScoringConfig,
    active_alerts: list[Alert],
) -> HourScore:
    best = None  # (wave_score, face, component)
    max_face = None
    for c in _components_for_hour(hour, snap):
        face = face_height_ft(c.height_ft, c.period_s, cfg)
        max_face = face if max_face is None else max(max_face, face)
        ws = min(
            MAX_SCORE,
            size_score(face, prefs)
            * period_quality(c.period_s, cfg)
            * swell_direction_factor(c.direction_deg, spot, cfg),
        )
        if best is None or ws > best[0]:
            best = (ws, face, c)

    cls = (
        wind_class(hour.wind_dir_deg, spot, cfg)
        if hour.wind_dir_deg is not None and hour.wind_kt is not None
        else None
    )
    wf = wind_factor(hour.wind_kt, cls, cfg)
    wx = weather_factor(hour, active_alerts, cfg)

    vetoes: list[Veto] = []
    v = cfg.vetoes
    if (hour.wind_kt or 0) >= v.gale_sustained_kt or (hour.gust_kt or 0) >= v.gale_gust_kt:
        vetoes.append(
            Veto(code="gale", detail=f"wind {hour.wind_kt or 0:.0f} kt, gusts {hour.gust_kt or 0:.0f} kt")
        )
    if max_face is not None and max_face < prefs.min_rideable_ft:
        vetoes.append(
            Veto(code="flat", detail=f"est. face {max_face:.1f} ft < {prefs.min_rideable_ft:g} ft minimum")
        )
    if max_face is not None and prefs.max_ft is not None and max_face > prefs.max_ft:
        vetoes.append(
            Veto(code="too_big", detail=f"est. face {max_face:.1f} ft > {prefs.max_ft:g} ft maximum")
        )

    wave_score = best[0] if best else 0.0
    score = 0.0 if vetoes or best is None else min(MAX_SCORE, wave_score * wf * wx)
    return HourScore(
        time=hour.time,
        face_ft=round(best[1], 2) if best else None,
        period_s=best[2].period_s if best else None,
        swell_dir_deg=best[2].direction_deg if best else None,
        wave_score=round(wave_score, 3),
        wind_kt=hour.wind_kt,
        wind_class=cls,
        wind_factor=wf,
        weather_factor=round(wx, 3),
        score=round(score, 3),
        vetoes=vetoes,
    )


def shark_veto(snap: ConditionsSnapshot, cfg: ScoringConfig, ratings: list[SharkRating]) -> Veto | None:
    """One veto per run, naming the highest-threat shark (SPEC-shark-threat §6)."""
    top = veto_rating(ratings, cfg.sharks)
    if top:
        p = top.worst_ping
        return Veto(
            code="shark",
            detail=f"{top.name} ({common_name(top.species)}), {p.distance_mi:.1f} mi, "
            f"{p.age_h:.0f} h ago, threat {top.threat:.2f}",
        )
    if cfg.sharks and cfg.sharks.on_missing_data == "sleep" and snap.sharks and snap.sharks.status == "unavailable":
        return Veto(code="shark", detail="shark tracker unavailable (on_missing_data: sleep)")
    return None


def window_vetoes(
    snap: ConditionsSnapshot, cfg: ScoringConfig, active_alerts: list[Alert], ratings: list[SharkRating]
) -> list[Veto]:
    out: list[Veto] = []
    v = cfg.vetoes
    for h in snap.hours:
        if (h.thunder_pct or 0) >= v.thunder_pct or _has_thunder_words(h.weather_text, cfg):
            out.append(
                Veto(
                    code="lightning",
                    detail=f"{h.time:%H:%M}: thunder {h.thunder_pct or 0:.0f}%"
                    + (f", '{h.weather_text}'" if h.weather_text else ""),
                )
            )
            break
    else:
        if _has_thunder_words(snap.marine_text, cfg):
            out.append(Veto(code="lightning", detail="thunderstorm wording in marine forecast"))
    severe = [a.event for a in active_alerts if a.event in v.severe_events]
    if severe:
        out.append(Veto(code="severe_warning", detail=", ".join(severe)))
    has_waves = bool(snap.swell_components) or any(
        h.wave_height_ft is not None and h.wave_period_s is not None for h in snap.hours
    )
    if not has_waves:
        out.append(Veto(code="no_wave_data", detail="no usable wave forecast or observation"))
    shark = shark_veto(snap, cfg, ratings)
    if shark:
        out.append(shark)
    return out


def best_block(hourly: list[HourScore], block_hours: int) -> BlockScore | None:
    if not hourly:
        return None
    n = max(1, min(block_hours, len(hourly)))
    best = None
    for i in range(len(hourly) - n + 1):
        chunk = hourly[i : i + n]
        mean = sum(h.score for h in chunk) / n
        if best is None or mean > best.score:
            best = BlockScore(start=chunk[0].time, end=chunk[-1].time, score=round(mean, 3), hours=chunk)
    return best


def score_snapshot(
    snap: ConditionsSnapshot,
    spot: Spot,
    prefs: Preferences,
    cfg: ScoringConfig,
    block_hours: int = 2,
) -> ScoreResult:
    active = [a for a in snap.alerts if alert_active(a, snap.window_start, snap.window_end)]
    hourly = [score_hour(h, snap, spot, prefs, cfg, active) for h in snap.hours]
    block = best_block(hourly, block_hours)
    ratings = rate_sharks(snap.sharks, cfg.sharks)  # never touches the hourly or base score
    vetoes = window_vetoes(snap, cfg, active, ratings)
    if block:
        # An hour-level veto blocks the decision only if it applies to every hour of the best block.
        common = None
        for h in block.hours:
            codes = {v.code for v in h.vetoes}
            common = codes if common is None else common & codes
        seen = {v.code for v in vetoes}
        for v in block.hours[0].vetoes:
            if v.code in (common or set()) and v.code not in seen:
                vetoes.append(v)
                seen.add(v.code)
    return ScoreResult(
        scoring_version=cfg.version,
        vetoes=vetoes,
        hourly=hourly,
        best_block=block,
        base_score=block.score if block else 0.0,
        sharks=ratings,
    )
