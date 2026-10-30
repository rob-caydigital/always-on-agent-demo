# Surf Check Agent — Shark Threat Veto (Spec addendum)

| | |
|---|---|
| **Status** | v0.2, open questions resolved 2026-10-30 |
| **Extends** | [SPEC.md](SPEC.md) v0.1 (adds a veto to §6.1, a source to §5, failure rows to §10) |
| **Requested by** | Cornelius |
| **Date** | 2026-10-30 |
| **Next steps** | Step 2: architecture changes · Step 3: build and test |

---

## 1. Goal

Before the agent wakes me, it checks whether any **tagged shark has pinged within 10 miles of the spot** recently. Each nearby shark gets a **threat rating** based on species, life stage, distance and how recent the ping is. A great white counts for much more than a whitetip. If the threat is high enough, the decision is **NO-GO and the Alexa routine does not fire**, no matter how good the waves are.

**One-line success test:** I'm never woken up to surf when a great white, tiger or bull shark pinged near the pier in the last 24 hours, and a single old ping from a low-threat species doesn't cost me a good morning.

### What this is not

OCEARCH tags only report a position when the shark's dorsal fin breaks the surface, and only a few hundred sharks are tagged at all. **"No shark reported" means "no tagged shark surfaced nearby," not "no sharks."** The agent must never present the absence of pings as a safety signal (see §7).

## 2. Scope

### In scope
- One new data source: the OCEARCH tracker's backing endpoints on Mapotic (§4).
- A deterministic threat score per shark and a new hard veto, `shark` (§5, §6).
- Shark findings in the decision record, the explanation and the eval suite.
- Everything configurable in `config/sharks.yaml` (species weights, radius, lookback, threshold).

### Out of scope
- Any guarantee of safety. This is an extra veto on top of the existing ones, not a shark-safety feature.
- Untagged sightings (beach closures, lifeguard flags, social media, drone reports). Possible later; see §12.
- Non-shark animals in the same feed (turtles, seals, swordfish, alligators, dolphins). They're filtered out.
- Waking me with a "sharks nearby, stay home" message. A shark veto means silence, like every other veto.

## 3. User stories

1. **As a surfer**, if a high-threat shark pinged inside 10 miles in the lookback window, I'm not woken, and the decision record says which shark, how far, how long ago, and its threat rating.
2. **As a surfer**, a lone low-threat ping (say, an adult blue shark 5 miles out, 10 hours ago) is mentioned in the explanation but doesn't block a GO.
3. **As a surfer**, I can change the species weights, radius, lookback and threshold in `config/sharks.yaml` without touching code.
4. **As a surfer**, if the shark feed is down, I still get my normal decision, and the record says the shark check didn't run.
5. **As the builder**, the eval suite proves the shark veto has 100% recall and that the model can't override it.

## 4. Data source

There is no official, documented OCEARCH API. The ocearch.org tracker runs on **Mapotic**, and its backing endpoints are open JSON with no key. That's acceptable for a short-lived personal PoC.

| Endpoint | Returns | Cost |
|---|---|---|
| `GET https://www.mapotic.com/api/v1/maps/3413/pois.geojson/` | Every tagged animal as GeoJSON, each with its **latest** position | 1 request, ~430 KB |
| `GET https://www.mapotic.com/api/v1/maps/3413/pois/{id}/motion/` | One animal's full ping history (timestamp + coordinates) | 1 request per animal |

Neither endpoint filters by location or date, so the agent pulls everything and filters locally.

### Fields used (verified with one live request, 2026-10-30)

| Field | Example | Use |
|---|---|---|
| `geometry.coordinates` | `[-79.78037, 32.20996]` (lon, lat) | Latest position |
| `properties.id` | `288211` | Key for the motion endpoint |
| `properties.name` | `"Grey Lady"` | Explanation and record |
| `properties.category_name.en` | `"Sharks"` | Keep only `Sharks` |
| `properties.species` | `"White Shark (Carcharodon carcharias)"` | Species weight (§5.1) |
| `properties.stage_of_life` | `Adult`, `Sub-Adult`, `Juvenile`, `Young of the Year` | Life-stage factor (§5.2) |
| `properties.length` | `"12 ft 5 in."` (null for 4 sharks) | Record and explanation only |
| `properties.last_move_datetime` | `"2018-12-30T10:28:52Z"` (UTC) | Latest ping time; picks which sharks need their tracks pulled |

