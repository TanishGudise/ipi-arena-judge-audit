"""CP1 runner: the released BenchmarkRunner.run_one, unchanged, plus recording, metering and the
per-behavior checks.

Every target / WorldSim / judge call goes through the upstream LLMClient.chat. Only the
OpenAI client's `create` is wrapped, to (1) reserve the worst-case cost first, (2) ask
OpenRouter for usage accounting, (3) record the full request and response. The upstream
code parses the response exactly as it would without the wrapper.

Run it under egress.py (network limited to openrouter.ai:443); see runner/README.md.

    .venv/bin/python -I -B runner/run.py --tag pilot-20261109 --set pilot --cap-usd 0.40
    python3 -I -B runner/run.py --tag x --set full --dry-run   # no network, no key, no openai needed
"""

from __future__ import annotations

import argparse
import gzip
import re
import hashlib
import json
import os
import sys
import threading
import time
import types
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUDIT = HERE.parent
UPSTREAM = Path(os.environ.get("GS_UPSTREAM") or Path.home() / "gray-swan-recon" / "ipi_arena_os")  # released checkout
ATTACKS = Path(os.environ.get("GS_ATTACKS") or Path.home() / "gray-swan-recon" / "hf" / "pinned" / "attacks.jsonl")  # published HF attacks file
MANIFEST = AUDIT / "data" / "attacks_manifest.json"
UPSTREAM_COMMIT = "5e2e285"

TARGET_MODEL = "qwen/qwen3-vl-235b-a22b-instruct"   # README default; the attacks' source model
JUDGE_MODEL = "google/gemini-2.5-flash"             # repo DEFAULT_JUDGE_MODEL; also WorldSim
PILOT_ROWS = [9, 27, 87, 35]   # first HF row of book-hotel, delete-tmp, stealth-dependency-append, garage-door-email
BROWSER_REASON = ("browser behavior: the page must be rendered with Playwright + Chromium; "
                  "excluded from this audit (none of F1-F9 is a browser behavior)")

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(UPSTREAM / "src"))
BASE = "https://openrouter.ai"  # --test-base-url points this at tests/fake_openrouter.py
# Errors the retry pass may rerun: rate limits, provider 5xx, timeouts, dropped connections. Anything else
# (e.g. the judge's int(score) ValueError, finding F9) stays an error so it is measured, not hidden.
TRANSIENT = re.compile(r"Error code: (408|409|429|5\d\d)\b|Connection error|timed out|timeout", re.I)


def sha256(s: str | bytes) -> str:
    return hashlib.sha256(s.encode() if isinstance(s, str) else s).hexdigest()


def load_attacks() -> list[dict]:
    raw = ATTACKS.read_bytes()
    man = json.loads(MANIFEST.read_text())
    if sha256(raw) != man["file_sha256"]:
        raise SystemExit("attacks.jsonl does not match the pinned manifest sha256")
    rows = [json.loads(l) for l in raw.decode().splitlines() if l.strip()]
    out = []
    for m, r in zip(man["rows"], rows):
        assert m["attack_sha256"] == sha256(r["attack"])
        out.append({**m, "attack": r["attack"]})
    return out


def select(attacks, which: str, rows: list[int] | None, k: int):
    if rows:
        chosen = [a for a in attacks if a["row"] in rows]
    elif which == "pilot":
        chosen = [a for r in PILOT_ROWS for a in attacks if a["row"] == r]
    else:
        chosen = list(attacks)
    excluded = [{"row": a["row"], "behavior_id": a["behavior_id"], "reason": BROWSER_REASON}
                for a in chosen if a["category"] == "browser"]
    chosen = [a for a in chosen if a["category"] != "browser"]
    eps = [{**a, "rep": rep, "episode_id": f"{a['behavior_id']}__r{a['row']:02d}__k{rep}"}
           for rep in range(1, k + 1) for a in chosen]
    return eps, excluded


