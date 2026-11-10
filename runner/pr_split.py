"""Per-PR replay summary for the two-PR option: flips and ASR of each PR branch replayed alone.

    .venv/bin/python -I -B runner/pr_split.py --tag full-20261109
Reads runs/<tag>/rescore/{released,fixed,pr_judge-mechanics,pr_behavior-definitions}.jsonl; writes pr_split.json.
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import analyze as A  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--tag", required=True)
a = ap.parse_args()
d = A.R.AUDIT / "runs" / a.tag / "rescore"
L = lambda f: {json.loads(l)["episode_id"]: json.loads(l) for l in open(d / f) if l.strip()}
rel, fix = L("released.jsonl"), L("fixed.jsonl")
o = lambda r: "error" if r["error"] else ("success" if r["is_break"] else "no")
s261 = sorted(e for e in rel if not rel[e]["error"])
out = {}
for n in ("judge-mechanics", "behavior-definitions"):
    p = L(f"pr_{n}.jsonl")
    fl = sorted(e for e in rel if o(rel[e]) != o(p[e]))
    out[n] = {"flips": len(fl),
              "by": {k: sum(1 for e in fl if f"{o(rel[e])}->{o(p[e])}" == k) for k in ("no->success", "success->no", "error->success", "error->no")},
              "asr_upstream_style": A.rate(sum(1 for r in p.values() if r["is_break"] and not r["error"]), sum(1 for r in p.values() if not r["error"])),
              "asr_all_267": A.rate(sum(1 for r in p.values() if r["is_break"] and not r["error"]), len(p)),
              "asr_same_261": A.rate(sum(1 for e in s261 if p[e]["is_break"]), len(s261)),
              "request_mismatches": sum(1 for r in p.values() if r["fidelity_mismatches"] or r["consumed"] != r["recorded"]),
              "errors": sum(1 for r in p.values() if r["error"]), "flip_ids": fl}
full = sorted(e for e in rel if o(rel[e]) != o(fix[e]))
a1, a2 = set(out["judge-mechanics"]["flip_ids"]), set(out["behavior-definitions"]["flip_ids"])
out["union_equals_full_flips"] = sorted(a1 | a2) == full
out["disjoint"] = not (a1 & a2)
(d / "pr_split.json").write_text(json.dumps(out, indent=1))
for n in ("judge-mechanics", "behavior-definitions"):
    v = out[n]
    print(n, v["flips"], v["by"], A.fmt(v["asr_upstream_style"]), A.fmt(v["asr_all_267"]), A.fmt(v["asr_same_261"]), "mismatches", v["request_mismatches"])
print("union == full flips:", out["union_equals_full_flips"], "disjoint:", out["disjoint"])