**Snapshot of the feed on 2026-10-30:** 470 animals, 399 of them sharks. Species: tiger 149, white 140, shortfin mako 29, blue 26, hammerhead (unspecified) 12, whale 10, blacktip 9, silky 6, bull 6, scalloped hammerhead 6, great hammerhead 5, dusky 1. **No oceanic whitetip is currently tagged in the feed**, but it's still in the weight table in case one appears. Only **1 shark (a white shark) pinged anywhere in the last 24 hours**, and 8 in the last 7 days. The nearest latest position to Juno is a bull shark 21 km away whose last ping was over 13 years ago. **Expect this veto to fire rarely.**

### Fetch rules
1. Fetch the GeoJSON **once per run**, in parallel with the NOAA sources. Cache it on disk for **30 minutes** (courtesy, and it keeps repeated runs and evals off their servers).
2. Keep features where `category_name.en == "Sharks"` and `last_move_datetime` falls inside the lookback window (§6).
3. For **each** of those sharks, fetch `/motion/` and use every ping inside the lookback window, not just the latest. A shark that passed the pier 12 hours ago and is now 40 miles away still counts. With the current feed that's about 1 extra request a run.
4. If more than `max_track_requests` (default 25) sharks qualify, pull tracks for the closest 25 by latest position and log `shark_tracks_truncated`.
5. Distances are great-circle (haversine) from the spot's `lat`/`lon` in SPEC §4. **"10 miles" means statute miles (16.09 km)**; config stores `radius_mi: 10`.
6. Send the same descriptive `User-Agent` as the NWS calls.
7. Save the raw GeoJSON and every motion response under `data/runs/<id>/raw/` so replays and promoted eval fixtures are exact.

> **Courtesy and risk:** these endpoints are unofficial and could change or vanish at any time. OCEARCH's data-sharing policy expects formal use to be coordinated with them. If this ever becomes more than a personal PoC, contact OCEARCH first.

## 5. Threat rating

Each shark gets one threat score from its **worst ping** inside the radius and the lookback window:

```
threat = species_weight × stage_factor × recency_factor × distance_factor     (0.0 – 1.0)
```

All four tables live in `config/sharks.yaml`. The starting values below are **heuristics**, ranked roughly by the species' record of unprovoked bites on people. They should be reviewed (§11) and tuned, not treated as science.

### 5.1 Species weight

| Tier | Weight | Species |
|---|---|---|
| High | **1.0** | White shark, tiger shark, bull shark |
| Medium | **0.5** | Shortfin mako, oceanic whitetip, great hammerhead, scalloped hammerhead, hammerhead (unspecified), blacktip |
| Low | **0.25** | Blue shark, silky shark, dusky shark, whitetip reef shark |
| None | **0.0** | Whale shark, basking shark (filter feeders) |
| Unknown species | **0.5** | Any shark whose `species` doesn't match the table; logged as `shark_species_unmapped` |

Matching uses the scientific name in parentheses (e.g. `Carcharodon carcharias`), falling back to the common name, so wording changes in the feed don't silently drop a great white to "unknown."

### 5.2 Life-stage factor

| `stage_of_life` | Factor |
|---|---|
| Adult | 1.0 |
| Sub-Adult | 0.8 |
| Juvenile | 0.6 |
| Young of the Year | 0.4 |
| missing | 1.0 (assume adult) |

### 5.3 Recency factor (age of the ping at run time)

| Ping age | Factor |
|---|---|
| ≤ 6 h | 1.0 |
| 6–12 h | 0.85 |
| 12–24 h | 0.7 |
| > 24 h (`lookback_hours`) | ignored |

### 5.4 Distance factor

| Distance from the spot | Factor |
|---|---|
| ≤ 3 mi | 1.0 |
| 3–6 mi | 0.8 |
| 6–10 mi | 0.6 |
| > 10 mi (`radius_mi`) | ignored |

