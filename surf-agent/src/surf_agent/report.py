"""Per-run visual report: the conditions and the decision, with jokes and real numbers.

Pure rendering from what the run already saved: no fetching, no Claude call. Text that came from
NWS or Claude is escaped before it reaches the page. The output is one self-contained HTML file
(inline CSS and SVG) so it opens offline at 5 AM.
"""

from __future__ import annotations

import html
import itertools
import json
import math
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .config import Preferences, ScoringConfig, Spot
from .models import (
    Alert,
    Assessment,
    ConditionsSnapshot,
    HourConditions,
    HourScore,
    ScoreResult,
    SwellComponent,
)
from .scoring import (
    _components_for_hour,
    alert_active,
    angle_off,
    face_height_ft,
    period_quality,
    size_score,
    swell_direction_factor,
)

REPORT_NAME = "report.html"

_POINTS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
_LABEL = {"GO": "Go", "MAYBE": "Maybe", "NO_GO": "No go"}
_CLASS = {"GO": "go", "MAYBE": "maybe", "NO_GO": "nogo"}
_WIND_WORDS = {
    "offshore": "offshore", "cross_offshore": "cross-offshore", "cross": "cross-shore",
    "cross_onshore": "cross-onshore", "onshore": "onshore",
}


def _e(v) -> str:
    return html.escape(str(v), quote=True)


def _compass(deg: float) -> str:
    return _POINTS[round((deg % 360) / 22.5) % 16]


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _n(v: float | None, fmt: str = "{:.1f}") -> str:
    return "n/a" if v is None else fmt.format(v)


def _pick(key: str, options: list[str]) -> str:
    """Same joke for the same session, a different one another day."""
    return options[zlib.crc32(key.encode()) % len(options)]


@dataclass
class _Facts:
    record: dict
    snap: ConditionsSnapshot
    score: ScoreResult
    spot: Spot
    prefs: Preferences
    cfg: ScoringConfig
    assessment: Assessment | None
    best: HourScore | None  # the strongest hour of the best block
    hour: HourConditions | None  # raw forecast for that hour
    swell: SwellComponent | None  # the swell train that set the wave score
    alerts: list[Alert]  # active during the window

    @property
    def decision(self) -> str:
        return self.record["decision"]

    @property
    def final(self) -> float:
        return float(self.record["final_score"])

    @property
    def key(self) -> str:
        return self.snap.window_start.date().isoformat()

    @property
    def wind_kt(self) -> float | None:
        return self.hour.wind_kt if self.hour else None

    @property
    def wind_dir(self) -> float | None:
        return self.hour.wind_dir_deg if self.hour else None

    @property
    def max_pop(self) -> float | None:
        vals = [h.pop_pct for h in self.snap.hours if h.pop_pct is not None]
        return max(vals) if vals else None

    @property
    def max_thunder(self) -> float | None:
        vals = [h.thunder_pct for h in self.snap.hours if h.thunder_pct is not None]
        return max(vals) if vals else None


def _facts(record, snap, score, spot, prefs, cfg, assessment) -> _Facts:
    hours = score.best_block.hours if score.best_block else score.hourly
    best = max(hours, key=lambda h: h.score, default=None)
    by_time = {h.time: h for h in snap.hours}
    hour = by_time.get(best.time) if best else None
    swell = None
    if best and hour and best.face_ft is not None:
        comps = _components_for_hour(hour, snap)
        swell = next(
            (c for c in comps if c.period_s == best.period_s
             and abs(face_height_ft(c.height_ft, c.period_s, cfg) - best.face_ft) < 0.05),
            comps[0] if comps else None,
        )
    alerts = [a for a in snap.alerts if alert_active(a, snap.window_start, snap.window_end)]
    return _Facts(record, snap, score, spot, prefs, cfg, assessment, best, hour, swell, alerts)


# --- Jokes. Every one is keyed to a real number; none of them changes the decision. ---

_VETO_JOKES = {
    "lightning": "Lightning in the forecast. You are a tall, wet object holding a stick.",
    "severe_warning": "The weather service issued an actual warning. Stay in bed like a sensible person.",
    "gale": "Gale-force wind. The ocean is being blow-dried.",
    "flat": "Flat. Lake Juno is open for paddleboards and ducks.",
    "too_big": "Too big. The ocean is lifting heavy today and wants no spotters.",
    "no_wave_data": "No wave data. The agent fails closed, so you stay asleep.",
}


def _headline(f: _Facts) -> str:
    vetoes = f.record.get("vetoes") or []
    if vetoes:
        return _VETO_JOKES.get(vetoes[0]["code"], "Vetoed. Not even Claude can save this one.")
    if f.decision == "GO":
        return _pick(f.key, [
            "Go. Wax the board, skip the coffee.",
            "Go. This is the session you'll brag about at work.",
            "Go. The ocean called and it said please.",
        ])
    if f.decision == "MAYBE":
        return _pick(f.key, [
            "Maybe. The ocean is being vague on purpose.",
            "A solid shrug. Worth a look, not worth an alarm.",
        ])
    cls = f.best.wind_class if f.best else None
    if cls in ("onshore", "cross_onshore") and (f.wind_kt or 0) >= 10:
        return _pick(f.key, [
            "Blown out. The ocean is a washing machine stuck on spin cycle.",
            "Blown out. Somebody left the ocean's leaf blower on.",
            "Blown out. The waves are being shoved over before they're ready.",
        ])
    if f.best and f.best.period_s is not None and f.best.period_s < 6:
        return "Wind chop. The ocean is shivering, not surfing."
    return _pick(f.key, ["Not worth the alarm. The pillow wins this round.", "Stay in bed. The ocean has nothing for you."])


def _size_joke(face: float | None, prefs: Preferences) -> str:
    if face is None:
        return "No size data. The ocean is keeping secrets."
    if face < prefs.min_rideable_ft:
        return "Ankle biters. Bring a magnifying glass."
    if face < 4:
        return "Knee to waist high. Longboard weather."
    if face < 6:
        return "Chest to head high. The good stuff, size-wise."
    if face < 8:
        return "Overhead. Big enough to make your coffee taste better."
    return "Well overhead. Your leash is about to earn its keep."


