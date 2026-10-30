"""Parse the "Wave Detail" line of an NWS Coastal Waters Forecast period.

Example: "Wave Detail: N 6 ft at 7 seconds and E 4 ft at 4 seconds." -> two SwellComponents.
"""

from __future__ import annotations

import re

from ..models import SwellComponent

COMPASS = {
    "N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5, "E": 90, "ESE": 112.5, "SE": 135, "SSE": 157.5,
    "S": 180, "SSW": 202.5, "SW": 225, "WSW": 247.5, "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5,
}

_DETAIL = re.compile(r"Wave Detail:\s*(.+?)(?:\.\s|\.$|$)", re.IGNORECASE | re.DOTALL)
_COMPONENT = re.compile(
    r"\b(NNE|ENE|ESE|SSE|SSW|WSW|WNW|NNW|NE|SE|SW|NW|N|E|S|W)\s+(\d+(?:\.\d+)?)\s*ft\s+at\s+(\d+(?:\.\d+)?)\s*seconds?",
    re.IGNORECASE,
)


def parse_wave_detail(text: str | None) -> list[SwellComponent]:
    if not text:
        return []
    m = _DETAIL.search(text)
    if not m:
        return []
    return [
        SwellComponent(
            direction_deg=COMPASS[d.upper()], height_ft=float(h), period_s=float(p), source="cwf"
        )
        for d, h, p in _COMPONENT.findall(m.group(1))
    ]
