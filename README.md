# Solari-Native Hybrid Computer Use Agent (CUA)
## Architecture, RFC Specification, and Implementation Roadmap

[![Status](https://img.shields.io/badge/Status-RFC%20%26%20Planning%20Phase-yellow.svg)](#)
[![Phase](https://img.shields.io/badge/Phase-Pre--Implementation%20Review-blue.svg)](#)
[![Architecture](https://img.shields.io/badge/Architecture-Reflex%20%2B%20Cortex%20Cascade-brightgreen.svg)](#)
[![Cost Target](https://img.shields.io/badge/Cost%20Reduction->75%25-orange.svg)](#)

This repository houses the formal architectural specification, peer-reviewed research foundations, and multi-phase implementation roadmap for building a **Solari-Native Hybrid Computer Use Agent (CUA)**.

---

## 1. Problem Statement: The "Vision Tax" in Computer Use Agents

Standard frontier Computer Use Agents (e.g., Anthropic Computer Use, OpenAI operator baselines) execute an unoptimized, monolithic observation-action loop:
1. Render full desktop / browser display buffer to raster images.
2. Compress and transmit high-resolution PNG/JPEG frames over the public internet to cloud VLMs.
3. Compute visual tokens (typically $1,600 - 3,200$ tokens per screenshot).
4. Run 70B+ parameter generative inference to emit single low-level mouse clicks or keystrokes.

### Prohibitive Realities of the Monolithic Loop
* **Latency Bottleneck:** Median per-step latency spans $1,800\,\text{ms} - 3,500\,\text{ms}$, rendering multi-step flows painfully slow.
* **Cost Inefficiency:** Routine deterministic interactions (clicking "Next", typing in a form field) cost $\$0.01 - \$0.05$ per action.
* **Fragility:** Visual coordinate grounding often drifts due to DPI scaling, anti-aliasing artifacts, and client-side CSS shifts.

---

## 2. Proposed Architectural Solution: Asymmetric Cascading

The Solari Hybrid CUA replaces continuous per-step cloud VLM queries with an asymmetric two-tier architecture running inside **Solari Ephemeral MicroVMs**:

```
                    +-----------------------------------------------------------+
                    |                 SOLARI CLOUD INFRASTRUCTURE               |
                    |  +-----------------------------------------------------+  |
                    |  |       Solari MicroVM (Firecracker / Ephemeral)      |  |
                    |  |                                                     |  |
                    |  |  +-------------------+       +-------------------+  |  |
                    |  |  | Linux AT-SPI D-Bus|       | Headless Chromium |  |  |
                    |  |  | (Desktop OS Tree) |       | (CDP A11y / DOM)  |  |  |
                    |  |  +---------+---------+       +---------+---------+  |  |
                    |  |            |                           |            |  |
                    |  |            +-------------+-------------+            |  |
                    |  |                          | (Zero-Copy Interprocess) |  |
                    |  |                          v                          |  |
                    |  |              [ Dual Perception Stream ]             |  |
                    |  |                          |                          |  |
                    |  |                          v                          |  |
                    |  |            +----------------------------+           |  |
                    |  |            |       REFLEX ENGINE        |           |  |
                    |  |            | - Playwright RPA Locators  |           |  |
                    |  |            | - Invariant Selector Cache |           |  |
                    |  |            | - In-VM INT4 VLM (2B/3B)   |           |  |
                    |  +------------+--------------+-------------+-----------+  |
                    +------------------------------|----------------------------+
                                                   |
                               [Anomaly Escalation: <=18% of Steps]
                                                   v
                                    +-----------------------------+
                                    |        CORTEX ENGINE        |
                                    |  (Frontier Reasoning / Jev) |
                                    |  - High-order Re-planning   |
                                    |  - Sub-goal Decompilation   |
                                    +--------------+--------------+
                                                   |
                                                   v
                                 [Compacted Sub-Goal DSL Sequence]
                                                   |
                                                   +---> (Back to Reflex Engine)
```

1. **Perception**: Zero-copy CDP Accessibility tree extraction (`Accessibility.getFullAXTree`) and Linux AT-SPI D-Bus listening. Token overhead drops by $>84\%$; state extraction executes in $<1\,\text{ms}$.
2. **Reflex Engine**: Executes $80-90\%$ of routine steps deterministically (Playwright locators, invariant LRU selector cache with Levenshtein self-healing, in-VM INT4 quantized SLM) at **sub-10ms** execution latencies.
3. **Cascading Gatekeepers**: Local `ModernBERT` sequence classifiers detect cyclic loops, identical state stalls, and semantic drift.
4. **Cortex Engine**: Escalates only on anomalies ($\le 18\%$ of steps) to a frontier reasoning API (e.g. Jev / Claude 3.5 Sonnet) to perform root-cause re-planning, returning a decompiled sub-goal plan to the local Reflex queue.

---

## 3. Core Specification Documents

All research grounding, detailed task plans, and architectural specifications are located in:

| Document | Description |
| :--- | :--- |
| [`IMPLEMENTATION_PLAN.md`](./IMPLEMENTATION_PLAN.md) | **Primary Roadmap:** Complete 4-phase to-do list with strict research citations, benchmark targets, and risk mitigation strategies. |
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | **Technical Deep-Dive:** Comprehensive system design covering perception pipelines, gatekeeper math, and microVM lifecycle. |
| [`docs/ORCHESTRATOR_REVIEW_CHECKLIST.md`](./docs/ORCHESTRATOR_REVIEW_CHECKLIST.md) | **Review Gates:** Checklist and sign-off criteria for the orchestrator or lead engineer before code implementation commences. |

---

## 4. Key Performance Targets vs. Industry Baselines

| Metric / Dimension | Traditional VLM Agent (Claude 3.5 Sonnet / GPT-4o) | Local Pure SLM Agent (UI-TARS-7B / Qwen2-VL-7B) | **Solari Hybrid CUA (Target)** | Primary Citation / Justification |
| :--- | :--- | :--- | :--- | :--- |
| **Routine Step Latency ($p_{50}$)** | $1,800\,\text{ms} - 3,500\,\text{ms}$ | $250\,\text{ms} - 600\,\text{ms}$ | **$<10\,\text{ms}$ (Reflex) / $1,400\,\text{ms}$ (Escalated)** | Agache et al. (2020), Zhou et al. (2024) |
| **Per-Task Cost** | $\$0.40 - \$1.50$ | $\approx \$0.00$ | **$\le \$0.10$ ($>75\%$ cost reduction)** | Wei et al. (2026), Chen et al. (2023) |
| **Perception Token Load** | $1,600 - 3,200$ tokens / step | N/A (Image patch tokens) | **$\le 1,200$ tokens / step (pruned A11y)** | Deng et al. (2023, Mind2Web) |
| **WebArena-Verified Success** | $32.0\% - 38.0\%$ | $18.5\% - 27.0\%$ | **$\ge 38.5\%$** | Zhou et al. (2024), Koh et al. (2024) |
| **OSWorld Success Rate** | $22.0\% - 29.0\%$ | $12.0\% - 18.0\%$ | **$\ge 32.0\%$** | Xie et al. (2024, OSWorld) |
| **Step Efficiency Ratio (vs. Human)** | $1.85 - 2.50$ | $2.40 - 3.80$ | **$\le 1.30$** | Xie et al. (2024, OSWorld-Human) |

---

## 5. Review & Approval Protocol

This repository is currently held in **RFC Review Mode**. No production code should be merged until:
1. The **Orchestrator** reviews and signs off on the 4-phase sequence in `IMPLEMENTATION_PLAN.md`.
2. The benchmark baseline expectations and hardware constraints (microVM CPU/vGPU offloading) are validated.
3. The mitigation strategies for dynamic canvas elements and AT-SPI desktop synchronization are approved.
