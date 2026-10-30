"""Per-run artifacts and the decision log (SPEC §7.2, ARCHITECTURE §8)."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from .sources.http import raw_filename

RAW_KINDS = {"buoy": "text", "wind": "text"}


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(obj, BaseModel):
        text = obj.model_dump_json(indent=2)
    else:
        text = json.dumps(obj, indent=2, default=str)
    path.write_text(text, encoding="utf-8")


def save_run(run_dir: Path, raw: dict[str, str], meta: dict, artifacts: dict) -> None:
    for name, body in raw.items():
        p = run_dir / "raw" / raw_filename(name, RAW_KINDS.get(name, "json"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    write_json(run_dir / "raw" / "meta.json", meta)
    for name, obj in artifacts.items():
        if obj is not None:
            write_json(run_dir / f"{name}.json", obj)


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, default=str) + "\n")
