"""surf-check command line (SPEC §7.3)."""

from __future__ import annotations

import json
import logging
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

import typer
import yaml
from dotenv import load_dotenv

from .config import ROOT, ConfigError, load_scoring, load_settings, load_spot
from .pipeline import DATA_DIR, RunOptions, make_notifier, run_once
from .records import append_jsonl

app = typer.Typer(add_completion=False, help="Should I go surfing this morning?")

for _stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def _load(spot_id: str | None):
    load_dotenv(ROOT / ".env")
    try:
        settings = load_settings()
        spot = load_spot(spot_id or settings.active_spot)
        return settings, spot, load_scoring()
    except ConfigError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2) from e


def _print_result(res) -> None:
    d, r = res.decision, res.record
    color = {"GO": "green", "MAYBE": "yellow", "NO_GO": "red"}[d.decision]
    typer.secho(f"\n{d.decision}  score {d.final_score:.1f}/10", fg=color, bold=True)
    typer.echo(
        f"Session {r['session_date']} {res.snapshot.window_start:%H:%M}-{res.snapshot.window_end:%H:%M}"
        f"  (base {d.base_score:.2f}, adjustment {d.adjustment_applied:+.2f}, confidence {d.confidence})"
    )
    for v in d.vetoes:
        typer.secho(f"  veto: {v.code}: {v.detail}", fg="red")
    typer.echo(f"\n{d.explanation}")
    typer.echo(f'\nSpoken: "{d.spoken_message}"')
    if d.guardrail_notes:
        typer.secho("Guardrails: " + "; ".join(d.guardrail_notes), fg="yellow")
    if res.snapshot.missing_sources:
        typer.secho("Missing sources: " + ", ".join(res.snapshot.missing_sources), fg="yellow")
    if res.notify:
        typer.echo(f"\nWake-up [{res.notify.mode}]: {res.notify.detail}")
        for c in res.notify.calls:
            typer.echo(f"  {c.method} {c.url} {json.dumps(c.body)} -> {c.status}")
    else:
        typer.echo("\nWake-up: not needed; letting you sleep.")
    llm = r["llm"]
    if llm["used"] or llm["error"]:
        u = llm["usage"]
        typer.echo(
            f"\nLLM {llm['model']} ({llm['effort']}): {u.get('input_tokens', 0)} in / {u.get('output_tokens', 0)} out"
            f", cache read {u.get('cache_read_input_tokens', 0)}, ${llm['cost_usd']:.4f}, {llm['latency_s']:.1f}s"
            + (f", error: {llm['error']}" if llm["error"] else "")
        )
    typer.echo(f"Record: {res.run_dir}")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run", help="Decide, but don't wake anyone."),
    spot: str = typer.Option(None, "--spot", help="Spot id from config/spots/."),
    replay: Path = typer.Option(None, "--replay", help="Re-run against saved data (a run dir or raw dir)."),
    day: str = typer.Option(None, "--date", help="Session date YYYY-MM-DD (default: next session)."),
    no_llm: bool = typer.Option(False, "--no-llm", help="Deterministic score only."),
    force_wake: bool = typer.Option(False, "--force-wake", help="Ignore once-per-day limit; allow replay wakes."),
    model: str = typer.Option(None, "--model"),
    effort: str = typer.Option(None, "--effort"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Run the surf check (default command)."""
    if ctx.invoked_subcommand is not None:
        return
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(levelname)s %(message)s")
    settings, spot_cfg, scoring = _load(spot)
    opts = RunOptions(
        dry_run=dry_run, replay=replay, day=date.fromisoformat(day) if day else None,
        use_llm=not no_llm, force_wake=force_wake, model=model, effort=effort,
    )
    _print_result(run_once(settings, spot_cfg, scoring, opts))


@app.command()
def feedback(
    day: str = typer.Option(..., "--date", help="Session date YYYY-MM-DD"),
    went: bool = typer.Option(..., "--went/--skipped"),
    rating: int = typer.Option(None, "--rating", min=1, max=5, help="1 (junk) to 5 (epic)"),
    note: str = typer.Option("", "--note"),
):
    """Record how the session actually was (feeds tuning and evals)."""
    entry = {"date": day, "went": went, "rating": rating, "note": note,
             "recorded_at": datetime.now().isoformat(timespec="seconds")}
    append_jsonl(DATA_DIR / "feedback.jsonl", entry)
    typer.echo(f"Saved feedback for {day}.")


@app.command()
def promote(
    run_id: str = typer.Argument(..., help="Run id under data/runs/"),
    expected: str = typer.Option(..., "--expected", help="GO, MAYBE or NO_GO"),
    scenario_id: str = typer.Option(None, "--id"),
    rationale: str = typer.Option("", "--rationale"),
):
    """Turn a real run into a labeled eval scenario (SPEC §8.3)."""
    src = DATA_DIR / "runs" / run_id / "raw"
    if not src.is_dir():
        typer.secho(f"No raw data at {src}", fg="red", err=True)
        raise typer.Exit(1)
    sid = scenario_id or f"real-{run_id.lower()}"
    dest = ROOT / "evals" / "scenarios" / "real" / sid
    shutil.copytree(src, dest / "raw", dirs_exist_ok=True)
    (dest / "scenario.yaml").write_text(
        yaml.safe_dump({"id": sid, "description": f"Promoted from run {run_id}", "tags": ["real"],
                        "expect": {"decision": expected.upper(), "allowed_decisions": [expected.upper()],
                                   "vetoes": [], "rationale": rationale}}, sort_keys=False),
        encoding="utf-8",
    )
    typer.echo(f"Created {dest}")


@app.command("notify-test")
def notify_test(message: str = typer.Option("Surf check test. If you hear this, the wake-up works.")):
    """Fire the Alexa routine and announcement once (simulated if VOICEMONKEY_TOKEN is unset)."""
    settings, _, _ = _load(None)
    res = make_notifier(settings).wake(message)
    typer.echo(f"[{res.mode}] {res.detail}")
    for c in res.calls:
        typer.echo(f"  {c.method} {c.url} {json.dumps(c.body)} -> {c.status} {c.response}")


@app.command("eval")
def eval_cmd(
    model: list[str] = typer.Option(None, "--model", help="Repeat to compare models."),
    effort: str = typer.Option(None, "--effort"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Deterministic only (free)."),
    batch: bool = typer.Option(False, "--batch", help="Use the Message Batches API (50% cost)."),
    only: str = typer.Option(None, "--only", help="Comma-separated id substrings to run."),
    yes: bool = typer.Option(False, "--yes", help="Skip the cost confirmation."),
    judge: bool = typer.Option(False, "--judge", help="Also grade explanations with the LLM judge."),
    judge_model: str = typer.Option("claude-opus-5-5", "--judge-model"),
    judge_effort: str = typer.Option("medium", "--judge-effort"),
):
    """Run the eval suite (ARCHITECTURE §9)."""


    sys.path.insert(0, str(ROOT))  # evals/ lives next to src/, not inside the package
    from evals.runner import run_suite

    settings, spot, scoring = _load(None)
    code = run_suite(settings, spot, scoring, models=model or [settings.llm.model], effort=effort,
                     use_llm=not no_llm, batch=batch, only=only, assume_yes=yes,
                     use_judge=judge, judge_model=judge_model, judge_effort=judge_effort)
    raise typer.Exit(code)


if __name__ == "__main__":
    app()
