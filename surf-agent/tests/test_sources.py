"""L1: parsers and the snapshot builder against real NOAA responses captured 2026-10-30."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from conftest import FIXTURES

from surf_agent.snapshot import build_snapshot, session_window, target_date
from surf_agent.sources.cwf_parse import parse_wave_detail
from surf_agent.sources.http import LiveFetcher, ReplayFetcher, Request, SourceError, fetch_all
from surf_agent.sources.nws import parse_interval

RAW = FIXTURES / "raw_2026-10-30"
TZ = ZoneInfo("America/New_York")
RUN_AT = datetime(2026, 10, 30, 16, 50, tzinfo=TZ)  # fixtures captured ~16:45 EDT


def test_parse_interval():
    s, e = parse_interval("2026-10-30T20:00:00+00:00/P1DT13H")
    assert (e - s).total_seconds() == 37 * 3600


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Seas 5 to 7 ft. Wave Detail: N 6 ft at 7 seconds and E 4 ft at 4 seconds. A slight chance of tstms.",
         [(0, 6, 7), (90, 4, 4)]),
        ("Wave Detail: ENE 3 ft at 11 seconds.", [(67.5, 3, 11)]),
        ("Seas 2 ft or less.", []),
        (None, []),
    ],
)
def test_parse_wave_detail(text, expected):
    got = [(c.direction_deg, c.height_ft, c.period_s) for c in parse_wave_detail(text)]
    assert got == expected


def test_target_date(settings):
    assert target_date(datetime(2026, 11, 2, 5, 0, tzinfo=TZ), settings.session).day == 2
    assert target_date(datetime(2026, 11, 2, 9, 0, tzinfo=TZ), settings.session).day == 3


def test_session_window_handles_dst(spot, settings):
    # Before DST ends (sunrise ~07:29): window 07:05-08:30 -> 2 hours.
    start, _end, _, hours = session_window(spot, settings.session, datetime(2026, 10, 31).date())
    assert start.hour == 7 and len(hours) == 2
    # After (sunrise ~06:30): window ~06:06-08:30 -> 3 hours.
    start, _end, _, hours = session_window(spot, settings.session, datetime(2026, 11, 2).date())
    assert start.hour == 6 and len(hours) == 3


def test_snapshot_from_real_fixtures(spot, settings):
    snap = asyncio.run(build_snapshot(ReplayFetcher(RAW), spot, settings.session, RUN_AT))
    assert snap.window_start.date().isoformat() == "2026-10-31"  # evening run previews tomorrow
    assert snap.marine_period_name == "Sat"
    assert [(c.direction_deg, c.height_ft, c.period_s) for c in snap.swell_components] == [(45, 7, 6)]
    h = snap.hours[0]
    assert h.time.hour == 7
    assert h.wind_kt == pytest.approx(19.0, abs=0.05)  # 35.188 km/h at 11:00Z -> 19.0 kt
    assert h.wave_height_ft and h.wave_height_ft > 3
    assert {a.event for a in snap.alerts} >= {"Small Craft Advisory", "Rip Current Statement"}
    assert snap.buoy_now and snap.buoy_now.height_ft == pytest.approx(1.8 * 3.28084)
    assert snap.wind_now and snap.wind_now.station == "LKWF1"
    assert snap.tides and all(t.kind in "HL" for t in snap.tides)
    assert snap.missing_sources == []


def test_missing_sources_are_reported_not_raised(spot, settings, tmp_path):
    (tmp_path / "gridpoint.json").write_text((RAW / "gridpoint.json").read_text())
    snap = asyncio.run(build_snapshot(ReplayFetcher(tmp_path), spot, settings.session, RUN_AT))
    assert set(snap.missing_sources) >= {"marine_forecast", "alerts", "buoy", "wind", "tides"}
    assert snap.hours[0].wave_height_ft is not None


def test_live_fetcher_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] == 1 else httpx.Response(200, text="ok")

    async def go():
        async with LiveFetcher("test", retries=2, transport=httpx.MockTransport(handler)) as f:
            f_sleep = asyncio.sleep
            asyncio.sleep = lambda *_: f_sleep(0)
            try:
                return await f.fetch(Request("x", "https://example.test/x", kind="text"))
            finally:
                asyncio.sleep = f_sleep

    assert asyncio.run(go()) == "ok" and calls["n"] == 2


def test_fetch_all_returns_exceptions():
    async def go():
        async with LiveFetcher("test", retries=0,
                               transport=httpx.MockTransport(lambda r: httpx.Response(404))) as f:
            return await fetch_all(f, [Request("a", "https://example.test/a")])

    assert isinstance(asyncio.run(go())["a"], SourceError)
