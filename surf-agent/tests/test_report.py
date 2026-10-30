"""The per-run visual report: written on every run, honest about the numbers, safe with outside text."""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from conftest import FIXTURES

from surf_agent import pipeline
from surf_agent.models import Alert, Assessment, ConditionsSnapshot, ScoreResult, Veto
from surf_agent.pipeline import RunOptions, run_once
from surf_agent.report import REPORT_NAME, rebuild_report, render_report

RAW = FIXTURES / "raw_2026-10-30"
GO_DAY = FIXTURES / "raw_demo_go_day"


def _run(settings, spot, cfg, tmp_path, replay):
    return run_once(settings, spot, cfg, RunOptions(replay=replay, use_llm=False), data_dir=tmp_path)


def test_every_run_writes_a_report(settings, spot, cfg, tmp_path):
    res = _run(settings, spot, cfg, tmp_path, RAW)
    assert res.report == res.run_dir / REPORT_NAME and res.report.exists()
    page = res.report.read_text(encoding="utf-8")
    assert 'class="hero nogo"' in page
    assert f"<b>{res.decision.final_score:.2f}</b> / 10" in page
    # Self-contained: opens offline, nothing loaded from the network.
    assert not re.search(r'(src|href)="https?:', page)


def test_go_day_shows_a_surfer_and_a_green_flag(settings, spot, cfg, tmp_path):
    res = _run(settings, spot, cfg, tmp_path, GO_DAY)
    assert res.decision.decision == "GO"
    page = res.report.read_text(encoding="utf-8")
    assert 'class="hero go"' in page and 'class="flag go"' in page
    assert 'class="board"' in page  # the surfer is out on a wave


def test_rebuild_matches_the_original(settings, spot, cfg, tmp_path):
    res = _run(settings, spot, cfg, tmp_path, RAW)
    original = res.report.read_text(encoding="utf-8")
    res.report.unlink()
    assert rebuild_report(res.run_dir, spot, settings.preferences, cfg).read_text(encoding="utf-8") == original


def test_outside_text_is_escaped(settings, spot, cfg, tmp_path):
    res = _run(settings, spot, cfg, tmp_path, RAW)
    snap = res.snapshot.model_copy(deep=True)
    snap.alerts.append(Alert(event="<script>alert(1)</script>", severity="Minor"))
    snap.marine_text = '<img src=x onerror="boom">'
    record = {**res.record, "explanation": "<b>bold claim</b>", "llm": {**res.record["llm"], "used": True}}
    assessment = Assessment(adjustment=0, adjustment_reason="<i>x</i>", data_concerns=["<u>y</u>"],
                            confidence="high", explanation="", spoken_message="")
    page = render_report(record, snap, res.score, spot, settings.preferences, cfg, assessment)
    for raw in ("<script>alert", "<img src=x", "<b>bold", "<i>x</i>", "<u>y</u>"):
        assert raw not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_no_wave_data_still_renders(settings, spot, cfg):
    start = datetime(2026, 11, 2, 7, 0, tzinfo=ZoneInfo(spot.timezone))
    snap = ConditionsSnapshot(spot_id=spot.id, run_at=start - timedelta(hours=14),
                              window_start=start, window_end=start + timedelta(hours=1),
                              missing_sources=["gridpoint", "marine_forecast"])
    veto = Veto(code="no_wave_data", detail="no wave forecast")
    score = ScoreResult(scoring_version=cfg.version, vetoes=[veto], hourly=[], best_block=None, base_score=0.0)
    record = {"decision": "NO_GO", "final_score": 0.0, "base_score": 0.0, "adjustment": 0.0,
              "vetoes": [veto.model_dump()], "llm": {"used": False}, "notify": None,
              "explanation": "No wave data.", "spoken_message": "No surf data today."}
    page = render_report(record, snap, score, spot, settings.preferences, cfg)
    assert "No wave data. The agent fails closed" in page
    assert "Division by zero avoided" in page


def test_a_broken_report_never_breaks_the_run(settings, spot, cfg, tmp_path, monkeypatch):
    def boom(*_, **__):
        raise RuntimeError("renderer exploded")

    monkeypatch.setattr(pipeline, "write_report", boom)
    res = _run(settings, spot, cfg, tmp_path, RAW)
    assert res.report is None
    assert (res.run_dir / "record.json").exists()
    assert (tmp_path / "decisions.jsonl").exists()
