# Surf Check Agent — Specification (Step 1)

| | |
|---|---|
| **Status** | Draft v0.1, for review |
| **Owner** | Rob McClanahan |
| **Date** | 2026-10-30 |
| **Next steps** | Step 2: architecture · Step 3: build and test |

---

## 1. Goal

Every morning the agent decides whether conditions at my surf spot are worth getting up for. If they are, it triggers an Alexa routine to wake me up with a short spoken summary. If they aren't, it lets me sleep and logs why.

**One-line success test:** I'm woken up on good mornings, I'm not woken up on bad ones, and I'm *never* woken up to surf into a thunderstorm.

## 2. Scope

### In scope (v1)
- Run **manually** from the command line (scheduling comes later; see §11).
- One configured spot at a time. The default is **Juno Beach Pier, Juno Beach, FL**.
- Pull NOAA/NWS marine and weather data, score conditions, and decide **GO / MAYBE / NO-GO**.
- Trigger an Alexa routine on GO (and optionally on MAYBE).
- Dry-run mode: does everything except wake me.
- Evals: a labeled scenario suite that grades the decisions.
- A decision log I can review and give feedback on.

### Out of scope (v1)
- Scheduled or always-on execution (planned for v2).
- Multiple spots compared against each other ("which spot is best today"). The config should allow it later.
- Crowd levels, water temperature and webcams (these may be nice-to-haves later).
- A mobile app or UI.

## 3. User stories

1. **As a surfer**, when I run `surf-check` it tells me GO / MAYBE / NO-GO, gives a score out of 10, and gives me 2–3 sentences of reasoning.
2. **As a surfer**, when the decision is GO, my Alexa wakes me and says something like *"Surf's up at Juno Pier: 3 feet at 11 seconds, light offshore wind, score 8 out of 10."*
3. **As a surfer**, I can change the spot, my size preferences and the thresholds in a config file without touching code.
4. **As a surfer**, I can run `--dry-run` to see the decision without waking anyone.
5. **As a surfer**, I can tell the agent afterwards whether it was right ("went, it was great" / "it said go but it was junk"), and that feedback goes into future tuning and evals.
6. **As the builder**, I can run the eval suite and get accuracy, safety-veto and cost numbers in under a few minutes.

## 4. Spot configuration (default: Juno Beach Pier)

Everything about a spot lives in config. Starting values:

```yaml
spot:
  id: juno-beach-pier
  name: Juno Beach Pier
  lat: 26.8933            # verified 2026-10-30 (OSM pier centroid)
  lon: -80.0557
  timezone: America/New_York
  beach_facing_deg: 73    # ENE (shore bearing ~343°, ±5°). Offshore wind = ~253° (WSW)
  swell_window_deg: [20, 140]   # swell directions that actually reach the beach (NNE through SE)
  noaa:                   # all verified live 2026-10-30; see ARCHITECTURE.md §11
    nws_grid: MFL/111,100 # offshore cell (cleaner swell fields than pier cell 110,100)
    marine_zone: AMZ650   # Coastal waters Jupiter Inlet to Deerfield Beach, 0–20 NM
    alert_zones: [FLZ168, AMZ650]   # land + marine; one point alone misses half the alerts
    wave_buoy: "41114"    # Fort Pierce (CDIP 134), ~76 km NNW. Nearest active wave buoy
    wind_station: LKWF1   # Lake Worth Pier C-MAN, 31 km S; fallback KDJT (PBI airport)
    tide_station: "8722495"   # Jupiter Inlet south jetty, 6 km; fallback 8722670

session:
  start: dawn             # civil dawn (~30 min before sunrise)
  end: "08:30"            # before work
  best_block_hours: 2     # judged on best 2h block, or the whole window if shorter

preferences:                # all heights are estimated wave FACE height in feet
  min_rideable_ft: 3        # below this = flat for me → NO-GO
  max_ft: null              # optional "too big" cap. Off by default (no max)
  go_threshold: 6.0         # score ≥ this → GO
  maybe_threshold: 4.5      # score in [maybe, go) → MAYBE
  wake_on_maybe: false      # MAYBE days: sleep in
  on_missing_data: sleep    # fail closed: no wake, log error
```

> **Local note:** Juno is mostly a short-period wind-swell spot. Long-period groundswell (12s+) is rare and should score very well when it shows up. NE wind is the classic "blown out" direction. W/SW/NW are offshore.

## 5. Data required

All sources are free NOAA/NWS public APIs. Exact endpoints get confirmed in step 2.

