"""Print one or more episodes compactly for hand-checking (tool calls, final message, judge, check).

    .venv/bin/python -I -B runner/show.py --tag full-20261109 EPISODE_ID [EPISODE_ID ...]
Tool results (which carry the injected attack text) are not printed unless --tool-results is given.
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

AUDIT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--tool-results", action="store_true")
    ap.add_argument("--width", type=int, default=700)
    ap.add_argument("ids", nargs="+")
    a = ap.parse_args()
    W = a.width
    for eid in a.ids:
        d = json.load(gzip.open(AUDIT / "runs" / a.tag / "episodes" / f"{eid}.json.gz", "rt"))
        s = d["summary"]
        print("#" * 100)
        print(f"{eid} | steps {s['steps']} | error {s['error']} | compare {s['compare']}")
        print(f"check: {s['check']['action']} - {s['check']['reason'][:300]} {s['check']['variants']}")
        print(f"released: break={s['released']['is_break']} tool_criterion={s['released']['tool_criterion']!r} "
              f"passed={s['released']['tool_criterion_passed']} criteria={s['released']['criteria']}")
        for c in d["calls"]:
            resp = c["response"] or {}
            msg = (resp.get("choices") or [{}])[0].get("message") or {}
            if c["role"] == "target":
                print(f"  [target {resp.get('provider')}] " + ((msg.get("content") or "")[:W].replace("\n", " | ")))
                for t in msg.get("tool_calls") or []:
                    print(f"     CALL {t['function']['name']} {t['function']['arguments'][:W]}")
            elif c["role"] == "worldsim" and a.tool_results:
                print(f"  [worldsim] {(msg.get('content') or '')[:W]}".replace("\n", " "))
            elif c["role"] == "judge":
                print(f"  [judge] {(msg.get('content') or '')[:W * 2]}".replace("\n", " "))
            if c.get("error"):
                print(f"  [{c['role']} ERROR] {c['error'][:200]}")
        tj = d["upstream_result"]["judge_raw"].get("tool_judge")
        if tj:
            print(f"  tool_judge: {tj['reason']}")


if __name__ == "__main__":
    main()
