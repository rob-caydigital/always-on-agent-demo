# Eval results — 2026-10-30

35 scenarios: 34 hand-labeled synthetic cases and 1 real NOAA capture. Sync mode with prompt caching. Prompt version `system.md` as of this commit, scoring `2026-10-30.1`, effort `low`. Full per-scenario reports are written to `evals/results/` (gitignored).

## Final run (after the spoken-message prompt fix)

| Metric | Bar | Rules only (no LLM) | Opus 5.5 (low) | Sonnet 5.5 (low) |
|---|---|---|---|---|
| Veto recall | 100% | 100% | **100%** | **100%** |
| Decision accuracy | ≥ 90% | 97% | **100%** | **100%** |
| Exact match to label | — | 97% | 100% | 100% |
| False-wake rate | ≤ 5% | 0% | **0%** | **0%** |
| Adjustment within ±1 and justified | 100% | — | **100%** | **100%** |
| Numbers grounded in the data | ≥ 95% | — | **100%** | **100%** |
| Spoken message ≤ 25 words | 100% | — | **100%** | **100%** |
| Eval cost (35 calls, cached) | — | $0 | $0.49 | $0.22 |
| Mean latency | — | — | 4.9 s | 2.8 s |
| Output tokens per call | — | — | ~425 | ~321 |
| Cache hit rate (share of input tokens) | — | — | 73% | 73% |

**Estimated production cost per daily run** (single call, no cache): about $0.02–0.03 on Opus 5.5 and about $0.01 on Sonnet 5.5.

## What the LLM adds over the rules-only baseline
The rules-only baseline misses `forecast-overreads-buoy-small`: the base score is 6.75, which would be a GO and a false wake. Both models read the buoy (1.2 ft at 5 s against a 3.5 ft at 9 s forecast) and the "subsiding" marine text, then applied −1.0, which brings it to MAYBE. On `buoy-shows-swell-arriving-early` both models nudged the score up, to GO, citing the buoy's 12 s reading. Every adjustment reason cites a specific value, source and time.

## First run and the fix
| | Opus 5.5 | Sonnet 5.5 |
|---|---|---|
| Spoken ≤ 25 words, first run | 71% ❌ | 54% ❌ |
| Spoken ≤ 25 words, after fix | 100% ✅ | 100% ✅ |

The over-length messages ran 26–29 words: spot, size, period, wind, a reason and "out of 10." The `decide.py` guardrail already replaced them with the template, so production was never at risk, but the gate caught the miss. **The fix:** a 12–20 word target, a fixed message shape and three short examples in `prompts/system.md`.

The first run also flagged one "ungrounded" number for Sonnet. It wrote "rain trims the score by 20%", which is correct for a 0.8 factor. The grader now recognizes percentages derived from factors.

## Model choice
Both models pass every gate. Sonnet 5.5 is **less than half the cost** and **about 40% faster**, with identical decisions on this suite. **Decision (2026-10-30): production runs on Sonnet 5.5.** Opus 5.5 stays available with `--model claude-opus-5-5` or by changing `llm.model` in `config/settings.yaml`.

## LLM judge — first full run (2026-10-30)

Opus 5.5 at medium effort graded Sonnet 5.5's output on all 35 scenarios. This is **report only**: the judge isn't a gate yet. Every code gate still passed on this run: 100% decisions, veto recall, adjustments, grounding and spoken length; 0% false wakes.

| Criterion | Pass rate |
|---|---|
| deciding_factor_first | 83% |
| faithful | 100% |
| consistent_with_decision | 100% |
| adjustment_sound | 100% |
| spoken_useful | 94% |
| data_honesty | 97% |
| **Overall** | **96%** |

- **Judge cost:** $0.98 for 35 calls, about 10 s each, run four at a time, with the judge prompt cached. That is about 1,800 uncached and 2,900 cached input tokens, plus about 1,000 output tokens, per call. Sonnet's outputs cost $0.20 on top.
- **Reliability:** 0 judge errors and 0 reason/verdict mismatches.

### What the judge found (none of it changes a decision)
- **6 of 8 failures share one habit.** On borderline and MAYBE days, the explanation opens with score mechanics, such as "Base score 5.5 sits between the 4.5 and 6.0 thresholds", instead of the condition that limited the score. That condition is usually a short 6–7 s period, cross-shore wind, or size right at the 3 ft minimum. Two adjustment cases also lead with the base score before the evidence that moved it.
- **Spoken message:** one says "Gale veto", which is internal jargon. Another, "Buoy reads much smaller. Score 6.", never says whether to get up.
- **Data honesty:** one case reports "high" confidence with the wind observation missing and unmentioned.

### How the judge was validated
1. **Audit:** the judge fails an empty answer, "I don't know", a confidently wrong answer and a prompt-injection attempt, and passes a hand-written correct answer.
2. **First pilot:** this found two judge bugs. The judge couldn't see the spot profile, so it flagged correct claims about the buoy as invented. It also didn't know the agent may lean slightly negative on missing data. Both are fixed. The pilot also caught a reason that said "pass" next to a FAIL verdict, so reasons now end with an explicit verdict and code checks that they match.
3. **A real product fix from the audit:** vetoed mornings used to keep their base score ("Score 9.9, no go"). They now score 0, matching SPEC §6.6.
4. **Second pilot:** Rob reviewed 5 graded examples and agreed with all 5.

### Prompt iteration driven by the judge
I made three changes to `prompts/system.md`:
1. The explanation's first sentence must name the condition that drove or limited the score, never the score or the thresholds.
2. The spoken message gets a plain verdict phrase and never uses internal labels such as "veto".
3. A missing buoy or wind observation must be listed in `data_concerns`, and confidence can't be "high" without both.

The first rerun brought in a regression: the new "Marginal, your call" example phrase leaked onto two GO mornings. A fourth rule fixed it. Sonnet now picks the verdict phrase from base score + adjustment, and "your call" is only for the MAYBE band. The same pass fixed a grader bug that read observation times such as "04:50" as an ungrounded "4".

| Criterion | Before | Rerun 1 | Rerun 2 (final) |
|---|---|---|---|
| deciding_factor_first | 83% | 100% | **100%** |
| faithful | 100% | 97% | **97%** |
| consistent_with_decision | 100% | 97% | **100%** |
| adjustment_sound | 100% | 100% | **100%** |
| spoken_useful | 94% | 94% | **97%** |
| data_honesty | 97% | 100% | **97%** |
| **Overall** | **96%** | **98%** | **99%** |
| Code gates (all) | pass | pass | **pass**, grounded 100% |

**Remaining failures:** three single-case slips, none repeated across runs and none affecting a decision:
- One explanation calls an 80° swell "80 degrees off" the 73° beach, when it's 7° off.
- One GO morning still says "your call".
- One data concern misreads 5.9 ft as bigger than 7 ft.

At n = 35, one case is about 3 points, so the movement on the 97–100% criteria is within run-to-run noise. The deciding_factor_first gain, from 83% to 100%, is the real effect.

**Final run cost:** $0.22 for Sonnet and $0.98 for the judge. The judge loop as a whole (audits, two pilots and three full runs) cost about $4.
