# Hybrid decision reels (ARC Index, ARC)

Two 19.2-second cuts of the 2026-10-01 System-1 / System-2 measurements. Outputs:

- `artifacts/hybrid-decision-index-showreel.mp4` (`?v=index`): which page of a letter states a field.
- `artifacts/hybrid-decision-arc-showreel.mp4` (`?v=arc`): which `[#N]` form field takes a value.

Every figure on screen is measured. Sources:

- `results/benchmark_runs/ablation_20261001-054054.json` (`benchmark/run_arc_benchmark.py`, report in the `.md` next to it): page Recall@1 56.9% → 100%, MRR 0.738 → 1.000, Recall@2 76.4% → 100%, page lookup p50 296 ms / p95 804 ms, Pass@1 A/B/C/D 0 / 75.0 / 0 / 91.7%. Pin decisions in run C: 24% kept by Laya (18/18 right), 76% escalated.
- `results/benchmark_runs/calibration_20261001-053044.json` (`benchmark/calibrate_threshold.py`): Laya 15% right on page lookups; threshold 0.44 fitted there.
- `artifacts/benchmarks/arc_index_live_local_blocks_hints_20261001-054124.json` and `…_hybrid_20261001-055818.json` (`scripts/run_arc_index_live.py --binder hints|hybrid`): reworded-label form 3/6 → 6/6 fields received; every live pin decision escalated to Colab; field matching 4.6–6.0 s per form; fill + submit 1.1 s.

Caveats, also on screen: synthetic letters and a small task set; Laya timed on an 8-thread CPU (~38 ms on an L4); no stall occurred in any run, so stall recovery is untested; "Colab model" is Qwen3.8-27B AWQ scored through SGLang `/v1/score` behind a Cloudflare quick tunnel.

- `reel.html`: canvas + frame loop. Motion helpers are copied from `media/arc-index-bench-reel/bench.html`; the look uses the deployed page's own tokens (`index.html`: void #0A0A0F, primary #2563EB / glow #60A5FA, secondary #FF6B6B for the losing side, Inter + JetBrains Mono, 44 px top-masked grid, glass cards, pill chips, the ARC mark). Scenes and numbers live in `scenes.js` (`VARIANTS`).
- `render.py`: `python render.py keys|all <index|arc> [out_dir]`.

Scene order: title, the bridge (3.4-7.6 s: why local Laya was tested, why Qwen on Colab answers, the 0.44 escalation contract; same scene in both variants), then the measured bars, runs, cost and end card (the original 15 s timings shifted by `SHIFT = 4.2`).

`soundtrack.py` synthesizes the 48 kHz stereo bed and foley on this timeline (numpy only, voices from `media/arc-index-bench-reel/soundtrack.py`): `python soundtrack.py` writes `soundtrack.wav`. Encode:

    ffmpeg -framerate 60 -i frames_<v>/f%04d.png -i soundtrack.wav -c:v libx264 -crf 18 -pix_fmt yuv420p -c:a aac -b:a 192k -shortest -movflags +faststart hybrid-decision-<v>-showreel.mp4

Poster (end card): `ffmpeg -ss 18.0 -i hybrid-decision-<v>-showreel.mp4 -frames:v 1 -update 1 -q:v 3 hybrid-decision-<v>-showreel-poster.jpg`. Any timing measured against the reel's end (the closing fade, the soundtrack tail) uses `DUR = 19.2`, not the old 15 s cut.