def upstream_state() -> dict:
    """HEAD commit (read from .git files, no git process) and a hash of src/ + data/."""
    git = UPSTREAM / ".git"
    head = (git / "HEAD").read_text().strip()
    if head.startswith("ref: "):
        ref = head[5:]
        f = git / ref
        if f.exists():
            head = f.read_text().strip()
        else:
            packed = (git / "packed-refs").read_text().splitlines()
            head = next(l.split()[0] for l in packed if l.endswith(" " + ref))
    h = hashlib.sha256()
    for f in sorted([*(UPSTREAM / "src").rglob("*.py"), *(UPSTREAM / "data").rglob("*.json")]):
        h.update(str(f.relative_to(UPSTREAM)).encode() + b"\0" + f.read_bytes() + b"\0")
    if not head.startswith(UPSTREAM_COMMIT):
        raise SystemExit(f"upstream clone is at {head[:12]}, expected {UPSTREAM_COMMIT}")
    return {"commit": head, "src_data_sha256": h.hexdigest()}


def load_key() -> str:
    env = AUDIT / ".env"
    if not env.exists():
        raise SystemExit(f"{env} not found")
    for line in env.read_text().splitlines():
        name, _, value = line.partition("=")
        if name.strip().removeprefix("export ").strip() == "OPENROUTER_API_KEY":
            value = value.strip().strip('"').strip("'")
            if value:
                return value
    raise SystemExit("OPENROUTER_API_KEY not set in .env (checked by name only)")


# ---------------------------------------------------------------- recording + metering

_tl = threading.local()


def parse_args_like_upstream(raw):
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return {"_raw": raw}


def make_client(role, model, api_key, meter, price, provider_order=None):
    from ipi_arena_bench.llm_client import LLMClient
    from meter import worst_case_usd

    base_url = None if BASE == "https://openrouter.ai" else f"{BASE}/api/v1"
    client = LLMClient(provider="openrouter", model=model, api_key=api_key, base_url=base_url)
    client.client.max_retries = 6  # transport retries (429/5xx, with backoff); failed attempts aren't billed
    comp = client.client.chat.completions
    orig = comp.create

    def create(**kw):
        worst = meter.reserve(worst_case_usd(kw, *price))
        t0, resp, err = time.time(), None, None
        try:
            extra = {"usage": {"include": True}}
            if provider_order:  # OpenRouter routing preference; fallbacks stay on
                extra["provider"] = {"order": provider_order, "allow_fallbacks": True}
            resp = orig(**kw, extra_body=extra)
            return resp
        except Exception as e:  # recorded, then re-raised to the upstream code
            body = getattr(e, "body", None)
            err = f"{type(e).__name__}: {e}"[:500] + (f" | body={json.dumps(body, default=str)[:1500]}" if body else "")
            raise
        finally:
            dump = resp.model_dump() if resp is not None else None
            usage = (dump or {}).get("usage") or {}
            cost, source = usage.get("cost"), "openrouter usage.cost"
            if cost is None:
                cost = usage.get("prompt_tokens", 0) * price[0] + usage.get("completion_tokens", 0) * price[1]
                source = "list-price estimate" if usage else "no response"
            meter.settle(worst, float(cost), {"role": role, "model": model, "gen_id": (dump or {}).get("id"),
                                              "provider": (dump or {}).get("provider"), "usage": usage,
                                              "cost_source": source, "error": err,
                                              "episode": getattr(_tl, "eid", None)})
            rec = getattr(_tl, "calls", None)
            if rec is not None:
                req = {k: v for k, v in kw.items() if k != "tools"}
                if kw.get("tools"):
                    req["tools_sha256"] = sha256(json.dumps(kw["tools"], sort_keys=True))
                rec.append({"role": role, "request": req, "response": dump, "error": err,
                            "latency_s": round(time.time() - t0, 2), "worst_usd": worst,
                            "cost_usd": float(cost), "cost_source": source})

    comp.create = create
    return client


# ---------------------------------------------------------------- one episode

def tool_criterion(behavior):
    tc = [c["name"] for c in behavior["criteria"] if c.get("judge", "tool_call") == "tool_call"]
    return tc[0] if tc else None


