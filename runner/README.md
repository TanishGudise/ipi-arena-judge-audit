# CP1 runner

Runs the released `ipi_arena_os` (`~/gray-swan-recon/ipi_arena_os` @ `5e2e285`, unchanged) on the pinned HF attacks and compares the released judge with a per-behavior check written from each objective.

| File | What it does |
|---|---|
| `run.py` | Wraps `BenchmarkRunner.run_one`. Records every target / WorldSim / judge request and response, meters spend, runs the per-behavior check, writes `runs/<tag>/results.jsonl` (one summary per episode) and `runs/<tag>/episodes/<id>.json.gz` (full record). Resumable by tag. |
| `oracle.py` | The per-behavior checks and the pre-registered rule they follow (module docstring). Written 2026-11-09 before any model output. |
| `meter.py` | Worst-case reservation before every call, settle with OpenRouter's `usage.cost`, ledger per tag; `CapReached` stops the run. |
| `egress.py` | Runs the runner under `sandbox-exec` with egress only through a loopback CONNECT proxy that allows `openrouter.ai:443`, and file writes only in the run directory. |
| `report.py` | Per-episode table (released judge vs check), tokens, cost, projection for the full run. |

Inputs: attacks from `~/gray-swan-recon/hf/pinned/attacks.jsonl` (HF `sureheremarv/ipi_arena_attacks` @ `2ef2d97`, sha256 checked against `data/attacks_manifest.json`; the attack text is not copied into this repo). Browser rows (6, all `salesforce-unauthorized-admin-creation`) are excluded and listed in each run's meta.

## Commands

```bash
python3 -I -B tests/test_oracle.py && python3 -I -B tests/test_meter.py   # $0
python3 -I -B runner/run.py --tag dry --set full --k 3 --dry-run           # $0, no key, no network
.venv/bin/python -I -B tests/test_e2e_fake.py                              # $0, local fake OpenRouter
```

Pilot (paid, cap $0.40; key read from `.env` by name, never printed):

```bash
T=pilot-20261109; caffeinate -i python3 -I -B runner/egress.py --allow openrouter.ai:443 \
  --log runs/$T/egress.log --write-dir runs/$T -- \
  .venv/bin/python -I -B runner/run.py --tag $T --set pilot --cap-usd 0.40
python3 -I -B runner/report.py --tag $T
```
