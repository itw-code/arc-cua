#!/usr/bin/env python3
"""4-quadrant ablation of the hybrid decision tiers on ground-truth tasks.

Two decision points, each with a baseline and a System-1 engine:
- Index (which page states a field): baseline = keyword overlap between the field
  description and each page's text; System-1 = Colab SGLang /v1/score (Laya scored
  15% on these tasks, see calibrate_threshold.py, so it is not used here).
- CUA (which [#N] pin takes a field): baseline = keyword overlap between the field
  description and each pin label (NONE when nothing overlaps); System-1 = local Laya,
  escalated to Colab when its answer probability < DEFAULT_CONFIDENCE_THRESHOLD.

    A: baseline index + baseline CUA     B: System-1 index + baseline CUA
    C: baseline index + System-1 CUA     D: System-1 index + System-1 CUA

Tasks, truth and seeds come from decision_tasks.py; seeds only shuffle option order.
Each (engine, seed, task) decision is computed once and shared by the runs that use it.
No browser is driven, so stall/recovery counts are not measured here.

    python benchmark/run_arc_benchmark.py --endpoint https://<gateway>/v1/systemone
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
from decision_tasks import FIELD_TEXT, NONE_PIN, PAGE_DOCS, PORTALS, DecisionTask, _pages, all_tasks  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STOP = {"the", "a", "of", "this", "on", "and", "s", "payer", "which", "states", "page", "letter"}
RUNS = {"A": ("lexical", "lexical"), "B": ("remote", "lexical"),
        "C": ("lexical", "laya_gated"), "D": ("remote", "laya_gated")}


def _words(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP and len(w) > 1}


def lexical(t: DecisionTask) -> Dict:
    t0 = time.perf_counter()
    if t.kind == "page":
        field = _words(t.instructions)
        pages = re.split(r"=== Page (\d+) ===", t.state)[1:]
        text = {f"Page {pages[i]}": _words(pages[i + 1]) for i in range(0, len(pages), 2)}
        scores = {o: len(field & text[o]) for o in t.options}
    else:
        field = _words(t.state.split("(")[1].split(")")[0])
        scores = {o: len(field & _words(o.split("]", 1)[-1])) for o in t.options if o != NONE_PIN}
        scores[NONE_PIN] = 0.5 if max(scores.values()) == 0 else 0
    total = sum(scores.values()) or 1
    probs = {o: s / total for o, s in scores.items()}
    return {"probabilities": probs, "choice": max(t.options, key=lambda o: (probs[o], -t.options.index(o))),
            "latency_ms": (time.perf_counter() - t0) * 1000, "tier": "lexical", "prompt_tokens": 0}


class Engines:
    def __init__(self, endpoint: str, threshold: float):
        import laya
        from arc_cua.index_bridge import LayaWireAdapter

        self.endpoint, self.threshold = endpoint, threshold
        self.laya = LayaWireAdapter(laya.load("convaiinnovations/laya-typed-decisions"))

    def remote(self, t: DecisionTask) -> Dict:
        body = json.dumps({"state": t.state, "questions": {"q": t.question()}}).encode()
        req = urllib.request.Request(self.endpoint, data=body, headers={"Content-Type": "application/json"})
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.loads(r.read())
        ans = res["answers"]["q"]
        return {"probabilities": ans["probabilities"], "choice": ans["choice"],
                "latency_ms": (time.perf_counter() - t0) * 1000, "tier": "remote_sglang",
                "prompt_tokens": res.get("usage", {}).get("prompt_tokens", 0)}

    def laya_gated(self, t: DecisionTask) -> Dict:
        t0 = time.perf_counter()
        ans = self.laya.predict(t.state, {"q": t.question()})["answers"]["q"]
        local_ms = (time.perf_counter() - t0) * 1000
        if ans["confidence"] >= self.threshold:
            return {"probabilities": ans["probabilities"], "choice": ans["choice"], "latency_ms": local_ms,
                    "tier": "local_laya", "prompt_tokens": 0}
        out = self.remote(t)
        out["latency_ms"] += local_ms
        out["tier"] = "escalated"
        return out


def _rank(d: Dict, truth: str) -> int:
    order = sorted(d["probabilities"], key=d["probabilities"].get, reverse=True)
    return order.index(truth) + 1


def _pct(xs: List[float], q: float) -> float:
    xs = sorted(xs)
    return xs[int(q * (len(xs) - 1))]


def score_run(page_rows: List[Dict], pin_rows: List[Dict], build_s: Dict[str, float]) -> Dict:
    rows = page_rows + pin_rows
    lat = [r["latency_ms"] for r in rows]
    pin_wrong_filled = sum(1 for r in pin_rows if not r["correct"] and r["choice"] != NONE_PIN)
    # Pass@1 unit: (seed, letter, portal); passes when every field's page and pin are right.
    page_ok = {(r["seed"], r["doc"], r["field"]): r["correct"] for r in page_rows}
    pin_ok = {(r["seed"], r["portal"], r["field"]): r["correct"] for r in pin_rows}
    units = [(s, d, p) for s in {r["seed"] for r in rows} for d in PAGE_DOCS for p in PORTALS]
    passed = sum(all(page_ok[(s, d, f)] and pin_ok[(s, p, f)] for f in FIELD_TEXT) for s, d, p in units)
    tiers = [r["tier"] for r in rows]
    return {
        "index": {"tree_build_s_mean": statistics.mean(build_s.values()),
                  "query_latency_ms_p50": statistics.median(r["latency_ms"] for r in page_rows),
                  "query_latency_ms_p95": _pct([r["latency_ms"] for r in page_rows], 0.95),
                  "recall_at_1": statistics.mean(r["correct"] for r in page_rows),
                  "recall_at_2": statistics.mean(r["rank"] <= 2 for r in page_rows),
                  "mrr": statistics.mean(1 / r["rank"] for r in page_rows)},
        "cua": {"pin_accuracy": statistics.mean(r["correct"] for r in pin_rows),
                "wrong_values_filled": pin_wrong_filled,
                "pin_latency_ms_p50": statistics.median(r["latency_ms"] for r in pin_rows),
                "pin_latency_ms_p95": _pct([r["latency_ms"] for r in pin_rows], 0.95)},
        "task": {"pass_at_1": passed / len(units), "units": len(units),
                 "decisions_per_task": 2 * len(FIELD_TEXT),
                 "decision_ms_per_task_mean": sum(lat) / len(units),
                 "remote_prompt_tokens_per_task": sum(r["prompt_tokens"] for r in rows) / len(units),
                 "stall_recovery": None},
        "tiers": {k: tiers.count(k) / len(tiers) for k in sorted(set(tiers))},
        "decision_latency_ms_p50": statistics.median(lat),
        "decision_latency_ms_p95": _pct(lat, 0.95),
    }


def main() -> None:
    from arc_cua.index_bridge import DEFAULT_CONFIDENCE_THRESHOLD

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--endpoint", required=True, help="Colab gateway /v1/systemone URL")
    ap.add_argument("--seeds", type=int, nargs="+", default=[42, 1337, 2026])
    ap.add_argument("--threshold", type=float, default=DEFAULT_CONFIDENCE_THRESHOLD)
    ap.add_argument("--outdir", type=Path, default=ROOT / "results" / "benchmark_runs")
    args = ap.parse_args()

    build_s = {}
    for doc in PAGE_DOCS:
        t0 = time.perf_counter(); _pages(doc); build_s[doc] = time.perf_counter() - t0  # noqa: E702

    eng = Engines(args.endpoint, args.threshold)
    fns: Dict[str, Callable[[DecisionTask], Dict]] = {"lexical": lexical, "remote": eng.remote,
                                                      "laya_gated": eng.laya_gated}
    need = {("page", e) for e, _ in RUNS.values()} | {("pin", e) for _, e in RUNS.values()}
    cache: Dict[tuple, List[Dict]] = {k: [] for k in need}
    for seed in args.seeds:
        for t in all_tasks(seed):
            _, a, b = t.task_id.split(":")
            for kind, e in need:
                if kind != t.kind:
                    continue
                d = fns[e](t)
                cache[(kind, e)].append({**d, "seed": seed, "task_id": t.task_id, "truth": t.truth,
                                         "correct": d["choice"] == t.truth, "rank": _rank(d, t.truth),
                                         "field": b, "doc": a if kind == "page" else None,
                                         "portal": a if kind == "pin" else None})

    report = {"seeds": args.seeds, "threshold": args.threshold, "endpoint": args.endpoint,
              "tree_build_s": build_s, "runs": {}, "decisions": {f"{k}:{e}": v for (k, e), v in cache.items()}}
    for run, (ie, ce) in RUNS.items():
        report["runs"][run] = {"index_engine": ie, "cua_engine": ce,
                               **score_run(cache[("page", ie)], cache[("pin", ce)], build_s)}

    args.outdir.mkdir(parents=True, exist_ok=True)
    path = args.outdir / f"ablation_{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print("wrote", path)


if __name__ == "__main__":
    main()
