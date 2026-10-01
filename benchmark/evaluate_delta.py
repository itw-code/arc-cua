#!/usr/bin/env python3
"""Markdown delta tables for an ablation JSON written by run_arc_benchmark.py.

    python benchmark/evaluate_delta.py [results/benchmark_runs/ablation_<ts>.json]
Writes <same name>.md next to the JSON and prints it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
METRICS = [  # (section, key, label, format, higher_is_better)
    ("index", "recall_at_1", "Index Recall@1", "{:.1%}", True),
    ("index", "recall_at_2", "Index Recall@2", "{:.1%}", True),
    ("index", "mrr", "Index MRR", "{:.3f}", True),
    ("index", "query_latency_ms_p50", "Index query p50 (ms)", "{:.1f}", False),
    ("index", "query_latency_ms_p95", "Index query p95 (ms)", "{:.1f}", False),
    ("cua", "pin_accuracy", "Pin accuracy", "{:.1%}", True),
    ("cua", "wrong_values_filled", "Wrong values filled", "{:d}", False),
    ("cua", "pin_latency_ms_p50", "Pin p50 (ms)", "{:.1f}", False),
    ("cua", "pin_latency_ms_p95", "Pin p95 (ms)", "{:.1f}", False),
    ("task", "pass_at_1", "Task Pass@1", "{:.1%}", True),
    ("task", "decision_ms_per_task_mean", "Decision time / task (ms)", "{:.0f}", False),
    ("task", "remote_prompt_tokens_per_task", "Remote prompt tokens / task", "{:.0f}", False),
]


def render(data: dict) -> str:
    runs = data["runs"]
    out = [f"# Ablation: seeds {data['seeds']}, System-1 threshold {data['threshold']}", "",
           "| Metric | " + " | ".join(f"{r} ({v['index_engine']} / {v['cua_engine']})" for r, v in runs.items())
           + " | D − A |", "|---" * (len(runs) + 2) + "|"]
    for sec, key, label, fmt, _ in METRICS:
        vals = [runs[r][sec][key] for r in runs]
        delta = runs["D"][sec][key] - runs["A"][sec][key]
        out.append(f"| {label} | " + " | ".join(fmt.format(v) for v in vals) + f" | {delta:+.3g} |")
    out += ["", "| Run | Decision tiers |", "|---|---|"]
    out += [f"| {r} | " + ", ".join(f"{k} {p:.0%}" for k, p in v["tiers"].items()) + " |" for r, v in runs.items()]
    out += ["", f"Tree build (pypdf text, mean): {runs['A']['index']['tree_build_s_mean']:.3f} s. "
            "Stall/recovery: not measured (no browser in this harness)."]
    return "\n".join(out) + "\n"


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else sorted((ROOT / "results" / "benchmark_runs").glob("ablation_*.json"))[-1]
    md = render(json.loads(path.read_text(encoding="utf-8")))
    path.with_suffix(".md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