def _period_joke(period: float | None) -> str:
    if period is None:
        return ""
    if period < 6:
        return "That's not swell, that's the ocean shivering."
    if period < 8:
        return "Short-period wind swell: waves arrive like a crowded elevator, all at once and nobody's happy."
    if period < 10:
        return "Medium period. Some shape, some sets."
    if period < 13:
        return "Real swell. Actual lines on the horizon."
    return "Groundswell. Rare at Juno. Tell your boss you have a dentist appointment."


def _wind_joke(f: _Facts) -> str:
    kt, cls = f.wind_kt, f.best.wind_class if f.best else None
    if kt is None or cls is None:
        return "No wind data. Lick a finger and hold it up."
    if kt < 5:
        text = "Glassy. The ocean looks ironed."
    elif cls == "offshore":
        text = "Offshore. The wind is holding the waves up for you."
    elif cls == "cross_offshore":
        text = "Cross-offshore. Honestly pretty decent."
    elif cls == "cross":
        text = "Sidewinder. The waves will be lumpy from the side."
    elif cls == "cross_onshore":
        text = "Mostly onshore. Expect lumps."
    elif kt >= 15:
        text = "Dead onshore. It's pushing the waves over before they can break properly."
    else:
        text = "Onshore. Mushy, but rideable-ish."
    gale = f.cfg.vetoes.gale_sustained_kt
    if 0 < gale - kt <= 3:
        text += f" It missed the automatic gale veto by {gale - kt:.0f} kt. It didn't need it."
    return text


def _sky_joke(f: _Facts) -> str:
    thunder, pop = f.max_thunder, f.max_pop
    if thunder is not None and thunder >= f.cfg.vetoes.thunder_pct:
        return "Thunder in the forecast. Hard pass."
    if pop is None or pop < 10:
        return "Dry, apart from the ocean."
    if pop < 30:
        return "A small chance of rain. You'd get wet anyway."
    return "Rain likely. On the bright side, nobody else will be out."


def _alerts_joke(n: int) -> str:
    if n == 0:
        return "No advisories. The weather service has no notes."
    if n == 1:
        return "One advisory. The weather service is mildly concerned."
    return f"{n} advisories. That's the weather service saying no without being rude."


# --- SVG pieces ---

