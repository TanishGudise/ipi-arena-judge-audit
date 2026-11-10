"""Reconcile a run's ledger with OpenRouter's own per-generation cost records (free GETs).

Run under egress.py like run.py:
    python3 -I -B runner/egress.py --allow openrouter.ai:443 --log runs/T/egress.log --write-dir runs/T -- \
        .venv/bin/python -I -B runner/reconcile.py --tag T
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    a = ap.parse_args()
    rd = run.AUDIT / "runs" / a.tag
    key = run.load_key()
    ledger = [json.loads(l) for l in (rd / "ledger.jsonl").read_text().splitlines() if l.strip()]
    rows, mism = [], 0
    for x in ledger:
        if not x.get("gen_id"):
            rows.append({"gen_id": None, "ledger": x["cost_usd"], "openrouter": None, "note": (x.get("error") or "")[:60]})
            continue
        g = None
        for attempt in range(5):
            try:
                g = run.openrouter_get(f"generation?id={x['gen_id']}", key).get("data", {})
                break
            except Exception as e:  # generation stats can lag a few seconds
                g = {"_error": str(e)[:120]}
                time.sleep(2 * (attempt + 1))
        oc = g.get("total_cost") if isinstance(g, dict) else None
        same = oc is not None and abs(oc - x["cost_usd"]) < 1e-9
        mism += 0 if same else 1
        rows.append({"gen_id": x["gen_id"], "role": x.get("role"), "provider": g.get("provider_name"),
                     "ledger": x["cost_usd"], "openrouter": oc, "match": same, "err": g.get("_error")})
    status = run.key_status(key)
    out = {"calls": len(ledger), "ledger_total": sum(x["cost_usd"] for x in ledger),
           "openrouter_total": sum(r["openrouter"] or 0 for r in rows), "mismatches": mism,
           "key_now": status, "checked": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "rows": rows}
    (rd / "reconcile.json").write_text(json.dumps(out, indent=1))
    print(json.dumps({k: v for k, v in out.items() if k != "rows"}, indent=1))
    for r in rows:
        print(r)


if __name__ == "__main__":
    main()
