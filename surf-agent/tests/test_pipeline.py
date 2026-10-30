"""L2: the whole pipeline on replayed real data, with a stubbed Claude client."""

import json
from types import SimpleNamespace

import pytest
from conftest import FIXTURES

from surf_agent import llm
from surf_agent.notify.voicemonkey import VoiceMonkeyNotifier
from surf_agent.pipeline import RunOptions, run_once

RAW = FIXTURES / "raw_2026-10-30"
GO_DAY = FIXTURES / "raw_demo_go_day"


class StubClient:
    """Mimics client.with_options(...).beta.messages.create(...) and records the request."""

    def __init__(self, payload: dict | None = None, stop_reason="end_turn"):
        self.payload = payload or {
            "adjustment": 0, "adjustment_reason": "", "data_concerns": [], "confidence": "high",
            "explanation": "Onshore 19 kt wind over 6 s wind swell.",
            "spoken_message": "No surf today at Juno Pier. Onshore wind 19 knots. Score 0 out of 10.",
        }
        self.stop_reason = stop_reason
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def with_options(self, **_):
        return self

    def _create(self, **params):
        self.requests.append(params)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            model=params["model"],
            content=[SimpleNamespace(type="text", text=json.dumps(self.payload))],
            usage=SimpleNamespace(input_tokens=4000, output_tokens=500,
                                  cache_creation_input_tokens=0, cache_read_input_tokens=0),
        )


def test_replay_real_day_no_go(settings, spot, cfg, tmp_path):
    client = StubClient()
    res = run_once(settings, spot, cfg, RunOptions(replay=RAW), data_dir=tmp_path, client=client)
    assert res.decision.decision == "NO_GO"
    assert res.notify is None
    req = client.requests[0]
    # Request shape: structured output, effort, refusal fallback, no caching on a single daily call.
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["output_config"]["effort"] == "low"
    assert req["fallbacks"] == "default" and llm.FALLBACK_BETA in req["betas"]
    assert "cache_control" not in req["system"][1]
    # Static system prompt carries no run-specific data (cache-stable prefix).
    assert "2026" not in req["system"][0]["text"] and "2026" not in req["system"][1]["text"]
    # Artifacts written for replay / promotion.
    assert (res.run_dir / "raw" / "gridpoint.json").exists()
    assert (res.run_dir / "record.json").exists()
    assert (tmp_path / "decisions.jsonl").read_text().count("\n") == 1


def test_replayed_run_dir_replays_identically(settings, spot, cfg, tmp_path):
    first = run_once(settings, spot, cfg, RunOptions(replay=RAW, use_llm=False), data_dir=tmp_path)
    again = run_once(settings, spot, cfg, RunOptions(replay=first.run_dir, use_llm=False), data_dir=tmp_path / "2")
    assert again.score.model_dump() == first.score.model_dump()


def test_llm_refusal_falls_back_to_deterministic(settings, spot, cfg, tmp_path):
    res = run_once(settings, spot, cfg, RunOptions(replay=RAW), data_dir=tmp_path,
                   client=StubClient(stop_reason="refusal"))
    assert res.decision.decision == "NO_GO" and not res.decision.llm_used
    assert any("llm_unavailable: refusal" in n for n in res.decision.guardrail_notes)


@pytest.fixture
def go_day():
    """Real capture edited into a GO morning (see fixtures/raw_demo_go_day/README.md)."""
    return GO_DAY


def test_go_day_wakes_once_simulated(settings, spot, cfg, tmp_path, go_day):
    notifier = VoiceMonkeyNotifier(token=None, trigger_device="surf-wake", speaker_device="bedroom")
    opts = RunOptions(replay=go_day, use_llm=False, force_wake=True)
    first = run_once(settings, spot, cfg, opts, data_dir=tmp_path, notifier=notifier)
    assert first.decision.decision == "GO"
    assert first.notify.mode == "simulated" and first.notify.woke
    # A second GO the same day is suppressed unless forced.
    opts2 = RunOptions(replay=go_day, use_llm=False, dry_run=False)
    second = run_once(settings, spot, cfg, opts2, data_dir=tmp_path, notifier=notifier)
    assert second.notify.mode == "dry_run"  # replays default to dry-run
    third = run_once(settings, spot, cfg, RunOptions(replay=go_day, use_llm=False), data_dir=tmp_path,
                     notifier=notifier)
    assert not third.notify.woke


def test_dry_run_never_calls_notifier(settings, spot, cfg, tmp_path, go_day):
    class Boom:
        def wake(self, message):
            raise AssertionError("notifier must not be called in dry run")

    res = run_once(settings, spot, cfg, RunOptions(replay=go_day, use_llm=False, dry_run=True, force_wake=True),
                   data_dir=tmp_path, notifier=Boom())
    assert res.notify.mode == "dry_run" and not res.notify.woke


@pytest.mark.live
def test_static_prefix_is_cacheable(settings, spot):
    """The system prompt + spot profile must exceed the model's minimum cacheable prefix (512 tokens)."""
    import anthropic

    params = llm.build_params(
        *_minimal_inputs(spot, settings), spot, settings.preferences, settings.llm, cache=True
    )
    n = anthropic.Anthropic().messages.count_tokens(
        model=params["model"], system=params["system"], messages=[{"role": "user", "content": "x"}]
    ).input_tokens
    assert n >= 512


def _minimal_inputs(spot, settings):
    from surf_agent.config import load_scoring
    from surf_agent.scoring import score_snapshot
    from surf_agent.testing import make_snapshot

    snap = make_snapshot(conditions={"wave_height_ft": 3, "wave_period_s": 8})
    return snap, score_snapshot(snap, spot, settings.preferences, load_scoring())
