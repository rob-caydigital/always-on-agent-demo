"""L1: the scoring engine against SPEC §6 tables and the §6.6 worked examples."""

import pytest

from surf_agent.decide import decide
from surf_agent.scoring import (
    angle_off,
    face_height_ft,
    period_quality,
    score_snapshot,
    size_score,
    swell_direction_factor,
    wind_class,
    wind_factor,
)
from surf_agent.testing import make_snapshot

# SPEC §6.6, with Juno's verified orientation (beach faces 73°, offshore 253°).
# (label, height, period, swell_dir, wind_kt, wind_dir, thunder_pct, expected_score, decision, veto)
WORKED_EXAMPLES = [
    ("3ft@20s offshore - epic", 3, 20, 90, 6, 270, 0, 10.0, "GO", None),
    ("3ft@3s chop", 3, 3, 90, 3, 270, 0, 2.2, "NO_GO", None),
    ("3ft@9s ENE 14kt blown out", 3, 9, 90, 14, 67.5, 0, 2.25, "NO_GO", None),
    ("3ft@8s SW 4kt", 3, 8, 90, 4, 225, 0, 8.25, "GO", None),
    ("3ft@7s glassy - maybe", 3, 7, 90, 3, 0, 0, 5.5, "MAYBE", None),
    ("3ft@7s cross-shore 8kt", 3, 7, 90, 8, 343, 0, 4.25, "NO_GO", None),
    ("2ft@8s flat", 2, 8, 90, 4, 225, 0, 0.0, "NO_GO", "flat"),
    ("2.5ft@10s glassy, 40% thunder", 2.5, 10, 90, 3, 270, 40, 0.0, "NO_GO", "lightning"),
    ("6ft@14s W 8kt - no max", 6, 14, 90, 8, 270, 0, 10.0, "GO", None),
    ("5ft@9s NE 22kt gale", 5, 9, 45, 22, 45, 0, 0.0, "NO_GO", "gale"),
]


@pytest.mark.parametrize(
    "label,h,p,sd,wk,wd,thunder,expected,decision,veto", WORKED_EXAMPLES, ids=[e[0] for e in WORKED_EXAMPLES]
)
def test_worked_examples(label, h, p, sd, wk, wd, thunder, expected, decision, veto, spot, prefs, cfg):
    snap = make_snapshot(
        conditions=dict(
            wave_height_ft=h, wave_period_s=p, swell_dir_deg=sd,
            wind_kt=wk, wind_dir_deg=wd, thunder_pct=thunder, pop_pct=10,
        )
    )
    result = score_snapshot(snap, spot, prefs, cfg)
    d = decide(result, None, prefs, spot)
    if veto is None:
        assert result.base_score == pytest.approx(expected, abs=0.01)
        assert not result.vetoes
    else:
        assert veto in {v.code for v in result.vetoes}
    assert d.decision == decision


@pytest.mark.parametrize(
    "period,mult,quality",
    [(5.99, 1.0, 0.2), (6, 1.1, 0.5), (8, 1.2, 0.75), (10, 1.4, 1.0), (13, 1.6, 1.05), (16, 1.8, 1.1)],
)
def test_period_buckets_lower_inclusive(period, mult, quality, cfg):
    assert face_height_ft(1, period, cfg) == pytest.approx(mult)
    assert period_quality(period, cfg) == quality


def test_size_score_no_max(prefs):
    assert size_score(2.99, prefs) == 0
    assert size_score(3.0, prefs) == 10
    assert size_score(20, prefs) == 10


def test_size_score_with_max(prefs):
    p = prefs.model_copy(update={"max_ft": 8})
    assert size_score(5, p) == 10
    assert size_score(6.5, p) == pytest.approx(7.5)
    assert size_score(8, p) == 5


def test_angle_off():
    assert angle_off(350, 10) == 20
    assert angle_off(73, 253) == 180


@pytest.mark.parametrize(
    "direction,factor",
    [(73, 1.0), (103, 1.0), (133, 0.8), (150, 0.5), (0, 0.5), (200, 0.2), (None, 0.8)],
)
def test_swell_direction_factor(direction, factor, spot, cfg):
    assert swell_direction_factor(direction, spot, cfg) == factor


@pytest.mark.parametrize(
    "wind_dir,cls",
    [(253, "offshore"), (283, "offshore"), (300, "cross_offshore"), (343, "cross"),
     (30, "cross_onshore"), (73, "onshore")],
)
def test_wind_class(wind_dir, cls, spot, cfg):
    assert wind_class(wind_dir, spot, cfg) == cls


