"""Fetch every source in parallel and normalize into one ConditionsSnapshot (SPEC §5)."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import Session, SharkConfig, Spot
from .models import ConditionsSnapshot, SharkReport, SharkSighting
from .sharks import haversine_mi, qualifying_pings
from .sources import ndbc, nws, ocearch, tides
from .sources.cwf_parse import parse_wave_detail
from .sources.http import Fetcher, NotCaptured, fetch_all
from .sources.sun import dawn_and_sunrise

log = logging.getLogger(__name__)

FORECAST_MAX_AGE = timedelta(hours=12)
OBS_MAX_AGE = timedelta(hours=2)


def target_date(run_at: datetime, session: Session) -> date:
    """Before the session ends today -> today; otherwise preview tomorrow morning."""
    end = time.fromisoformat(session.end)
    return run_at.date() if run_at.time() < end else run_at.date() + timedelta(days=1)


def session_window(spot: Spot, session: Session, day: date) -> tuple[datetime, datetime, datetime, list[datetime]]:
    tz = ZoneInfo(spot.timezone)
    dawn, sunrise = dawn_and_sunrise(spot.lat, spot.lon, day, tz)
    start = dawn if session.start == "dawn" else sunrise
    end = datetime.combine(day, time.fromisoformat(session.end), tzinfo=tz)
    hours = []
    h = start.replace(minute=0, second=0, microsecond=0)
    while h < end:
        hours.append(h)
        h += timedelta(hours=1)
    return start, end, sunrise, hours


async def shark_report(
    fetcher: Fetcher, feed: str | Exception, spot: Spot, run_at: datetime, cfg: SharkConfig
) -> SharkReport:
    """Feed -> sharks pinging inside the radius in the lookback window (SPEC-shark-threat §4, §8)."""
    if isinstance(feed, NotCaptured):
        return SharkReport(status="not_captured", detail=str(feed))
    if isinstance(feed, Exception):
        return SharkReport(status="unavailable", detail=str(feed))
    try:
        n_animals, sharks = ocearch.parse_feed(feed)
    except Exception as e:  # noqa: BLE001 - includes FeedSchemaError and bad JSON
        log.error("shark_feed_schema_changed: %s", e)
        return SharkReport(status="unavailable", detail=f"feed schema changed: {e}")

    lookback = timedelta(hours=cfg.lookback_hours)
    recent = [s for s in sharks if s.last_move and timedelta(0) <= run_at - s.last_move <= lookback]
    recent.sort(key=lambda s: haversine_mi(spot.lat, spot.lon, s.lat, s.lon))
    to_track = recent[: cfg.max_track_requests]
    cache_s = cfg.cache_minutes * 60
    tracks = await fetch_all(fetcher, [ocearch.track_request(s.id, cache_s) for s in to_track])

    nearby: list[SharkSighting] = []
    fetched = 0
    for s in recent:
        points = [(s.last_move, s.lat, s.lon)]
        body = tracks.get(f"shark_track_{s.id}")
        track_used = False
        if isinstance(body, str):
            try:
                points += ocearch.parse_track(body)
                track_used = True
                fetched += 1
            except Exception as e:  # noqa: BLE001
                log.warning("shark_track_failed:%s %s", s.id, e)
        elif body is not None:
            log.warning("shark_track_failed:%s %s", s.id, body)
        pings = qualifying_pings(points, spot.lat, spot.lon, run_at, cfg)
        if pings:
            nearby.append(
                SharkSighting(
                    id=s.id, name=s.name, species=s.species, stage_of_life=s.stage_of_life,
                    length=s.length, pings=pings, track_used=track_used,
                )
            )
    if len(recent) > len(to_track):
        log.warning("shark_tracks_truncated: %d sharks, %d tracked", len(recent), len(to_track))
    return SharkReport(
        status="checked",
        feed_animals=n_animals,
        feed_sharks=len(sharks),
        sharks_in_lookback=len(recent),
        tracks_fetched=fetched,
        tracks_truncated=len(recent) > len(to_track),
        nearby=nearby,
    )


async def build_snapshot(
    fetcher: Fetcher,
    spot: Spot,
    session: Session,
    run_at: datetime,
    day: date | None = None,
    sharks: SharkConfig | None = None,
) -> ConditionsSnapshot:
    tz = ZoneInfo(spot.timezone)
    run_at = run_at.astimezone(tz)
    day = day or target_date(run_at, session)
    start, end, sunrise, hours = session_window(spot, session, day)
    noaa = spot.noaa

    reqs = [
        *nws.requests_for(noaa),
        ndbc.realtime_request("buoy", noaa.wave_buoy),
        ndbc.realtime_request("wind", noaa.wind_station),
    ]
    if noaa.tide_station:
        reqs.append(tides.tide_request(noaa.tide_station, day - timedelta(days=1)))
    if sharks and sharks.enabled:
        reqs.append(ocearch.feed_request(sharks.cache_minutes * 60))
    raw = await fetch_all(fetcher, reqs)

    snap = ConditionsSnapshot(
        spot_id=spot.id, run_at=run_at, window_start=start, window_end=end, sunrise=sunrise
    )
    missing = snap.missing_sources

    def failed(name: str, why: object) -> None:
        log.warning("source %s unavailable: %s", name, why)
        missing.append(name)

    # Gridpoint forecast -> hourly conditions (always produce one row per window hour).
    from .models import HourConditions

    snap.hours = [HourConditions(time=h) for h in hours]
    body = raw.get("gridpoint")
    if isinstance(body, str):
        try:
            parsed, updated = nws.parse_gridpoint(body, hours)
            snap.source_times["gridpoint"] = updated
            if run_at - updated > FORECAST_MAX_AGE:
                failed("gridpoint", f"stale ({updated.isoformat()})")
            else:
                snap.hours = parsed
        except Exception as e:  # noqa: BLE001 - a bad source never fails the run
            failed("gridpoint", e)
    else:
        failed("gridpoint", body)

    # Marine forecast text -> period text + parsed swell components.
    body = raw.get("marine_forecast")
    if isinstance(body, str):
        try:
            name, text, updated = nws.pick_marine_period(body, day, run_at.date())
            if updated:
                snap.source_times["marine_forecast"] = updated
            if text is None:
                failed("marine_forecast", f"no period found for {day}")
            elif updated and run_at - updated > FORECAST_MAX_AGE:
                failed("marine_forecast", f"stale ({updated.isoformat()})")
            else:
                snap.marine_period_name, snap.marine_text = name, text
                snap.swell_components = parse_wave_detail(text)
        except Exception as e:  # noqa: BLE001
            failed("marine_forecast", e)
    else:
        failed("marine_forecast", body)

    body = raw.get("alerts")
    if isinstance(body, str):
        try:
            snap.alerts = nws.parse_alerts(body)
        except Exception as e:  # noqa: BLE001
            failed("alerts", e)
    else:
        failed("alerts", body)

    body = raw.get("buoy")
    if isinstance(body, str):
        try:
            obs = ndbc.parse_buoy(body, noaa.wave_buoy)
            if obs and run_at - obs.observed_at <= OBS_MAX_AGE:
                snap.buoy_now = obs
                snap.source_times["buoy"] = obs.observed_at
            else:
                failed("buoy", "no recent observation")
        except Exception as e:  # noqa: BLE001
            failed("buoy", e)
    else:
        failed("buoy", body)

    wind = None
    body = raw.get("wind")
    if isinstance(body, str):
        try:
            wind = ndbc.parse_wind(body, noaa.wind_station)
        except Exception as e:  # noqa: BLE001
            log.warning("wind parse failed: %s", e)
    if (wind is None or run_at - wind.observed_at > OBS_MAX_AGE) and noaa.wind_station_fallback:
        fb = (await fetch_all(fetcher, [nws.station_request(noaa.wind_station_fallback)]))["wind_fallback"]
        try:
            wind = nws.parse_station_obs(fb, noaa.wind_station_fallback) if isinstance(fb, str) else None
        except Exception as e:  # noqa: BLE001
            log.warning("fallback wind parse failed: %s", e)
            wind = None
    if wind and run_at - wind.observed_at <= OBS_MAX_AGE:
        snap.wind_now = wind
        snap.source_times["wind"] = wind.observed_at
    else:
        failed("wind", "no recent observation")

    if noaa.tide_station:
        body = raw.get("tides")
        if isinstance(body, str):
            try:
                events = tides.parse_tides(body, tz)
                lo, hi = start - timedelta(hours=7), end + timedelta(hours=7)
                snap.tides = [t for t in events if lo <= t.time <= hi]
            except Exception as e:  # noqa: BLE001
                failed("tides", e)
        else:
            failed("tides", body)

    if sharks is not None:
        if not sharks.enabled:
            snap.sharks = SharkReport(status="disabled")
        else:
            snap.sharks = await shark_report(fetcher, raw["sharks"], spot, run_at, sharks)
            if snap.sharks.status == "unavailable":
                failed("sharks", snap.sharks.detail)

    return snap
