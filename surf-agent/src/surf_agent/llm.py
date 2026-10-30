"""One structured Claude call per run (ARCHITECTURE §5).

The request is laid out for prompt caching: tools (none) -> system [static rubric, spot profile]
-> user [volatile snapshot]. Nothing time-varying is in `system`, so the prefix is byte-identical
across runs and eval scenarios.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
import yaml

from .config import ROOT, LLMSettings, Preferences, Spot
from .models import Assessment, ConditionsSnapshot, ScoreResult

PROMPT_PATH = ROOT / "prompts" / "system.md"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# $ per million tokens: (input, output, cache read). Cache writes (5-min TTL) are 1.25x input.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}

ASSESSMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "adjustment": {"type": "number", "description": "Score adjustment, -1.0 to 1.0"},
        "adjustment_reason": {"type": "string"},
        "data_concerns": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "explanation": {"type": "string"},
        "spoken_message": {"type": "string"},
    },
    "required": [
        "adjustment", "adjustment_reason", "data_concerns", "confidence", "explanation", "spoken_message",
    ],
    "additionalProperties": False,
}


def system_prompt(path: Path = PROMPT_PATH) -> str:
    return path.read_text(encoding="utf-8")


def prompt_version(path: Path = PROMPT_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def spot_profile(spot: Spot, prefs: Preferences) -> str:
    profile = {
        "spot": {
            "name": spot.name,
            "spoken_name": spot.spoken_name,
            "beach_facing_deg": spot.beach_facing_deg,
            "offshore_wind_deg": spot.offshore_deg,
            "swell_window_deg": list(spot.swell_window_deg),
            "wave_buoy": spot.noaa.wave_buoy,
            "wind_station": spot.noaa.wind_station,
            "notes": spot.notes,
        },
        "preferences": prefs.model_dump(),
    }
    return "# Spot profile and preferences\n\n" + yaml.safe_dump(profile, sort_keys=True)


def _round(x, nd=1):
    if isinstance(x, float):
        return round(x, nd)
    if isinstance(x, dict):
        return {k: _round(v, nd) for k, v in x.items()}
    if isinstance(x, list):
        return [_round(v, nd) for v in x]
    return x


def user_message(snapshot: ConditionsSnapshot, score: ScoreResult) -> str:
    """Compact, deterministic JSON (sorted keys, rounded, no nulls) to keep tokens down."""
    payload = {
        "conditions_snapshot": _round(snapshot.model_dump(mode="json", exclude_none=True)),
        "deterministic_score": _round(
            score.model_dump(mode="json", exclude_none=True, exclude={"best_block": {"hours"}})
        ),
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"Assess this morning's conditions and return the JSON object.\n\n{body}"


def build_params(
    snapshot: ConditionsSnapshot,
    score: ScoreResult,
    spot: Spot,
    prefs: Preferences,
    llm: LLMSettings,
    *,
    cache: bool,
) -> dict:
    """Plain Messages API params (also used as-is for Batch API requests)."""
    profile_block = {"type": "text", "text": spot_profile(spot, prefs)}
    if cache:
        profile_block["cache_control"] = {"type": "ephemeral"}
    output_config: dict = {"format": {"type": "json_schema", "schema": ASSESSMENT_SCHEMA}}
    if not llm.model.startswith("claude-haiku"):  # Haiku 4.5 does not accept effort
        output_config["effort"] = llm.effort
    return {
        "model": llm.model,
        "max_tokens": llm.max_tokens,
        "system": [{"type": "text", "text": system_prompt()}, profile_block],
        "messages": [{"role": "user", "content": user_message(snapshot, score)}],
        "output_config": output_config,
    }


@dataclass
class LLMOutcome:
    assessment: Assessment | None
    model: str
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    latency_s: float = 0.0
    error: str | None = None
    raw_text: str | None = None


def usage_dict(usage) -> dict:
    if usage is None:
        return {}
    return {
        "input_tokens": usage.input_tokens or 0,
        "output_tokens": usage.output_tokens or 0,
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
    }


def cost_usd(model: str, usage: dict, batch: bool = False) -> float:
    p_in, p_out, p_read = PRICES.get(model, PRICES["claude-opus-5-5"])
    c = (
        usage.get("input_tokens", 0) * p_in
        + usage.get("cache_creation_input_tokens", 0) * p_in * 1.25
        + usage.get("cache_read_input_tokens", 0) * p_read
        + usage.get("output_tokens", 0) * p_out
    ) / 1_000_000
    return c * (0.5 if batch else 1.0)


def parse_response(message, model: str) -> LLMOutcome:
    """Shared by sync calls and Batch API results."""
    usage = usage_dict(getattr(message, "usage", None))
    if message.stop_reason == "refusal":
        return LLMOutcome(None, model, usage, error="refusal")
    if message.stop_reason == "max_tokens":
        return LLMOutcome(None, model, usage, error="max_tokens")
    text = next((b.text for b in message.content if b.type == "text"), None)
    if text is None:
        return LLMOutcome(None, model, usage, error="no text block")
    try:
        return LLMOutcome(Assessment.model_validate_json(text), model, usage, raw_text=text)
    except Exception as e:  # noqa: BLE001
        return LLMOutcome(None, model, usage, error=f"invalid output: {e}", raw_text=text)


def assess(client: anthropic.Anthropic, params: dict, timeout_s: float = 60) -> LLMOutcome:
    """Sync call with server-side refusal fallback. Never raises: errors become LLMOutcome.error."""
    model = params["model"]
    t0 = time.perf_counter()
    try:
        message = client.with_options(timeout=timeout_s).beta.messages.create(
            **params, betas=[FALLBACK_BETA], fallbacks="default"
        )
    except anthropic.APIError as e:
        return LLMOutcome(None, model, error=f"{type(e).__name__}: {e}", latency_s=time.perf_counter() - t0)
    out = parse_response(message, model)
    out.latency_s = time.perf_counter() - t0
    out.model = getattr(message, "model", model)
    out.cost_usd = cost_usd(model, out.usage)
    return out
