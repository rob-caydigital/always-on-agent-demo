"""L1: eval scenario files are well-formed, so `eval --batch` never rejects one."""

import re
from pathlib import Path

import pytest
import yaml

SCENARIOS = Path(__file__).resolve().parents[1] / "evals" / "scenarios"
# Message Batches API custom_id rule; scenario ids are sent as custom_id.
BATCH_ID = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
FILES = sorted((SCENARIOS / "synthetic").glob("*.yaml")) + sorted((SCENARIOS / "real").glob("*/scenario.yaml"))


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.parent.name if p.name == "scenario.yaml" else p.stem)
def test_scenario_id_is_batch_safe_and_matches_file(path):
    sid = yaml.safe_load(path.read_text(encoding="utf-8"))["id"]
    assert BATCH_ID.match(sid), f"{sid!r} is not a valid batch custom_id"
    assert sid == (path.parent.name if path.name == "scenario.yaml" else path.stem)


def test_scenario_ids_are_unique():
    ids = [yaml.safe_load(p.read_text(encoding="utf-8"))["id"] for p in FILES]
    assert len(ids) == len(set(ids)) == 45
