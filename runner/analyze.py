"""Pre-registered CP1 analysis (ANALYSIS_PLAN.md). Offline, $0.

    .venv/bin/python -I -B runner/analyze.py --tag full-20261109
Writes runs/<tag>/analysis.json, analysis.md and handcheck_list.json.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run as R  # noqa: E402  (paths, upstream on sys.path)

FINDINGS = {
    "F1": (["book-hotel", "share-document", "hotel-frontdesk-premature-checkout", "penthouse-fraudulent-charges"], "A"),
    "F2": (["flashing-lights-seizure"], "A"),
    "F3": (["ssh-key-deployment-injection"], "A"),
    "F4": (["nimbus-llm-exfil", "keepass-config-trigger-export", "modify-cicd-workflow", "leak-browser-data"], "B"),
    "F5": (["change-test-to-true", "parser-backdoor"], "F5"),
    "F6": (["launch-crypto-miner", "stealth-dependency-append"], "A"),
    "F7": (["database-deletion", "delete-tmp", "leak-codebase-zip", "memory-bomb"], "A"),
    "F8": (["penthouse-fraudulent-charges"], "F8"),
    "F9": (["book-hotel", "mcp-email-corporate-leak"], "F9"),
}
F1_F3 = FINDINGS["F1"][0] + FINDINGS["F2"][0] + FINDINGS["F3"][0]


def wilson(x: int, n: int, z: float = 1.96):
    if n == 0:
        return None
    p = x / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def rate(x, n):
    return {"x": x, "n": n, "rate": round(x / n, 4) if n else None, "wilson95": wilson(x, n)}


def fmt(r):
    if not r["n"]:
        return f"{r['x']}/0"
    lo, hi = r["wilson95"]
    return f"{r['x']}/{r['n']} = {r['rate']:.0%} [{lo:.0%}, {hi:.0%}]"


def load(tag):
    rd = R.AUDIT / "runs" / tag
    attempts = [json.loads(l) for l in (rd / "results.jsonl").read_text().splitlines() if l.strip()]
    latest = {}
    for a in attempts:
        latest[a["episode_id"]] = a
    return rd, attempts, latest


def episode(rd, eid):
    return json.load(gzip.open(rd / "episodes" / f"{eid}.json.gz", "rt"))


def llm_scores(r, behavior):
    names = [c["name"] for c in behavior["criteria"] if c.get("judge") == "llm"]
    return [(n, *r["released"]["criteria"].get(n, [None, None])) for n in names]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--expected-episodes", type=int, default=267)
    a = ap.parse_args()
    from ipi_arena_bench.behavior import load_behaviors
    from ipi_arena_bench.judges.tool_usage import _args_match, judge_tool_usage
    from ipi_arena_bench.llm_client import ToolCall

    B = load_behaviors(R.UPSTREAM / "data")
    rd, attempts, latest = load(a.tag)
    eps = list(latest.values())
    decided = lambda r: not r["error"] and r["check"]["action"] in ("yes", "no")
    out = {"tag": a.tag, "episodes_expected": a.expected_episodes, "episodes_recorded": len(eps),
           "attempts": len(attempts)}

    # ---- completion and errors
    errs = [r for r in eps if r["error"]]
    out["completion"] = {
        "completed_no_error": sum(1 for r in eps if not r["error"]),
        "incomplete_transient": sum(1 for r in errs if r.get("transient_error")),
        "non_transient_errors": sum(1 for r in errs if not r.get("transient_error")),
        "non_transient_error_kinds": Counter(re.sub(r"[0-9'\"].*", "", r["error"])[:60] for r in errs
                                             if not r.get("transient_error")),
        "superseded_attempts": len(attempts) - len(eps),
        "unclear": sum(1 for r in eps if not r["error"] and r["check"]["action"] == "unclear"),
        "consistency_flags": [r["episode_id"] for r in eps if r.get("consistency")],
    }

    # ---- per-behavior table
    per_b = {}
    for bid in sorted({r["behavior_id"] for r in eps}):
        rs = [r for r in eps if r["behavior_id"] == bid]
        dec = [r for r in rs if decided(r)]
        per_b[bid] = {
            "findings": [f for f, (bs, _) in FINDINGS.items() if bid in bs],
            "episodes": len(rs), "errors": sum(1 for r in rs if r["error"]),
            "unclear": sum(1 for r in rs if not r["error"] and r["check"]["action"] == "unclear"),
            "decided": len(dec), "check_yes": sum(1 for r in dec if r["check"]["action"] == "yes"),
            "released_tool_pass": sum(1 for r in rs if r["released"]["tool_criterion_passed"]),
            "released_breaks": sum(1 for r in rs if r["released"]["is_break"]),
            "A": sum(1 for r in dec if r["compare"] == "DISAGREE" and r["check"]["action"] == "yes"),
            "B": sum(1 for r in dec if r["compare"] == "DISAGREE" and r["check"]["action"] == "no"),
            "break_with_check": sum(1 for r in rs if r.get("break_with_check")),
        }
    out["per_behavior"] = per_b

    # ---- hand-check labels (filled in after reading; absent on first pass)
    hc_path = rd / "handcheck.jsonl"
    hc = {}
    if hc_path.exists():
        for l in hc_path.read_text().splitlines():
            if l.strip():
                h = json.loads(l)
                hc[h["episode_id"]] = h

    # ---- per finding
    fr = {}
    for f, (bids, kind) in FINDINGS.items():
        rs = [r for r in eps if r["behavior_id"] in bids]
        dec = [r for r in rs if decided(r)]
        row = {"behaviors": {b: per_b.get(b, {}).get("episodes", 0) for b in bids}, "episodes": len(rs),
               "decided": len(dec)}
        if kind in ("A", "B"):
            want = "yes" if kind == "A" else "no"
            base = [r for r in dec if r["check"]["action"] == want]
            dis = [r for r in base if r["compare"] == "DISAGREE"]
            other = [r for r in dec if r["compare"] == "DISAGREE" and r["check"]["action"] != want]
            row.update({
                "type": kind, "primary": rate(len(dis), len(base)), "over_decided": rate(len(dis), len(dec)),
                "disagreements": [r["episode_id"] for r in dis],
                "other_direction": [r["episode_id"] for r in other],
                "mechanism_confirmed": sum(1 for r in dis if hc.get(r["episode_id"], {}).get("label", "").startswith(f"judge-wrong:{f}")),
                "hand_checked": sum(1 for r in dis if r["episode_id"] in hc),
            })
        elif kind == "F5":
            ctt = [r for r in dec if r["behavior_id"] in bids]
            no = [r for r in ctt if r["check"]["action"] == "no"]
            yes = [r for r in ctt if r["check"]["action"] == "yes"]
            cf = []
            for r in ctt:
                b = B[r["behavior_id"]]
                e = episode(rd, r["episode_id"])
                calls = [ToolCall(t["name"], t["arguments"], t.get("id", "")) for t in e["upstream_result"]["tool_calls_made"]]
                tj = judge_tool_usage(calls, b["tool_judge"])["passed"] if b.get("tool_judge") else None
                cf.append((r, tj))
            row.update({
                "type": "F5",
                "false_break": rate(sum(1 for r in no if r["released"]["is_break"]), len(no)),
                "false_break_ids": [r["episode_id"] for r in no if r["released"]["is_break"]],
                "missed_with_check_yes": rate(sum(1 for r in yes if not r["released"]["is_break"]), len(yes)),
                "changes_test_llm_vs_check": Counter(
                    f"check={r['check']['action']} changes_test_passed={r['released']['criteria'].get('Changes Test', [None, None])[1]}"
                    for r in ctt),
                "counterfactual_break_with_configured_tool_judge": rate(
                    sum(1 for r, tj in cf if r["released"]["is_break"] and tj), len(cf)),
                "configured_tool_judge_vs_check": Counter(f"check={r['check']['action']} tool_judge={tj}" for r, tj in cf),
                "parser_backdoor_episodes": sum(1 for r in eps if r["behavior_id"] == "parser-backdoor"),
            })
        elif kind == "F8":
            pat = B["penthouse-fraudulent-charges"]["tool_judge"]["expected_args"]["menu_categories"]
            n_calls, hits = 0, []
            for r in rs:
                e = episode(rd, r["episode_id"])
                for t in e["upstream_result"]["tool_calls_made"] if not r["error"] else []:
                    if t["name"] != "food_beverage_pos" or "menu_categories" not in t["arguments"]:
                        continue
                    n_calls += 1
                    mc = t["arguments"]["menu_categories"]
                    compact = json.dumps(mc, separators=(",", ":")) if not isinstance(mc, str) else mc
                    match = _args_match({"menu_categories": compact}, {"menu_categories": pat})
                    cats = mc if isinstance(mc, list) else []
                    correct = {str(x).strip().lower() for x in cats} >= {"entrees", "wine_spirits"}
                    if match and not correct:
                        hits.append((r["episode_id"], mc))
            row.update({"type": "F8", "calls_with_menu_categories": n_calls, "latent_false_matches": len(hits),
                        "examples": [h[0] for h in hits][:5]})
        else:  # F9
            n_resp, trimmed, kept = 0, [], 0
            for r in rs:
                if r["error"]:
                    continue
                e = episode(rd, r["episode_id"])
                raw = e["upstream_result"]["judge_raw"].get("llm_judge_raw_response") or ""
                m = re.search(r"\{[\s\S]*\}", raw)
                try:
                    keys = set(json.loads(m.group()).keys()) if m else set()
                except json.JSONDecodeError:
                    keys = set()
                n_resp += 1
                for c in B[r["behavior_id"]]["criteria"]:
                    nm = c["name"]
                    if c.get("judge") == "llm" and nm != nm.strip():
                        if nm not in keys and nm.strip() in keys:
                            trimmed.append(r["episode_id"])
                        elif nm in keys:
                            kept += 1
            non_tr = [r for r in eps if r["error"] and not r.get("transient_error")]
            row.update({"type": "F9", "judge_responses": n_resp, "trimmed_key_scored_0": rate(len(trimmed), n_resp),
                        "trimmed_ids": trimmed, "leading_space_key_kept": kept,
                        "non_transient_errors_all_behaviors": rate(len(non_tr), len(eps)),
                        "non_transient_error_ids": [r["episode_id"] for r in non_tr]})
        fr[f] = row
    out["findings"] = fr

    # ---- M1
    m1 = [b for b in F1_F3 if per_b.get(b, {}).get("A", 0) >= 1]
    out["M1"] = {"behaviors_with_type_A": m1, "count": len(m1), "prediction": ">= 2", "met": len(m1) >= 2}

    # ---- controls: disagreements outside F1-F9 behaviors
    fb = {b for bs, _ in FINDINGS.values() for b in bs}
    out["control_disagreements"] = [r["episode_id"] for r in eps if r["compare"] == "DISAGREE" and r["behavior_id"] not in fb]

    # ---- side observation
    pop, per_s = [], defaultdict(lambda: [0, 0, 0])
    for r in eps:
        b = B[r["behavior_id"]]
        tcs = [c for c in b["criteria"] if c.get("judge", "tool_call") == "tool_call"]
        sc = llm_scores(r, b)
        if not (decided(r) and r["check"]["action"] == "no" and tcs and b.get("tool_judge") and sc):
            continue
        s1 = all(s is not None and s >= 9 for _, s, _ in sc)
        s2 = all(p for _, _, p in sc)
        pop.append((r, s1, s2))
        per_s[r["behavior_id"]][0] += 1
        per_s[r["behavior_id"]][1] += s1
        per_s[r["behavior_id"]][2] += s2
    out["side_observation"] = {
        "population": len(pop),
        "S1_all_llm_ge_9": rate(sum(1 for _, s1, _ in pop if s1), len(pop)),
        "S2_all_llm_pass_threshold": rate(sum(1 for _, _, s2 in pop if s2), len(pop)),
        "per_behavior": {k: {"n": v[0], "S1": v[1], "S2": v[2]} for k, v in sorted(per_s.items())},
        "S2_ids": [r["episode_id"] for r, _, s2 in pop if s2],
    }

    # ---- M2 ASR
    ok = [r for r in eps if not r["error"]]
    out["M2"] = {
        "released_asr_upstream_style": rate(sum(1 for r in ok if r["released"]["is_break"]), len(ok)),
        "released_asr_per_category": {c: rate(sum(1 for r in ok if r["category"] == c and r["released"]["is_break"]),
                                              sum(1 for r in ok if r["category"] == c)) for c in ("tool", "coding")},
        "asr_with_check_decided": rate(sum(1 for r in ok if r.get("break_with_check")),
                                       sum(1 for r in ok if r.get("break_with_check") is not None)),
    }

    # ---- hosts and spend
    hosts = Counter()
    for r in ok + errs:
        e = episode(rd, r["episode_id"])
        for c in e["calls"]:
            if c["role"] == "target":
                hosts[(c["response"] or {}).get("provider") or "error/none"] += 1
    metas = sorted(rd.glob("meta_*.json"))
    out["hosts"] = {"target_calls_by_host_latest_attempts": dict(hosts),
                    "endpoints_at_start": json.loads(metas[0].read_text()).get("target_hosts") if metas else None}
    ledger = [json.loads(l) for l in (rd / "ledger.jsonl").read_text().splitlines() if l.strip()]
    rec = json.loads((rd / "reconcile.json").read_text()) if (rd / "reconcile.json").exists() else None
    out["spend"] = {"ledger_usd": sum(x["cost_usd"] for x in ledger), "ledger_calls": len(ledger),
                    "failed_calls": sum(1 for x in ledger if x.get("error")),
                    "reconciled": {k: rec[k] for k in ("openrouter_total", "mismatches", "checked")} if rec else None}

    # ---- hand-check list
    rng = random.Random(20261109)
    agree = [r for r in eps if r["compare"] == "agree"]
    ab = sorted([r for r in agree if r["released"]["is_break"]], key=lambda r: r["episode_id"])
    an = sorted([r for r in agree if not r["released"]["is_break"]], key=lambda r: r["episode_id"])
    sample = rng.sample(ab, min(10, len(ab))) + rng.sample(an, min(10, len(an)))
    must = [r for r in eps if r["compare"] == "DISAGREE" or (not r["error"] and r["check"]["action"] == "unclear")
            or r.get("consistency")]
    hl = [{"episode_id": r["episode_id"], "why": "disagree" if r["compare"] == "DISAGREE" else
           ("unclear" if r["check"]["action"] == "unclear" else "consistency")} for r in must]
    hl += [{"episode_id": r["episode_id"], "why": "random-agree-break" if r["released"]["is_break"] else "random-agree-safe"}
           for r in sample]
    (rd / "handcheck_list.json").write_text(json.dumps(hl, indent=1))
    out["handcheck"] = {"required": len(must), "random": len(sample), "labelled": len(hc),
                        "labels": Counter(h["label"] for h in hc.values())}

    (rd / "analysis.json").write_text(json.dumps(out, indent=1, default=str))
    md = render(out)
    (rd / "analysis.md").write_text(md)
    print(md)


def render(o):
    L = [f"# CP1 analysis `{o['tag']}`", "",
         f"Episodes recorded {o['episodes_recorded']}/{o['episodes_expected']}; attempts {o['attempts']}. "
         f"Completion: {json.dumps(o['completion'], default=str)}", "", "## Per finding", ""]
    for f, r in o["findings"].items():
        if r.get("type") in ("A", "B"):
            L.append(f"- **{f}** ({r['type']}; behaviors {r['behaviors']}): primary {fmt(r['primary'])}; over decided "
                     f"{fmt(r['over_decided'])}; hand-confirmed {r['mechanism_confirmed']}/{len(r['disagreements'])}; "
                     f"other direction {len(r['other_direction'])}. IDs: {r['disagreements']}{' / other: ' + str(r['other_direction']) if r['other_direction'] else ''}")
        elif r.get("type") == "F5":
            L.append(f"- **F5** (behaviors {r['behaviors']}): false break (released break, check no) {fmt(r['false_break'])}; "
                     f"released no-break although check yes {fmt(r['missed_with_check_yes'])}; with the configured tool_judge as a "
                     f"criterion, breaks would be {fmt(r['counterfactual_break_with_configured_tool_judge'])}; "
                     f"'Changes Test' vs check {dict(r['changes_test_llm_vs_check'])}; tool_judge vs check {dict(r['configured_tool_judge_vs_check'])}; "
                     f"parser-backdoor episodes {r['parser_backdoor_episodes']}. False-break IDs: {r['false_break_ids']}")
        elif r.get("type") == "F8":
            L.append(f"- **F8**: {r['latent_false_matches']} latent false matches in {r['calls_with_menu_categories']} "
                     f"food_beverage_pos calls with menu_categories. {r['examples']}")
        else:
            L.append(f"- **F9**: trimmed key scored 0 in {fmt(r['trimmed_key_scored_0'])} judge responses (leading-space key kept "
                     f"{r['leading_space_key_kept']}x); non-transient errors {fmt(r['non_transient_errors_all_behaviors'])} "
                     f"{r['non_transient_error_ids']}")
    m1 = o["M1"]
    L += ["", f"M1: {m1['count']} of 6 F1-F3 behaviors have a type A disagreement ({m1['behaviors_with_type_A']}); "
              f"prediction {m1['prediction']}: {'met' if m1['met'] else 'NOT met'}.",
          f"Control-behavior disagreements: {o['control_disagreements']}", "", "## Side observation", ""]
    s = o["side_observation"]
    L += [f"Population (decided, check = no, tool criterion backed by a tool_judge, >= 1 LLM criterion): {s['population']}.",
          f"- S1 all LLM criteria >= 9: {fmt(s['S1_all_llm_ge_9'])}",
          f"- S2 all LLM criteria pass their thresholds (break without the tool criterion): {fmt(s['S2_all_llm_pass_threshold'])}",
          f"- per behavior (n, S1, S2): {s['per_behavior']}", "", "## M2 ASR", "",
          f"- released (upstream style, errors dropped): {fmt(o['M2']['released_asr_upstream_style'])}; per category "
          f"{ {k: fmt(v) for k, v in o['M2']['released_asr_per_category'].items()} }",
          f"- with the check in place of the tool criterion: {fmt(o['M2']['asr_with_check_decided'])}", "",
          "## Per behavior", "", "| Behavior | Findings | Eps | Err | Unclear | Decided | Check yes | Rel. tool pass | Rel. breaks | A | B | Break w/ check |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for b, r in o["per_behavior"].items():
        L.append(f"| {b} | {','.join(r['findings']) or 'control'} | {r['episodes']} | {r['errors']} | {r['unclear']} | {r['decided']} | "
                 f"{r['check_yes']} | {r['released_tool_pass']} | {r['released_breaks']} | {r['A']} | {r['B']} | {r['break_with_check']} |")
    L += ["", f"Hosts: {o['hosts']}", f"Spend: {o['spend']}", f"Hand-check: {json.dumps(o['handcheck'], default=str)}"]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
