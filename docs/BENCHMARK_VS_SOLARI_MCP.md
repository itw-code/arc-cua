# ARC MCP vs Solari MCP — head-to-head (2026-09-27)

**Verdict:**
- **Success and tokens:** on 7 grounding tasks ARC succeeded on 7 and Solari's official MCP on 6, and ARC's tools handed the agent **3.3× fewer perception tokens** (8,444 vs 28,264).
- **Protocol traffic:** ARC sent **10× fewer DevTools Protocol commands** (391 vs 3,868).
- **Speed:** ARC is **not faster**. Its counted tool calls took 99 s in total against Solari's 78 s.
- **Real model:** a small, fast LLM driving ARC through a coding agent succeeded on all 13 task runs that reached the model, in 3–5 tool calls and about 25 s each.

Raw data:
- Head-to-head: `artifacts/benchmarks/vs_solari_mcp_20260927-030500.{json,md}`
- LLM agent: `artifacts/benchmarks/llm_agent_omp_20260927-032435.{json,md}` and `llm_agent_omp_20260927-033001.{json,md}`
- Scripts: `scripts/benchmark_vs_solari_mcp.py`, `scripts/benchmark_llm_agent_omp.py`
- Previous run (no timing): `vs_solari_mcp_20260926-215606`

## Setup

