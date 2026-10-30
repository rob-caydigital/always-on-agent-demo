"""Build snapshots from compact descriptions. Shared by tests and eval scenarios."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .models import Alert, BuoyObs, ConditionsSnapshot, HourConditions, SwellComponent, WindObs  # noqa: F401  (re-exported for scenarios)

TZ = ZoneInfo("America/New_York")
# A fixed post-DST morning: dawn 06:06, window to 08:30 -> hours 06:00, 07:00, 08:00.
DEFAULT_START = datetime(2026, 11, 15, 6, 6, tzinfo=TZ)
DEFAULT_END = datetime(2026, 11, 15, 8, 30, tzinfo=TZ)


def make_snapshot(
    *,
    conditions: dict | None = None,
    hours: list[dict] | None = None,
    swell_components: list[dict] | None = None,
    alerts: list[dict] | None = None,
    marine_text: str | None = None,
    missing_sources: list[str] | None = None,
    n_hours: int = 3,
    **extra,
) -> ConditionsSnapshot:
    """`conditions` applies to every hour; `hours[i]` overrides fields for hour i."""
    base = conditions or {}
    hrs = []
    for i in range(n_hours):
        t = DEFAULT_START.replace(minute=0) + timedelta(hours=i)
        fields = dict(base)
        if hours and i < len(hours):
            fields.update(hours[i])
        hrs.append(HourConditions(time=t, **fields))
    return ConditionsSnapshot(
        spot_id="juno-beach-pier",
        run_at=DEFAULT_START.replace(hour=5, minute=0),
        window_start=DEFAULT_START,
        window_end=DEFAULT_END,
        hours=hrs,
        swell_components=[SwellComponent(**c) for c in (swell_components or [])],
        alerts=[Alert(**a) for a in (alerts or [])],
        marine_text=marine_text,
        missing_sources=missing_sources or [],
        **extra,
    )
