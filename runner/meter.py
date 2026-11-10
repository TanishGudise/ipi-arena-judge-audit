"""Spend meter: worst-case reservation before every paid call, settle with the real cost after.

- reserve() blocks while in-flight reservations could push spend past the cap (wait,
  don't skip); it raises CapReached only when nothing is in flight and the next call's
  worst case still doesn't fit.
- Every settled call is appended to a ledger (JSONL), and a restarted run resumes the
  total from it, so the cap holds across restarts of the same run tag.
- CapReached derives from BaseException so the upstream runner's `except Exception`
  can't turn it into an "errored episode".
"""

from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path


class CapReached(BaseException):
    pass


class SpendMeter:
    def __init__(self, cap_usd: float, ledger_path: Path):
        self.cap = float(cap_usd)
        self.ledger_path = Path(ledger_path)
        self.cv = threading.Condition()
        self.spent = 0.0
        self.reserved = 0.0
        self.calls = 0
        if self.ledger_path.exists():
            for line in self.ledger_path.read_text().splitlines():
                if line.strip():
                    self.spent += float(json.loads(line)["cost_usd"])
                    self.calls += 1
        self.start_spent = self.spent

    def reserve(self, worst_usd: float) -> float:
        with self.cv:
            while self.spent + self.reserved + worst_usd > self.cap:
                if self.reserved <= 0:
                    raise CapReached(
                        f"cap ${self.cap:.2f}: spent ${self.spent:.4f}, next call worst case ${worst_usd:.4f}")
                self.cv.wait()
            self.reserved += worst_usd
            return worst_usd

    def settle(self, worst_usd: float, actual_usd: float, entry: dict) -> None:
        with self.cv:
            self.reserved -= worst_usd
            self.spent += actual_usd
            self.calls += 1
            entry = {"ts": time.time(), "cost_usd": actual_usd, "worst_usd": worst_usd,
                     "over_worst": actual_usd > worst_usd, **entry}
            with self.ledger_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
            self.cv.notify_all()


def worst_case_usd(kwargs: dict, price_in: float, price_out: float, safety: float = 1.5) -> float:
    """Upper bound for one chat call: chars/2 as prompt tokens (real ratio is ~3.5-4
    chars/token), max_tokens as completion, prices times a provider-variance factor."""
    chars = len(json.dumps(kwargs.get("messages", []))) + len(json.dumps(kwargs.get("tools") or []))
    prompt_tokens = math.ceil(chars / 2)
    completion_tokens = int(kwargs.get("max_tokens") or 4096)
    return safety * (prompt_tokens * price_in + completion_tokens * price_out)
