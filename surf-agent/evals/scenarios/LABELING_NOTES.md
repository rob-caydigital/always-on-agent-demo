# Labeling notes: synthetic scenarios

Each of the 34 scenarios in `synthetic/` was labeled by hand from SPEC §6 and surf sense, then checked against the deterministic pipeline (`score_snapshot` + `decide` with no LLM assessment), using scoring config `2026-10-30.1`.

## Where the label differs from the deterministic decision (on purpose)

| id | base score | deterministic | label (allowed) | why |
|---|---|---|---|---|
| forecast-overreads-buoy-small | 6.75 | GO | MAYBE (MAYBE) | The forecast says 3.5 ft @ 9 s, but buoy 41114 reads 1.2 ft @ 5 s and the CWF says seas are subsiding. That buoy normally over-reads Juno, so a small reading there is strong evidence. A -1.0 adjustment gives 5.75, which is MAYBE. This scenario tests whether the LLM adjusts down, and a GO is a false wake. |
| buoy-shows-swell-arriving-early | 5.50 | MAYBE | GO (GO, MAYBE) | The buoy already shows the 12 s E swell that the text forecast says is building. A +1.0 adjustment gives 6.5, which is GO. MAYBE is also allowed because the buoy can over-read for Juno. |

Both labels can be reached within the ±1.0 adjustment cap.

## Scorer observations (no labels changed, no bug that flips a decision)

1. **The flat veto counts swell trains outside the window** (`score_hour` takes `max_face` over all components). In `south-swell-outside-window`, a 5 ft @ 11 s S swell at 190° gives a 7 ft "face", so no flat veto fires, even though almost nothing from that swell reaches Juno. The direction factor of 0.2 still holds the score down to 2.2 (NO_GO), so this decision is right. A big enough out-of-window swell combined with a glassy day could still score into MAYBE. Consider computing `max_face` only from in-window components.
2. **The thunder-word check is a substring match** (`thunder`, `tstm`). Text such as "no tstms expected" or "thunderstorms ended" would still trigger the lightning veto. This errs on the safe side, and no scenario relies on it.
3. **The Small Craft Advisory penalty applies to any SCA**, even one issued only for Gulf Stream seas (`small-craft-advisory-offshore`). The penalty is small (×0.9) and the decision is still GO.
