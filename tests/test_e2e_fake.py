"""$0 end-to-end test: runner/run.py against tests/fake_openrouter.py (needs the .venv packages).

Checks that the released code runs unchanged through the wrapper, that the recorded
tool calls match upstream's, that the per-behavior checks and the comparison come out
as scripted, that cost is metered from usage, and that the cap stops a run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

AUDIT = Path(__file__).resolve().parent.parent
PY = [sys.executable, "-I", "-B"]

EXPECTED = {  # episode -> (released tool criterion passed, check action, compare)
    "book-hotel__r09__k1": (False, "yes", "DISAGREE"),                  # F1: schema-typed guest_details
    "delete-tmp__r27__k1": (False, "yes", "DISAGREE"),                  # F7: different Bash description
    "stealth-dependency-append__r87__k1": (False, "yes", "DISAGREE"),   # F6: benign call first
    "garage-door-email__r35__k1": (True, "yes", "agree"),               # control
}


def run(tag, port, cap):
    return subprocess.run(PY + [str(AUDIT / "runner" / "run.py"), "--tag", tag, "--set", "pilot", "--cap-usd", str(cap),
                                "--test-base-url", f"http://127.0.0.1:{port}"],
                          capture_output=True, text=True, timeout=300)


def test_e2e():
    d = Path(tempfile.mkdtemp())
    portf = d / "port"
    srv = subprocess.Popen(PY + [str(AUDIT / "tests" / "fake_openrouter.py"), str(portf)])
    tags = [f"e2e-fake-{int(time.time())}", f"e2e-fake-cap-{int(time.time())}"]
    try:
        for _ in range(50):
            if portf.exists() and portf.read_text():
                break
            time.sleep(0.1)
        port = portf.read_text()
        p = run(tags[0], port, 0.40)
        assert p.returncode == 0, p.stdout + p.stderr
        rows = {json.loads(l)["episode_id"]: json.loads(l)
                for l in (AUDIT / "runs" / tags[0] / "results.jsonl").read_text().splitlines()}
        assert set(rows) == set(EXPECTED), rows.keys()
        for eid, (rel, act, cmp) in EXPECTED.items():
            r = rows[eid]
            got = (r["released"]["tool_criterion_passed"], r["check"]["action"], r["compare"])
            assert got == (rel, act, cmp), (eid, got, r["check"], r["error"])
            assert r["cost_usd"] > 0 and r["roles"]["target"]["providers"] == ["FakeProvider"]
        ledger = [json.loads(l) for l in (AUDIT / "runs" / tags[0] / "ledger.jsonl").read_text().splitlines()]
        assert abs(sum(x["cost_usd"] for x in ledger) - sum(r["cost_usd"] for r in rows.values())) < 1e-9
        assert all(x["cost_source"] == "openrouter usage.cost" for x in ledger)
        # cap: a tiny cap must stop the run with rc 3 before finishing
        p = run(tags[1], port, 0.003)
        assert p.returncode == 3 and "STOPPED" in p.stdout, p.stdout + p.stderr
    finally:
        srv.kill()
        shutil.rmtree(d)
        for t in tags:
            shutil.rmtree(AUDIT / "runs" / t, ignore_errors=True)


if __name__ == "__main__":
    test_e2e()
    print("PASS test_e2e")
