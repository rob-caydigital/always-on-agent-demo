from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field


class NotifyCall(BaseModel):
    """One outbound call, recorded with the token redacted."""

    method: str
    url: str
    body: dict
    status: int | None = None
    response: dict | str | None = None


class NotifyResult(BaseModel):
    mode: Literal["live", "simulated", "dry_run", "skipped"]
    woke: bool  # True if the wake-up routine fired (or, in simulated mode, would have)
    detail: str
    calls: list[NotifyCall] = Field(default_factory=list)
    announce_ok: bool | None = None


class Notifier(Protocol):
    def wake(self, message: str) -> NotifyResult: ...


class WakeState:
    """At most one wake-up per spot per day (ARCHITECTURE §7)."""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def already_woken(self, spot_id: str, day: date) -> bool:
        return self._load().get(spot_id, {}).get("date") == day.isoformat()

    def record(self, spot_id: str, day: date, mode: str) -> None:
        state = self._load()
        state[spot_id] = {"date": day.isoformat(), "mode": mode}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, indent=2), encoding="utf-8")
