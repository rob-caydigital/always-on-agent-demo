"""L1: OCEARCH feed parsing and the shark report, against trimmed real responses captured 2026-10-30."""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from conftest import FIXTURES

from surf_agent.config import RecencyBucket
from surf_agent.snapshot import shark_report
from surf_agent.sources import ocearch
from surf_agent.sources.http import LiveFetcher, NotCaptured, ReplayFetcher, SourceError

RAW = FIXTURES / "ocearch_2026-10-30"
RUN_AT = datetime(2026, 10, 30, 21, 50, tzinfo=UTC)
CONTENDER = 2344847  # white shark, last ping 2026-10-30 17:44Z at 42.4307, -67.5134


def feed_body() -> str:
    return (RAW / "sharks.json").read_text(encoding="utf-8")


def report(spot, scfg, feed=None, raw_dir=RAW, run_at=RUN_AT):
    fetcher = ReplayFetcher(raw_dir)
    return asyncio.run(shark_report(fetcher, feed_body() if feed is None else feed, spot, run_at, scfg))


def test_parse_feed_keeps_only_sharks():
    n, sharks = ocearch.parse_feed(feed_body())
    assert n == 6 and len(sharks) == 5  # one turtle dropped
    by_id = {s.id: s for s in sharks}
    c = by_id[CONTENDER]
    assert c.species == "White Shark (Carcharodon carcharias)"
    assert (round(c.lat, 4), round(c.lon, 4)) == (42.4307, -67.5134)
    assert c.last_move == datetime(2026, 10, 30, 17, 44, 19, 809000, tzinfo=UTC)
    assert any(s.length is None for s in sharks)
    assert any(s.last_move is None for s in sharks)


@pytest.mark.parametrize(
    "body",
    [
        "{}",
        json.dumps({"features": [{"properties": {"id": 1, "category_name": {"en": "Sharks"}}, "geometry": None}]}),
        json.dumps({"features": [{"properties": {"id": 1, "category_name": {"en": "Turtles"}}}]}),
    ],
)
def test_parse_feed_schema_change(body):
    with pytest.raises(ocearch.FeedSchemaError):
        ocearch.parse_feed(body)


def test_parse_track_keeps_duplicates_for_later_dedupe():
    pts = ocearch.parse_track((RAW / f"shark_track_{CONTENDER}.json").read_text(encoding="utf-8"))
    assert len(pts) == 8
    assert pts[0] == pts[1]  # the feed repeats pings under new import ids
    assert pts[-1][0] == datetime(2026, 10, 30, 17, 44, 19, 809000, tzinfo=UTC)


def test_nothing_near_juno(spot, cfg):
    rep = report(spot, cfg.sharks)
    assert rep.status == "checked"
    assert (rep.feed_animals, rep.feed_sharks, rep.sharks_in_lookback, rep.tracks_fetched) == (6, 5, 1, 1)
    assert rep.nearby == []


def test_shark_at_the_spot_is_found_and_deduped(spot, cfg):
    at_contender = spot.model_copy(update={"lat": 42.4307, "lon": -67.5134})
    rep = report(at_contender, cfg.sharks)
    assert [s.id for s in rep.nearby] == [CONTENDER]
    s = rep.nearby[0]
    assert s.track_used and s.name == "Contender" and s.stage_of_life
    assert len(s.pings) == 1  # latest position and the identical last track ping collapse
    assert s.pings[0].distance_mi == 0 and s.pings[0].age_h == pytest.approx(4.09, abs=0.01)


def test_old_track_ping_counts_when_inside_lookback(spot, cfg):
    # Contender was off Nova Scotia on 2026-10-06; with a long lookback that ping qualifies even
    # though the latest position is ~400 mi away.
    long = cfg.sharks.model_copy(
        update={"lookback_hours": 800, "recency_factors": [RecencyBucket(max_hours=800, factor=0.5)]}
    )
    near_oct6 = spot.model_copy(update={"lat": 46.31349, "lon": -62.06796})
    rep = report(near_oct6, long)
    pings = rep.nearby[0].pings
    # Only the 2026-10-06 23:38Z ping is inside 10 mi; the two earlier that day are ~25 mi south.
    assert [(p.time.day, p.distance_mi) for p in pings] == [(6, 0)]


def test_track_missing_falls_back_to_latest_position(spot, cfg, tmp_path):
    (tmp_path / "sharks.json").write_text(feed_body(), encoding="utf-8")
    at_contender = spot.model_copy(update={"lat": 42.4307, "lon": -67.5134})
    rep = report(at_contender, cfg.sharks, raw_dir=tmp_path)
    assert rep.tracks_fetched == 0
    assert rep.nearby[0].track_used is False and len(rep.nearby[0].pings) == 1


def test_track_cap(spot, cfg):
    capped = cfg.sharks.model_copy(update={"max_track_requests": 0})
    rep = report(spot, capped)
    assert rep.tracks_truncated and rep.tracks_fetched == 0


def test_feed_failures(spot, cfg):
    assert report(spot, cfg.sharks, feed=NotCaptured("sharks: not in replay data")).status == "not_captured"
    down = report(spot, cfg.sharks, feed=SourceError("sharks: HTTP 503"))
    assert down.status == "unavailable" and "503" in down.detail
    changed = report(spot, cfg.sharks, feed="<html>maintenance</html>")
    assert changed.status == "unavailable" and "schema" in changed.detail


def test_live_fetcher_caches_feed_without_cache_headers(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, text=feed_body())  # Mapotic sends no Cache-Control

    async def go():
        for _ in range(2):
            async with LiveFetcher("test", cache_dir=tmp_path, transport=httpx.MockTransport(handler)) as f:
                await f.fetch(ocearch.feed_request(1800))

    asyncio.run(go())
    assert len(calls) == 1
