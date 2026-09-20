# Architecture Specification: Solari-Native Hybrid Computer Use Agent (CUA)

## 1. System Overview

The Solari Hybrid CUA is an asymmetric, dual-tier autonomous agent runtime. It addresses the latency and cost bottlenecks of visual foundation models through two collaborating execution loops:
1. **The Reflex Loop (In-VM, Sub-Millisecond)**: Operates entirely within the local execution environment (Solari Firecracker MicroVM). It consumes structured accessibility and DOM representations, consults an invariant selector cache, and relies on an in-VM quantized small language/multimodal model (e.g., Qwen2-VL-2B or UI-TARS-2B) to execute routine single-hop actions.
2. **The Cortex Loop (Cloud Escalation, Event-Driven)**: An escalation tier triggered only when local monitors detect trajectory anomalies (repetitive loops, stalled progress, locator exceptions, or low grounding confidence). It invokes a frontier reasoning model (e.g. Jev / Claude 3.5 Sonnet) with multi-modal context to perform root-cause diagnosis, re-plan the global trajectory, and decompile the solution into a sequence of deterministic sub-goals for the Reflex loop.

---

## 2. Perception Subsystem

### 2.1 Zero-Copy Accessibility Tree Extraction (Web)
Traditional web agents inject heavy JavaScript scripts to serialize the DOM into HTML strings or capture raster screenshots. The Solari Hybrid CUA interfaces directly with the browser engine process via the Chrome DevTools Protocol (CDP) over a local Unix Domain Socket (`/tmp/chromium-cdp.sock`):
* Calls `Accessibility.getFullAXTree`.
* Runs a C++/Rust native sanitizer to prune redundant nodes:
  - Discards unlabelled containers (`div`, `span` lacking click handlers, ARIA labels, or text nodes).
  - Eliminates hidden subtrees (`aria-hidden="true"`, `display: none`, `visibility: hidden`).
  - Assigns unique, monotonic numerical indices to every actionable element: `[#12] Button 'Submit Order'`.
* Output representation: YAML-style linearized string consuming $\le 1,200$ tokens for standard complex web pages (an 84% reduction compared to raw HTML).

### 2.2 Linux Desktop AT-SPI D-Bus Daemon
For operating system tasks (OSWorld), the agent attaches to the Linux accessibility bus (`org.a11y.Bus`):
* Recursively queries `org.a11y.atspi.Accessible` interfaces across GTK, Qt, and Electron processes.
* Subscribes to D-Bus signal streams:
  - `window:activate`
  - `object:state-changed` (focused, selected, expanded)
  - `object:text-changed`
* Correlates screen coordinates with application window bounds directly from X11 (`_NET_ACTIVE_WINDOW`), ensuring the tree remains synchronized without visual diffing.

### 2.3 Visual Fallback Trigger (Local OmniParser)
When an interaction targets HTML5 `<canvas>` (e.g. Figma, Google Docs) or unannotated WebGL/video components, the AXTree yields empty nodes.
* A local visual fallback daemon executes:
  - Grabs an in-memory frame buffer screenshot via `/dev/shm/xwd` or CDP `Page.captureScreenshot`.
  - Runs INT8 ONNX-quantized OmniParser (YOLOv8 icon detection + PaddleOCR/TrOCR) on the VM's local CPU/vGPU.
  - Generates Set-of-Mark bounding boxes locally ($<45\,\text{ms}$).
  - If detector confidence falls below $\tau = 0.70$, routes immediately to the Cortex Engine.

---

## 3. The Reflex Engine

### 3.1 Sub-Goal Playwright Compiler
Actions are emitted as structured symbolic tuples:
$$\text{Action} = \langle \text{Verb}, \text{TargetLocator}, \text{Parameters} \rangle$$
Verbs include:
* `CLICK`: Dispatches mouse-down, click, mouse-up sequence with natural Bézier motion.
* `TYPE`: Dispatches keyboard key-down, char input, key-up with humanized jitter ($20-50\,\text{ms}$).
* `SELECT`: Direct DOM option mutation.
* `SCROLL`: Delta scroll event along the vertical or horizontal axis.

