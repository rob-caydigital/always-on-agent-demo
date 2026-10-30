"""Data passed between pipeline stages. All heights are feet, speeds knots, directions degrees true."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

DecisionKind = Literal["GO", "MAYBE", "NO_GO"]


class SwellComponent(BaseModel):
    """One swell train, e.g. from the CWF line "Wave Detail: E 4 ft at 9 seconds"."""

    direction_deg: float | None
    height_ft: float
    period_s: float
    source: str = "cwf"


class HourConditions(BaseModel):
    time: datetime  # local, top of hour
    wave_height_ft: float | None = None
    wave_period_s: float | None = None
    swell_dir_deg: float | None = None
    wind_kt: float | None = None
    gust_kt: float | None = None
    wind_dir_deg: float | None = None
    pop_pct: float | None = None
    thunder_pct: float | None = None
    weather_text: str | None = None


class BuoyObs(BaseModel):
    station: str
    observed_at: datetime
    height_ft: float | None
    period_s: float | None
    direction_deg: float | None


class WindObs(BaseModel):
    station: str
    observed_at: datetime
    wind_kt: float | None
    gust_kt: float | None
    direction_deg: float | None


class Alert(BaseModel):
    event: str
    severity: str | None = None
    headline: str | None = None
    onset: datetime | None = None
    ends: datetime | None = None


class TideEvent(BaseModel):
    time: datetime
    kind: Literal["H", "L"]
    height_ft: float


class ConditionsSnapshot(BaseModel):
    """Everything the scorer and Claude see, trimmed to the session window."""

    spot_id: str
    run_at: datetime
    window_start: datetime
    window_end: datetime
    sunrise: datetime | None = None
    hours: list[HourConditions] = Field(default_factory=list)
    swell_components: list[SwellComponent] = Field(default_factory=list)
    marine_period_name: str | None = None
    marine_text: str | None = None
    buoy_now: BuoyObs | None = None
    wind_now: WindObs | None = None
    alerts: list[Alert] = Field(default_factory=list)
    tides: list[TideEvent] = Field(default_factory=list)
    missing_sources: list[str] = Field(default_factory=list)
    source_times: dict[str, datetime] = Field(default_factory=dict)


class Veto(BaseModel):
    code: Literal["lightning", "severe_warning", "flat", "too_big", "gale", "no_wave_data"]
    detail: str


class HourScore(BaseModel):
    time: datetime
    face_ft: float | None
    period_s: float | None
    swell_dir_deg: float | None
    wave_score: float
    wind_kt: float | None
    wind_class: str | None
    wind_factor: float
    weather_factor: float
    score: float
    vetoes: list[Veto] = Field(default_factory=list)


class BlockScore(BaseModel):
    start: datetime
    end: datetime
    score: float
    hours: list[HourScore]


class ScoreResult(BaseModel):
    scoring_version: str
    vetoes: list[Veto]
    hourly: list[HourScore]
    best_block: BlockScore | None
    base_score: float


class Assessment(BaseModel):
    """Claude's structured output. Bounds are enforced in decide.py, not trusted from the model."""

    adjustment: float
    adjustment_reason: str
    data_concerns: list[str]
    confidence: Literal["high", "medium", "low"]
    explanation: str
    spoken_message: str


class Decision(BaseModel):
    decision: DecisionKind
    final_score: float
    base_score: float
    adjustment_applied: float
    vetoes: list[Veto]
    reasons: list[str]
    explanation: str
    spoken_message: str
    confidence: str
    llm_used: bool
    guardrail_notes: list[str] = Field(default_factory=list)
