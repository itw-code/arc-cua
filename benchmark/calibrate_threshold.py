#!/usr/bin/env python3
"""Fit the System-1 escalation threshold on ground-truth decision tasks.

Runs every task in `decision_tasks.py` (x seeds, which shuffle option order) through
local Laya and, if --endpoint is given, the Colab /v1/systemone gateway. Records the
answer, its probability and the top-2 margin, then reports, per threshold, the share
of decisions System-1 keeps (coverage) and how many of those are right (precision).

The fitted threshold is the lowest one at which every kept decision was right. It is
fitted and reported on the same small set; the leave-one-source-out column shows how
far that holds on sources the threshold never saw.

    python benchmark/calibrate_threshold.py --endpoint https://<gateway>/v1/systemone
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from decision_tasks import DecisionTask, all_tasks  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def run_laya(tasks: List[DecisionTask]) -> List[Dict]:
    import laya
    from arc_cua.index_bridge import LayaWireAdapter

    agent = LayaWireAdapter(laya.load("convaiinnovations/laya-typed-decisions"))
    return [_record(t, lambda t=t: agent.predict(t.state, {"q": t.question()})) for t in tasks]


def run_remote(tasks: List[DecisionTask], endpoint: str) -> List[Dict]:
    def call(t: DecisionTask) -> Dict:
        body = json.dumps({"state": t.state, "questions": {"q": t.question()}}).encode()
        req = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    return [_record(t, lambda t=t: call(t)) for t in tasks]


def _record(t: DecisionTask, fn) -> Dict:
    t0 = time.perf_counter()
    ans = fn()["answers"]["q"]
    ms = (time.perf_counter() - t0) * 1000
    probs = sorted(ans["probabilities"].values(), reverse=True)
    return {"task_id": t.task_id, "kind": t.kind, "source": t.source, "truth": t.truth,
            "choice": ans["choice"], "correct": ans["choice"] == t.truth, "p": probs[0],
            "margin": probs[0] - (probs[1] if len(probs) > 1 else 0.0), "latency_ms": ms}


def fit(rows: List[Dict], key: str) -> Optional[float]:
    """Lowest threshold at which every decision with rows[key] >= threshold is correct."""
    wrong = [r[key] for r in rows if not r["correct"]]
    cut = max(wrong) if wrong else -1.0
    kept = [r[key] for r in rows if r[key] > cut]
    return min(kept) if kept else None


def curve(rows: List[Dict], key: str, t: Optional[float]) -> Dict:
    if t is None:
        return {"threshold": None, "coverage": 0.0, "kept": 0, "precision": None}
    kept = [r for r in rows if r[key] >= t]
    return {"threshold": t, "kept": len(kept), "coverage": len(kept) / len(rows),
            "precision": sum(r["correct"] for r in kept) / len(kept) if kept else None}


def loso(rows: List[Dict], key: str) -> Dict:
    """Fit on all sources but one, apply to the held-out one; pool the held-out decisions."""
    kept = right = 0
    for src in sorted({r["source"] for r in rows}):
        t = fit([r for r in rows if r["source"] != src], key)
        held = [r for r in rows if r["source"] == src and t is not None and r[key] >= t]
        kept += len(held)
        right += sum(r["correct"] for r in held)
    return {"coverage": kept / len(rows), "kept": kept, "precision": right / kept if kept else None}


def summarize(rows: List[Dict]) -> Dict:
    lat = sorted(r["latency_ms"] for r in rows)
    out = {"n": len(rows), "accuracy": sum(r["correct"] for r in rows) / len(rows),
           "latency_ms_p50": statistics.median(lat), "latency_ms_p95": lat[int(0.95 * (len(lat) - 1))],
           "by_kind": {}}
    for kind in sorted({r["kind"] for r in rows}):
        sub = [r for r in rows if r["kind"] == kind]
        out["by_kind"][kind] = {"n": len(sub), "accuracy": sum(r["correct"] for r in sub) / len(sub)}
    for key in ("p", "margin"):
        out[f"fit_{key}"] = curve(rows, key, fit(rows, key))
        out[f"loso_{key}"] = loso(rows, key)
        out[f"at_0.70_{key}"] = curve(rows, key, 0.70)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seeds", type=int, nargs="+", default=[42, 1337, 2026])
    ap.add_argument("--endpoint", help="Colab gateway /v1/systemone URL (System-2 comparison)")
    ap.add_argument("--outdir", type=Path, default=ROOT / "results" / "benchmark_runs")
    args = ap.parse_args()

    tasks = [t for s in args.seeds for t in all_tasks(s)]
    report = {"seeds": args.seeds, "tasks_per_seed": len(all_tasks()), "engines": {}}
    runs = {"laya_local": run_laya(tasks)}
    if args.endpoint:
        runs["sglang_remote"] = run_remote(tasks, args.endpoint)
    for name, rows in runs.items():
        report["engines"][name] = {"summary": summarize(rows), "rows": rows}

    args.outdir.mkdir(parents=True, exist_ok=True)
    path = args.outdir / f"calibration_{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=1), encoding="utf-8")
    for name, e in report["engines"].items():
        print(name, json.dumps(e["summary"], indent=1))
    print("wrote", path)


if __name__ == "__main__":
    main()