| Need | Source (candidate) | Used for |
|---|---|---|
| Wave height, period, swell direction (forecast) | NWS gridpoint forecast (`api.weather.gov/gridpoints/...`: `waveHeight`, `wavePeriod`, `primarySwellHeight/Direction`, `windWaveHeight`) | Main wave input |
| Marine text forecast | NWS Coastal Waters Forecast for the marine zone | "Seas 2–3 ft, dominant period 8 s", advisories, thunderstorm wording |
| Live wave observations | NDBC buoy (nearshore) | Checks the forecast against what's actually out there right now |
| Wind speed, gusts, direction | NWS hourly forecast + nearby C-MAN station | Wind scoring |
| Rain and thunder | NWS hourly forecast (`probabilityOfPrecipitation`, short forecast text) + active alerts for the point | Vetoes and penalties |
| Active hazards | NWS alerts (`/alerts/active?point=`) | Small Craft Advisory, Marine Weather Statement, Rip Current, Special Marine Warning |
| Sunrise | Computed locally | Session window |
| Tide (optional v1) | NOAA CO-OPS predictions | Small modifier |

**Data rules**
- Every input records its source and timestamp. Data older than its freshness limit counts as missing (buoy > 2h old, forecast > 12h old).
- When sources disagree (forecast says 3 ft, buoy says 1 ft), the agent says so and leans on the observation for "right now" and on the forecast for the trend.

## 6. Decision logic

The design principle is that **a deterministic scoring engine is the backbone, and the LLM interprets and explains.** The math is testable and repeatable. The LLM handles what a formula can't: free-text forecasts, sources that disagree, trends ("building through the morning"), and a clear explanation.

### 6.1 Hard vetoes (always NO-GO; nothing overrides them)

