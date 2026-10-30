"""One run: fetch -> snapshot -> score -> assess (Claude) -> decide -> notify -> record -> report."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic

from . import llm
from .config import ROOT, ScoringConfig, Settings, Spot
from .decide import decide, should_wake
from .models import ConditionsSnapshot, Decision, ScoreResult
from .notify.base import NotifyResult, WakeState
from .notify.console import DryRunNotifier
from .notify.voicemonkey import VoiceMonkeyNotifier
from .records import append_jsonl, save_run
from .report import write_report
from .scoring import score_snapshot
from .snapshot import build_snapshot
from .sources.http import LiveFetcher, ReplayFetcher

DATA_DIR = ROOT / "data"

log = logging.getLogger(__name__)


@dataclass
class RunOptions:
    dry_run: bool = False
    replay: Path | None = None
    day: date | None = None
    use_llm: bool = True
    force_wake: bool = False
    model: str | None = None
    effort: str | None = None


@dataclass
class RunResult:
    run_id: str
    run_dir: Path
    snapshot: ConditionsSnapshot
    score: ScoreResult
    outcome: llm.LLMOutcome | None
    decision: Decision
    notify: NotifyResult | None
    record: dict
    report: Path | None = None


def _replay_paths(path: Path) -> tuple[Path, dict]:
    raw = path / "raw" if (path / "raw").is_dir() else path
    meta_p = raw / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
    return raw, meta


def make_notifier(settings: Settings) -> VoiceMonkeyNotifier:
    n = settings.notify
    token = os.environ.get("VOICEMONKEY_TOKEN") or None
    # Simulated mode (no token) uses placeholder device names so the demo shows both calls.
    return VoiceMonkeyNotifier(
        token=token,
        trigger_device=os.environ.get("VOICEMONKEY_TRIGGER_DEVICE") or "surf-wake",
        speaker_device=os.environ.get("VOICEMONKEY_SPEAKER_DEVICE") or (None if token else "bedroom"),
        voice=n.voice,
        language=n.language,
        announce_delay_s=n.announce_delay_s,
    )


def run_once(
    settings: Settings,
    spot: Spot,
    scoring_cfg: ScoringConfig,
    opts: RunOptions,
    *,
    data_dir: Path = DATA_DIR,
    client: anthropic.Anthropic | None = None,
    notifier=None,
) -> RunResult:
    tz = ZoneInfo(spot.timezone)
    llm_cfg = settings.llm.model_copy(
        update={k: v for k, v in {"model": opts.model, "effort": opts.effort}.items() if v}
    )

    # 1-2. Fetch + snapshot (live or replay).
    if opts.replay:
        raw_dir, meta = _replay_paths(opts.replay)
        run_at = datetime.fromisoformat(meta["run_at"]) if "run_at" in meta else datetime.now(tz)
        fetcher = ReplayFetcher(raw_dir)
    else:
        run_at = datetime.now(tz)
        contact = os.environ.get("NWS_CONTACT_EMAIL", "unknown")
        fetcher = LiveFetcher(f"surf-check-agent ({contact})", cache_dir=data_dir / "http-cache")

    async def _snap():
        async with fetcher:
            return await build_snapshot(fetcher, spot, settings.session, run_at, opts.day)

    snapshot = asyncio.run(_snap())

    # 3. Deterministic score.
    score = score_snapshot(snapshot, spot, settings.preferences, scoring_cfg, settings.session.best_block_hours)

    # 4. Claude assessment. Skipped when there is nothing to assess; falls back to deterministic on error.
    outcome = None
    no_data = any(v.code == "no_wave_data" for v in score.vetoes)
    if opts.use_llm and llm_cfg.enabled and not no_data:
        # A single daily call never reads its own cache entry, so "auto" means off here (ARCHITECTURE §5).
        params = llm.build_params(
            snapshot, score, spot, settings.preferences, llm_cfg, cache=llm_cfg.prompt_cache == "on"
        )
        outcome = llm.assess(client or anthropic.Anthropic(), params, timeout_s=llm_cfg.timeout_s)

    # 5. Decide (guardrails live here).
    decision = decide(
        score, outcome.assessment if outcome else None, settings.preferences, spot, llm_cfg.max_adjustment
    )
    if outcome and outcome.error:
        decision.guardrail_notes.append(f"llm_unavailable: {outcome.error}")

    # 6. Notify. Replays never wake anyone unless forced.
    notify_result = None
    day = snapshot.window_start.date()
    state = WakeState(data_dir / "state" / "wakeups.json")
    if should_wake(decision, settings.preferences):
        dry = opts.dry_run or (opts.replay is not None and not opts.force_wake)
        if dry:
            notify_result = DryRunNotifier().wake(decision.spoken_message)
        elif state.already_woken(spot.id, day) and not opts.force_wake:
            notify_result = NotifyResult(mode="skipped", woke=False, detail=f"already woken for {day}")
        else:
            notify_result = (notifier or make_notifier(settings)).wake(decision.spoken_message)
            if notify_result.woke:
                state.record(spot.id, day, notify_result.mode)

    # 7. Record.
    run_id = run_at.strftime("%Y%m%dT%H%M%S")
    if opts.replay:  # several replays of the same capture must not overwrite each other
        run_id += "-replay-" + datetime.now(tz).strftime("%Y%m%dT%H%M%S%f")
    run_dir = data_dir / "runs" / run_id
    record = {
        "run_id": run_id,
        "run_at": run_at.isoformat(),
        "spot_id": spot.id,
        "session_date": day.isoformat(),
        "window": [snapshot.window_start.isoformat(), snapshot.window_end.isoformat()],
        "decision": decision.decision,
        "final_score": decision.final_score,
        "base_score": decision.base_score,
        "adjustment": decision.adjustment_applied,
        "vetoes": [v.model_dump() for v in decision.vetoes],
        "best_block": (
            [score.best_block.start.isoformat(), score.best_block.end.isoformat()] if score.best_block else None
        ),
        "explanation": decision.explanation,
        "spoken_message": decision.spoken_message,
        "confidence": decision.confidence,
        "guardrail_notes": decision.guardrail_notes,
        "missing_sources": snapshot.missing_sources,
        "llm": {
            "used": decision.llm_used,
            "model": outcome.model if outcome else None,
            "effort": llm_cfg.effort if outcome else None,
            "prompt_version": llm.prompt_version(),
            "usage": outcome.usage if outcome else {},
            "cost_usd": round(outcome.cost_usd, 5) if outcome else 0.0,
            "latency_s": round(outcome.latency_s, 2) if outcome else 0.0,
            "error": outcome.error if outcome else None,
        },
        "scoring_version": score.scoring_version,
        "notify": notify_result.model_dump() if notify_result else None,
        "dry_run": opts.dry_run,
        "replay": str(opts.replay) if opts.replay else None,
    }
    save_run(
        run_dir,
        fetcher.raw,
        {"run_at": run_at.isoformat(), "spot_id": spot.id, "session_date": day.isoformat()},
        {
            "snapshot": snapshot,
            "score": score,
            "assessment": outcome.assessment if outcome else None,
            "record": record,
        },
    )
    append_jsonl(data_dir / "decisions.jsonl", record)

    # 8. Visual report. Cosmetic only: by now the decision is made and recorded, so a rendering
    # bug costs the picture, never the run.
    try:
        report = write_report(
            run_dir, record, snapshot, score, spot, settings.preferences, scoring_cfg,
            outcome.assessment if outcome else None,
        )
    except Exception:
        log.exception("report rendering failed for run %s", run_id)
        report = None
    return RunResult(run_id, run_dir, snapshot, score, outcome, decision, notify_result, record, report)
