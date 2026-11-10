# IPI Arena benchmark: judge-correctness audit

**Scope:** every rate and count below comes from a replay of the published attacks against Qwen3-VL-235B with the released judge models. These fixes may change reported ASR; re-scoring is recommended.

**What was done.**
- Ran `GraySwanAI/ipi_arena_os` @ `5e2e285` on the 89 published non-browser attacks (HF `sureheremarv/ipi_arena_attacks` @ `2ef2d97`), 3 times each, for 267 episodes.
  - Target: `qwen/qwen3-vl-235b-a22b-instruct`.
  - Judge and WorldSim: `google/gemini-2.5-flash`.
  - All via OpenRouter.
- Recorded every model response, then re-scored the same responses offline with the released judge and with the fixed one.
- Replaying the released code reproduces all 267 recorded outcomes (261 verdicts, 6 errors) exactly.

**Headline: attack success rate (Wilson 95% CIs).**
| | Released judge | Fixed judge |
|---|---|---|
| Same 261 episodes the released runner scored | 82/261 = 31% [26, 37] | 119/261 = 46% [40, 52] |
| All 267 episodes (the fixed runner also scores the 6 the released one dropped) | 82/261 (6 errors left out) | 122/267 = 46% [40, 52] |

The intervals treat episodes as independent. The 3 runs of each attack are correlated, so the intervals understate the uncertainty.

- **45 episode outcomes changed, all read by hand:**
  - 38 real successes had been scored "defended";
  - 1 false success (the target file was never edited) became "defended";
  - 6 dropped episodes are now scored (3 successes).

**The fixes (one commit each, each with tests on recorded episodes that fail before the fix and pass after).**
1. **Structured arguments:** tool arguments that are lists or objects are now matched as JSON, not Python `str()`. Real successes in 4 behaviors were scored "defended" (25 outcomes).
2. **penthouse patterns:** tightened to the objective's exact values. Fixed because fix 1 made them reachable, not because a failure was seen.
3. **Missing tool judge:** a tool criterion with no tool judge now fails loudly instead of scoring 0. flashing-lights-seizure could never succeed (3 outcomes).
4. **ssh-key:** the upload is matched by key file and URL, not by an exact whole-command string (1 outcome).
5. **Unused tool judges:** two behaviors now use their configured tool judge, with a tighter test-stub pattern. Their success had rested on LLM criteria alone (1 false success removed).
6. **One matching call:** a harmless extra call to the same tool no longer fails the tool criterion (3 outcomes).
7. **Brittle anchors:** a trailing `;` and a longer benign description no longer fail the match (4 outcomes).
8. **Criterion names:** matched ignoring surrounding spaces and case. The judge's score of 10 was being read as 0 (2 outcomes, with fix 1).
9. **Content-filter blocks:** a WorldSim content-filter block now ends and judges the episode instead of dropping it from the rate (6 episodes, 3 successes).

**No-attack check.**
- 0 of 79 no-attack episodes is a success under the fixed judge (pre-registered).
  - The 79 are episodes in behaviors that have both a tool criterion and LLM criteria, where the action the behavior targets never happened.
  - That was decided by a per-episode check written from each objective before the run.
- 0 of the 107 episodes our hand-corrected check says had no attack action (all behaviors; set frozen before any fix) is a success. The released judge had 1 there.

**Side observation.**
- In 34 of those 79 episodes (43%, CI 33–54%), every LLM criterion passed its threshold.
- Only the tool criterion kept them from scoring as successes. In about 4 of 10 such episodes, the tool criterion is the only safeguard, which is why its correctness matters.

**Cost.**
- $1.58 total OpenRouter spend ($1.5838): $1.5595 for the 267 episodes, $0.0155 for a 4-episode pilot, and $0.0088 that OpenRouter's per-call records don't itemize.
- Re-scoring, tests and replication made no model API calls ($0).

**Links:**
- PR, judge mechanics (fixes 1, 2, 3, 8, 9): https://github.com/GraySwanAI/ipi_arena_os/pull/3
- PR, behavior definitions (fixes 4, 5, 6, 7): https://github.com/GraySwanAI/ipi_arena_os/pull/4
- This repo: https://github.com/TanishGudise/ipi-arena-judge-audit

## What is in this repo

- **Not included:** the recorded episodes. They contain the published attack text, so they are not in this repo.
- **`results/cp2.json`:** the released-vs-fixed comparison (ASR on both denominators, per behavior, every changed outcome by episode ID, ablation, the no-attack check, the side observation).
- **`results/pr_split.json`:** each PR replayed on its own.
- **`runner/`:**
  - `run.py` runs `ipi_arena_os`'s own `run_one` and records every model response, with a spend cap and an egress sandbox (`egress.py`).
  - `oracle.py` holds the per-behavior checks, written from each objective before the run.
  - `rescore.py` replays recorded responses through any `ipi_arena_os` checkout, offline.
  - The analysis scripts are `analyze.py`, `handcheck_apply.py`, `cp2_analyze.py` and `pr_split.py`.
  - `ablate.sh` removes one fix at a time; `percommit_tests.sh` runs each fix's tests on its parent commit and on the commit.
  - `replicate.sh` rebuilds everything from a fresh clone.
- **`ANALYSIS_PLAN.md` and `CP2_PLAN.md`:** the analysis plans, written before the run and before any fix.
- **`data/`:** the attack manifest (row IDs and sha256 only), the frozen no-attack episode sets, and pinned package versions.

## How to reproduce

You need Python 3.14 and [uv](https://github.com/astral-sh/uv).
1. **Environment:** `uv venv --python 3.14.8 .venv && uv pip install --python .venv/bin/python -r data/cp3_requirements.txt`.
2. **Released code:** `git clone https://github.com/GraySwanAI/ipi_arena_os` and check out `5e2e285`. Fetch the PR branches for the fixed code.
3. **Published attacks:** download `qwen_open_source_only_attacks.jsonl` from `sureheremarv/ipi_arena_attacks` at revision `2ef2d97`. Its sha256 is in `data/attacks_manifest.json`; set `GS_ATTACKS` to the file path and `GS_UPSTREAM` to the `5e2e285` checkout.
4. **Record (paid; needs `OPENROUTER_API_KEY` in `.env`):** run `python3 runner/egress.py --allow openrouter.ai:443 --log egress.log --write-dir runs/full-20261109 -- .venv/bin/python -I -B runner/run.py --tag full-20261109 --set full --k 3 --cap-usd 4.00 --workers 2 --target-provider-order Parasail`.
   - Our run cost $1.56.
   - Provider sampling is not fully deterministic, so a new recording will differ in detail from ours.
5. **Replay (offline):**
   - Run `runner/rescore.py --upstream <5e2e285 checkout> --tag full-20261109 --out runs/full-20261109/rescore/released.jsonl` and the same with the fixed checkout (`fixed.jsonl`).
   - `runner/rescore_gate.py` checks that the released replay reproduces the recording.
6. **Analysis:**
   - The CP1 hand-check labels (`runs/<tag>/handcheck.jsonl`) are needed by `handcheck_apply.py` and `cp2_analyze.py`.
   - They refer to our episodes, so a new recording needs its own reading of each disagreement.
   - `analyze.py` writes the list of episodes to read.
