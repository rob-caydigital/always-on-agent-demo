"""Code-based graders (ARCHITECTURE §9). Each returns plain data so results are easy to diff."""

from __future__ import annotations

import re
from datetime import datetime

from surf_agent.decide import MAX_SPOKEN_WORDS
from surf_agent.models import Assessment, ConditionsSnapshot, Decision, ScoreResult

# SPEC §8.2 pass bars.
GATES = {
    "veto_recall": 1.0,
    "decision_accuracy": 0.90,
    "false_wake_rate": 0.05,  # maximum
    "adjustment_ok": 1.0,
    "grounded": 0.95,
    "spoken_ok": 1.0,
}

_NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")


def _numbers(obj, out: set[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, str):
        for m in _NUM.findall(obj):
            out.add(float(m))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numbers(v, out)


def _time_numbers(snap: ConditionsSnapshot, out: set[float]) -> None:
    times: list[datetime] = [snap.window_start, snap.window_end, *(h.time for h in snap.hours)]
    if snap.sunrise:
        times.append(snap.sunrise)
    times += [t.time for t in snap.tides]
    for t in times:
        out.update({float(t.hour), float(t.hour % 12 or 12), float(t.minute)})


def allowed_numbers(snap: ConditionsSnapshot, score: ScoreResult, decision: Decision, extra) -> set[float]:
    out: set[float] = {0.0, 1.0, 2.0, 10.0, float(MAX_SPOKEN_WORDS)}
    _numbers(snap.model_dump(mode="json"), out)
    _numbers(score.model_dump(mode="json"), out)
    _numbers(decision.model_dump(mode="json", exclude={"explanation", "spoken_message"}), out)
    _numbers(extra, out)
    _time_numbers(snap, out)
    # Factors are often stated as percentages ("rain trims the score by 20%" for a 0.8 factor).
    out |= {round((1 - v) * 100, 1) for v in list(out) if 0 < v < 1}
    out |= {round(v * 100, 1) for v in list(out) if 0 < v <= 1.1}
    return out


def ungrounded_numbers(text: str, allowed: set[float]) -> list[float]:
    """Numbers in `text` that don't match any input value (allowing for rounding)."""
    bad = []
    for m in _NUM.findall(text):
        n = float(m)
        if not any(abs(n - v) <= max(0.6, 0.1 * abs(v)) for v in allowed):
            bad.append(n)
    return bad


def grade(
    expect: dict,
    decision: Decision,
    assessment: Assessment | None,
    snap: ConditionsSnapshot,
    score: ScoreResult,
    extra_numbers=None,
) -> dict:
    allowed = expect.get("allowed_decisions") or [expect["decision"]]
    expected_vetoes = set(expect.get("vetoes") or [])
    got_vetoes = {v.code for v in decision.vetoes}
    g = {
        "decision": decision.decision,
        "expected": expect["decision"],
        "pass": decision.decision in allowed,
        "exact": decision.decision == expect["decision"],
        "false_wake": decision.decision == "GO" and expect["decision"] == "NO_GO",
        "veto_case": bool(expected_vetoes),
        "veto_ok": (not expected_vetoes)
        or (decision.decision == "NO_GO" and expected_vetoes <= got_vetoes),
        "missing_vetoes": sorted(expected_vetoes - got_vetoes),
        "final_score": decision.final_score,
        "base_score": decision.base_score,
    }
    if assessment is not None:
        allowed_nums = allowed_numbers(snap, score, decision, extra_numbers)
        bad = ungrounded_numbers(assessment.explanation + " " + assessment.spoken_message, allowed_nums)
        g.update(
            {
                "raw_adjustment": assessment.adjustment,
                "adjustment_ok": abs(assessment.adjustment) <= 1.0
                and (assessment.adjustment == 0 or bool(assessment.adjustment_reason.strip())),
                "spoken_ok": len(assessment.spoken_message.split()) <= MAX_SPOKEN_WORDS,
                "spoken_words": len(assessment.spoken_message.split()),
                "grounded": not bad,
                "ungrounded": bad,
                "confidence": assessment.confidence,
            }
        )
    return g


def summarize(rows: list[dict], llm_used: bool) -> dict:
    n = len(rows)
    veto_rows = [r for r in rows if r["veto_case"]]
    no_go_rows = [r for r in rows if r["expected"] == "NO_GO"]
    m = {
        "scenarios": n,
        "decision_accuracy": sum(r["pass"] for r in rows) / n if n else 0,
        "exact_match": sum(r["exact"] for r in rows) / n if n else 0,
        "veto_recall": sum(r["veto_ok"] for r in veto_rows) / len(veto_rows) if veto_rows else 1.0,
        "false_wake_rate": sum(r["false_wake"] for r in rows) / len(no_go_rows) if no_go_rows else 0.0,
    }
    if llm_used:
        llm_rows = [r for r in rows if "adjustment_ok" in r]
        k = len(llm_rows) or 1
        m.update(
            {
                "llm_answered": len(llm_rows),
                "adjustment_ok": sum(r["adjustment_ok"] for r in llm_rows) / k,
                "grounded": sum(r["grounded"] for r in llm_rows) / k,
                "spoken_ok": sum(r["spoken_ok"] for r in llm_rows) / k,
            }
        )
    gates = {}
    for key, bar in GATES.items():
        if key not in m:
            continue
        gates[key] = m[key] <= bar if key == "false_wake_rate" else m[key] >= bar
    m["gates"] = gates
    m["all_gates_pass"] = all(gates.values())
    return m
