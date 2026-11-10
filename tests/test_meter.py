"""Spend meter: cap enforcement, waiting on in-flight reservations, resume from the ledger."""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))
from meter import CapReached, SpendMeter, worst_case_usd  # noqa: E402


def test_cap_raises_when_nothing_in_flight():
    with tempfile.TemporaryDirectory() as d:
        m = SpendMeter(0.10, Path(d) / "l.jsonl")
        w = m.reserve(0.06)
        m.settle(w, 0.05, {})
        try:
            m.reserve(0.06)  # 0.05 + 0.06 > 0.10
        except CapReached:
            return
        raise AssertionError("expected CapReached")


def test_waits_for_in_flight_then_proceeds():
    with tempfile.TemporaryDirectory() as d:
        m = SpendMeter(0.10, Path(d) / "l.jsonl")
        w1 = m.reserve(0.08)  # in flight
        got = []

        def second():
            got.append(m.reserve(0.05))  # must wait: 0.08 + 0.05 > 0.10

        t = threading.Thread(target=second)
        t.start()
        time.sleep(0.2)
        assert not got, "second reservation should be waiting"
        m.settle(w1, 0.01, {})  # real cost was small; 0.01 + 0.05 fits
        t.join(2)
        assert got == [0.05]


def test_resume_from_ledger():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "l.jsonl"
        m = SpendMeter(1.0, p)
        for _ in range(3):
            m.settle(m.reserve(0.1), 0.07, {"role": "t"})
        m2 = SpendMeter(1.0, p)
        assert abs(m2.spent - 0.21) < 1e-9 and m2.calls == 3


def test_worst_case_bounds_real_ratio():
    msgs = [{"role": "user", "content": "x" * 35000}]  # ~10K real tokens at 3.5 chars/token
    w = worst_case_usd({"messages": msgs, "max_tokens": 4096}, 0.21e-6, 1.9e-6)
    real_max = 10000 * 0.21e-6 + 4096 * 1.9e-6
    assert w > real_max


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
