"""Build snapshots from compact descriptions. Shared by tests and eval scenarios."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .models import (  # noqa: F401  (re-exported for scenarios)
    Alert,
    BuoyObs,
    ConditionsSnapshot,
    HourConditions,
    SharkPing,
    SharkReport,
    SharkSighting,
    SwellComponent,
    WindObs,
)

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
    sharks: list[dict] | None = None,
    sharks_status: str | None = None,
    **extra,
) -> ConditionsSnapshot:
    """`conditions` applies to every hour; `hours[i]` overrides fields for hour i.

    `sharks` describes nearby sharks as {species, stage, distance_mi, age_h, name?}; each becomes one
    ping relative to the fixed run time. `sharks_status` overrides the report status (e.g. "unavailable").
    Leaving both out gives `sharks=None`, as for snapshots that predate the shark check.
    """
    run_at = DEFAULT_START.replace(hour=5, minute=0)
    if sharks is not None or sharks_status is not None:
        missing_sources = list(missing_sources or [])
        if sharks_status == "unavailable":
            missing_sources.append("sharks")
        extra["sharks"] = SharkReport(
            status=sharks_status or "checked",
            detail="sharks: HTTP 503 (synthetic)" if sharks_status == "unavailable" else None,
            nearby=[
                SharkSighting(
                    id=i + 1,
                    name=s.get("name", f"Shark {i + 1}"),
                    species=s["species"],
                    stage_of_life=s.get("stage"),
                    track_used=True,
                    pings=[
                        SharkPing(
                            time=run_at - timedelta(hours=s["age_h"]),
                            lat=0.0,
                            lon=0.0,
                            distance_mi=s["distance_mi"],
                            age_h=s["age_h"],
                        )
                    ],
                )
                for i, s in enumerate(sharks or [])
            ],
        )
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
        run_at=run_at,
        window_start=DEFAULT_START,
        window_end=DEFAULT_END,
        hours=hrs,
        swell_components=[SwellComponent(**c) for c in (swell_components or [])],
        alerts=[Alert(**a) for a in (alerts or [])],
        marine_text=marine_text,
        missing_sources=missing_sources or [],
        **extra,
    )
