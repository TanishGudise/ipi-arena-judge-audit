"""Per-episode report for a run: released judge vs per-behavior check, tokens, cost, projection.

    python3 -I -B runner/report.py --tag pilot-20261109 [--full-sizes data/sizes_full_k3.json]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

AUDIT = Path(__file__).resolve().parent.parent


def fmt_tok(r):
    if not r:
        return "-"
    s = f"{r['prompt_tokens']:,}/{r['completion_tokens']:,}"
    return s + (f" (r {r['reasoning_tokens']:,})" if r.get("reasoning_tokens") else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--full-sizes", default=str(AUDIT / "data" / "sizes_full_k3.json"))
    ap.add_argument("--pilot-sizes", default=str(AUDIT / "data" / "sizes_pilot_k1.json"))
    a = ap.parse_args()
    run = AUDIT / "runs" / a.tag
    attempts = [json.loads(l) for l in (run / "results.jsonl").read_text().splitlines() if l.strip()]
    latest = {}
    for r in attempts:
        latest[r["episode_id"]] = r
    rows = list(latest.values())
    retried = [r for r in attempts if latest[r["episode_id"]] is not r]
    ledger = [json.loads(l) for l in (run / "ledger.jsonl").read_text().splitlines() if l.strip()]
    ends = sorted(run.glob("end_*.json"))
    end = json.loads(ends[-1].read_text()) if ends else {}
    metas = sorted(run.glob("meta_*.json"))
    meta = json.loads(metas[0].read_text()) if metas else {}

    out = [f"# Run `{a.tag}`", ""]
    out.append(f"Target `{meta.get('target_model')}`; WorldSim and judge `{meta.get('judge_model')}`; "
               f"upstream `{meta.get('upstream_commit')}`; HF revision `{str(meta.get('dataset'))[:12]}`; "
               f"cap ${meta.get('cap_usd')}.")
    if meta.get("excluded"):
        out.append(f"Excluded: {len(meta['excluded'])} rows ({meta['excluded'][0]['reason']}).")
    out += ["", "| Episode | Steps | Tool calls | Released tool criterion | Released LLM criteria (score) | Released break | "
                "Check (objective) | Compare | Target tok in/out | WorldSim tok in/out | Judge tok in/out | Cost |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        rel = r["released"]
        llm = ", ".join(f"{k.strip()[:22]} {v[0]}" for k, v in rel["criteria"].items() if k != rel["tool_criterion"])
        tc = rel["tool_criterion_passed"]
        tc_s = "n/a" if tc is None else ("pass" if tc else "fail")
        chk = r["check"]
        out.append(f"| {r['episode_id']} | {r['steps']} | {', '.join(r['tool_calls']) or '-'} | {tc_s} | {llm or '-'} | "
                   f"{'BREAK' if rel['is_break'] else 'no'}{' (error)' if r['error'] else ''} | "
                   f"{chk['action']}: {chk['reason'][:90]}{' [' + '; '.join(chk['variants']) + ']' if chk['variants'] else ''} | "
                   f"{r['compare']} | {fmt_tok(r['roles'].get('target'))} | {fmt_tok(r['roles'].get('worldsim'))} | "
                   f"{fmt_tok(r['roles'].get('judge'))} | ${r['cost_usd']:.4f} |")
    total = sum(r["cost_usd"] for r in attempts)
    if retried:
        out += ["", "Superseded attempts (retried with --retry-errors): " + "; ".join(
            f"{r['episode_id']}: {r['error'][:70]} (${r['cost_usd']:.4f})" for r in retried)]
    led = sum(x["cost_usd"] for x in ledger)
    over = sum(1 for x in ledger if x.get("over_worst"))
    sources = sorted({x.get("cost_source") for x in ledger})
    out += ["", f"- Episodes: {len(rows)}; errors: {sum(1 for r in rows if r['error'])}; "
                f"released breaks: {sum(1 for r in rows if r['released']['is_break'])}; "
                f"disagreements: {sum(1 for r in rows if r['compare'] == 'DISAGREE')}; "
                f"unclear: {sum(1 for r in rows if r['compare'] == 'unclear')}.",
            f"- Cost: episodes ${total:.4f}; ledger ${led:.4f} over {len(ledger)} calls (sources: {sources}); "
            f"calls over their worst-case reservation: {over}.",
            ]
    rec = run / "reconcile.json"
    if rec.exists():
        rc = json.loads(rec.read_text())
        billed = sum(1 for r in rc["rows"] if r["gen_id"])
        out.append(f"- Reconciled with OpenRouter generation records ({rc['checked']}): {billed} billed calls, "
                   f"ledger ${rc['ledger_total']:.6f} vs OpenRouter ${rc['openrouter_total']:.6f}, mismatches {rc['mismatches']}; "
                   f"{len(rc['rows']) - billed} failed calls billed $0. Key counter then: usage ${rc['key_now']['usage']}, "
                   f"limit ${rc['key_now']['limit']} (the counter lags the generation records).")
    else:
        out.append(f"- OpenRouter key usage delta (last run segment, lags): {end.get('key_usage_delta')}.")
    for role in ("target", "worldsim", "judge"):
        rs = [r["roles"].get(role) for r in rows if r["roles"].get(role)]
        if rs:
            out.append(f"- {role}: {sum(x['calls'] for x in rs)} calls, {sum(x['prompt_tokens'] for x in rs):,} prompt / "
                       f"{sum(x['completion_tokens'] for x in rs):,} completion tokens "
                       f"({sum(x['reasoning_tokens'] for x in rs):,} reasoning), ${sum(x['cost_usd'] for x in rs):.4f}, "
                       f"providers {sorted({p for x in rs for p in x['providers']})}.")

    # Projection: target cost scales with the first prompt's size; WorldSim + judge per episode.
    try:
        full = json.loads(Path(a.full_sizes).read_text())["episodes"]
        pilot_sizes = {e["episode_id"]: e["first_prompt_chars"] for e in json.loads(Path(a.pilot_sizes).read_text())["episodes"]}
        ok = [r for r in rows if not r["error"] and r["episode_id"] in pilot_sizes and r["roles"].get("target")]
        ratios = [r["roles"]["target"]["cost_usd"] / pilot_sizes[r["episode_id"]] for r in ok]
        other = [r["cost_usd"] - r["roles"]["target"]["cost_usd"] for r in ok]
        if ok:
            n = len(full)
            chars = sum(e["first_prompt_chars"] for e in full)
            mid = sum(ratios) / len(ratios) * chars + n * sum(other) / len(other)
            hi = max(ratios) * chars + n * max(other)
            naive = n * sum(r["cost_usd"] for r in ok) / len(ok)
            out += ["", f"Projection for the full run ({n} episodes, from {len(ok)} pilot episodes, so rough): "
                        f"size-scaled ${mid:.2f}; high (max target $/char and max WorldSim+judge per episode) ${hi:.2f}; "
                        f"naive mean x n ${naive:.2f}."]
            # 5-step bound: every episode uses all 5 target steps and 4 WorldSim calls, per-call sizes above pilot maxima.
            import gzip
            ratios_ct = []
            for r in ok:
                d = json.load(gzip.open(run / "episodes" / f"{r['episode_id']}.json.gz", "rt"))
                first = next(c for c in d["calls"] if c["role"] == "target" and c["response"])
                ratios_ct.append(pilot_sizes[r["episode_id"]] / first["response"]["usage"]["prompt_tokens"])
            cpt = min(ratios_ct)  # fewest chars per token = most tokens = conservative
            (ti, to), (gi, go) = meta["list_price_per_token"][meta["target_model"]], meta["list_price_per_token"][meta["judge_model"]]
            bound = 0.0
            for e in full:
                t0 = e["first_prompt_chars"] / cpt
                bound += sum((t0 + s * 1500) * ti for s in range(5)) + 5 * 800 * to   # target: +1500 tok/step, 800 out/step
                bound += 4 * (3000 * gi + 300 * go) + (2500 * gi + 500 * go)          # 4 WorldSim + 1 judge
            out.append(f"5-step bound (all {n} episodes run 5 target steps + 4 WorldSim calls; {cpt:.2f} chars/token, "
                       f"the lowest pilot ratio; list prices): ${bound:.2f}.")
    except FileNotFoundError:
        pass
    text = "\n".join(out) + "\n"
    (run / "report.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