Unlike SPEC §6.2, these buckets are **upper-inclusive** ("≤ 3 mi", "≤ 24 h"), because closer and newer should always win a tie. That includes the outer edges: a ping at exactly 10.0 mi or exactly 24 hours old counts.

### 5.5 Worked examples

| Shark | Species | Stage | Recency | Distance | **Threat** | Veto at 0.2? |
|---|---|---|---|---|---|---|
| Adult white shark, 2 mi, 6 h ago | 1.0 | 1.0 | 1.0 | 1.0 | **1.00** | Yes |
| Adult tiger shark, 9 mi, 20 h ago | 1.0 | 1.0 | 0.7 | 0.6 | **0.42** | Yes |
| Young-of-the-year bull shark, 8 mi, 18 h ago | 1.0 | 0.4 | 0.7 | 0.6 | **0.17** | No (mentioned) |
| Adult great hammerhead, 4 mi, 10 h ago | 0.5 | 1.0 | 0.85 | 0.8 | **0.34** | Yes |
| Juvenile blacktip, 9 mi, 20 h ago | 0.5 | 0.6 | 0.7 | 0.6 | **0.13** | No (mentioned) |
| Adult oceanic whitetip, 1 mi, 2 h ago | 0.5 | 1.0 | 1.0 | 1.0 | **0.50** | Yes |
| Adult blue shark, 5 mi, 10 h ago | 0.25 | 1.0 | 0.85 | 0.8 | **0.17** | No (mentioned) |
| Whale shark, 1 mi, 2 h ago | 0.0 | — | — | — | **0.00** | No (not mentioned) |
| Adult white shark, 10.5 mi, 2 h ago | — | — | — | — | outside radius | No |
| Adult white shark, 2 mi, 30 h ago | — | — | — | — | outside lookback | No |

These double as the first unit tests and eval cases (§9).

## 6. Decision logic

### 6.1 New hard veto

Added to SPEC §6.1:

| Veto | Default rule |
|---|---|
| **Shark** | Any shark with `threat > 0` and `threat ≥ veto_threshold` (**default 0.2**) within `radius_mi` (10) in the last `lookback_hours` (24) |

With the defaults, **any adult white, tiger or bull shark** that pinged inside 10 miles in the past 24 hours vetoes the morning (its lowest possible threat is 1.0 × 1.0 × 0.7 × 0.6 = 0.42). Lower-threat or older/farther pings only veto when they're close and recent.

**Decided 2026-10-30:** only sharks at or above the threshold block the wake (see §13). Sharks below it are reported, not vetoed. `veto_threshold: 0.0` would make any tagged shark inside 10 miles block the wake; it stays possible in config but isn't the default.

### 6.2 Where it sits
- It's a **window-level veto**, like `lightning` and `severe_warning`: it applies to the whole session, not to individual hours.
- Like every veto it's computed in code by the scorer and enforced in `decide.py`. **The LLM can't override it**, and the notifier can't fire on a vetoed run (ARCHITECTURE §5 guardrails 1 and 5 already cover this once `shark` is a veto code).
- Sub-threshold sharks **don't change the score**. They're reported in the record and may be mentioned in the explanation, nothing more. One threshold keeps the behavior easy to reason about.
- The LLM must **not** use shark reports as a reason for its ±1 adjustment. Sharks are handled only by the veto. This goes in the system prompt and is checked by an eval grader (§9).

## 7. Outputs

### 7.1 Decision record (SPEC §7.2) gains a `sharks` block
- `status`: `checked` | `unavailable` | `not_captured` (replay fixtures recorded before this feature)
- `feed_fetched_at`, `sharks_in_feed`, `sharks_in_lookback`, `tracks_fetched`
- For every shark inside the radius: `id`, `name`, `species`, `stage_of_life`, `length`, closest ping time and position, `distance_mi`, each factor, `threat`
- `max_threat` and whether the veto fired

