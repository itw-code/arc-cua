# ARC MCP vs Solari MCP — head-to-head (2026-09-26)

**Verdict:** on 7 grounding tasks, ARC succeeded on 7 and Solari's official MCP on 6, and ARC's tools handed the agent **3.4× fewer perception tokens** (8,310 vs 28,332). They made a similar number of calls (27 vs 23) and took similar wall time. ARC's edge is that every actionable element comes with a handle (`[#N]`) inside a hard ~1,200-token budget. Solari's `read_page` gives cheap text with no handles, or links and HTML that cost 2–7k tokens on real pages and still leave selector-building to the agent.

Raw data: `artifacts/benchmarks/vs_solari_mcp_20260926-215606.{json,md}` · Script: `scripts/benchmark_vs_solari_mcp.py`

## Setup

- Both servers ran over stdio exactly as an agent host runs them: `arc-cua-mcp` (backend `solari`) and `npx -y @solarisdk/mcp` v0.4.6 (`mode: "fast"`, captcha off).
- Both used **Solari cloud browsers** from the fast pool on the free plan, with the same 800×600 viewport.
- **No LLM.** Each task is a scripted policy standing in for an agent:
  - **ARC:** `arc_inspect`, then match the named `[#N]` line, then `arc_act` by index. On a truncated tree, it falls back to `arc_inspect(query=…)`.
  - **Solari:** `read_page` with `links` for link targets and `html` for form controls, then derive a `querySelector` selector with an HTML parser, then `solari_browser_click` / `_type`.
- Tokens are estimated as chars/4 for both. Verification reads are excluded from the counts.

## Part A — what one "look at the page" costs

| Page | ARC `arc_inspect` | ARC actionable (visible / total) | Solari `text` | Solari `links` | Solari `html` |
|---|---:|---:|---:|---:|---:|
| example.com | 82 | 1 / 1 | 37 | 42 | 101 |
| news.ycombinator.com | 1,224 (T) | 109 / 229 | 1,032 | 5,642 | 7,516 (T) |
| en.wikipedia.org/wiki/Web_browser | 1,239 (T) | 55 / 486 | 5,037 | 6,198 | 7,519 (T) |
| github.com/microsoft/playwright | 1,225 (T) | 46 / 265 | 2,635 | 6,160 | 7,519 (T) |
| docs.python.org asyncio | 1,239 (T) | 76 / 102 | 1,010 | 2,255 | 6,861 |
| MDN `<input>` | 1,061 (T) | 39 / 1,522 | 7,527 (T) | 6,683 | 7,528 (T) |
| httpbin.org/forms/post | 468 | 16 / 16 | 59 | 22 | 359 |
| todomvc (React) | 466 | 12 / 12 | 174 | 376 | 755 |

(T) = truncated by the tool. ARC truncates by evicting plain text first, then affordances. Solari truncates the output tail.

## Part B — grounding tasks

| Task | ARC | Solari | ARC calls / tokens | Solari calls / tokens |
|---|:-:|:-:|---:|---:|
| HN: click "new" | ✅ | ✅ | 3 / 1,224 | 3 / 5,642 |
| HN: 2nd "N comments" link | ✅ | ✅ | 3 / 1,224 | 3 / 5,642 |
| Wikipedia: search | ✅ | ❌ | 6 / 2,466 | 3 / 7,519 |
| httpbin: fill form, pick radio, submit | ✅ | ✅ | 5 / 468 | 5 / 359 |
| GitHub: Issues tab | ✅ | ✅ | 3 / 1,223 | 3 / 6,160 |
| Python docs: "Coroutines and tasks" | ✅ | ✅ | 3 / 1,239 | 3 / 2,255 |
| TodoMVC: add item | ✅ | ✅ | 4 / 466 | 3 / 755 |

**Why Solari missed Wikipedia:** at 800 px Wikipedia collapses its search box behind a button. The HTML still contains `input[name=search]`, so the HTML-reading policy typed into a box that wasn't visible. HTML doesn't show visibility. The accessibility tree does, so ARC's policy saw a `Search` button, clicked it, and re-inspected, at the cost of 3 extra calls.

## Caveats

- **Scripted policies, not agents.** I wrote both policies. The Solari side assumes an agent that can parse HTML and pick the right link among duplicates. A real LLM agent may do better (for example, screenshot and click coordinates) or worse. The token counts are what each tool *hands* the agent, which holds regardless of the policy.
- **Small sample:** 8 pages and 7 tasks, one run each, public sites that change daily.
- **Solari `text` is the cheapest read on most pages,** but it carries no handles. An agent that only needs to *read* content should use it.
- **ARC on MDN kept 39 of 1,522 affordances visible.** On very large pages the agent relies on `arc_inspect(query=…)` to reach the rest.
- **The first run of this benchmark found three ARC bugs, all fixed before this run:**
  - The cap overflowed to 6,746 tokens on MDN: links wrapping unnamed `<code>` elements blocked eviction.
  - JSON-escaped names (` `) made pages unreadable.
  - Evicted links couldn't be found by name, so `query` was added.

  It also found bugs in my own Solari policy (a Playwright-only selector, picking the wrong duplicate link).
- **Timing is roughly equal.** Both are dominated by Solari network round trips, about 1–2 s per call.