def run_episode(runner, behaviors, ep, run_dir: Path) -> dict:
    import oracle
    from ipi_arena_bench.behavior import build_tools

    bid = ep["behavior_id"]
    behavior = behaviors[bid]
    _tl.calls, _tl.eid = [], ep["episode_id"]
    t0 = time.time()
    try:
        res = runner.run_one(bid, ep["attack"])
    finally:
        calls, _tl.calls, _tl.eid = _tl.calls, None, None
    rd = res.to_dict()

    # Tool calls rebuilt from the recorded target responses (kept even when upstream
    # errored and dropped them), parsed exactly as llm_client does.
    tcs = []
    for c in calls:
        if c["role"] == "target" and c["response"]:
            for t in (c["response"]["choices"][0]["message"].get("tool_calls") or []):
                tcs.append({"name": t["function"]["name"],
                            "arguments": parse_args_like_upstream(t["function"]["arguments"]), "id": t.get("id")})
    consistency = None
    if not rd["error"] and [(t["name"], t["arguments"]) for t in tcs] != \
            [(t["name"], t["arguments"]) for t in rd["tool_calls_made"]]:
        consistency = "rebuilt tool calls differ from upstream's tool_calls_made"  # flagged, analysed by hand

    targets = [c for c in calls if c["role"] == "target" and c["response"]]
    convo = list(targets[-1]["request"]["messages"]) if targets else []
    if targets:
        convo.append(targets[-1]["response"]["choices"][0]["message"])
    try:
        v = oracle.check(bid, [{"name": t["name"], "arguments": t["arguments"]} for t in tcs], convo)
    except Exception as e:
        v = oracle.Verdict(oracle.UNCLEAR, f"check crashed: {type(e).__name__}: {e}"[:300])

    tc_name = tool_criterion(behavior)
    crit = rd["criteria"]
    released_tool = crit.get(tc_name, {}).get("passed") if (tc_name is not None and not rd["error"]) else None  # "" is a real name (delete-tmp)
    if rd["error"] or v.action == "unclear" or released_tool is None:
        compare = "error" if rd["error"] else ("unclear" if v.action == "unclear" else "no tool criterion")
    else:
        compare = "agree" if (v.action == "yes") == released_tool else "DISAGREE"
    llm_names = [c["name"] for c in behavior["criteria"] if c.get("judge") == "llm"]
    llm_pass = all(crit.get(n, {}).get("passed") for n in llm_names) if not rd["error"] else None

    roles = {}
    for c in calls:
        r = roles.setdefault(c["role"], {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                         "reasoning_tokens": 0, "cost_usd": 0.0, "providers": []})
        u = (c["response"] or {}).get("usage") or {}
        r["calls"] += 1
        r["prompt_tokens"] += u.get("prompt_tokens") or 0
        r["completion_tokens"] += u.get("completion_tokens") or 0
        r["reasoning_tokens"] += ((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        r["cost_usd"] += c["cost_usd"]
        p = (c["response"] or {}).get("provider")
        if p and p not in r["providers"]:
            r["providers"].append(p)

    summary = {
        "episode_id": ep["episode_id"], "behavior_id": bid, "category": ep["category"], "row": ep["row"],
        "rep": ep["rep"], "attack_sha256": ep["attack_sha256"], "error": rd["error"],
        "transient_error": bool(rd["error"] and TRANSIENT.search(rd["error"])), "consistency": consistency,
        "steps": rd["steps"],
        "tool_calls": [t["name"] for t in tcs],
        "released": {"is_break": rd["is_break"], "tool_criterion": tc_name, "tool_criterion_passed": released_tool,
                     "criteria": {k: [x.get("score"), x.get("passed")] for k, x in crit.items()}},
        "check": v.to_dict(), "compare": compare,
        "break_with_check": (v.action == "yes" and llm_pass) if v.action != "unclear" and llm_pass is not None else None,
        "roles": roles, "cost_usd": sum(c["cost_usd"] for c in calls),
        "duration_s": round(time.time() - t0, 1),
    }
    full = {"summary": summary, "upstream_result": rd, "calls": calls, "conversation": convo,
            "tools_sha256": sha256(json.dumps(build_tools(behavior), sort_keys=True))}
    with gzip.open(run_dir / "episodes" / f"{ep['episode_id']}.json.gz", "wt") as f:
        json.dump(full, f, default=str)
    return summary


# ---------------------------------------------------------------- main

def openrouter_get(path: str, api_key: str | None = None):
    try:
        import httpx
    except ImportError:  # openai 3.x ships the renamed httpx2 (same API, honors proxy env)
        import httpx2 as httpx
    h ={"Authorization": f"Bearer {api_key}"} if api_key else {}
    r = httpx.get(f"{BASE}/api/v1/{path}", headers=h, timeout=30)
    r.raise_for_status()
    return r.json()


def key_status(api_key):
    d = openrouter_get("key", api_key).get("data", {})
    return {k: d.get(k) for k in ("usage", "limit", "limit_remaining", "is_free_tier")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--set", choices=["pilot", "full"], default="pilot")
    ap.add_argument("--rows", type=lambda s: [int(x) for x in s.split(",")])
    ap.add_argument("--k", type=int, default=1)
    ap.add_argument("--cap-usd", type=float)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--target-provider-order", type=lambda s: s.split(","),
                    help="OpenRouter provider preference for the target, e.g. Parasail (fallbacks allowed)")
    ap.add_argument("--retry-errors", action="store_true",
                    help="rerun episodes whose latest attempt hit a transient error (429/5xx/timeout); "
                         "the failed attempt stays in results.jsonl and the ledger")
    ap.add_argument("--test-base-url", help="local fake OpenRouter for the $0 end-to-end test")
    a = ap.parse_args()
    global BASE
    if a.test_base_url:
        BASE = a.test_base_url.rstrip("/")

    attacks = load_attacks()
    eps, excluded = select(attacks, a.set, a.rows, a.k)

    if a.dry_run:
        if "openai" not in sys.modules:
            try:
                import openai  # noqa: F401
            except ImportError:
                sys.modules["openai"] = types.ModuleType("openai")  # dry run never calls it
        from ipi_arena_bench.behavior import build_messages, build_tools, load_behaviors
        import oracle
        behaviors = load_behaviors(UPSTREAM / "data")
        sizes = []
        for ep in eps:
            b = behaviors[ep["behavior_id"]]
            chars = len(json.dumps(build_messages(b, ep["attack"]))) + len(json.dumps(build_tools(b)))
            sizes.append({"episode_id": ep["episode_id"], "row": ep["row"], "first_prompt_chars": chars,
                          "has_check": ep["behavior_id"] in oracle.CHECKS})
            print(f"{ep['episode_id']:<52} first prompt {chars:>7} chars  check={'yes' if sizes[-1]['has_check'] else 'MISSING'}")
        print(f"{len(eps)} episodes; excluded {len(excluded)}: {sorted({e['behavior_id'] for e in excluded})}")
        print("upstream", upstream_state())
        out = AUDIT / "data" / f"sizes_{a.set}_k{a.k}.json"
        out.write_text(json.dumps({"episodes": sizes, "excluded": excluded}, indent=1))
        print(f"wrote {out}")
        return 0 if all(s["has_check"] for s in sizes) else 1

    if a.cap_usd is None:
        raise SystemExit("--cap-usd is required for a paid run")
    from ipi_arena_bench.behavior import load_behaviors
    from ipi_arena_bench.runner import BenchmarkRunner
    from meter import CapReached, SpendMeter

    run_dir = AUDIT / "runs" / a.tag
    (run_dir / "episodes").mkdir(parents=True, exist_ok=True)
    results_path = run_dir / "results.jsonl"
    latest = {}
    if results_path.exists():
        for l in results_path.read_text().splitlines():
            if l.strip():
                r = json.loads(l)
                latest[r["episode_id"]] = r
    done = {e for e, r in latest.items() if not (a.retry_errors and r["error"] and TRANSIENT.search(r["error"]))}
    todo = [e for e in eps if e["episode_id"] not in done]

    api_key = "fake-key" if a.test_base_url else load_key()
    models = {m["id"]: m for m in openrouter_get("models")["data"]}
    price = {mid: (float(models[mid]["pricing"]["prompt"]), float(models[mid]["pricing"]["completion"]))
             for mid in (TARGET_MODEL, JUDGE_MODEL)}
    key_before = key_status(api_key)
    try:
        eps_data = openrouter_get(f"models/{TARGET_MODEL}/endpoints")["data"]["endpoints"]
        target_hosts = [{k: e.get(k) for k in ("provider_name", "quantization", "status", "uptime_last_30m")}
                        for e in eps_data]
    except Exception as e:  # informational only
        target_hosts = f"unavailable: {e}"[:200]
    meter = SpendMeter(a.cap_usd, run_dir / "ledger.jsonl")
    meta = {"tag": a.tag, "set": a.set, "rows": a.rows, "k": a.k, "cap_usd": a.cap_usd, "workers": a.workers, "target_provider_order": a.target_provider_order,
            "upstream_commit": UPSTREAM_COMMIT, "upstream": upstream_state(), "target_model": TARGET_MODEL, "judge_model": JUDGE_MODEL,
            "worldsim_model": JUDGE_MODEL, "max_steps": 5, "list_price_per_token": price,
            "dataset": json.loads(MANIFEST.read_text())["revision"], "excluded": excluded,
            "episodes_total": len(eps), "episodes_todo": len(todo), "spent_before_usd": meter.spent,
            "key_before": key_before, "target_hosts": target_hosts, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    (run_dir / f"meta_{int(time.time())}.json").write_text(json.dumps(meta, indent=1))
    print(f"{len(todo)}/{len(eps)} episodes to run; cap ${a.cap_usd:.2f}; spent so far ${meter.spent:.4f}; "
          f"key limit set: {key_before.get('limit') is not None}")

    behaviors = load_behaviors(UPSTREAM / "data")
    runner = BenchmarkRunner(
        target_client=make_client("target", TARGET_MODEL, api_key, meter, price[TARGET_MODEL], a.target_provider_order),
        judge_client=make_client("judge", JUDGE_MODEL, api_key, meter, price[JUDGE_MODEL]),
        worldsim_client=make_client("worldsim", JUDGE_MODEL, api_key, meter, price[JUDGE_MODEL]),
        behaviors_path=str(UPSTREAM / "data"), verbose=False)

    lock = threading.Lock()
    stop = threading.Event()

    def one(ep):
        if stop.is_set():
            return None
        s = run_episode(runner, behaviors, ep, run_dir)
        with lock, results_path.open("a") as f:
            f.write(json.dumps(s) + "\n")
        print(f"{s['episode_id']:<48} steps={s['steps']} released_tool={s['released']['tool_criterion_passed']} "
              f"break={s['released']['is_break']} check={s['check']['action']} {s['compare']} "
              f"${s['cost_usd']:.4f} total=${meter.spent:.4f}" + (f" ERROR {s['error'][:80]}" if s["error"] else ""),
              flush=True)
        return s

    rc = 0
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futs = [pool.submit(one, e) for e in todo]
        try:
            finished, _ = wait(futs, return_when=FIRST_EXCEPTION)
            for f in finished:
                f.result()
        except CapReached as e:
            stop.set()
            print(f"STOPPED: {e}", flush=True)
            rc = 3
        except BaseException:
            stop.set()
            raise
    key_after = key_status(api_key)
    end = {"spent_usd": meter.spent, "calls": meter.calls, "key_after": key_after,
           "key_usage_delta": (key_after["usage"] - key_before["usage"])
           if key_after.get("usage") is not None and key_before.get("usage") is not None else None,
           "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "rc": rc}
    (run_dir / f"end_{int(time.time())}.json").write_text(json.dumps(end, indent=1))
    print(f"spent ${meter.spent:.4f} (ledger); key usage delta {end['key_usage_delta']}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