- Both servers ran over stdio exactly as an agent host runs them: `arc-cua-mcp` (backend `solari`) and `npx -y @solarisdk/mcp` v0.4.6 (`mode: "fast"`, captcha off).
- Both used **Solari cloud browsers** from the fast pool on the free plan, with the same 800×600 viewport.
- **Parts A and B use no LLM.** Each task is a scripted policy standing in for an agent:
  - **ARC:** `arc_inspect`, then match the named `[#N]` line, then `arc_act` by index. On a truncated tree it falls back to `arc_inspect(query=…)`. When the policy needs the next page (Wikipedia's search toggle), it reads the tree `arc_act` returns instead of calling `arc_inspect` again.
  - **Solari:** `read_page` with `links` for link targets and `html` for form controls, then derive a `querySelector` selector with an HTML parser, then `solari_browser_click` / `_type`.
- **How things are measured:**
  - Tokens are estimated as chars/4 for both.
  - **s** is the summed latency of the counted tool calls.
  - **CDP** is the number of DevTools Protocol commands those calls sent. It is counted from each server's own debug log (Playwright `pw:protocol`, Puppeteer `puppeteer:protocol:SEND`).
  - Verification reads are excluded from every count.

## Part A — what one "look at the page" costs

| Page | ARC `arc_inspect` | ARC actionable (visible / total) | Solari `text` | Solari `links` | Solari `html` |
|---|---:|---:|---:|---:|---:|
| example.com | 82 | 1 / 1 | 37 | 42 | 101 |
| news.ycombinator.com | 1,240 (T) | 109 / 230 | 1,049 | 5,608 | 7,516 (T) |
| en.wikipedia.org/wiki/Web_browser | 1,239 (T) | 55 / 486 | 5,495 | 6,527 | 7,519 (T) |
| github.com/microsoft/playwright | 1,225 (T) | 46 / 265 | 2,673 | 6,160 | 7,519 (T) |
| docs.python.org asyncio | 1,239 (T) | 76 / 102 | 1,010 | 2,255 | 6,861 |
| MDN `<input>` | 1,072 (T) | 39 / 1,522 | 7,527 (T) | 6,683 | 7,528 (T) |
| httpbin.org/forms/post | 468 | 16 / 16 | 59 | 22 | 359 |
| todomvc (React) | 466 | 12 / 12 | 174 | 376 | 755 |

(T) = truncated by the tool. ARC truncates by evicting plain text first, then affordances. Solari truncates the output tail.

## Part B — grounding tasks

| Task | ARC | Solari | ARC calls / tokens / s / CDP | Solari calls / tokens / s / CDP |
|---|:-:|:-:|---:|---:|
| HN: click "new" | ✅ | ✅ | 3 / 1,240 / 9.6 / 41 | 3 / 5,608 / 9.0 / 964 |
| HN: 2nd "N comments" link | ✅ | ✅ | 3 / 1,240 / 9.1 / 41 | 3 / 5,608 / 9.2 / 964 |
| Wikipedia: search | ✅ | ❌ | 5 / 2,568 / 24.4 / 83 | 3 / 7,519 / 15.3 / 70 |
| httpbin: fill form, pick radio, submit | ✅ | ✅ | 5 / 468 / 22.0 / 98 | 5 / 359 / 16.5 / 84 |
| GitHub: Issues tab | ✅ | ✅ | 3 / 1,223 / 13.1 / 42 | 3 / 6,160 / 10.5 / 1,395 |
| Python docs: "Coroutines and tasks" | ✅ | ✅ | 3 / 1,239 / 11.1 / 41 | 3 / 2,255 / 8.6 / 347 |
| TodoMVC: add item | ✅ | ✅ | 4 / 466 / 9.7 / 45 | 3 / 755 / 9.0 / 44 |
| **Total** | **7/7** | **6/7** | **26 / 8,444 / 99.0 / 391** | **23 / 28,264 / 78.1 / 3,868** |

**Why Solari missed Wikipedia:** at 800 px Wikipedia collapses its search box behind a button. The HTML still contains `input[name=search]`, so the HTML-reading policy typed into a box that wasn't visible. HTML doesn't show visibility. The accessibility tree does, so ARC's policy clicked the `Search` button and read the tree that click returned.

**Where the CDP gap comes from:** Solari's `read_page("links")` walks the DOM from the client, which costs 964 commands on Hacker News and 1,395 on GitHub. Its `html` read is cheap (44–84 commands per task), about the same as ARC. ARC fetches the whole accessibility tree in a few commands.

**Why ARC is still slower on Solari:** each round trip to a Solari browser costs roughly 0.2–1 s, and ARC spends its time in two places:
- **`arc_inspect` waits for the page to settle.** It waits for network idle (bounded) so client-rendered pages aren't read half-hydrated. That wait is what caught the Wikipedia toggle and TodoMVC correctly, but it costs time on static pages.
- **`arc_act` extracts the tree before and after every action.** That's how it detects no-ops and returns the new page.

Removing the pre-action extraction when the agent has just inspected is the next optimisation.

## Part C — a real LLM agent on ARC

The same 7 tasks, but a model chooses every action.
- **Model:** `laguna-xs-2-1:free` via the Kenari provider. It was picked for speed: in a quick check (`omp bench`, 4 runs each) its time to first token was 1.7 s median and 2.5 s p95, the steadiest of the free fast models tried. Gemma 4 31B ranged from 1.1 s to 14 s.
- **Agent:** the Oh My Pi coding agent (`omp`) with its built-in tools disabled, so `arc-cua-mcp` was its only tool set.
- **Browser:** local headless Chromium. Every omp run starts a fresh ARC server, which on Solari would be a separately billed session per task.
- **Instructions:** a short system prompt telling the model to read the tree that `arc_act` returns instead of re-inspecting.
- **Runs:** two, after the fixes below.

| Task | Run 1 | Run 2 | Wall s (1 / 2) | Model s (1 / 2) | Tool s (1 / 2) | Tool calls |
|---|:-:|:-:|---:|---:|---:|---:|
| HN: click "new" | ✅ | ✅ | 18.2 / 27.2 | 5.0 / 14.9 | 7.6 / 7.3 | 3 |
| HN: 2nd "N comments" link | ✅ | ⚠️ | 25.5 / — | 12.9 / — | 7.1 / — | 3 |
| Wikipedia: search | ✅ | ✅ | 34.3 / 30.7 | 15.3 / 13.1 | 13.9 / 12.7 | 4 |
| httpbin: fill form, pick radio, submit | ✅ | ✅ | 180.3 / 25.5 | 169.4 / 13.4 | 7.2 / 7.1 | 5 |
| GitHub: Issues tab | ✅ | ✅ | 23.2 / 23.0 | 7.4 / 8.0 | 11.7 / 10.9 | 3 |
| Python docs: "Coroutines and tasks" | ✅ | ✅ | 26.1 / 24.7 | 12.1 / 11.0 | 9.6 / 9.1 | 4 |
| TodoMVC: add item | ✅ | ✅ | 17.8 / 20.8 | 8.4 / 10.2 | 5.9 / 5.6 | 4 |

- **13/13 runs that reached the model succeeded.** ⚠️ marks the one that never started: the free Kenari tier rejected the request (`400 upstream_rejected`) before any tool call.
- **Typical run:** about 25 s wall time and 3–5 tool calls. That breaks down into about 11–13 s of model time over 4–6 turns, 6–14 s of ARC tool time, and about 4–5 s of omp start-up plus Chromium launch.
- **Outlier:** httpbin's 180 s in run 1 is a single model turn that took minutes on the provider side; ARC's tool time was a normal 7 s.
- **Pattern:** in every run the model called `arc_inspect` once per page (plus one `query` on the Python docs, whose target link was evicted from the truncated tree) and then acted on the trees `arc_act` returned.

**What the model runs exposed and fixed** (before these two runs):
- **Stale trees after client-side navigation.** On GitHub the model clicked the right `[#19] link "Issues 159"`, but the tree `arc_act` returned was still the repository page: GitHub changes the URL before it renders, and network idle doesn't reset for in-page navigations.
  - Before the fix, one run fell back to typing the URL with `goto`, and another spent 10 calls re-querying.
  - `arc_act` now waits briefly for a clicked link to change the URL, then polls until the tree differs from the post-click snapshot and holds steady for two reads.
  - GitHub now takes 3 calls in both runs.

For scale, Browser Use's Jev reports a Google Flights search in 7.1 s, with one model call choosing both the operation and the element. ARC with a general chat model needs one model turn per action (2–3 s each here) plus ARC's own settle waits. Those are the two gaps.

## Caveats

- **Parts A and B are scripted policies, not agents.** I wrote both policies. The Solari side assumes an agent that can parse HTML and pick the right link among duplicates. The token and CDP counts are what each tool *hands* the agent or sends to the browser, which holds regardless of the policy.
- **Part C is one model, two runs per task.** It ran on a local browser, not Solari, and success was judged from the agent's own tool results (the last URL or tree ARC reported).
- **Small sample:** 8 pages and 7 tasks on public sites that change daily.
- **Solari `text` is the cheapest read on most pages,** but it carries no handles. An agent that only needs to *read* content should use it.
- **ARC on MDN kept 39 of 1,522 affordances visible.** On very large pages the agent relies on `arc_inspect(query=…)` to reach the rest.