### 3.2 Invariant Selector Cache & Self-Healing
To eliminate redundant inference calls across recurring workflows:
* The cache stores an LRU mapping of `(GoalHash, ContextHash) -> SelectorTuple`.
* `SelectorTuple` includes:
  1. Primary: Semantic test attribute (`data-testid`, `id`).
  2. Secondary: ARIA role and normalized accessible name (`button[name='Checkout']`).
  3. Tertiary: Anchored structural XPath.
* **Self-Healing**: If the primary selector fails due to dynamic class name mutation, the engine computes normalized Levenshtein string distance against visible AXNode labels, automatically repairing the locator without cloud intervention.

---

## 4. Cascading Gatekeepers & Anomaly Detection

### 4.1 ModernBERT Stuck Monitor
To identify infinite loops or dead ends:
* Computes a 64-bit SimHash of the current cleaned accessibility tree: $H(s_t)$.
* Compares consecutive states:
  - If $H(s_t) == H(s_{t-1})$ after an action expected to trigger navigation $\implies$ potential zero-pixel trap or unhandled click.
  - If $H(s_t) == H(s_{t-2})$ and $a_t == a_{t-2} \implies$ cyclic oscillation.
* A fine-tuned `ModernBERT-base` sequence classifier inspects the sliding history window:
  $$\tau_{t-4:t} = \{(s_{t-4}, a_{t-4}), \dots, (s_t, a_t)\}$$
  Predicts the stuck probability $P(\text{stuck})$. If $P(\text{stuck}) > \alpha_{\text{escalate}}$ for $N=2$ consecutive ticks, triggers Cortex escalation.

### 4.2 Semantic Milestone Monitor
* Computes the semantic progress delta:
  $$\text{Progress}(g, s_t, s_0) = \cos(\mathbf{e}_g, \mathbf{e}_{s_t} - \mathbf{e}_{s_0})$$
  using an in-VM embedding model (`bge-micro-v2`).
* If progress stalls across $N \ge 3$ consecutive steps or diverges from the high-level intent vector $g$, an anomaly interrupt is dispatched.

### 4.3 Asymmetric Cooldown & Hysteresis Dampening
Prevents high-frequency oscillation between Reflex and Cortex:
* Once Cortex returns a recovery plan, Reflex enters a **cooldown lock** for $M=3$ local steps.
* During cooldown, transient rendering delays or micro-stalls do not trigger Cortex escalation, unless an unrecoverable exception (process crash, page 404, locator not found) occurs.

---

## 5. Cortex Escalation Protocol & Sub-Goal Decompilation

When escalation triggers:
1. **Payload Assembly**:
   - High-level task objective $g$.
   - History of executed actions and observed state hashes $\tau_{0:t}$.
   - Pruned accessibility tree + compressed screenshot with Set-of-Mark annotations.
   - Detected failure signature (`CYCLIC_LOOP`, `CANVAS_OCCLUSION`, `LOCATOR_NOT_FOUND`, `SEMANTIC_DRIFT`).
2. **Frontier Inference**:
   - Sent via high-speed gRPC streaming to the frontier model (Jev / Claude 3.5 Sonnet).
   - Prompt format enforces a structured JSON recovery schema containing root cause analysis and a linear array of sub-goals.
3. **Plan Decompilation**:
   - Cortex response is converted into Reflex DSL commands:
     ```json
     [
       {"action": "CLICK", "target": "#close-modal-btn"},
       {"action": "TYPE", "target": "#search-input", "value": "2026 Financial Report"},
       {"action": "PRESS_KEY", "key": "Enter"}
     ]
     ```
   - Injected into the Reflex FIFO queue; local execution resumes instantly.

---

## 6. Solari MicroVM Lifecycle & Snapshotting

Built on Firecracker lightweight virtualization:
* **MicroVM Startup**: Replaced with UFFD (Userfaultfd) memory snapshots.
* **State Forking**: Before attempting risky actions (e.g. submitting a form, executing shell scripts, destructive deletions), the agent takes a sub-5ms memory-COW snapshot.
* If the Stuck Monitor triggers or an irrecoverable error occurs, the VM state can be rolled back to the pre-action snapshot in $<5\,\text{ms}$, allowing speculative alternative paths without permanent environment corruption.