| Veto | Default rule |
|---|---|
| Lightning / thunder | Any thunderstorm wording, or thunder probability ≥ 20%, in the session window |
| Severe marine warning | An active Special Marine Warning, Severe Thunderstorm Warning or Tornado Warning |
| Flat | Estimated face height < `min_rideable_ft` (3 ft) |
| Too big *(only if `max_ft` is set; off by default)* | Estimated face height > `max_ft` |
| Gale | Sustained wind ≥ 20 kt or gusts ≥ 28 kt |
| No usable wave data | All wave sources are missing or stale (the agent fails closed and doesn't wake me) |

### 6.2 Wave score (0–10): size × period × swell direction

Period changes both **how big the waves actually break** and **how good they are**. 3 ft at 20 s is a powerful, organized groundswell that breaks well overhead. 3 ft at 3 s is local chop. The scoring handles that in two steps:

**Step A: Estimate the face height** = forecast wave height × period multiplier.
**Step B: Wave quality** = period quality factor.

| Dominant period | Face multiplier | Quality factor | What it feels like |
|---|---|---|---|
| < 6 s | 1.0 | 0.2 | Wind chop, mushy, closeouts |
| 6–8 s | 1.1 | 0.5 | Typical FL wind swell, weak |
| 8–10 s | 1.2 | 0.75 | Decent, rideable shape |
| 10–13 s | 1.4 | 1.0 | Good, real lines |
| 13–16 s | 1.6 | 1.05 | Groundswell, punchy |
| 16 s + | 1.8 | 1.1 | Rare and excellent; watch size |

**Size score (0–10), based on the estimated face height:** 0 below `min_rideable_ft` (a veto). 10 at or above it, with no upper limit by default. If `max_ft` is configured, the score tapers linearly from 10 to 5 over the last 3 ft below `max_ft`.

**Bucket boundaries** in every table are lower-inclusive. For example, exactly 8 s falls in "8–10 s" and exactly 10 kt falls in "10–15 kt".

**Swell direction factor:** the swell reaches the beach only if it's inside `swell_window_deg`.

| Angle off beach-facing direction | Factor |
|---|---|
| ≤ 30° | 1.0 |
| 30–60° | 0.8 |
| 60–80° | 0.5 |
| outside the window | 0.2 |

`wave_score = min(10, size_score × quality_factor × direction_factor)`

### 6.3 Wind factor (0–1.1)

Wind direction is classified relative to the beach. Offshore = `beach_facing_deg + 180` (≈ 253° at Juno).
- Offshore: within ±30° of 253°
- Cross-offshore: 30–67°
- Cross-shore: 67–112°
- Cross-onshore: 112–150°
- Onshore: > 150°

| Sustained speed | Offshore | Cross-off | Cross | Cross-on | Onshore |
|---|---|---|---|---|---|
| 0–5 kt (glassy) | 1.1 | 1.1 | 1.1 | 1.1 | 1.1 |
| 5–10 kt | 1.1 | 1.0 | 0.85 | 0.7 | 0.6 |
| 10–15 kt | 1.0 | 0.85 | 0.6 | 0.45 | 0.3 |
| 15–20 kt | 0.7 | 0.6 | 0.35 | 0.2 | 0.1 |
| ≥ 20 kt | veto | veto | veto | veto | veto |

### 6.4 Weather factor (0.7–1.0)
- Rain probability < 30%: 1.0. 30–60%: 0.9. > 60%: 0.8.
- Small Craft Advisory active: × 0.9 (and mentioned in the explanation).
- Tide (optional): ± 0.05 once a preferred tide is configured.

### 6.5 Final score and decision

```
score = min(10, wave_score × wind_factor × weather_factor)
GO     if no veto and score ≥ go_threshold (6.0)
MAYBE  if no veto and maybe_threshold (4.5) ≤ score < go_threshold
NO-GO  otherwise
```

The score is computed for each hour in the session window (dawn to 8:30 AM). The decision uses the **best contiguous 2-hour block**, or the whole window when it's shorter than 2 hours (e.g. late October, when sunrise is about 7:25). I only need one good stretch before work, not the whole morning.

**LLM adjustment:** the agent may nudge the score by **at most ±1.0**, and only with a stated, evidence-based reason. Examples: the text forecast mentions a building swell the gridded numbers don't show yet, or the buoy is reading much smaller than the forecast. It can **never** override a veto.

### 6.6 Worked examples (these become the first eval cases)

| Scenario | Face est. | Wave | Wind | Weather | Score | Decision |
|---|---|---|---|---|---|---|
| 3 ft @ 20 s, E swell, W 6 kt, clear | 5.4 ft | 10 (capped) | 1.1 | 1.0 | **10** | GO (epic) |
| 3 ft @ 3 s, E, light wind (< 5 kt) | 3.0 ft | 2.0 | 1.1 | 1.0 | **2.2** | NO-GO (chop) |
| 3 ft @ 9 s, ENE 14 kt (onshore) | 3.6 ft | 7.5 | 0.3 | 1.0 | **2.3** | NO-GO (blown out) |
| 3 ft @ 8 s, SW 4 kt | 3.6 ft | 7.5 | 1.1 | 1.0 | **8.3** | GO |
| 3 ft @ 7 s, glassy (< 5 kt) | 3.3 ft | 5.0 | 1.1 | 1.0 | **5.5** | MAYBE → sleep in |
| 3 ft @ 7 s, cross-shore 8 kt | 3.3 ft | 5.0 | 0.85 | 1.0 | **4.3** | NO-GO (just under MAYBE) |
| 2 ft @ 8 s, SW 4 kt | 2.4 ft | — | — | — | — | NO-GO (flat veto, < 3 ft) |
| 2.5 ft @ 10 s, glassy, 40% T-storms | 3.5 ft | — | — | — | — | NO-GO (lightning veto) |
| 6 ft @ 14 s, W 8 kt | 9.6 ft | 10 (capped) | 1.1 | 1.0 | **10** | GO (no max configured) |
| 5 ft @ 9 s, NE 22 kt | 6.0 ft | — | — | — | — | NO-GO (gale veto) |

> These numbers are **starting heuristics**. They're meant to be tuned with the eval suite and with my real-world feedback (§8.3), not treated as gospel.

## 7. Actions and outputs

### 7.1 Wake-up (Alexa)
- **Mechanism:** Voice Monkey (free Alexa skill). I create a trigger device called `surf-wakeup`, and **I design the routine myself in the Alexa app** (alarm, lights, volume, whatever I want). The agent's job is only to fire the trigger and hand over the spoken summary.
- The agent calls the Voice Monkey HTTPS endpoint. If Voice Monkey can't attach announcement text to a routine trigger, the agent makes a separate announce call right after (confirm in step 2). The trigger name and API token come from environment variables and are never stored in code or config.
- Spoken message: ≤ 25 words, for example *"Surf's up at Juno Pier. Three feet at eleven seconds, light offshore wind. Score eight. Go get it."*
- **Idempotent:** at most one wake-up per spot per day, even if I run the agent twice.
- **Dry run:** prints the call it *would* make and doesn't send it.
- The notifier sits behind an interface, so IFTTT, Home Assistant or a phone push could replace or back up Voice Monkey later.

### 7.2 Decision record (always written)
A JSON record per run, appended to a local log:
- Timestamp, spot, decision, final score, the best window, and every component score and veto
- The LLM adjustment (if any) and its reason
- A 2–3 sentence human explanation
- Raw input summary, with source and timestamp for each value
- Whether a wake-up was sent, and the result
- Model, tokens in/out, cache read/write tokens, cost and latency

### 7.3 CLI (manual for v1)
```
surf-check                     # run for the default spot, wake if GO
surf-check --dry-run           # decide, don't wake
surf-check --spot <id>         # another configured spot
surf-check --replay <fixture>  # run against saved data (evals and debugging)
surf-check feedback --date 2026-10-30 --went yes --rating 4 --note "clean, chest high"
```

## 8. Evaluation requirements

### 8.1 Scoring-engine tests (deterministic, no LLM)
- Unit tests for each table in §6, including boundaries (exactly 6.0 s, exactly 20 kt, and so on).
- Every worked example in §6.6 must pass exactly.

### 8.2 Agent eval suite (end-to-end, with LLM)
- **≥ 25 labeled scenarios**, each a fixture of saved real-format API responses plus an expected decision. At minimum they cover:
  - Each veto (lightning with perfect waves is the key one)
  - Flat glassy days, blown-out days, too-big days
  - Rare long-period groundswell
  - The forecast disagreeing with the buoy
  - Missing or stale data, and API errors
  - A swell outside the window (e.g. a big S swell that doesn't reach Juno)
  - Borderline MAYBE cases
- **Metrics and pass bars:**

| Metric | Target |
|---|---|
| Safety-veto recall (never GO when a veto applies) | **100%, hard gate** |
| Decision accuracy (GO/MAYBE/NO-GO vs label) | ≥ 90% |
| False-wake rate (GO when the label is NO-GO) | ≤ 5% |
| LLM adjustment within ±1.0 and justified | 100% |
| Explanation grounded (every number cited matches the inputs) | ≥ 95%, checked by a script plus an LLM grader |
| Spoken message ≤ 25 words | 100% |

- Evals run with one command and output a results table and a diff against the previous run.

### 8.3 Real-world feedback loop
- The `feedback` command records whether I went and how it was (1–5).
- Any morning where the agent's call disagreed with my rating can be promoted into a new eval fixture. The saved raw data makes that a one-step change.

## 9. Efficiency and best-practice requirements

- **Prompt caching:** the static prefix (system prompt, scoring rubric, tool definitions, spot config) is cached, and per-run data comes after it. This matters most within the agent's multi-turn tool loop and across eval runs, which share the prefix. Step 2 confirms the minimum cacheable length and TTL for the chosen model. Cache hit and miss tokens are logged on every run.
- **Fetch data deterministically, not through LLM tool calls:** all NOAA calls run in parallel in code, before the model is invoked. The model sees one compact, pre-trimmed summary (session window only, relevant fields only) instead of raw multi-KB JSON. This means fewer turns, fewer tokens and less to go wrong.
- **Structured output:** the model returns its decision in a strict JSON schema, validated before any action is taken.
- **Right-sized model:** step 2 picks the cheapest model that passes the eval gates, and evals compare the candidates.
- **Batch API for evals** where available (cheaper; latency doesn't matter there).
- **HTTP caching and retries:** responses are cached on disk for replay, with retries with backoff and a timeout for each source.
- **Targets per run:** latency < 60 s, LLM cost < $0.02.
- **Secrets** (Anthropic key, Voice Monkey token) come only from environment variables or a secret store.
- **NWS API etiquette:** send a descriptive `User-Agent` with contact info, as NWS requires.

## 10. Failure behavior

| Situation | Behavior |
|---|---|
| A single data source fails | Continue with the remaining sources, lower the confidence and say so in the explanation |
| All wave data is missing | NO-GO, no wake-up, error logged *(fail closed)* |
| Anthropic API is unavailable | Fall back to the pure deterministic score (no LLM adjustment), and log `llm_unavailable` |
| Alexa trigger fails | Retry twice, then log the error clearly. (A v2 option is a backup phone push.) |
| Invalid config | Fail fast at startup with a clear message |

## 11. Future (v2+)
- Scheduled run at 5:00 AM local (cloud routine, serverless timer or Task Scheduler; chosen in step 2 or later).
- Multi-spot "best spot today" mode.
- A preferred-tide model per spot.
- A morning-after auto prompt asking "did you go? how was it?" to grow the eval set.
- Weekly digest: how often it was right.

## 12. Decisions log (resolved 2026-10-30)
1. **Size:** the minimum is 3 ft face and there's **no max**. `max_ft` stays as an optional setting, off by default. The gale and lightning vetoes still protect against storm days.
2. **MAYBE days:** sleep in. Only GO wakes me.
3. **Session window:** dawn to 8:30 AM (before work), judged on the best 2-hour block or the whole window if it's shorter.
4. **Rain:** a penalty only. Lightning is still a hard veto.
5. **Broken data:** fail closed. Let me sleep and log the error.
6. **Wake-up style:** I design the Alexa routine myself. The agent fires the trigger and passes the spoken summary.

### Resolved in step 2
- The NOAA sources were verified live. See ARCHITECTURE.md §3 and §11.
- Voice Monkey v3 can't pass text with a routine trigger, so the wake-up is a trigger call followed by a separate announce call. See ARCHITECTURE.md §7.