@pytest.mark.parametrize(
    "kt,cls,factor",
    [(4.9, "onshore", 1.1), (5, "onshore", 0.6), (10, "offshore", 1.0), (15, "cross", 0.35),
     (None, None, 0.85), (3, None, 1.1), (8, None, 0.85)],
)
def test_wind_factor_buckets(kt, cls, factor, cfg):
    assert wind_factor(kt, cls, cfg) == factor


def test_gust_alone_triggers_gale(spot, prefs, cfg):
    snap = make_snapshot(conditions=dict(wave_height_ft=4, wave_period_s=10, swell_dir_deg=90,
                                         wind_kt=12, gust_kt=28, wind_dir_deg=253))
    assert "gale" in {v.code for v in score_snapshot(snap, spot, prefs, cfg).vetoes}


def test_thunder_wording_in_marine_text_vetoes(spot, prefs, cfg):
    snap = make_snapshot(conditions=dict(wave_height_ft=4, wave_period_s=10, wind_kt=4, wind_dir_deg=253),
                         marine_text="Seas 3 ft. A slight chance of tstms.")
    assert "lightning" in {v.code for v in score_snapshot(snap, spot, prefs, cfg).vetoes}


def test_severe_alert_vetoes_and_small_craft_penalizes(spot, prefs, cfg):
    cond = dict(wave_height_ft=4, wave_period_s=8, swell_dir_deg=90, wind_kt=6, wind_dir_deg=253)
    sca = make_snapshot(conditions=cond, alerts=[{"event": "Small Craft Advisory"}])
    clean = make_snapshot(conditions=cond)
    assert score_snapshot(sca, spot, prefs, cfg).base_score == pytest.approx(
        score_snapshot(clean, spot, prefs, cfg).base_score * 0.9
    )
    smw = make_snapshot(conditions=cond, alerts=[{"event": "Special Marine Warning"}])
    assert "severe_warning" in {v.code for v in score_snapshot(smw, spot, prefs, cfg).vetoes}


def test_no_wave_data_vetoes(spot, prefs, cfg):
    snap = make_snapshot(conditions=dict(wind_kt=4, wind_dir_deg=253))
    assert "no_wave_data" in {v.code for v in score_snapshot(snap, spot, prefs, cfg).vetoes}


def test_best_component_wins(spot, prefs, cfg):
    """Two swells: short-period N chop and a cleaner E swell. The E swell drives the score."""
    snap = make_snapshot(
        conditions=dict(wind_kt=4, wind_dir_deg=253),
        swell_components=[
            {"direction_deg": 0, "height_ft": 6, "period_s": 5},
            {"direction_deg": 90, "height_ft": 3, "period_s": 11},
        ],
    )
    r = score_snapshot(snap, spot, prefs, cfg)
    assert r.best_block.hours[0].period_s == 11
    assert r.base_score == pytest.approx(10.0)


def test_best_block_whole_window_when_short(spot, prefs, cfg):
    snap = make_snapshot(n_hours=1, conditions=dict(wave_height_ft=3, wave_period_s=8,
                                                    swell_dir_deg=90, wind_kt=4, wind_dir_deg=225))
    r = score_snapshot(snap, spot, prefs, cfg, block_hours=2)
    assert len(r.best_block.hours) == 1


def test_best_block_picks_best_contiguous(spot, prefs, cfg):
    good = dict(wind_kt=4, wind_dir_deg=253)
    bad = dict(wind_kt=14, wind_dir_deg=73)
    snap = make_snapshot(conditions=dict(wave_height_ft=3, wave_period_s=8, swell_dir_deg=90),
                         hours=[bad, good, good])
    r = score_snapshot(snap, spot, prefs, cfg)
    assert r.best_block.start == snap.hours[1].time


def test_partial_gale_in_block_is_not_a_decision_veto(spot, prefs, cfg):
    cond = dict(wave_height_ft=3, wave_period_s=10, swell_dir_deg=90, wind_dir_deg=253)
    snap = make_snapshot(conditions=cond, hours=[{"wind_kt": 25}, {"wind_kt": 6}, {"wind_kt": 25}])
    r = score_snapshot(snap, spot, prefs, cfg)
    assert "gale" not in {v.code for v in r.vetoes}
    assert r.base_score == pytest.approx(5.0)  # (0 + 10) / 2
