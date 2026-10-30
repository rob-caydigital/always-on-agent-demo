"""L2: guardrails hold even when the model misbehaves (ARCHITECTURE §5, §9)."""

import pytest

from surf_agent.decide import decide, should_wake, spoken_message_ok
from surf_agent.models import Assessment
from surf_agent.scoring import score_snapshot
from surf_agent.testing import make_snapshot

GOOD = dict(wave_height_ft=3, wave_period_s=8, swell_dir_deg=90, wind_kt=4, wind_dir_deg=225)  # 8.25 GO
MAYBE = dict(wave_height_ft=3, wave_period_s=7, swell_dir_deg=90, wind_kt=3, wind_dir_deg=0)  # 5.5


def assessment(**kw):
    base = dict(adjustment=0.0, adjustment_reason="", data_concerns=[], confidence="high",
                explanation="Clean 3 ft at 8 s with light offshore wind.",
                spoken_message="Surf's up at Juno Pier. 3 feet at 8 seconds, light offshore wind. Score 8 out of 10.")
    base.update(kw)
    return Assessment(**base)


def _score(spot, prefs, cfg, **snap_kw):
    return score_snapshot(make_snapshot(**snap_kw), spot, prefs, cfg)


def test_veto_cannot_be_overridden(spot, prefs, cfg):
    s = _score(spot, prefs, cfg, conditions={**GOOD, "thunder_pct": 40})
    d = decide(s, assessment(adjustment=1.0, adjustment_reason="waves are perfect"), prefs, spot)
    assert d.decision == "NO_GO"
    assert not should_wake(d, prefs)
    assert any("veto in force" in n for n in d.guardrail_notes)


def test_adjustment_is_clamped(spot, prefs, cfg):
    s = _score(spot, prefs, cfg, conditions=MAYBE)
    d = decide(s, assessment(adjustment=5.0, adjustment_reason="feels good"), prefs, spot)
    assert d.adjustment_applied == 1.0
    assert d.final_score == pytest.approx(6.5)
    assert any("clamped" in n for n in d.guardrail_notes)


def test_adjustment_without_reason_is_dropped(spot, prefs, cfg):
    s = _score(spot, prefs, cfg, conditions=MAYBE)
    d = decide(s, assessment(adjustment=0.8, adjustment_reason="  "), prefs, spot)
    assert d.adjustment_applied == 0
    assert d.decision == "MAYBE"


def test_negative_adjustment_can_prevent_false_wake(spot, prefs, cfg):
    s = _score(spot, prefs, cfg, conditions={**GOOD, "wind_kt": 8, "wind_dir_deg": 343})  # 7.5 x 0.85 = 6.375
    d = decide(s, assessment(adjustment=-1.0, adjustment_reason="Buoy reads 1.2 ft at 5 s at 04:40"), prefs, spot)
    assert s.base_score >= prefs.go_threshold > d.final_score


@pytest.mark.parametrize(
    "msg",
    [
        "word " * 26,
        "Check https://surfline.com now",
        "**Surf's up** at Juno",
        "",
    ],
)
def test_bad_spoken_message_falls_back_to_template(msg, spot, prefs, cfg):
    s = _score(spot, prefs, cfg, conditions=GOOD)
    d = decide(s, assessment(spoken_message=msg), prefs, spot)
    assert spoken_message_ok(d.spoken_message)
    assert d.spoken_message.startswith("Surf's up at Juno Pier")


def test_no_llm_uses_templates(spot, prefs, cfg):
    s = _score(spot, prefs, cfg, conditions=GOOD)
    d = decide(s, None, prefs, spot)
    assert d.decision == "GO" and not d.llm_used and d.confidence == "low"
    assert spoken_message_ok(d.spoken_message)


def test_maybe_does_not_wake_by_default(spot, prefs, cfg):
    d = decide(_score(spot, prefs, cfg, conditions=MAYBE), None, prefs, spot)
    assert d.decision == "MAYBE" and not should_wake(d, prefs)
    assert should_wake(d, prefs.model_copy(update={"wake_on_maybe": True}))
