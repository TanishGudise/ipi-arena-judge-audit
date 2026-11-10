"""Offline re-score ($0): replay every recorded episode through run_one of a given ipi_arena_os checkout.

    .venv/bin/python -I -B runner/rescore.py --upstream PATH --tag full-20261109 --out FILE.jsonl

Real LLMClient objects are built; only their OpenAI client is swapped for a replayer that returns the
episode's recorded responses for that role (target / worldsim / judge), in order. Requests are compared
with the recorded ones (fidelity). A call the recording doesn't have raises NotRecorded, which run_one turns
into an episode error, so it shows up as "unscored" instead of being invented. Standalone on purpose: it
must not import runner/run.py, which puts the released checkout on sys.path.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from pathlib import Path

AUDIT = Path(__file__).resolve().parent.parent
ROLES = ("target", "worldsim", "judge")


class NotRecorded(RuntimeError):
    pass


class Obj:
    """Attribute view of a recorded response dict (missing keys read as None, like optional pydantic fields)."""

    def __init__(self, d):
        self._d = d

    def __getattr__(self, k):
        if k.startswith("__"):
            raise AttributeError(k)
        return wrap(self._d.get(k)) if isinstance(self._d, dict) else None

    def model_dump(self, *a, **kw):
        return self._d


def wrap(v):
    if isinstance(v, dict):
        return Obj(v)
    if isinstance(v, list):
        return [wrap(x) for x in v]
    return v


def canon(x):
    return json.dumps(x, sort_keys=True, ensure_ascii=False, default=str)


class Replayer:
    def __init__(self, role, recorded):
        self.role, self.recorded, self.i, self.seen = role, recorded, 0, []
        self.chat = Obj({})
        self.chat.completions = self  # client.chat.completions.create(...)

    def create(self, **kw):
        if self.i >= len(self.recorded):
            raise NotRecorded(f"NotRecorded: {self.role} call #{self.i + 1} has no recorded response")
        rec = self.recorded[self.i]
        self.i += 1
        self.seen.append((kw, rec))
        if rec.get("response") is None:  # recorded transport failure: re-raise its message
            raise RuntimeError(rec.get("error") or "recorded call failed")
        # Build the same object the OpenAI SDK builds from a response body (no validation).
        from openai._models import construct_type
        from openai.types.chat import ChatCompletion
        return construct_type(type_=ChatCompletion, value=rec["response"])

    def fidelity(self):
        """Compare each request with the recorded one; run at episode end (target lists are live references)."""
        out = []
        for n, (kw, rec) in enumerate(self.seen, 1):
            req = rec["request"]
            mine = {k: v for k, v in kw.items() if k != "tools"}
            if kw.get("tools"):
                mine["tools_sha256"] = hashlib.sha256(json.dumps(kw["tools"], sort_keys=True).encode()).hexdigest()
            diff = sorted(k for k in set(mine) | set(req) if canon(mine.get(k)) != canon(req.get(k)))
            if diff:
                out.append({"role": self.role, "call": n, "fields": diff})
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", required=True, help="ipi_arena_os checkout to score with")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()
    up = Path(a.upstream).resolve()
    sys.path.insert(0, str(up / "src"))
    import ipi_arena_bench
    assert Path(ipi_arena_bench.__file__).resolve().is_relative_to(up), ipi_arena_bench.__file__
    from ipi_arena_bench.behavior import load_behaviors
    from ipi_arena_bench.llm_client import LLMClient
    from ipi_arena_bench.runner import BenchmarkRunner

    behaviors = load_behaviors(up / "data")
    files = sorted((AUDIT / "runs" / a.tag / "episodes").glob("*.json.gz"))
    out = open(a.out, "w")
    n = 0
    for f in files:
        eid = f.name[: -len(".json.gz")]
        if a.only and eid not in a.only:
            continue
        d = json.load(gzip.open(f, "rt"))
        s = d["summary"]
        reps = {r: Replayer(r, [c for c in d["calls"] if c["role"] == r]) for r in ROLES}
        clients = {}
        for r in ROLES:
            c = LLMClient(provider="vllm", model=reps[r].recorded[0]["request"]["model"] if reps[r].recorded else f"replay/{r}",
                          base_url="http://127.0.0.1:9/v1")
            c.client = reps[r]
            clients[r] = c
        runner = BenchmarkRunner.__new__(BenchmarkRunner)  # same as __init__ but with the behaviors already loaded
        runner.target_client, runner.judge_client, runner.worldsim_client = clients["target"], clients["judge"], clients["worldsim"]
        runner.behaviors, runner.verbose, runner.max_steps = behaviors, False, 5
        res = runner.run_one(s["behavior_id"], d["upstream_result"]["attack_text"])
        rd = res.to_dict()
        fid = [x for r in ROLES for x in reps[r].fidelity()]
        tj = (rd.get("judge_raw") or {}).get("tool_judge") or {}
        row = {
            "episode_id": eid, "behavior_id": s["behavior_id"], "category": s["category"],
            "is_break": rd["is_break"], "error": rd["error"], "ended_by": rd.get("ended_by"),
            "criteria": {k: [v["score"], v["passed"]] for k, v in (rd["criteria"] or {}).items()},
            "tool_judge_reason": tj.get("reason"), "steps": rd["steps"],
            "consumed": {r: reps[r].i for r in ROLES}, "recorded": {r: len(reps[r].recorded) for r in ROLES},
            "fidelity_mismatches": fid,
            "needs_unrecorded_call": bool(rd["error"] and "NotRecorded" in rd["error"]),
        }
        out.write(json.dumps(row) + "\n")
        n += 1
    out.close()
    print(f"re-scored {n} episodes with {up} -> {a.out}")


if __name__ == "__main__":
    main()
