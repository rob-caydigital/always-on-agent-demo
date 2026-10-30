"""Load and validate YAML config. Invalid config fails fast at startup (SPEC §10)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


class Preferences(BaseModel):
    min_rideable_ft: float = 3.0
    max_ft: float | None = None
    go_threshold: float = 6.0
    maybe_threshold: float = 4.5
    wake_on_maybe: bool = False
    on_missing_data: Literal["sleep"] = "sleep"


class Session(BaseModel):
    start: Literal["dawn", "sunrise"] = "dawn"
    end: str = "08:30"
    best_block_hours: int = 2


class LLMSettings(BaseModel):
    enabled: bool = True
    model: str = "claude-sonnet-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    max_tokens: int = 4000
    timeout_s: float = 60
    prompt_cache: Literal["auto", "on", "off"] = "auto"
    max_adjustment: float = 1.0


class NotifySettings(BaseModel):
    provider: Literal["voicemonkey"] = "voicemonkey"
    voice: str = "Joanna"
    language: str = "en-US"
    announce_delay_s: float = 20


class Settings(BaseModel):
    active_spot: str
    preferences: Preferences
    session: Session
    llm: LLMSettings
    notify: NotifySettings


class NoaaIds(BaseModel):
    nws_wfo: str
    nws_grid_x: int
    nws_grid_y: int
    marine_zone: str
    alert_zones: list[str]
    wave_buoy: str
    wind_station: str
    wind_station_fallback: str | None = None
    tide_station: str | None = None


class Spot(BaseModel):
    id: str
    name: str
    spoken_name: str
    lat: float
    lon: float
    timezone: str
    beach_facing_deg: float
    swell_window_deg: tuple[float, float]
    notes: str = ""
    noaa: NoaaIds

    @property
    def offshore_deg(self) -> float:
        return (self.beach_facing_deg + 180) % 360


class PeriodBucket(BaseModel):
    min_s: float
    face_multiplier: float
    quality: float


class DirectionBucket(BaseModel):
    max_off_deg: float
    factor: float


class SwellDirection(BaseModel):
    buckets: list[DirectionBucket]
    outside_window_factor: float
    unknown_factor: float


class WindClass(BaseModel):
    name: str
    max_off_deg: float


class PopBucket(BaseModel):
    min_pct: float
    factor: float


class WeatherRules(BaseModel):
    pop_buckets: list[PopBucket]
    small_craft_factor: float


class VetoRules(BaseModel):
    thunder_pct: float
    thunder_words: list[str]
    severe_events: list[str]
    gale_sustained_kt: float
    gale_gust_kt: float


class SpeciesWeight(BaseModel):
    weight: float = Field(ge=0, le=1)
    aliases: list[str] = Field(default_factory=list)


class RecencyBucket(BaseModel):
    max_hours: float
    factor: float


class DistanceBucket(BaseModel):
    max_mi: float
    factor: float


class SharkConfig(BaseModel):
    """config/sharks.yaml (SPEC-shark-threat §10). Factor buckets are upper-inclusive."""

    enabled: bool = True
    radius_mi: float = 10
    lookback_hours: float = 24
    veto_threshold: float = 0.2
    on_missing_data: Literal["ignore", "sleep"] = "ignore"
    max_track_requests: int = 25
    cache_minutes: int = 30
    stale_after_hours: float = 2
    species_weights: dict[str, SpeciesWeight]  # keyed by scientific name
    unknown_species_weight: float = 0.5
    stage_factors: dict[str, float]
    recency_factors: list[RecencyBucket]
    distance_factors: list[DistanceBucket]

    @model_validator(mode="after")
    def _consistent(self):
        if self.cache_minutes > self.stale_after_hours * 60:
            raise ValueError("cache_minutes must not exceed stale_after_hours")
        if [b.max_hours for b in self.recency_factors] != sorted(b.max_hours for b in self.recency_factors):
            raise ValueError("recency_factors must be in ascending max_hours order")
        if [b.max_mi for b in self.distance_factors] != sorted(b.max_mi for b in self.distance_factors):
            raise ValueError("distance_factors must be in ascending max_mi order")
        if not self.recency_factors or self.recency_factors[-1].max_hours != self.lookback_hours:
            raise ValueError("the last recency bucket must end at lookback_hours")
        if not self.distance_factors or self.distance_factors[-1].max_mi != self.radius_mi:
            raise ValueError("the last distance bucket must end at radius_mi")
        return self


class ScoringConfig(BaseModel):
    version: str
    period_buckets: list[PeriodBucket]
    swell_direction: SwellDirection
    wind_classes: list[WindClass]
    wind_factors: list[dict[str, float]]
    wind_unknown_factor: float
    weather: WeatherRules
    vetoes: VetoRules
    sharks: SharkConfig | None = None  # loaded from sharks.yaml; None = shark check off


class ConfigError(RuntimeError):
    pass


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _validate(model: type[BaseModel], data: dict, path: Path):
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"Invalid config in {path}:\n{e}") from e


def load_settings(config_dir: Path = CONFIG_DIR) -> Settings:
    path = config_dir / "settings.yaml"
    return _validate(Settings, _load_yaml(path), path)


def load_scoring(config_dir: Path = CONFIG_DIR) -> ScoringConfig:
    path = config_dir / "scoring.yaml"
    cfg = _validate(ScoringConfig, _load_yaml(path), path)
    sharks_path = config_dir / "sharks.yaml"
    if sharks_path.exists():
        data = _load_yaml(sharks_path)
        cfg.sharks = _validate(SharkConfig, data.get("sharks") or {}, sharks_path)
        # Pin both tables in eval reports and decision records.
        digest = hashlib.sha256(sharks_path.read_bytes()).hexdigest()[:8]
        cfg.version = f"{cfg.version}+sharks-{digest}"
    return cfg


def load_spot(spot_id: str, config_dir: Path = CONFIG_DIR) -> Spot:
    path = config_dir / "spots" / f"{spot_id}.yaml"
    return _validate(Spot, _load_yaml(path), path)
