"""Write a trimmed, verbatim fixture from a recorded CP1 episode for an upstream test.

    .venv/bin/python -I -B runner/make_fixture.py --tag full-20261109 --out DIR EPISODE_ID [--judge-reply] [--worldsim-error]

Keeps only what the tests read: the model's tool calls (name, arguments, id, in order), and on request the
judge's raw reply or the WorldSim error body. No user message, tool result or attack text is copied.
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

AUDIT = Path(__file__).resolve().parent.parent
SOURCE = ("Recorded with ipi_arena_os 5e2e285 run_one: target qwen/qwen3-vl-235b-a22b-instruct, "
          "WorldSim and judge google/gemini-2.5-flash, via OpenRouter, 2026-11-09; attack = published HF row")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--judge-reply", action="store_true")
    ap.add_argument("--worldsim-error", action="store_true")
    ap.add_argument("--target-responses", action="store_true", help="include the target's raw responses (tool calls + content)")
    ap.add_argument("ids", nargs="+")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for eid in a.ids:
        d = json.load(gzip.open(AUDIT / "runs" / a.tag / "episodes" / f"{eid}.json.gz", "rt"))
        s = d["summary"]
        calls = []
        for c in d["calls"]:
            if c["role"] != "target" or not c["response"]:
                continue
            msg = (c["response"].get("choices") or [{}])[0].get("message") or {}
            for t in msg.get("tool_calls") or []:
                calls.append({"name": t["function"]["name"], "arguments": json.loads(t["function"]["arguments"] or "{}"),
                              "id": t.get("id") or ""})
        fx = {"episode_id": eid, "behavior_id": s["behavior_id"], "hf_row": s["row"], "rep": s["rep"], "source": SOURCE,
              "released_verdict": {"is_break": d["upstream_result"]["is_break"], "error": d["upstream_result"]["error"]},
              "tool_calls": calls}
        if a.judge_reply:
            fx["judge_reply"] = next(((c["response"]["choices"][0]["message"]["content"]) for c in d["calls"]
                                      if c["role"] == "judge" and c["response"] and c["response"].get("choices")), None)
        if a.worldsim_error:
            fx["worldsim_error_response"] = next((c["response"] for c in d["calls"]
                                                  if c["role"] == "worldsim" and c["response"] and c["response"].get("error")), None)
        if a.target_responses:
            fx["target_responses"] = [{"content": (c["response"]["choices"][0]["message"].get("content")),
                                       "tool_calls": c["response"]["choices"][0]["message"].get("tool_calls")}
                                      for c in d["calls"] if c["role"] == "target" and c["response"]]
        name = f"{eid}.json"
        (out / name).write_text(json.dumps(fx, indent=1, ensure_ascii=False) + "\n")
        print(out / name)


if __name__ == "__main__":
    main()
