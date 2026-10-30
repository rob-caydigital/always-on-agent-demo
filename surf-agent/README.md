# Surf Check Agent

Decides at 5 AM whether it's worth getting up to surf Juno Beach Pier, and if so, wakes you through an Alexa routine.

- Spec: [docs/SPEC.md](docs/SPEC.md)
- Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

```
NOAA (parallel fetch) → conditions snapshot → deterministic score + vetoes
   → one Claude call (structured output, ±1 adjustment) → decision in code → Voice Monkey → Alexa
```

## Setup

```bash
cd surf-agent
uv sync
cp .env.example .env    # then fill in what you have
```

- **Claude:** works with `ant auth login` or `ANTHROPIC_API_KEY`.
- **Shark check:** no setup. Each run reads OCEARCH's public tracker (unofficial Mapotic endpoints, cached 30 min) and vetoes the wake-up when a tagged shark rated at or above the threat threshold pinged within 10 miles in the last 24 hours. Tune it in `config/sharks.yaml`; see [docs/SPEC-shark-threat.md](docs/SPEC-shark-threat.md). "No tagged shark reported" is never a safety signal.
- **Voice Monkey:** optional. Without `VOICEMONKEY_TOKEN`, the wake-up runs in **simulated** mode. It records the exact `/trigger` and `/announce` calls it would send and treats them as sent. The steps for setting up the Alexa routine are in ARCHITECTURE §7.

## Use

```bash
uv run surf-check --dry-run            # live NOAA data + Claude, never wakes anyone
uv run surf-check                      # the real thing: wakes you on GO (simulated without a token)
uv run surf-check --no-llm --dry-run   # deterministic score only (free)
uv run surf-check notify-test          # fire the Alexa routine once

# Replay saved data (offline; replays never wake unless --force-wake)
uv run surf-check --replay tests/fixtures/raw_2026-10-30             # real capture: blown-out NO_GO
uv run surf-check --replay tests/fixtures/raw_demo_go_day --force-wake   # demo GO + simulated wake
uv run surf-check --replay data/runs/<run-id>                        # any past run

# Visual report (every run writes data/runs/<run-id>/report.html)
uv run surf-check report --open        # rebuild and open the latest run's report
uv run surf-check report <run-id>      # rebuild one run's report from its saved data

# Feedback loop
uv run surf-check feedback --date 2026-10-31 --went --rating 4 --note "clean, chest high"
uv run surf-check promote <run-id> --expected NO_GO   # turn a real morning into an eval scenario
```

The session is the next morning from dawn to 08:30. A run before 08:30 checks today; a run after that previews tomorrow.

## Tests and evals

```bash
uv run pytest                          # L1 unit + L2 pipeline/guardrail tests (no network, no LLM)
uv run surf-check eval --no-llm        # deterministic baseline on all scenarios (free)
uv run surf-check eval                 # L3: real Claude calls (asks to confirm the estimated cost)
uv run surf-check eval --model claude-opus-5-5 --model claude-sonnet-5-5   # side-by-side
uv run surf-check eval --batch         # Message Batches API, 50% cost
uv run surf-check eval --judge         # + Opus 5.5 judge grading explanation quality (report only, ~$1)
uv run pytest -m live tests/test_judge.py   # judge audit: must fail bad outputs, pass a correct one (~$0.30)
```

- **Scenarios** live in `evals/scenarios/`. `synthetic/` holds 34 hand-labeled cases (see `LABELING_NOTES.md`). `real/` holds captured NOAA data plus a label.
- **Reports** go to `evals/results/` as JSON and Markdown, with a change column against the previous run of the same model.
- **Gates** (SPEC §8.2):

| Gate | Bar |
|---|---|
| Veto recall | 100% |
| Decision accuracy | ≥ 90% |
| False-wake rate | ≤ 5% |
| Adjustment within ±1 and justified | 100% |
| Explanation numbers grounded in the data | ≥ 95% |
| Spoken message ≤ 25 words | 100% |

## Efficiency notes

- **Workflow:** a fixed workflow, not a tool loop. One Claude call per run on **Sonnet 5.5** at low effort: about 4–5K input tokens, roughly $0.01 and 3–5 s. (Opus 5.5 also passes every gate, at about $0.02–0.03 and 5–8 s.)
- **Caching:** the static prompt prefix is about 2.7K tokens and byte-stable: no dates, and keys sorted. Caching is **off** for the single daily call, because nothing would read it within the 5-minute TTL. It's **on** for evals, where 35 calls share the prefix.
- **Fetching:** sources are fetched in parallel, cached on disk per `Cache-Control`, and retried with backoff. A failed source is reported, never fatal.
- **Guardrails** live in code (`decide.py`):
  - Vetoes always win.
  - Adjustments are clamped to ±1, and one without a reason is dropped.
  - Bad spoken text is replaced by a template.
  - A Claude refusal or error falls back to the deterministic decision.
