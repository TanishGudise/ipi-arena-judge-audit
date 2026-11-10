"""Gate for the offline re-score: the released-code replay must reproduce every recorded result.

    .venv/bin/python -I -B runner/rescore_gate.py --tag full-20261109 --replay runs/full-20261109/rescore/released.jsonl
"""

import argparse
import gzip
import json
from pathlib import Path

AUDIT = Path(__file__).resolve().parent.parent

ap = argparse.ArgumentParser()
ap.add_argument("--tag", required=True)
ap.add_argument("--replay", required=True)
a = ap.parse_args()
rows = {json.loads(l)["episode_id"]: json.loads(l) for l in open(a.replay) if l.strip()}
bad, fid = [], []
for f in sorted((AUDIT / "runs" / a.tag / "episodes").glob("*.json.gz")):
    d = json.load(gzip.open(f, "rt"))
    u, eid = d["upstream_result"], d["summary"]["episode_id"]
    r = rows[eid]
    rc = {k: [v["score"], v["passed"]] for k, v in (u["criteria"] or {}).items()}
    if r["is_break"] != u["is_break"] or r["error"] != u["error"] or r["criteria"] != rc:
        bad.append(eid)
    if r["fidelity_mismatches"] or r["consumed"] != r["recorded"]:
        fid.append(eid)
print(f"episodes {len(rows)}; verdict/criteria/error mismatches {len(bad)} {bad[:5]}; "
      f"request or consumption mismatches {len(fid)} {fid[:5]}")
raise SystemExit(1 if bad or fid or len(rows) != 267 else 0)
