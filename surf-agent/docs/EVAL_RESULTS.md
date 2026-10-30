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

## Model choice (open for Rob)
Both models pass every gate. Sonnet 5.5 is **less than half the cost** and **about 40% faster**, with identical decisions on this suite. The config still defaults to Opus 5.5, as decided in ARCHITECTURE §12. To switch, change `llm.model` in `config/settings.yaml`.
