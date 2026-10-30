"""L2: the shark veto through the whole pipeline, on the demo GO day with a stubbed Claude client."""

import json
import shutil

import pytest
from conftest import FIXTURES
from test_pipeline import StubClient

from surf_agent.decide import decide, spoken_message_ok
from surf_agent.models import Assessment
from surf_agent.pipeline import RunOptions, run_once
from surf_agent.scoring import score_snapshot
from surf_agent.testing import make_snapshot

GO_DAY = FIXTURES / "raw_demo_go_day"  # run_at 2026-10-30T16:50-04:00 = 20:50Z
WHITE_NEAR_PIER = {
    "type": "Feature",
    "geometry": {"type": "Point", "coordinates": [-80.0400, 26.9100]},  # ~1.5 mi NE of the pier
    "properties": {
        "id": 9001, "name": "Pierside", "category_name": {"en": "Sharks"},
        "species": "White Shark (Carcharodon carcharias)", "stage_of_life": "Adult",
        "length": "13 ft", "last_move_datetime": "2026-10-30T17:50:00Z",
    },
}


class Boom:
    def wake(self, message):
        raise AssertionError("notifier must not be called")


def go_day_with(tmp_path, feed: str | None):
    raw = tmp_path / "raw"
    shutil.copytree(GO_DAY, raw)
    if feed is not None:
        (raw / "sharks.json").write_text(feed, encoding="utf-8")
    return raw


def feed(*features) -> str:
    return json.dumps({"type": "FeatureCollection", "features": list(features)})


def stub(**overrides):
    payload = {
        "adjustment": 0, "adjustment_reason": "", "data_concerns": [], "confidence": "high",
        "explanation": "Clean 4 ft groundswell with light offshore wind.",
        "spoken_message": "Juno Pier: 4 feet at 11 seconds, light offshore wind. Clean and fun. Score 9.",
    }
    return StubClient({**payload, **overrides})


def test_go_day_baseline_is_go_without_shark_data(settings, spot, cfg, tmp_path):
    res = run_once(settings, spot, cfg, RunOptions(replay=GO_DAY, use_llm=False), data_dir=tmp_path)
    assert res.decision.decision == "GO"
    assert res.snapshot.sharks.status == "not_captured"
    assert res.record["sharks"]["status"] == "not_captured" and "sharks" not in res.snapshot.missing_sources


def test_shark_veto_beats_perfect_waves_and_positive_adjustment(settings, spot, cfg, tmp_path):
    raw = go_day_with(tmp_path, feed(WHITE_NEAR_PIER))
    client = stub(adjustment=1.0, adjustment_reason="Buoy shows 4 ft at 11 s at 16:30, above forecast.")
    res = run_once(settings, spot, cfg, RunOptions(replay=raw, force_wake=True), data_dir=tmp_path,
                   client=client, notifier=Boom())
    assert res.decision.decision == "NO_GO" and res.notify is None
    shark = next(v for v in res.decision.vetoes if v.code == "shark")
    assert "Pierside (White Shark)" in shark.detail and "threat 1.00" in shark.detail
    rec = res.record["sharks"]
    assert rec["status"] == "checked" and rec["vetoed"] and rec["max_threat"] == 1.0
    assert rec["nearby"][0]["rating"]["worst_ping"]["distance_mi"] == pytest.approx(1.5, abs=0.2)
    # Claude saw the shark in its input.
    assert "Pierside" in client.requests[0]["messages"][0]["content"]
    # The raw feed is saved, so the run replays (and can be promoted) exactly.
    assert (res.run_dir / "raw" / "sharks.json").exists()


def test_report_headline_for_shark_veto(settings, spot, cfg, tmp_path):
    raw = go_day_with(tmp_path, feed(WHITE_NEAR_PIER))
    res = run_once(settings, spot, cfg, RunOptions(replay=raw, use_llm=False), data_dir=tmp_path)
    assert res.decision.decision == "NO_GO" and res.decision.final_score == 0
    page = res.report.read_text(encoding="utf-8")
    assert "A tagged shark surfaced near the pier." in page


def test_shark_below_threshold_does_not_block(settings, spot, cfg, tmp_path):
    blue = json.loads(json.dumps(WHITE_NEAR_PIER))
    blue["properties"].update(species="Blue Shark (Prionace glauca)", stage_of_life="Juvenile", name="Azure")
    blue["geometry"]["coordinates"] = [-80.0557, 26.8933 + 8 / 69.093]  # 8 mi north
    raw = go_day_with(tmp_path, feed(blue))
    res = run_once(settings, spot, cfg, RunOptions(replay=raw, use_llm=False), data_dir=tmp_path)
    assert res.decision.decision == "GO"
    assert res.score.sharks[0].threat == pytest.approx(0.25 * 0.6 * 1.0 * 0.6, abs=0.01)
    assert not res.record["sharks"]["vetoed"]


@pytest.mark.parametrize("on_missing,expected", [("ignore", "GO"), ("sleep", "NO_GO")])
def test_feed_unavailable(settings, spot, cfg, tmp_path, on_missing, expected):
    raw = go_day_with(tmp_path, "<html>down for maintenance</html>")
    cfg2 = cfg.model_copy(update={"sharks": cfg.sharks.model_copy(update={"on_missing_data": on_missing})})
    res = run_once(settings, spot, cfg2, RunOptions(replay=raw, use_llm=False), data_dir=tmp_path)
    assert res.snapshot.sharks.status == "unavailable" and "sharks" in res.snapshot.missing_sources
    assert res.decision.decision == expected
    assert "shark tracker unavailable" in res.decision.explanation.lower()


def test_explanation_claiming_safety_is_replaced(settings, spot, cfg, tmp_path):
    raw = go_day_with(tmp_path, feed())
    res = run_once(settings, spot, cfg, RunOptions(replay=raw), data_dir=tmp_path,
                   client=stub(explanation="Clean 4 ft groundswell and no sharks around, so go surf."))
    assert res.decision.decision == "GO"
    assert "sharks" not in res.decision.explanation.lower()
    assert "explanation failed shark wording check; using template" in res.decision.guardrail_notes


def test_adjustment_citing_sharks_is_dropped(settings, spot, cfg):
    snap = make_snapshot(
        conditions={"wave_height_ft": 4, "wave_period_s": 11, "swell_dir_deg": 90, "wind_kt": 5, "wind_dir_deg": 260},
        sharks=[{"species": "Blue Shark (Prionace glauca)", "stage": "Adult", "distance_mi": 5, "age_h": 10}],
    )
    score = score_snapshot(snap, spot, settings.preferences, cfg)
    a = Assessment(adjustment=-1.0, adjustment_reason="A blue shark was reported 5 mi away 10 h ago.",
                   data_concerns=[], confidence="high", explanation="Good waves.",
                   spoken_message="Juno Pier: 4 feet at 11 seconds. Score 9.")
    d = decide(score, a, settings.preferences, spot)
    assert d.adjustment_applied == 0 and d.decision == "GO"
    assert any("cites sharks" in n for n in d.guardrail_notes)


@pytest.mark.parametrize(
    "msg,ok",
    [
        ("Juno Pier: 4 feet at 11 seconds, light offshore wind. Score 9.", True),
        ("Juno Pier: 4 feet at 11 seconds. Shark nearby, no go. Score 0.", False),
    ],
)
def test_spoken_message_never_mentions_sharks(msg, ok):
    assert spoken_message_ok(msg) is ok