### 7.2 Explanation and spoken message rules
- On a shark veto, the explanation names the deciding shark, for example *"No-go: an adult tiger shark (Lando) pinged 4.2 miles from the pier 5 hours ago, threat 0.80."* Every number must match the record (the existing grounding grader).
- Sub-threshold sharks may be mentioned in one short clause.
- **Never** say or imply the water is safe from sharks: no "no sharks," "shark-free," "all clear," or "safe." When nothing was found, either say nothing about sharks or say *"no tagged sharks reported nearby."*
- If the shark check didn't run, the explanation says so in one clause, e.g. *"Shark tracker unavailable."*
- The spoken wake-up message is only sent on a GO, so it never needs shark content. It must not contain any shark wording.

## 8. Failure behavior (added to SPEC §10)

| Situation | Behavior |
|---|---|
| Mapotic unreachable, times out, or returns non-JSON | `sharks.status = unavailable`, add `sharks` to `missing_sources`, **no shark veto**, decision proceeds on the other sources, explanation notes it. Controlled by `on_missing_data` (default `ignore`; `sleep` = fail closed). Decided 2026-10-30; see §13 |
| Feed schema changed (e.g. no `species`/`category_name`) | Treated as unavailable, plus a loud `shark_feed_schema_changed` log entry |
| GeoJSON cache older than 2 h and refresh fails | Treated as unavailable (stale data isn't used) |
| One `/motion/` request fails | Use that shark's latest position only; log `shark_track_failed:<id>` |
| A shark's species isn't in the weight table | Weight 0.5; log `shark_species_unmapped:<species>` |
| Replay fixture has no shark files | `sharks.status = not_captured`, no veto, **not** counted as missing. Existing eval scenarios keep passing unchanged |

## 9. Evaluation requirements

### 9.1 Unit tests (L1, no network, no LLM)
- Each factor table, including the boundaries: exactly 3, 6 and 10.0 mi, 10.01 mi, exactly 6 h, 12 h and 24 h, and 24 h plus one minute.
- Every row of §5.5 matches exactly.
- Species matching by scientific name, common name, and an unknown species.
- Haversine distance against two known reference points.
- Parsing of the captured GeoJSON and motion responses, including a null `length`, a null `last_move_datetime`, and non-shark categories.

### 9.2 Pipeline tests (L2, stubbed LLM)
- Shark veto + perfect waves + an assessment with adjustment +1 → NO_GO, notifier not called.
- Feed unavailable with `on_missing_data: ignore` → normal GO; with `sleep` → NO_GO.
- Explanation from a stubbed model saying "no sharks around" fails the wording check.

### 9.3 Agent eval scenarios (L3, added to `evals/scenarios/synthetic/`)

| Scenario | Expected |
|---|---|
| `shark-white-2mi-today-perfect-waves` | NO_GO, `must_include_veto: shark` |
| `shark-tiger-9mi-20h` | NO_GO (threat 0.42) |
| `shark-yoy-bull-8mi-18h-good-waves` | GO, explanation mentions the shark |
| `shark-blue-5mi-10h-good-waves` | GO |
| `shark-whale-shark-1mi-good-waves` | GO, no shark mention |
| `shark-white-10-5mi-today-good-waves` | GO (outside radius) |
| `shark-white-2mi-30h-ago-good-waves` | GO (outside lookback) |
| `shark-passed-pier-12h-ago-now-40mi` | NO_GO (track ping counts, latest position doesn't) |
| `shark-feed-unavailable-good-waves` | GO, explanation notes tracker unavailable |
| `shark-veto-plus-lightning` | NO_GO with both vetoes |

### 9.4 Pass bars (added to SPEC §8.2)

| Metric | Target |
|---|---|
| Shark-veto recall | **100%, hard gate** (folded into the existing safety-veto recall) |
| No "safe from sharks" wording in any explanation or spoken message | **100%** |
| No LLM adjustment citing sharks | **100%** |

## 10. Configuration

```yaml
# config/sharks.yaml
sharks:
  enabled: true
  radius_mi: 10
  lookback_hours: 24
  veto_threshold: 0.2          # 0.0 = any tagged shark inside the radius vetoes
  on_missing_data: ignore      # ignore | sleep
  max_track_requests: 25
  cache_minutes: 30
  stale_after_hours: 2
  species_weights:             # keyed by scientific name; common names are aliases
    Carcharodon carcharias: {weight: 1.0, aliases: [White Shark, Great White]}
    Galeocerdo cuvier:      {weight: 1.0, aliases: [Tiger Shark]}
    Carcharhinus leucas:    {weight: 1.0, aliases: [Bull Shark]}
    Isurus oxyrinchus:      {weight: 0.5, aliases: [Shortfin Mako Shark]}
    Carcharhinus longimanus: {weight: 0.5, aliases: [Oceanic Whitetip]}
    Sphyrna mokarran:       {weight: 0.5, aliases: [Great Hammerhead]}
    Sphyrna lewini:         {weight: 0.5, aliases: [Scalloped Hammerhead]}
    Sphyrnidae:             {weight: 0.5, aliases: [Hammerhead Shark]}
    Carcharhinus limbatus:  {weight: 0.5, aliases: [Blacktip Shark]}
    Prionace glauca:        {weight: 0.25, aliases: [Blue Shark]}
    Carcharhinus falciformis: {weight: 0.25, aliases: [Silky Shark]}
    Carcharhinus obscurus:  {weight: 0.25, aliases: [Dusky Shark]}
    Triaenodon obesus:      {weight: 0.25, aliases: [Whitetip Reef Shark]}
    Rhincodon typus:        {weight: 0.0, aliases: [Whale Shark]}
    Cetorhinus maximus:     {weight: 0.0, aliases: [Basking Shark]}
  unknown_species_weight: 0.5
  stage_factors: {Adult: 1.0, Sub-Adult: 0.8, Juvenile: 0.6, Young of the Year: 0.4}
  recency_factors:             # hours → factor, lower-inclusive; beyond lookback = ignored
    - {max_hours: 6, factor: 1.0}
    - {max_hours: 12, factor: 0.85}
    - {max_hours: 24, factor: 0.7}
  distance_factors:            # miles → factor; beyond radius = ignored
    - {max_mi: 3, factor: 1.0}
    - {max_mi: 6, factor: 0.8}
    - {max_mi: 10, factor: 0.6}
```

## 11. Open questions (need an answer before step 2)

1. ~~**Strictness.**~~ Resolved; see §13.
2. ~~**Species tiers.**~~ Resolved; see §13.
3. ~~**Feed down.**~~ Resolved; see §13.
4. ~~**Lookback.**~~ Resolved; see §13.
5. ~~**Miles.**~~ Resolved; see §13.

All open questions are resolved. Next: step 2 (architecture changes).

## 12. Future
- Other shark signals: Florida beach-closure feeds, lifeguard flag status, or local sighting reports.
- Score penalty for sub-threshold sharks, if the eval and feedback show it's wanted.
- A "sharks nearby" line in a morning-after digest even on NO-GO days.
- Contact OCEARCH about formal data access if this outlives the PoC.

## 13. Decisions log

1. **Strictness (2026-10-30):** only sharks whose threat is at or above `veto_threshold` (default 0.2) block the wake. Sharks below the threshold are recorded and may be mentioned in the explanation, but don't change the decision or the score.
2. **Feed down (2026-10-30):** when the shark feed is unavailable, stale or has changed shape, the agent still decides on the other sources and wakes me on a GO (`on_missing_data: ignore`). The explanation and record say the shark check didn't run. Unlike missing wave data, this fails open, because the feed is unofficial and an outage shouldn't cancel every good morning.
3. **Species tiers (2026-10-30):** approved as written in §5.1, including blacktip at medium (0.5). They remain config values in `config/sharks.yaml`, to be tuned from evals and feedback.
4. **Lookback (2026-10-30):** 24 hours (`lookback_hours: 24`). Only pings from the last day count. The recency factor now splits that day into ≤ 6 h (1.0), 6–12 h (0.85) and 12–24 h (0.7), and the feed counts as stale after 2 hours instead of 6. Trade-off accepted: pings are sparse, so this will catch fewer sharks than a longer window, but it won't keep vetoing after a shark has moved on.
5. **Miles (2026-10-30):** statute miles. The 10-mile radius is 16.09 km (`radius_mi: 10`), measured great-circle from the spot's lat/lon.
