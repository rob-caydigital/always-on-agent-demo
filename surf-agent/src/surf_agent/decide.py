"""The only place a GO / MAYBE / NO_GO decision is made. Guardrails live here, not in the prompt."""

from __future__ import annotations

import re

from .config import Preferences, Spot
from .models import Assessment, Decision, ScoreResult

MAX_SPOKEN_WORDS = 25
_BAD_SPOKEN = re.compile(r"https?://|[<>*#`\[\]{}]|\bshark", re.IGNORECASE)
_SHARK = re.compile(r"\bshark", re.IGNORECASE)
# Tags only report when a shark surfaces, so never let an absence of pings read as safety (SPEC-shark-threat §7.2).
_UNSAFE_SHARK = re.compile(r"\bno sharks\b|\bshark[- ]free\b|\bsafe from sharks\b|\bno shark activity\b", re.IGNORECASE)
_ALL_CLEAR = re.compile(r"\ball clear\b", re.IGNORECASE)  # only unsafe when the text is about sharks


def shark_wording_ok(text: str) -> bool:
    return not (_UNSAFE_SHARK.search(text) or (_SHARK.search(text) and _ALL_CLEAR.search(text)))

_WIND_WORDS = {
    "offshore": "offshore",
    "cross_offshore": "cross-offshore",
    "cross": "cross-shore",
    "cross_onshore": "cross-onshore",
    "onshore": "onshore",
}


def _classify(score: float, prefs: Preferences) -> str:
    if score >= prefs.go_threshold:
        return "GO"
    if score >= prefs.maybe_threshold:
        return "MAYBE"
    return "NO_GO"


def spoken_message_ok(msg: str) -> bool:
    words = msg.split()
    return 0 < len(words) <= MAX_SPOKEN_WORDS and not _BAD_SPOKEN.search(msg)


def template_message(decision: str, score: ScoreResult, final: float, spot: Spot) -> str:
    """Deterministic fallback when there is no LLM output or it fails validation."""
    head = {"GO": "Surf's up", "MAYBE": "Marginal surf", "NO_GO": "No surf today"}[decision]
    block = score.best_block
    if not block or not block.hours:
        return f"{head} at {spot.spoken_name}."
    h = max(block.hours, key=lambda x: x.score)
    parts = [f"{head} at {spot.spoken_name}."]
    if h.face_ft is not None and h.period_s is not None:
        parts.append(f"About {h.face_ft:.0f} feet at {h.period_s:.0f} seconds.")
    if h.wind_kt is not None:
        parts.append(f"{_WIND_WORDS.get(h.wind_class or '', 'Light')} wind {h.wind_kt:.0f} knots.".capitalize())
    parts.append(f"Score {final:.0f}.")
    return " ".join(parts)


SHARK_UNAVAILABLE = "Shark tracker unavailable."


def template_explanation(decision: str, score: ScoreResult, final: float) -> str:
    if score.vetoes:
        return "No-go: " + "; ".join(f"{v.code} ({v.detail})" for v in score.vetoes) + "."
    return f"Deterministic score {final:.1f}/10 for the best block; decision {decision}."


def decide(
    score: ScoreResult,
    assessment: Assessment | None,
    prefs: Preferences,
    spot: Spot,
    max_adjustment: float = 1.0,
    shark_status: str | None = None,
) -> Decision:
    notes: list[str] = []
    adj = 0.0
    if assessment is not None:
        adj = assessment.adjustment
        if adj != 0 and not assessment.adjustment_reason.strip():
            notes.append(f"adjustment {adj:+.2f} dropped: no reason given")
            adj = 0.0
        if adj != 0 and _SHARK.search(assessment.adjustment_reason):
            notes.append(f"adjustment {adj:+.2f} dropped: cites sharks (sharks are handled only by the veto)")
            adj = 0.0
        if abs(adj) > max_adjustment:
            clamped = max(-max_adjustment, min(max_adjustment, adj))
            notes.append(f"adjustment {adj:+.2f} clamped to {clamped:+.2f}")
            adj = clamped

    final = max(0.0, min(10.0, score.base_score + adj))
    reasons: list[str] = []
    if score.vetoes:
        # A vetoed morning scores 0: "score 10, but no go" is confusing to hear (SPEC §6.6 shows no score).
        decision = "NO_GO"
        final = 0.0
        reasons += [f"veto: {v.code} ({v.detail}); base score {score.base_score:.2f} not used" for v in score.vetoes]
        if assessment is not None and adj > 0:
            notes.append("positive adjustment ignored: veto in force")
    else:
        decision = _classify(final, prefs)
        reasons.append(
            f"score {final:.2f} (base {score.base_score:.2f}{adj:+.2f}) vs GO {prefs.go_threshold:g} / "
            f"MAYBE {prefs.maybe_threshold:g}"
        )

    if assessment is not None and spoken_message_ok(assessment.spoken_message):
        spoken = assessment.spoken_message.strip()
    else:
        if assessment is not None:
            notes.append("spoken_message failed validation; using template")
        spoken = template_message(decision, score, final, spot)

    explanation = (
        assessment.explanation.strip()
        if assessment is not None and assessment.explanation.strip()
        else template_explanation(decision, score, final)
    )
    if not shark_wording_ok(explanation):
        notes.append("explanation failed shark wording check; using template")
        explanation = template_explanation(decision, score, final)
    if shark_status == "unavailable" and not (_SHARK.search(explanation) and "unavailable" in explanation.lower()):
        explanation = f"{explanation} {SHARK_UNAVAILABLE}"

    return Decision(
        decision=decision,
        final_score=round(final, 2),
        base_score=round(score.base_score, 2),
        adjustment_applied=round(adj, 2),
        vetoes=score.vetoes,
        reasons=reasons,
        explanation=explanation,
        spoken_message=spoken,
        confidence=assessment.confidence if assessment else "low",
        llm_used=assessment is not None,
        guardrail_notes=notes,
    )


def should_wake(decision: Decision, prefs: Preferences) -> bool:
    return decision.decision == "GO" or (decision.decision == "MAYBE" and prefs.wake_on_maybe)
