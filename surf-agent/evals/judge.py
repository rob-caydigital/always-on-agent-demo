"""LLM judge for the qualitative parts of the assessment (ARCHITECTURE §9).

Grades the explanation / spoken message / adjustment reasoning against the case data with six
pass/fail criteria. Report-only until calibrated against Rob's own grades.

The judge must not be the model under test (self-preference bias); default is Opus 5.5.
No refusal fallback here: a silently substituted judge model would invalidate the grades.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from surf_agent import llm
from surf_agent.config import Preferences, Spot
from surf_agent.models import Assessment, ConditionsSnapshot, Decision, ScoreResult

JUDGE_PROMPT_PATH = Path(__file__).parent / "judge_prompt.md"
DEFAULT_JUDGE_MODEL = "claude-opus-5-5"
DEFAULT_JUDGE_EFFORT = "medium"

_STATED = re.compile(r"Verdict:\s*(PASS|FAIL)\W*$", re.IGNORECASE)

CRITERIA = [
    "deciding_factor_first",
    "faithful",
    "consistent_with_decision",
    "adjustment_sound",
    "spoken_useful",
    "data_honesty",
]

_VERDICT = {
    "type": "object",
    "properties": {"reason": {"type": "string"}, "pass": {"type": "boolean"}},
    "required": ["reason", "pass"],
    "additionalProperties": False,
}
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {c: _VERDICT for c in CRITERIA},
    "required": CRITERIA,
    "additionalProperties": False,
}


def judge_prompt_version() -> str:
    import hashlib

    return hashlib.sha256(JUDGE_PROMPT_PATH.read_bytes()).hexdigest()[:12]


def build_judge_params(
    snapshot: ConditionsSnapshot,
    score: ScoreResult,
    decision: Decision,
    output: dict,
    spot: Spot,
    prefs: Preferences,
    *,
    model: str = DEFAULT_JUDGE_MODEL,
    effort: str = DEFAULT_JUDGE_EFFORT,
    cache: bool = True,
) -> dict:
    """`output` is the assessment JSON as the model produced it (a dict, so null/garbage cases can be judged)."""
    system_block = {"type": "text", "text": JUDGE_PROMPT_PATH.read_text(encoding="utf-8")}
    if cache:
        system_block["cache_control"] = {"type": "ephemeral"}
    case = llm.user_message(snapshot, score).split("\n\n", 1)[1]  # same JSON the model saw
    final = {
        "decision": decision.decision,
        "final_score": decision.final_score,
        "base_score": decision.base_score,
        "adjustment_applied": decision.adjustment_applied,
        "vetoes": [v.model_dump() for v in decision.vetoes],
    }
    user = (
        f"<spot_profile>\n{llm.spot_profile(spot, prefs)}\n</spot_profile>\n\n"
        f"<case>\n{case}\n</case>\n\n"
        f"<final_decision>\n{json.dumps(final, sort_keys=True)}\n</final_decision>\n\n"
        f"<output_to_grade>\n{json.dumps(output, sort_keys=True, ensure_ascii=False)}\n</output_to_grade>\n\n"
        "Grade the output on all six criteria."
    )
    output_config: dict = {"format": {"type": "json_schema", "schema": JUDGE_SCHEMA}}
    if not model.startswith("claude-haiku"):
        output_config["effort"] = effort
    return {
        "model": model,
        "max_tokens": 8000,
        "system": [system_block],
        "messages": [{"role": "user", "content": user}],
        "output_config": output_config,
    }


@dataclass
class JudgeOutcome:
    verdicts: dict[str, bool] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    inconsistent: list[str] = field(default_factory=list)  # reason's stated verdict != pass flag
    model: str = ""
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    latency_s: float = 0.0
    error: str | None = None

    @property
    def score(self) -> float | None:
        return sum(self.verdicts.values()) / len(self.verdicts) if self.verdicts else None


def judge(client: anthropic.Anthropic, params: dict, timeout_s: float = 120) -> JudgeOutcome:
    model = params["model"]
    t0 = time.perf_counter()
    try:
        msg = client.with_options(timeout=timeout_s).messages.create(**params)
    except anthropic.APIError as e:
        return JudgeOutcome(model=model, error=f"{type(e).__name__}: {e}", latency_s=time.perf_counter() - t0)
    out = JudgeOutcome(model=getattr(msg, "model", model), latency_s=time.perf_counter() - t0)
    out.usage = llm.usage_dict(msg.usage)
    out.cost_usd = llm.cost_usd(model, out.usage)
    if out.model != model:
        out.error = f"served model {out.model} != requested {model}"
        return out
    if msg.stop_reason != "end_turn":
        out.error = f"stop_reason {msg.stop_reason}"
        return out
    text = next((b.text for b in msg.content if b.type == "text"), None)
    try:
        data = json.loads(text or "")
        out.verdicts = {c: bool(data[c]["pass"]) for c in CRITERIA}
        out.reasons = {c: data[c]["reason"] for c in CRITERIA}
        for c in CRITERIA:
            m = _STATED.search(out.reasons[c])
            if m and (m.group(1).upper() == "PASS") != out.verdicts[c]:
                out.inconsistent.append(c)
    except (ValueError, KeyError, TypeError) as e:
        out.error = f"unparseable judge output: {e}"
    return out


def assessment_output(a: Assessment | None) -> dict:
    """What gets graded: the model's raw structured output (empty dict when it produced nothing)."""
    return a.model_dump() if a is not None else {}