def _scene(f: _Facts) -> str:
    face = (f.best.face_ft if f.best and f.best.face_ft is not None else 0.0)
    period = (f.best.period_s if f.best and f.best.period_s else 8.0)
    kt = f.wind_kt or 0.0
    # +1 = blowing from the sea onto the beach (onshore), -1 = offshore.
    on = math.cos(math.radians(angle_off(f.wind_dir, f.spot.beach_facing_deg))) if f.wind_dir is not None else 0.0
    cross = math.sin(math.radians(angle_off(f.wind_dir, f.spot.beach_facing_deg))) if f.wind_dir is not None else 0.0
    amp = 4 + min(face, 12) * 3.2
    lam = 30 + period * 8
    chop = max(0.0, on) * min(kt, 25) * 0.35 + min(kt, 25) * 0.08

    def wave_y(x: float, base: float, a: float, c: float) -> float:
        crest = ((math.sin(2 * math.pi * x / lam) + 1) / 2) ** 2
        return base - a * crest - c * math.sin(2 * math.pi * x / (lam / 3.7) + 1.3) * 0.5

    def wave_path(base: float, a: float, c: float, shift: float = 0) -> str:
        pts = " ".join(f"L{x},{wave_y(x + shift, base, a, c):.1f}" for x in range(0, 724, 4))
        return f"M0,300 {pts} L720,300 Z"

    parts = [
        '<rect class="sky" width="720" height="300"/>',
        '<circle class="sun" cx="655" cy="150" r="24"/>',
    ]
    pop, thunder = f.max_pop or 0, f.max_thunder or 0
    if pop >= 20 or thunder >= f.cfg.vetoes.thunder_pct:
        for cx in (250, 470):
            parts.append(f'<g class="cloud"><ellipse cx="{cx}" cy="42" rx="46" ry="16"/>'
                         f'<ellipse cx="{cx - 24}" cy="34" rx="24" ry="16"/><ellipse cx="{cx + 18}" cy="28" rx="28" ry="20"/></g>')
            if pop >= 30:
                parts += [f'<line class="rain" x1="{cx + dx}" y1="62" x2="{cx + dx - 6}" y2="80"/>' for dx in (-30, -12, 6, 24)]
        if thunder >= f.cfg.vetoes.thunder_pct:
            parts.append('<polygon class="bolt" points="470,58 458,84 468,84 460,104 482,76 471,76 478,58"/>')

    parts.append(f'<path class="sea-back" d="{wave_path(172, amp * 0.35, chop * 0.4, lam / 2)}"/>')
    parts += [f'<rect class="pier" x="{x}" y="132" width="5" height="168"/>' for x in range(186, 600, 36)]
    parts.append('<rect class="pier" x="172" y="124" width="430" height="9"/>')
    parts.append('<line class="rail" x1="172" y1="112" x2="602" y2="112"/>')
    parts += [f'<line class="rail" x1="{x}" y1="112" x2="{x}" y2="124"/>' for x in range(176, 604, 18)]
    parts.append(f'<path class="sea" d="{wave_path(214, amp, chop)}"/>')
    if chop > 3:  # whitecaps on the crests
        for k in range(1, 12):
            x = lam / 4 + k * lam
            if 200 < x < 715:
                y = wave_y(x, 214, amp, chop)
                parts.append(f'<path class="foam" d="M{x - 9:.0f},{y + 2:.1f} q9,-8 18,0" />')

    # Wind streaks point where the wind is going: left (toward the beach) when onshore.
    n = min(8, int(kt / 3))
    for i in range(n):
        x, y = 260 + (i % 4) * 95 + (i // 4) * 40, 66 + (i // 4) * 26 + (i % 2) * 8
        dx, dy = -on * 46, cross * 12
        parts.append(f'<line class="gust" x1="{x}" y1="{y}" x2="{x + dx:.0f}" y2="{y + dy:.0f}" marker-end="url(#wind-head)"/>')

    # Flag on the pier, in lifeguard colors: hangs limp in calm air, stands out stiff downwind in a breeze.
    parts.append('<line class="pole" x1="596" y1="124" x2="596" y2="66"/>')
    if kt < 3:
        parts.append(f'<polygon class="flag {_CLASS[f.decision]}" points="596,68 602,68 602,96 596,96"/>')
    else:
        reach = (10 + min(kt, 30) * 1.3) * (-1 if on >= 0 else 1) * max(0.35, abs(on))
        droop = max(0, 14 - kt) * 0.9
        parts.append(f'<polygon class="flag {_CLASS[f.decision]}" points="596,68 {596 + reach:.0f},{70 + droop:.0f} {596 + reach:.0f},{84 + droop:.0f} 596,84"/>')

    if (f.snap.window_start.month, f.snap.window_start.day) == (10, 31):
        parts.append('<g><circle class="pumpkin" cx="540" cy="114" r="10"/><rect class="stem" x="538" y="101" width="4" height="5"/>'
                     '<polygon class="carve" points="534,111 538,111 536,107"/><polygon class="carve" points="542,111 546,111 544,107"/>'
                     '<path class="carve" d="M533,116 l3,3 l3,-3 l3,3 l3,-3 l2,2 v1 h-14 z"/></g>')

    parts.append('<path class="sand" d="M0,186 Q110,190 200,232 L232,300 L0,300 Z"/>')
    if f.decision == "GO":
        x = 430
        y = wave_y(x, 214, amp, chop) - 6
        parts.append(f'<g transform="translate({x},{y:.0f}) rotate(-10)"><ellipse class="board" cx="0" cy="0" rx="30" ry="5"/>'
                     '<line class="figure" x1="-6" y1="-2" x2="0" y2="-20"/><line class="figure" x1="8" y1="-2" x2="0" y2="-20"/>'
                     '<line class="figure" x1="0" y1="-20" x2="2" y2="-38"/><line class="figure" x1="-14" y1="-34" x2="18" y2="-30"/>'
                     '<circle class="head" cx="3" cy="-45" r="7"/></g>')
        parts.append('<g transform="translate(26,236)"><rect class="bedframe" x="0" y="8" width="110" height="12"/>'
                     '<rect class="bedframe" x="0" y="-14" width="8" height="34"/><ellipse class="pillow" cx="24" cy="4" rx="13" ry="6"/>'
                     '<path class="blanket" d="M38,8 q30,-4 70,0 v4 h-70 z"/></g>')
    elif f.decision == "MAYBE":
        parts.append('<g transform="translate(70,228)"><ellipse class="board" cx="22" cy="-10" rx="5" ry="30"/>'
                     '<line class="figure" x1="-4" y1="22" x2="0" y2="0"/><line class="figure" x1="6" y1="22" x2="0" y2="0"/>'
                     '<line class="figure" x1="0" y1="0" x2="0" y2="-22"/><line class="figure" x1="0" y1="-16" x2="18" y2="-8"/>'
                     '<circle class="head" cx="0" cy="-30" r="7"/><text class="think" x="-30" y="-44">?</text></g>')
    else:
        parts.append('<g transform="translate(26,236)"><rect class="bedframe" x="0" y="8" width="110" height="12"/>'
                     '<rect class="bedframe" x="0" y="-14" width="8" height="34"/><ellipse class="pillow" cx="24" cy="4" rx="13" ry="6"/>'
                     '<circle class="head" cx="26" cy="-1" r="8"/><path class="blanket" d="M34,8 q14,-22 40,-12 q20,6 34,4 v8 z"/>'
                     '<text class="zzz" x="40" y="-18" font-size="13">z</text><text class="zzz" x="52" y="-30" font-size="17">z</text>'
                     '<text class="zzz" x="67" y="-46" font-size="22">Z</text></g>')

    body = "".join(parts)
    return (
        '<svg viewBox="0 0 720 300" role="img" aria-labelledby="scene-t"><title id="scene-t">'
        f'Cartoon of Juno Pier: {face:.1f} ft faces at {period:.0f} s, {kt:.0f} kt wind</title>'
        '<defs><marker id="wind-head" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto">'
        '<path class="gust-head" d="M1,1 L9,5 L1,9 Z"/></marker></defs>'
        f'{body}</svg>'
    )


def _gauge(f: _Facts) -> str:
    p = f.prefs
    x0, w = 20, 600

    def x(v: float) -> float:
        return x0 + max(0.0, min(10.0, v)) / 10 * w

    final, base = f.final, float(f.record.get("base_score", f.final))
    s = [
        f'<rect class="z-nogo" x="{x0}" y="30" width="{x(p.maybe_threshold) - x0:.0f}" height="20" rx="4"/>',
        f'<rect class="z-maybe" x="{x(p.maybe_threshold):.0f}" y="30" width="{x(p.go_threshold) - x(p.maybe_threshold):.0f}" height="20"/>',
        f'<rect class="z-go" x="{x(p.go_threshold):.0f}" y="30" width="{x(10) - x(p.go_threshold):.0f}" height="20" rx="4"/>',
        f'<text class="zl" x="{(x0 + x(p.maybe_threshold)) / 2:.0f}" y="44">stay in bed</text>',
        f'<text class="zl" x="{(x(p.maybe_threshold) + x(p.go_threshold)) / 2:.0f}" y="44">coin flip</text>',
        f'<text class="zl" x="{(x(p.go_threshold) + x(10)) / 2:.0f}" y="44">go surf</text>',
    ]
    for v in (0, p.maybe_threshold, p.go_threshold, 10):
        s.append(f'<text class="tick" x="{x(v):.0f}" y="68">{v:g}</text>')
    if abs(final - base) > 1e-9:
        s.append(f'<line class="base-mark" x1="{x(base):.0f}" y1="26" x2="{x(base):.0f}" y2="54"/>')
    s.append(f'<polygon class="needle" points="{x(final) - 7:.0f},12 {x(final) + 7:.0f},12 {x(final):.0f},26"/>')
    s.append(f'<line class="needle-line" x1="{x(final):.0f}" y1="26" x2="{x(final):.0f}" y2="54"/>')
    anchor = "start" if final < 1.5 else ("end" if final > 8.5 else "middle")
    s.append(f'<text class="needle-label" x="{x(final):.0f}" y="9" text-anchor="{anchor}">{final:.2f}</text>')
    return (f'<svg viewBox="0 0 640 74" role="img" aria-label="Score {final:.2f} of 10; maybe at '
            f'{p.maybe_threshold:g}, go at {p.go_threshold:g}">{"".join(s)}</svg>')


def _compass_svg(f: _Facts) -> str:
    cx, cy, r = 120, 116, 84
    facing, offshore = f.spot.beach_facing_deg, f.spot.offshore_deg

    def pt(bearing: float, rad: float) -> tuple[float, float]:
        t = math.radians(bearing)
        return cx + rad * math.sin(t), cy - rad * math.cos(t)

    a1, a2 = pt(facing - 90, r), pt(facing + 90, r)
    s = [
        f'<circle class="c-sea" cx="{cx}" cy="{cy}" r="{r}"/>',
        # Land is the half of the disc behind the beach.
        f'<path class="c-land" d="M{a1[0]:.1f},{a1[1]:.1f} A{r},{r} 0 0 0 {a2[0]:.1f},{a2[1]:.1f} Z"/>',
        f'<line class="c-shore" x1="{a1[0]:.1f}" y1="{a1[1]:.1f}" x2="{a2[0]:.1f}" y2="{a2[1]:.1f}"/>',
    ]
    for label, b in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
        x, y = pt(b, r + 14)
        s.append(f'<text class="c-dir" x="{x:.0f}" y="{y + 4:.0f}">{label}</text>')

    def arrow(bearing: float, cls: str, dashed: bool = False) -> str:
        x1, y1 = pt(bearing, r - 6)
        x2, y2 = pt(bearing, 20)
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        return (f'<line class="{cls}" x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"{dash} '
                f'marker-end="url(#{cls}-head)"/>')

    s.append(arrow(offshore, "a-ideal", dashed=True))
    if f.swell and f.swell.direction_deg is not None:
        s.append(arrow(f.swell.direction_deg, "a-swell"))
    if f.wind_dir is not None:
        s.append(arrow(f.wind_dir, "a-wind"))
    heads = "".join(
        f'<marker id="{c}-head" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto">'
        f'<path class="{c}-fill" d="M1,1 L9,5 L1,9 Z"/></marker>'
        for c in ("a-ideal", "a-swell", "a-wind")
    )
    return (f'<svg viewBox="0 0 240 232" role="img" aria-label="Compass: wind, swell and the ideal offshore direction">'
            f'<defs>{heads}</defs>{"".join(s)}</svg>')


def _tide_at(t: datetime, tides) -> float | None:
    for a, b in itertools.pairwise(tides):
        if a.time <= t <= b.time:
            frac = (t - a.time) / (b.time - a.time)
            return a.height_ft + (b.height_ft - a.height_ft) * (1 - math.cos(math.pi * frac)) / 2
    return None


def _tide_svg(f: _Facts) -> tuple[str, str]:
    tides = sorted(f.snap.tides, key=lambda t: t.time)
    if len(tides) < 2:
        return "", "No tide data for this window."
    t0, t1 = tides[0].time, tides[-1].time
    lo, hi = min(t.height_ft for t in tides), max(t.height_ft for t in tides)
    span = (hi - lo) or 1.0
    X0, W, Y0, H = 30, 600, 36, 96

    def x(t: datetime) -> float:
        return X0 + (t - t0) / (t1 - t0) * W

    def y(h: float) -> float:
        return Y0 + (hi - h) / span * H

    steps = int((t1 - t0) / timedelta(minutes=10))
    pts = [t0 + timedelta(minutes=10 * i) for i in range(steps + 1)] + [t1]
    curve = " ".join(f"{'M' if i == 0 else 'L'}{x(t):.1f},{y(_tide_at(t, tides)):.1f}" for i, t in enumerate(pts))
    ws, we = max(f.snap.window_start, t0), min(f.snap.window_end, t1)
    s = []
    if ws < we:
        s.append(f'<rect class="t-window" x="{x(ws):.1f}" y="{Y0 - 14}" width="{x(we) - x(ws):.1f}" height="{H + 14}"/>')
        s.append(f'<text class="t-wlabel" x="{(x(ws) + x(we)) / 2:.0f}" y="{Y0 - 20}">your window</text>')
    s.append(f'<path class="t-fill" d="{curve} L{x(t1):.1f},{Y0 + H} L{x(t0):.1f},{Y0 + H} Z"/>')
    s.append(f'<path class="t-line" d="{curve}"/>')
    if f.snap.sunrise and t0 <= f.snap.sunrise <= t1:
        sx = x(f.snap.sunrise)
        s.append(f'<line class="t-sun" x1="{sx:.1f}" y1="{Y0}" x2="{sx:.1f}" y2="{Y0 + H}"/>')
        s.append(f'<text class="t-sunlabel" x="{sx + 4:.0f}" y="{Y0 + 12}">sunrise {_clock(f.snap.sunrise)}</text>')
    for t in tides:
        ty = y(t.height_ft) + (-10 if t.kind == "H" else 18)
        anchor = "start" if x(t.time) < X0 + 40 else ("end" if x(t.time) > X0 + W - 40 else "middle")
        s.append(f'<circle class="t-dot" cx="{x(t.time):.1f}" cy="{y(t.height_ft):.1f}" r="3.5"/>')
        s.append(f'<text class="t-ev" x="{x(t.time):.0f}" y="{ty:.0f}" text-anchor="{anchor}">'
                 f'{"High" if t.kind == "H" else "Low"} {t.height_ft:.1f} ft, {_clock(t.time)}</text>')
    tick = t0.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while tick < t1:
        if tick.hour % 3 == 0:
            s.append(f'<text class="t-tick" x="{x(tick):.0f}" y="{Y0 + H + 36}">{tick.strftime("%I %p").lstrip("0")}</text>')
        tick += timedelta(hours=1)
    svg = f'<svg viewBox="0 0 660 176" role="img" aria-label="Tide curve with the surf window shaded">{"".join(s)}</svg>'

    a, b = _tide_at(ws, tides), _tide_at(we, tides)
    if a is None or b is None:
        return svg, "The window falls outside the tide data."
    trend = "rising" if b > a + 0.05 else ("falling" if b < a - 0.05 else "slack")
    return svg, f"{trend.capitalize()} tide during your window: {a:.1f} ft to {b:.1f} ft."


# --- HTML sections ---

def _receipt(f: _Facts) -> str:
    rows: list[tuple[str, str, str]] = []
    b, sw = f.best, f.swell
    if b and b.face_ft is not None and b.period_s is not None:
        mult = b.face_ft / sw.height_ft if sw and sw.height_ft else None
        q = period_quality(b.period_s, f.cfg)
        d = swell_direction_factor(b.swell_dir_deg, f.spot, f.cfg)
        size = size_score(b.face_ft, f.prefs)
        how = f"{sw.height_ft:.0f} ft &times; {mult:.1f} at {b.period_s:.0f} s" if sw and mult else ""
        rows.append(("Wave size", f"{size:.1f}", f"{b.face_ft:.1f} ft faces ({how})"))
        rows.append(("&times; period quality", f"{q:.2f}",
                     f"a {b.period_s:.0f} s period keeps {q * 100:.0f}% of that" if q < 1 else "long-period bonus"))
        if b.swell_dir_deg is not None:
            off = angle_off(b.swell_dir_deg, f.spot.beach_facing_deg)
            rows.append(("&times; swell direction", f"{d:.2f}", f"{off:.0f}&deg; off the beach" + (", aimed right at us" if d >= 1 else "")))
        else:
            rows.append(("&times; swell direction", f"{d:.2f}", "direction unknown"))
        rows.append(("= wave score", f"{b.wave_score:.2f}", ""))
        cls = _WIND_WORDS.get(b.wind_class or "", "unknown")
        wind_note = f"{_n(b.wind_kt, '{:.0f}')} kt {cls}"
        if b.wind_factor <= 0.2:
            wind_note += f": the wind took {100 - b.wind_factor * 100:.0f}% of the score"
        elif b.wind_factor > 1:
            wind_note += ": clean-wind bonus"
        rows.append(("&times; wind", f"{b.wind_factor:.2f}", wind_note))
        why = []
        if any("small craft" in a.event.lower() for a in f.alerts):
            why.append("small craft advisory")
        wet = next((pb.min_pct for pb in f.cfg.weather.pop_buckets if pb.factor < 1), None)
        if f.hour and f.hour.pop_pct is not None and wet is not None and f.hour.pop_pct >= wet:
            why.append(f"{f.hour.pop_pct:.0f}% rain")
        rows.append(("&times; weather", f"{b.weather_factor:.2f}", ", ".join(why) or "nothing to complain about"))
        rows.append(("= best hour", f"{b.score:.2f}", _clock(b.time)))
    n_hours = len(f.score.best_block.hours) if f.score.best_block else 0
    rows.append((f"best {n_hours}-hour block" if n_hours else "base score", f"{f.record.get('base_score', 0):.2f}", "average"))
    for v in f.record.get("vetoes") or []:
        rows.append(("veto", "&rarr; 0", _e(v["detail"])))
    adj = float(f.record.get("adjustment") or 0)
    if (f.record.get("llm") or {}).get("used"):
        reason = _e(f.assessment.adjustment_reason) if f.assessment and f.assessment.adjustment_reason else (
            "Claude checked the math and declined to argue" if adj == 0 else "")
        rows.append(("Claude adjustment", f"{adj:+.2f}", reason))
    body = "".join(f'<tr><td>{a}</td><td class="num">{v}</td><td class="note">{n}</td></tr>' for a, v, n in rows)
    total = f'<tr class="total"><td>Total</td><td class="num">{f.final:.2f}</td><td class="note">out of 10</td></tr>'
    go = f.prefs.go_threshold
    if f.decision == "GO":
        tail = f"Clears the {go:g} go line by {f.final - go:.2f}. Alarm armed."
    elif f.final > 0:
        tail = f"The go line is {go:g}. You'd need {go / f.final:.1f}&times; this score to get out of bed."
    else:
        tail = f"The go line is {go:g}. You'd need infinitely more score. Division by zero avoided."
    return f'<table class="receipt">{body}{total}</table><p class="punch">{tail}</p>'


def _cards(f: _Facts, tide_note: str) -> str:
    b, sw, snap = f.best, f.swell, f.snap
    wave_lines = []
    if sw:
        wave_lines.append(f"Forecast swell: {sw.height_ft:.0f} ft"
                          + (f" from the {_compass(sw.direction_deg)}" if sw.direction_deg is not None else "")
                          + f" at {sw.period_s:.0f} s")
    if snap.buoy_now:
        bu = snap.buoy_now
        wave_lines.append(f"Buoy {_e(bu.station)} right now: {_n(bu.height_ft)} ft at {_n(bu.period_s, '{:.0f}')} s"
                          + (f" from the {_compass(bu.direction_deg)}" if bu.direction_deg is not None else ""))
    wave = _card("Waves", f"{_n(b.face_ft if b else None)} ft", "estimated face height", wave_lines,
                 _size_joke(b.face_ft if b else None, f.prefs) + " " + _period_joke(b.period_s if b else None))

    wind_lines, big, sub = [], "n/a", "no wind data"
    if f.wind_kt is not None:
        gust = f.hour.gust_kt if f.hour else None
        big = f"{f.wind_kt:.0f} kt"
        sub = (f"from the {_compass(f.wind_dir)}" if f.wind_dir is not None else "direction unknown") + (
            f", gusts {gust:.0f} kt" if gust is not None else "")
        if f.wind_dir is not None:
            off = angle_off(f.wind_dir, f.spot.offshore_deg)
            wind_lines.append(f"Ideal is offshore, from the {_compass(f.spot.offshore_deg)} ({f.spot.offshore_deg:.0f}&deg;). "
                              f"This comes from {f.wind_dir:.0f}&deg;, {off:.0f}&deg; away from ideal"
                              + (", about as wrong as a compass allows." if off >= 150 else "."))
        wind_lines.append(f"Gale veto at {f.cfg.vetoes.gale_sustained_kt:g} kt sustained or {f.cfg.vetoes.gale_gust_kt:g} kt gusts.")
    if snap.wind_now and snap.wind_now.wind_kt is not None:
        w = snap.wind_now
        wind_lines.append(f"Station {_e(w.station)} right now: {w.wind_kt:.0f} kt"
                          + (f" from the {_compass(w.direction_deg)}" if w.direction_deg is not None else ""))
    wind = _card("Wind", big, sub, wind_lines, _wind_joke(f))

    sky_lines = []
    if snap.sunrise:
        sky_lines.append(f"Sunrise {_clock(snap.sunrise)}")
    sky_lines.append(f"Rain chance up to {_n(f.max_pop, '{:.0f}')}%, thunder up to {_n(f.max_thunder, '{:.0f}')}% "
                     f"(veto at {f.cfg.vetoes.thunder_pct:g}%)")
    texts = sorted({h.weather_text for h in snap.hours if h.weather_text})
    if texts:
        sky_lines.append("Forecast says: " + _e(", ".join(texts)))
    sky_lines.append(_e(tide_note))
    sky = _card("Sky and tide", f"{_n(f.max_pop, '{:.0f}')}%", "rain chance in the window", sky_lines, _sky_joke(f))

    alert_lines = []
    for a in snap.alerts:
        later = a not in f.alerts
        alert_lines.append(f"{_e(a.event)}" + (f" ({_e(a.severity.lower())})" if a.severity else "")
                           + (", starts after your window" if later else ""))
    alerts = _card("Advisories", str(len(f.alerts)), "active during your window", alert_lines or ["None"],
                   _alerts_joke(len(f.alerts)))
    return wave + wind + sky + alerts


def _card(title: str, big: str, sub: str, lines: list[str], joke: str) -> str:
    items = "".join(f"<li>{line}</li>" for line in lines)
    return (f'<section class="card"><h3>{title}</h3><p class="big">{big}</p><p class="sub">{sub}</p>'
            f'<ul>{items}</ul><p class="joke">{_e(joke)}</p></section>')


def _claude_card(f: _Facts) -> str:
    llm = f.record.get("llm") or {}
    if llm.get("error"):
        head = f"Claude was unavailable ({_e(llm['error'])}), so the formula decided alone."
    elif not llm.get("used"):
        head = "Claude sat this one out. Deterministic score only."
    else:
        adj = float(f.record.get("adjustment") or 0)
        if adj == 0:
            mood = "Claude checked the math and declined to argue."
        elif adj > 0:
            mood = f"Claude saw something the formula missed and added {adj:.2f}."
        else:
            mood = f"Claude was more skeptical than the formula and took off {-adj:.2f}."
        head = (f"{mood} This verdict cost {llm.get('cost_usd', 0) * 100:.1f}&cent; and "
                f"{llm.get('latency_s', 0):.1f} s of thinking. Your sleep is worth more.")
    concerns = "".join(f"<li>{_e(c)}</li>" for c in (f.assessment.data_concerns if f.assessment else []))
    meta = (f'<p class="sub">{_e(llm.get("model") or "no model")} &middot; confidence {_e(f.record.get("confidence", "n/a"))}</p>'
            if llm.get("used") else "")
    return (f'<section class="card"><h3>What Claude thinks</h3>{meta}<p>{head}</p>'
            f'<blockquote>{_e(f.record.get("explanation", ""))}</blockquote>'
            + (f'<p class="sub">Data concerns</p><ul>{concerns}</ul>' if concerns else "") + "</section>")


def _alexa_card(f: _Facts) -> str:
    n = f.record.get("notify")
    if n is None:
        head, verb = "Alexa stays quiet. Sleep in.", "What she'd have said"
    elif n.get("mode") == "dry_run":
        head, verb = "Dry run: Alexa would have woken you. Nobody was harmed.", "What she'd say"
    elif n.get("mode") == "skipped":
        head, verb = "Alexa already woke you once today and has some self-respect.", "What she'd say"
    elif n.get("woke"):
        mode = " (simulated)" if n.get("mode") == "simulated" else ""
        head, verb = f"Alexa woke you{mode}. Rise and shred.", "What she said"
    else:
        head, verb = f"The wake-up failed: {_e(n.get('detail', ''))}", "What she'd have said"
    return (f'<section class="card"><h3>The alarm</h3><p>{head}</p><p class="sub">{verb}</p>'
            f'<blockquote class="spoken">&ldquo;{_e(f.record.get("spoken_message", ""))}&rdquo;</blockquote></section>')


def _hourly_table(f: _Facts) -> str:
    by_time = {h.time: h for h in f.snap.hours}
    rows = []
    for s in f.score.hourly:
        h = by_time.get(s.time)
        rows.append(
            "<tr>"
            f"<td>{_clock(s.time)}</td><td class='num'>{_n(s.face_ft)} ft</td><td class='num'>{_n(s.period_s, '{:.0f}')} s</td>"
            f"<td class='num'>{_n(s.wind_kt, '{:.0f}')} kt</td><td class='num'>{_n(h.gust_kt if h else None, '{:.0f}')} kt</td>"
            f"<td>{_compass(h.wind_dir_deg) if h and h.wind_dir_deg is not None else 'n/a'}</td>"
            f"<td>{_WIND_WORDS.get(s.wind_class or '', 'n/a')}</td>"
            f"<td class='num'>{_n(h.pop_pct if h else None, '{:.0f}')}%</td><td class='num'>{s.score:.2f}</td>"
            "</tr>"
        )
    if not rows:
        return '<p class="sub">No hourly data.</p>'
    return ('<table class="hourly"><thead><tr><th>Hour</th><th>Face</th><th>Period</th><th>Wind</th><th>Gust</th>'
            f'<th>From</th><th>Wind type</th><th>Rain</th><th>Score</th></tr></thead><tbody>{"".join(rows)}</tbody></table>')


def _footer(f: _Facts) -> str:
    r, snap = f.record, f.snap
    fresh = ", ".join(f"{_e(k)} {v.astimezone(snap.window_start.tzinfo):%b %d} {_clock(v.astimezone(snap.window_start.tzinfo))}"
                      for k, v in snap.source_times.items())
    bits = [f"Run {_e(r.get('run_id', ''))}", f"scoring {_e(r.get('scoring_version', ''))}"]
    if fresh:
        bits.append(f"data as of: {fresh}")
    if snap.missing_sources:
        bits.append("missing: " + _e(", ".join(snap.missing_sources)))
    if r.get("guardrail_notes"):
        bits.append("guardrails: " + _e("; ".join(r["guardrail_notes"])))
    if r.get("replay"):
        bits.append(f"replay of {_e(r['replay'])}")
    if r.get("dry_run"):
        bits.append("dry run")
    marine = f'<p class="marine">NWS marine forecast ({_e(snap.marine_period_name or "")}): {_e(snap.marine_text)}</p>' if snap.marine_text else ""
    return f'<footer>{marine}<p>{" &middot; ".join(bits)}</p><p>Jokes are rule-based on the numbers above; they never change the decision.</p></footer>'


_CSS = """
:root{--bg:#fbf7f2;--card:#fff;--ink:#1f2328;--muted:#5b6470;--line:#e6dfd5;
--go:#1f7a45;--maybe:#9a6212;--nogo:#b4352a;--go-bg:#e2f3e8;--maybe-bg:#fbefd9;--nogo-bg:#fbe4e0;
--wind:#d9480f;--swell:#1c6fc2;--ideal:#2b8a3e;
--sky:#fde3cf;--sea-back:#86c0db;--sea:#2f7fb0;--foam:#fff;--sand:#ecd5a6;--pier:#8a6a4a;--sun:#f6a821;
--cloud:#fff;--cloud-edge:#c9d2db;--rain:#4f86b5;--bed:#a9503a;--blanket:#3f73a8;--pillow:#fff;--skin:#f0c29a;
--figure:#2b2b2b;--board:#f3efe6;--pumpkin:#ef7d17;--stem:#4d7c2a;--carve:#3b2208}
@media (prefers-color-scheme:dark){:root{--bg:#131519;--card:#1c1f25;--ink:#e8e6e3;--muted:#a3aab4;--line:#2d323a;
--go:#5cc98a;--maybe:#e6ad4f;--nogo:#f07a6a;--go-bg:#15301f;--maybe-bg:#33270f;--nogo-bg:#3b1a16;
--wind:#ff8c52;--swell:#64adf2;--ideal:#63c574;
--sky:#1d2a46;--sea-back:#2d5f80;--sea:#173f5e;--foam:#dbe7f0;--sand:#8c7650;--pier:#5d4734;--sun:#f29a3a;
--cloud:#8b97aa;--cloud-edge:#6d788a;--rain:#86b6e0;--bed:#7f3b2a;--blanket:#2f5a87;--pillow:#cfd6de;--skin:#d6a47c;
--figure:#f2efe9;--board:#d9d3c5;--pumpkin:#f0892a;--stem:#6aa13c;--carve:#24150a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 40px}
h1{font-size:28px;line-height:1.2;margin:4px 0 8px;font-weight:650}
h3{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:0 0 6px;font-weight:600}
.hero{display:flex;gap:20px;align-items:center;margin-bottom:16px}
.stamp{flex:none;font-size:30px;font-weight:800;padding:10px 18px;border:4px solid;border-radius:10px;transform:rotate(-6deg);
text-transform:uppercase;letter-spacing:.04em}
.go .stamp{color:var(--go);background:var(--go-bg)}.maybe .stamp{color:var(--maybe);background:var(--maybe-bg)}
.nogo .stamp{color:var(--nogo);background:var(--nogo-bg)}
.eyebrow,.sub{color:var(--muted);margin:0}.sub b{color:var(--ink)}
.scene{margin:0 0 16px;border-radius:14px;overflow:hidden;border:1px solid var(--line)}
.scene svg{display:block;width:100%;height:auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:16px}.grid .card{margin:0}
.grid.two{grid-template-columns:repeat(auto-fit,minmax(300px,1fr));margin-bottom:16px}
.grid.four{margin-bottom:16px}
.big{font-size:30px;font-weight:700;margin:0;font-variant-numeric:tabular-nums}
ul{margin:8px 0;padding-left:18px;color:var(--muted)}li{margin:2px 0}
.joke{margin:10px 0 0;padding-top:10px;border-top:1px dashed var(--line);font-style:italic}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
.receipt td{padding:5px 6px;border-bottom:1px dotted var(--line);vertical-align:top}
.receipt td:first-child{white-space:nowrap}.num{text-align:right;white-space:nowrap;font-family:ui-monospace,Consolas,monospace}
.note{color:var(--muted);font-size:13px}.receipt .total td{border-bottom:none;border-top:2px solid var(--ink);font-weight:700;font-size:17px}
.punch{margin:10px 0 0;font-weight:600}
.hourly th,.hourly td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left}.hourly th{color:var(--muted);font-weight:600;font-size:13px}
.table-wrap{overflow-x:auto}
blockquote{margin:10px 0;padding:8px 12px;border-left:3px solid var(--line);color:var(--ink)}
.spoken{font-size:17px}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:13px;color:var(--muted);margin-top:4px}
.sw{display:inline-block;width:18px;height:3px;vertical-align:middle;margin-right:6px}
.compass-wrap{display:flex;flex-direction:column;align-items:center}.compass-wrap svg{width:100%;max-width:260px}
footer{color:var(--muted);font-size:12.5px}.marine{font-style:italic}
svg text{font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
.sky{fill:var(--sky)}.sun{fill:var(--sun)}.sea{fill:var(--sea)}.sea-back{fill:var(--sea-back)}.sand{fill:var(--sand)}
.pier{fill:var(--pier)}.rail{stroke:var(--pier);stroke-width:2}.foam{fill:none;stroke:var(--foam);stroke-width:2.5;stroke-linecap:round}
.cloud ellipse{fill:var(--cloud);stroke:var(--cloud-edge);stroke-width:1}.rain{stroke:var(--rain);stroke-width:2;stroke-linecap:round}
.bolt{fill:var(--sun)}.gust{stroke:var(--wind);stroke-width:2.5;stroke-linecap:round}.gust-head{fill:var(--wind)}
.pole{stroke:var(--figure);stroke-width:2}.flag.nogo{fill:var(--nogo)}.flag.maybe{fill:var(--maybe)}.flag.go{fill:var(--go)}
.bedframe{fill:var(--bed)}.pillow{fill:var(--pillow)}.blanket{fill:var(--blanket)}.head{fill:var(--skin)}
.figure{stroke:var(--figure);stroke-width:3;stroke-linecap:round}.board{fill:var(--board);stroke:var(--figure);stroke-width:1.5}
.zzz,.think{fill:var(--figure);font-weight:700}.think{font-size:28px}
.pumpkin{fill:var(--pumpkin)}.stem{fill:var(--stem)}.carve{fill:var(--carve)}
.z-nogo{fill:var(--nogo-bg)}.z-maybe{fill:var(--maybe-bg)}.z-go{fill:var(--go-bg)}
.zl{font-size:12px;fill:var(--muted);text-anchor:middle}.tick{font-size:12px;fill:var(--muted);text-anchor:middle}
.needle{fill:var(--ink)}.needle-line{stroke:var(--ink);stroke-width:2.5}.needle-label{font-size:13px;font-weight:700;fill:var(--ink)}
.base-mark{stroke:var(--muted);stroke-width:2;stroke-dasharray:3 3}
.c-sea{fill:var(--sea-back);opacity:.45}.c-land{fill:var(--sand)}.c-shore{stroke:var(--pier);stroke-width:2}
.c-dir{font-size:12px;fill:var(--muted);text-anchor:middle}
.a-wind{stroke:var(--wind);stroke-width:3.5}.a-swell{stroke:var(--swell);stroke-width:3.5}.a-ideal{stroke:var(--ideal);stroke-width:2.5}
.a-wind-fill{fill:var(--wind)}.a-swell-fill{fill:var(--swell)}.a-ideal-fill{fill:var(--ideal)}
.t-window{fill:var(--sun);opacity:.18}.t-wlabel{font-size:12px;fill:var(--muted);text-anchor:middle}
.t-fill{fill:var(--sea-back);opacity:.35}.t-line{fill:none;stroke:var(--swell);stroke-width:2.5}
.t-dot{fill:var(--swell)}.t-ev{font-size:12px;fill:var(--ink)}.t-tick{font-size:12px;fill:var(--muted);text-anchor:middle}
.t-sun{stroke:var(--sun);stroke-width:2;stroke-dasharray:4 3}.t-sunlabel{font-size:12px;fill:var(--muted)}
@media (max-width:560px){.hero{flex-direction:column;align-items:flex-start}h1{font-size:23px}}
"""


def render_report(
    record: dict,
    snapshot: ConditionsSnapshot,
    score: ScoreResult,
    spot: Spot,
    prefs: Preferences,
    cfg: ScoringConfig,
    assessment: Assessment | None = None,
) -> str:
    f = _facts(record, snapshot, score, spot, prefs, cfg, assessment)
    tide_svg, tide_note = _tide_svg(f)
    ws, we = snapshot.window_start, snapshot.window_end
    when = f"{ws:%A, %B} {ws.day} &middot; {_clock(ws)} to {_clock(we)}"
    go = prefs.go_threshold
    meter = (f"Score <b>{f.final:.2f}</b> / 10 &middot; go line {go:g} &middot; maybe line {prefs.maybe_threshold:g}"
             f" &middot; confidence {_e(record.get('confidence', 'n/a'))}")
    legend = ('<div class="legend"><span><i class="sw" style="background:var(--wind)"></i>wind (from)</span>'
              '<span><i class="sw" style="background:var(--swell)"></i>swell (from)</span>'
              '<span><i class="sw" style="background:var(--ideal)"></i>ideal offshore wind</span>'
              '<span><i class="sw" style="background:var(--sand);height:10px"></i>land</span>'
              '<span><i class="sw" style="background:var(--sea-back);height:10px"></i>ocean</span></div>')
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Surf check: {_e(spot.spoken_name)} {ws:%a %b} {ws.day}</title><style>{_CSS}</style></head>
<body><main>
<header class="hero {_CLASS[f.decision]}"><div class="stamp">{_LABEL[f.decision]}</div><div>
<p class="eyebrow">{_e(spot.name)} &middot; {when}</p><h1>{_e(_headline(f))}</h1><p class="sub">{meter}</p></div></header>
<figure class="scene">{_scene(f)}</figure>
<section class="card"><h3>Where the score landed</h3>{_gauge(f)}</section>
<div class="grid four">{_cards(f, tide_note)}</div>
<div class="grid two"><section class="card"><h3>The math</h3>{_receipt(f)}</section>
<section class="card compass-wrap"><h3>Wind vs. swell vs. beach</h3>{_compass_svg(f)}{legend}</section></div>
<section class="card"><h3>Tide</h3>{tide_svg}<p class="sub">{_e(tide_note)}</p></section>
<div class="grid two">{_claude_card(f)}{_alexa_card(f)}</div>
<section class="card"><h3>Hour by hour</h3><div class="table-wrap">{_hourly_table(f)}</div></section>
{_footer(f)}
</main></body></html>
"""


def write_report(run_dir: Path, record, snapshot, score, spot, prefs, cfg, assessment=None) -> Path:
    path = run_dir / REPORT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(record, snapshot, score, spot, prefs, cfg, assessment), encoding="utf-8")
    return path


def rebuild_report(run_dir: Path, spot: Spot, prefs: Preferences, cfg: ScoringConfig) -> Path:
    """Re-render a past run's report from its saved JSON (e.g. after a design change)."""
    record = json.loads((run_dir / "record.json").read_text(encoding="utf-8"))
    snapshot = ConditionsSnapshot.model_validate_json((run_dir / "snapshot.json").read_text(encoding="utf-8"))
    score = ScoreResult.model_validate_json((run_dir / "score.json").read_text(encoding="utf-8"))
    a_path = run_dir / "assessment.json"
    assessment = Assessment.model_validate_json(a_path.read_text(encoding="utf-8")) if a_path.exists() else None
    return write_report(run_dir, record, snapshot, score, spot, prefs, cfg, assessment)
