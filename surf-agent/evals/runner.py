"""Eval suite runner (ARCHITECTURE §9): scenarios -> score -> Claude (sync or Batch API) -> decide -> grade."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import anthropic
import typer
import yaml

from surf_agent import llm
from surf_agent.config import ScoringConfig, Settings, Spot
from surf_agent.decide import decide
from surf_agent.models import ConditionsSnapshot
from surf_agent.scoring import score_snapshot
from surf_agent.snapshot import build_snapshot
from surf_agent.sources.http import ReplayFetcher
from surf_agent.testing import make_snapshot

from .graders import GATES, grade, summarize

EVALS = Path(__file__).parent
SCENARIOS = EVALS / "scenarios"
RESULTS = EVALS / "results"
EST_OUTPUT_TOKENS = 900  # thinking + JSON at low effort; refined by real runs


@dataclass
class Scenario:
    id: str
    kind: str  # synthetic | real
    tags: list[str]
    expect: dict
    snapshot: ConditionsSnapshot


def load_scenarios(
    spot: Spot, settings: Settings, only: str | None = None, scoring: ScoringConfig | None = None
) -> list[Scenario]:
    out = []
    for p in sorted((SCENARIOS / "synthetic").glob("*.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        out.append(Scenario(d["id"], "synthetic", d.get("tags", []), d["expect"], make_snapshot(**d["input"])))
    for p in sorted((SCENARIOS / "real").glob("*/scenario.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        raw = p.parent / "raw"
        meta = json.loads((raw / "meta.json").read_text(encoding="utf-8"))
        snap = asyncio.run(
            build_snapshot(
                ReplayFetcher(raw), spot, settings.session, datetime.fromisoformat(meta["run_at"]),
                sharks=scoring.sharks if scoring else None,
            )
        )
        out.append(Scenario(d["id"], "real", d.get("tags", []), d["expect"], snap))
    if only:
        out = [s for s in out if only in s.id]
    return out


def _estimate_cost(client, params_list: list[dict], model: str, batch: bool) -> float:
    """count_tokens on one request, then assume the shared prefix is cached after the first call."""
    p = params_list[0]
    total_in = client.messages.count_tokens(
        model=p["model"], system=p["system"], messages=p["messages"]
    ).input_tokens
    prefix = client.messages.count_tokens(
        model=p["model"], system=p["system"], messages=[{"role": "user", "content": "x"}]
    ).input_tokens
    per_call_tail = total_in - prefix
    n = len(params_list)
    usage = {
        "input_tokens": per_call_tail * n,
        "cache_creation_input_tokens": prefix,
        "cache_read_input_tokens": prefix * (n - 1),
        "output_tokens": EST_OUTPUT_TOKENS * n,
    }
    return llm.cost_usd(model, usage, batch=batch)


def _run_sync(client, params_list, ids, timeout_s) -> dict[str, llm.LLMOutcome]:
    out = {}
    for sid, params in zip(ids, params_list):
        out[sid] = llm.assess(client, params, timeout_s=timeout_s)
        o = out[sid]
        typer.echo(f"  {sid:<42} {'ok' if o.assessment else o.error}  "
                   f"(cache read {o.usage.get('cache_read_input_tokens', 0)}, {o.latency_s:.1f}s)")
    return out


def _run_batch(client, params_list, ids, model) -> dict[str, llm.LLMOutcome]:
    batch = client.messages.batches.create(
        requests=[{"custom_id": sid, "params": p} for sid, p in zip(ids, params_list)]
    )
    typer.echo(f"  batch {batch.id} submitted ({len(ids)} requests); polling...")
    while True:
        b = client.messages.batches.retrieve(batch.id)
        if b.processing_status == "ended":
            break
        time.sleep(20)
    out = {}
    for r in client.messages.batches.results(batch.id):  # results arrive in any order: key by custom_id
        if r.result.type == "succeeded":
            o = llm.parse_response(r.result.message, model)
            o.cost_usd = llm.cost_usd(model, o.usage, batch=True)
        else:
            o = llm.LLMOutcome(None, model, error=f"batch {r.result.type}")
        out[r.custom_id] = o
    return out


def _previous(model_tag: str) -> dict | None:
    prior = sorted(RESULTS.glob(f"*-{model_tag}.json"))
    return json.loads(prior[-1].read_text(encoding="utf-8")) if prior else None


def _report_md(res: dict, prev: dict | None) -> str:
    m = res["metrics"]
    lines = [
        f"# Surf-check eval: {res['model_tag']}",
        "",
        (f"- Run: {res['started_at']}  |  scenarios: {m['scenarios']}  |  prompt {res['prompt_version']}  "
        f"|  scoring {res['scoring_version']}  |  mode: {res['mode']}"),
        (f"- Cost: ${res['cost_usd']:.4f}  |  mean latency {res['mean_latency_s']:.1f}s  "
        f"|  cache hit rate {res['cache_hit_rate']:.0%}"),
        "",
        "| Metric | Value | Bar | Pass |",
        "|---|---|---|---|",
    ]
    for key, bar in GATES.items():
        if key in m:
            op = "≤" if key == "false_wake_rate" else "≥"
            delta = ""
            if prev and key in prev["metrics"]:
                d = m[key] - prev["metrics"][key]
                delta = f" ({d:+.0%} vs prev)" if d else ""
            lines.append(f"| {key} | {m[key]:.0%}{delta} | {op} {bar:.0%} | {'✅' if m['gates'][key] else '❌'} |")
    lines += ["", f"**All gates pass: {'yes' if m['all_gates_pass'] else 'NO'}**", "", "## Scenarios", "",
              "| id | expected | got | base | final | adj | notes |", "|---|---|---|---|---|---|---|"]
    for r in res["rows"]:
        notes = []
        if not r["pass"]:
            notes.append("**FAIL**")
        if r["false_wake"]:
            notes.append("false wake")
        if r.get("missing_vetoes"):
            notes.append(f"missing veto {r['missing_vetoes']}")
        if r.get("ungrounded"):
            notes.append(f"ungrounded {r['ungrounded']}")
        if r.get("llm_error"):
            notes.append(f"llm error: {r['llm_error']}")
        adj = f"{r['raw_adjustment']:+.2f}" if "raw_adjustment" in r else ""
        lines.append(f"| {r['id']} | {r['expected']} | {r['decision']} | {r['base_score']:.2f} | "
                     f"{r['final_score']:.2f} | {adj} | {'; '.join(notes)} |")
    return "\n".join(lines) + "\n"


def run_suite(
    settings: Settings,
    spot: Spot,
    scoring: ScoringConfig,
    *,
    models: list[str],
    effort: str | None,
    use_llm: bool,
    batch: bool,
    only: str | None,
    assume_yes: bool,
) -> int:
    scenarios = load_scenarios(spot, settings, only, scoring)
    if not scenarios:
        typer.secho("No scenarios matched.", fg="red")
        return 1
    scores = {
        s.id: score_snapshot(s.snapshot, spot, settings.preferences, scoring, settings.session.best_block_hours)
        for s in scenarios
    }
    client = anthropic.Anthropic() if use_llm else None
    RESULTS.mkdir(parents=True, exist_ok=True)
    exit_code = 0

    for model in models if use_llm else ["deterministic"]:
        llm_cfg = settings.llm.model_copy(update={"model": model, **({"effort": effort} if effort else {})})
        outcomes: dict[str, llm.LLMOutcome] = {}
        if use_llm:
            # Evals send many calls with the same prefix back to back: cache it (prompt_cache "auto").
            cache = settings.llm.prompt_cache != "off"
            params = [
                llm.build_params(s.snapshot, scores[s.id], spot, settings.preferences, llm_cfg, cache=cache)
                for s in scenarios
            ]
            est = _estimate_cost(client, params, model, batch)
            typer.echo(f"\n{model} ({llm_cfg.effort}): {len(params)} calls, estimated ${est:.2f}"
                       + (" with Batch API discount" if batch else ""))
            if not assume_yes and not typer.confirm("Proceed?", default=True):
                return 1
            ids = [s.id for s in scenarios]
            outcomes = (_run_batch(client, params, ids, model) if batch
                        else _run_sync(client, params, ids, llm_cfg.timeout_s))

        rows = []
        for s in scenarios:
            o = outcomes.get(s.id)
            a = o.assessment if o else None
            d = decide(
                scores[s.id], a, settings.preferences, spot, llm_cfg.max_adjustment,
                shark_status=s.snapshot.sharks.status if s.snapshot.sharks else None,
            )
            row = {"id": s.id, "kind": s.kind, "tags": s.tags,
                   **grade(s.expect, d, a, s.snapshot, scores[s.id], extra_numbers=spot.model_dump(mode="json"))}
            if o and o.error:
                row["llm_error"] = o.error
            if a:
                row["adjustment_reason"] = a.adjustment_reason
                row["explanation"] = a.explanation
                row["spoken_message"] = a.spoken_message
            row["guardrail_notes"] = d.guardrail_notes
            rows.append(row)

        metrics = summarize(rows, llm_used=use_llm)
        usages = [o.usage for o in outcomes.values() if o.usage]
        read = sum(u.get("cache_read_input_tokens", 0) for u in usages)
        total_in = sum(u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                       + u.get("cache_creation_input_tokens", 0) for u in usages)
        lat = [o.latency_s for o in outcomes.values() if o.latency_s]
        model_tag = model if not use_llm else f"{model}-{llm_cfg.effort}"
        res = {
            "model_tag": model_tag,
            "mode": "deterministic" if not use_llm else ("batch" if batch else "sync"),
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "prompt_version": llm.prompt_version(),
            "scoring_version": scoring.version,
            "metrics": metrics,
            "cost_usd": sum(o.cost_usd for o in outcomes.values()),
            "mean_latency_s": sum(lat) / len(lat) if lat else 0.0,
            "cache_hit_rate": read / total_in if total_in else 0.0,
            "usage_total": {k: sum(u.get(k, 0) for u in usages) for k in
                            ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                             "cache_read_input_tokens")},
            "rows": rows,
        }
        if use_llm and len(usages) > 1 and read == 0:
            # Silent-invalidator alarm (ARCHITECTURE §5).
            metrics["gates"]["cache_reads"] = False
            metrics["all_gates_pass"] = False
        prev = _previous(model_tag)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        (RESULTS / f"{stamp}-{model_tag}.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
        md = _report_md(res, prev)
        (RESULTS / f"{stamp}-{model_tag}.md").write_text(md, encoding="utf-8")
        typer.echo("\n" + md)
        if not metrics["all_gates_pass"]:
            exit_code = 1
    return exit_code
