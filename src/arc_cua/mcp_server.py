"""ARC MCP server: token-budgeted AXTree perception and verified actions for coding agents.

Complements Solari's own MCP server (`@solarisdk/mcp`) rather than replacing it: ARC adds a
hard-budgeted accessibility tree with announced truncation, `[#N]`-indexed actions, and
no-op/stall detection, over a local Chromium, a Solari cloud browser, or any CDP endpoint.

Run: `arc-cua-mcp` (stdio). Solari backend needs `SOLARI_API_KEY` in the server's env.

All Playwright calls run on one dedicated worker thread, because Playwright's sync API is
bound to the thread that started it and MCP tool handlers run on the event loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import dataclasses
import json
import logging
from typing import Any, Callable, Literal, Optional, Tuple

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from arc_cua.browser_session import BrowserSession, BrowserSessionError
from arc_cua.index_bridge import ArcIndexBridge, load_schema

logger = logging.getLogger("arc_cua.mcp_server")

INSTRUCTIONS = """\
ARC drives one browser for you and shows it as a compact accessibility tree.
Loop: arc_open(url) -> arc_inspect -> arc_act on a [#N] index -> read the tree arc_act returns -> arc_act again.
arc_act returns the page after the action (same format as arc_inspect) and its [#N] indices replace the old ones,
so there is no need to call arc_inspect between actions.
- arc_inspect output is capped (~1200 tokens). "Truncated" plus '# !' manifest lines means content was
  dropped: plain text goes first, links/buttons last. Indices named in '# !DROPPED-ACTIONABLE [#N]' still
  work with arc_act. On a truncated tree, find a named element with arc_inspect(query="words") before
  concluding it is absent.
- arc_act reports state_changed. stall_suspected=true means repeated actions did nothing: stop repeating,
  re-inspect, and pick a different element.
- page_changed_since_inspect=true means the page moved before the action ran; check the returned tree.
- arc_screenshot costs far more tokens than arc_inspect; use it only when the tree cannot show what you need
  (canvas, charts, images, visual layout). Its marks match [#N]; click unindexed spots with target='coords:X,Y'.
- backend='solari' uses a billed Solari cloud browser; call arc_close when done so it is released.
"""


class BrowserWorker:
    """Runs every BrowserSession call on a single thread (Playwright sync API is thread-affine)."""

    def __init__(self, session_factory: Callable[[], BrowserSession] = BrowserSession):
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="arc-browser")
        self._session_factory = session_factory
        self._session: Optional[BrowserSession] = None

    def _get(self) -> BrowserSession:
        if self._session is None:
            self._session = self._session_factory()
        return self._session

    async def call(self, method: str, **kwargs: Any) -> Any:
        """Invoke `BrowserSession.<method>(**kwargs)` on the worker thread."""
        def run() -> Any:
            return getattr(self._get(), method)(**kwargs)
        try:
            return await asyncio.wrap_future(self._pool.submit(run))
        except BrowserSessionError as e:
            raise ToolError(str(e)) from e
        except ToolError:
            raise
        except Exception as e:
            # Config errors (e.g. missing SOLARI_API_KEY), Solari API errors, Playwright timeouts.
            raise ToolError(f"{type(e).__name__}: {e}") from e

    async def run(self, fn: Callable[[BrowserSession], Any]) -> Any:
        """Run `fn(session)` on the worker thread (for multi-call sequences like the index bridge)."""
        try:
            return await asyncio.wrap_future(self._pool.submit(lambda: fn(self._get())))
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"{type(e).__name__}: {e}") from e

    async def shutdown(self) -> None:
        """Close the browser (releasing any Solari session) and stop the worker thread."""
        if self._session is not None:
            with contextlib.suppress(Exception):
                await asyncio.wrap_future(self._pool.submit(self._session.shutdown))
        self._pool.shutdown(wait=False)


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)


def _default_pageindex_client(mode: str = "local") -> Any:
    """Build a PageIndex client lazily; `pageindex` is an optional dependency.

    local: documents indexed on this machine, models via LiteLLM (ARC_INDEX_MODEL, default
    gemini/gemini-3.8-flash with GEMINI_API_KEY). cloud: PAGEINDEX_API_KEY.
    """
    import os
    from arc_cua.pageindex_adapter import DEFAULT_MODEL, PageIndexAdapter
    try:
        from pageindex import PageIndexClient  # type: ignore
    except ImportError as e:
        raise ToolError("PageIndex is not installed: pip install pageindex") from e
    model = os.environ.get("ARC_INDEX_MODEL", DEFAULT_MODEL)
    client = PageIndexClient(index="cloud", chat_model=model) if mode == "cloud" else PageIndexClient(model=model)
    return PageIndexAdapter(client)


def build_server(
    worker: Optional[BrowserWorker] = None,
    pageindex_factory: Callable[[str], Any] = _default_pageindex_client,
) -> Tuple[MCPServer, BrowserWorker]:
    """Create the MCP server and the worker that owns its browser."""
    worker = worker or BrowserWorker()
    _pi: dict = {}

    def pageindex(mode: str = "local") -> Any:
        if mode not in _pi:
            _pi[mode] = pageindex_factory(mode)
        return _pi[mode]

    @contextlib.asynccontextmanager
    async def lifespan(_server: MCPServer):
        try:
            yield {}
        finally:
            await worker.shutdown()

    server = MCPServer("arc", instructions=INSTRUCTIONS, lifespan=lifespan)

    @server.tool()
    async def arc_open(
        url: Optional[str] = None,
        backend: Literal["local", "solari", "cdp"] = "local",
        cdp_url: Optional[str] = None,
        visible: bool = False,
        stealth: bool = False,
    ) -> str:
        """Open a browser (or reuse the open one) and optionally navigate to a URL.

        backend: 'local' launches Chromium on this machine; 'solari' provisions a Solari cloud
        browser (needs SOLARI_API_KEY; billed until arc_close); 'cdp' attaches to cdp_url.
        visible: show a local browser window. stealth: Solari stealth mode (paid plans).
        Switching backend or visibility closes the current browser and loses its page state.
        """
        return _json(await worker.call("open", url=url, backend=backend, cdp_url=cdp_url,
                                       visible=visible, stealth=stealth))

    @server.tool()
    async def arc_inspect(settle_ms: float = 5000.0, query: Optional[str] = None) -> str:
        """Return the current page as a token-budgeted accessibility tree with [#N] action indices.

        Waits up to settle_ms for client-rendered pages to hydrate. Lines like
        `[#3] button "Submit" css="#submit"` are actionable; pass 3 as arc_act's index.
        query: instead of the tree, list every link/button/field on the whole page whose
        role or name contains all these words - including ones a truncated tree dropped.
        """
        result = await worker.call("inspect", settle_ms=settle_ms, query=query)
        return f"URL: {result['url']}\n{result['text']}"

    @server.tool()
    async def arc_act(
        action: str,
        index: Optional[int] = None,
        target: Optional[str] = None,
        value: Optional[str] = None,
        timeout_ms: float = 5000.0,
        observe: bool = True,
    ) -> str:
        """Perform one action, report whether it changed the page, and return the page after it.

        action: click, dblclick, fill, type, select, press_key, scroll, wait, goto.
        index: [#N] from the last arc_inspect (preferred). target: CSS/XPath selector instead,
        or 'coords:X,Y' (page CSS pixels read off arc_screenshot) for click on unindexed content.
        value: text for fill/type, option for select, key for press_key (e.g. 'Enter'), URL for goto.
        Check state_changed and stall_suspected in the result before continuing.
        observe: append the post-action tree (as arc_inspect would return it); its [#N]
        indices become current. Set false to save tokens when chaining blind actions.
        """
        result = await worker.call("act", action=action, index=index, target=target,
                                   value=value, timeout_ms=timeout_ms, observe=observe)
        tree = result.pop("tree", None)
        return _json(result) + (f"\n\n{tree}" if tree else "")

    @server.tool()
    async def arc_screenshot(marks: bool = True, full_page: bool = False) -> list:
        """Screenshot the page (JPEG, CSS pixels). Use when arc_inspect cannot show something:
        canvas/WebGL, charts, images, or to check layout.

        marks: outline each [#N] from the last arc_inspect with its number (runs arc_inspect
        first if none). Image coordinates are page CSS pixels: to click something with no
        [#N], call arc_act(action='click', target='coords:X,Y').
        full_page: capture the whole scrollable page instead of the viewport.
        """
        shot = await worker.call("screenshot", marks=marks, full_page=full_page)
        caption = {k: shot[k] for k in ("url", "viewport", "full_page", "marked") if k in shot}
        if shot.get("note"):
            caption["note"] = shot["note"]
        return [Image(data=shot["image"], format=shot["format"]), _json(caption)]

    @server.tool()
    async def arc_index_document(file_path: str, mode: Literal["local", "cloud"] = "local") -> str:
        """Ingest a PDF/document into PageIndex (vectorless tree index). Returns doc_id and the tree structure."""
        def go() -> dict:
            bridge = ArcIndexBridge(pageindex(mode))
            doc_id = bridge.ingest_document(file_path)
            return {"doc_id": doc_id, "structure": bridge.pageindex.get_document_structure(doc_id)}
        try:
            return _json(await asyncio.to_thread(go))
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"{type(e).__name__}: {e}") from e

    @server.tool()
    async def arc_index_query(doc_id: str, query: str, target_fields: Optional[list[str]] = None) -> str:
        """Reasoning search over a document tree; answers carry <cite doc page block/> citations.

        target_fields: optional field names to extract, one `name: value <cite/>` line each.
        """
        prompt = query
        if target_fields:
            prompt += ("\nReturn one line per field as `field_name: value <cite doc=\"..\" page=\"..\" block=\"..\"/>`: "
                       + ", ".join(target_fields))
        try:
            text = await asyncio.to_thread(lambda: pageindex().chat(prompt, doc_id=doc_id))
        except Exception as e:
            raise ToolError(f"{type(e).__name__}: {e}") from e
        cites = [c.to_dict() for c in ArcIndexBridge.parse_citations(text, doc_id)]
        return _json({"answer": text, "citations": cites})

    @server.tool()
    async def arc_doc_to_action(
        doc_id: str,
        schema_id: str = "healthcare_denial_appeal",
        auto_submit: bool = False,
    ) -> str:
        """Extract fields from an indexed document, bind them to the open page's form, fill it, and
        return an audit receipt. schema_id: healthcare_denial_appeal, invoice_entry, or a JSON path.
        Open the target form with arc_open first. Nothing is submitted unless auto_submit=true."""
        try:
            schema = load_schema(schema_id)
            fields = await asyncio.to_thread(
                lambda: ArcIndexBridge(pageindex()).extract_action_fields(doc_id, schema))
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"{type(e).__name__}: {e}") from e
        ungrounded = [f.field_name for f in fields if not f.validation_status]

        def go(session: BrowserSession) -> dict:
            bridge = ArcIndexBridge(pageindex(), session)
            inspect = session.inspect()
            steps = bridge.bind_to_axtree(fields, schema, inspect["text"], inspect["action_index_map"])
            receipt = bridge.execute_and_verify(steps, schema, submit=auto_submit)
            return {"receipt": dataclasses.asdict(receipt), "ungrounded_fields": ungrounded,
                    "unbound_fields": sorted({f.field_name for f in fields if f.validation_status}
                                             - {s.source_field for s in steps})}
        return _json(await worker.run(go))

    @server.tool()
    async def arc_close() -> str:
        """Close the browser. Releases a Solari session (stops billing); safe to call twice."""
        return _json(await worker.call("close"))

    return server, worker


def main() -> None:
    """Console entry point: serve over stdio."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    server, _ = build_server()
    server.run("stdio")


if __name__ == "__main__":
    main()
