"""Load and validate YAML config. Invalid config fails fast at startup (SPEC §10)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ValidationError

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
    model: str = "claude-opus-5-5"
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


class ScoringConfig(BaseModel):
    version: str
    period_buckets: list[PeriodBucket]
    swell_direction: SwellDirection
    wind_classes: list[WindClass]
    wind_factors: list[dict[str, float]]
    wind_unknown_factor: float
    weather: WeatherRules
    vetoes: VetoRules


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
    return _validate(ScoringConfig, _load_yaml(path), path)


def load_spot(spot_id: str, config_dir: Path = CONFIG_DIR) -> Spot:
    path = config_dir / "spots" / f"{spot_id}.yaml"
    return _validate(Spot, _load_yaml(path), path)
