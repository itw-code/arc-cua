# ARC Index: Vectorless Document-to-Action Engine
## Comprehensive Architecture Specification & Executable Implementation Plan

> **Product Definition:** ARC Index couples VectifyAI's vectorless reasoning RAG (**PageIndex**) with the **ARC** in-VM reflex runtime on **Solari** ephemeral cloud browsers.
> **Mission:** Transform complex, un-API'd enterprise documents (denial letters, clinical charts, EOBs, vendor invoices, regulatory filings) into citation-backed, state-verified browser and desktop actions without manual data entry.
> **Repository:** `solari-hybrid-cua` · **Target Package:** `src/arc_cua/index_bridge.py` · **Timeline:** 24–48 Hours to Production.

---

## 1. Executive Vision & Value Proposition

### 1.1 The Enterprise Document-to-Action Bottleneck
Enterprise automation breaks down at the boundary between **unstructured documents** and **legacy software portals**:
- **Healthcare Revenue Cycle (Pinetree Health fit):** Over $19.7B is lost annually to claim denials. Denial explanations arrive as 10–50 page unstructured PDFs, EOBs, and scanned clinical summaries. To contest them, billing teams spend 20+ minutes per claim manually transcribing CPT codes, denial justification clauses, and patient IDs into payer portals (e.g. UnitedHealthcare, Availity, Epic).
- **Financial & Accounts Payable Operations:** Invoices arrive as PDFs with arbitrary layouts, multi-currency tables, and fine-print payment terms. Transcribing them into ERP portals (SAP, NetSuite, Coupa) requires human cross-referencing.

### 1.2 Why Existing Stacks Fail

| Approach | Architecture | Failure Mode |
|---|---|---|
| **Vector RAG + Selenium/RPA** | Chunking + Semantic Vector DB + Brittle CSS selectors | Similarity $\neq$ Relevance. Chunking slices tables and fine-print across arbitrary boundaries. RPA breaks on any DOM mutation. |
| **Monolithic Multimodal VLM (GPT-4o / Sonnet)** | Streaming 50-page PDF images + Full visual CUA loop | **The Vision Tax:** $0.48–$1.50 per task, 2,500ms step latency. High hallucination rate on numerical codes (CPT codes, tax IDs) because the model lacks explicit citation anchors. |
| **ARC Index (This Plan)** | **Hierarchical Document Tree (PageIndex)** + **In-VM Reflex Runner (ARC)** | **Zero vector DB, zero chunking.** Tree reasoning locates exact block-level citations ($0.001/page). ARC reflex loop enters data on Solari cloud browsers in sub-10ms steps ($0.0015/task), verified against ground truth. |

---

## 2. System Architecture & Information Flow

```text
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │                      COGNITIVE DOCUMENT TIER (PageIndex)                    │
 │                                                                             │
 │   Incoming PDF / Document                                                   │
 │             │                                                               │
 │             ▼                                                               │
 │   PageIndexClient (Flash / Cloud) ──► Hierarchical Tree Structure           │
 │                                       (Headers, Sections, Tables, Pages)    │
 │                                             │                               │
 │                                             ▼                               │
 │   LLM Reasoning Tree-Search       ──► Extracted Action Fields               │
 │   (Zero-Chunking, Exact Citations)     with <cite doc=.. page=.. block=../> │
 └─────────────────────────────────────┬───────────────────────────────────────┘
                                       │
                                       ▼
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │                    ACTION COMPILATION BRIDGE (index_bridge.py)              │
 │                                                                             │
 │   DocumentActionSchema ───────────► Field Validation & Grounding Check      │
 │   (Target Form Contract)            (Assert each field has citation anchor) │
 │                                             │                               │
 │                                             ▼                               │
 │   Active Browser AXTree   ────────► Form Affordance Binder                  │
 │   (Monotonic [#N] Affordances)      (Maps Field -> [#N] Input Element)      │
 │                                             │                               │
 │                                             ▼                               │
 │                                     Compiled ActionStep Sequence            │
 └─────────────────────────────────────┬───────────────────────────────────────┘
                                       │
                                       ▼
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │                      ACTUATION TIER (ARC on Solari)                         │
 │                                                                             │
 │   Solari Ephemeral MicroVM Cloud Browser (Cloud Hypervisor, 8ms startup)    │
 │             │                                                               │
 │             ▼                                                               │
 │   Sub-10ms In-VM Reflex Runner (PlaywrightExecutor via backendNodeId pins)  │
 │   - FILL [#3] with "CPT-99214" (derived from doc p.2 block 4)               │
 │   - SELECT [#7] option "CO-16"                                              │
 │   - CLICK [#12] "Submit Appeal"                                             │
 └─────────────────────────────────────┬───────────────────────────────────────┘
                                       │
                                       ▼
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │                    GROUND-TRUTH VERIFICATION & AUDIT TIER                   │
 │                                                                             │
 │   SimHash StateVerifier (64-bit Hamming Delta != 0, state_changed=True)     │
 │   Receipt Scraper: Captures Confirmation Number & Generates Audit JSON      │
 │   (Immutable proof: Document Citation <──► Form Input <──► Submission Ref)  │
 └─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Data Models & Interface Contracts

The bridge module `src/arc_cua/index_bridge.py` establishes formal, typed dataclasses ensuring end-to-end auditability.

### 3.1 Schemas & Data Structures

```python
from __future__ import annotations
import dataclasses
from typing import Any, Dict, List, Optional, Union

@dataclasses.dataclass(frozen=True)
class CitationAnchor:
    """Verifiable proof linking an extracted value back to source document bytes."""
    document: str
    page: int
    block_id: Optional[str] = None
    text_snippet: Optional[str] = None
    confidence: float = 1.0

@dataclasses.dataclass
class ExtractedField:
    """A single semantic value extracted via PageIndex tree reasoning."""
    field_name: str
    value: Any
    citation: CitationAnchor
    normalized_str: str = ""
    validation_status: bool = True
    error_message: Optional[str] = None

@dataclasses.dataclass
class FieldTarget:
    """Specification of how a document field binds to a portal form affordance."""
    field_name: str
    target_role: str                     # e.g., "textbox", "combobox", "radio"
    label_hints: List[str]               # e.g., ["Claim ID", "Claim #", "Reference Number"]
    input_verb: str = "FILL"             # "FILL", "TYPE", "SELECT", "CLICK"
    required: bool = True
    format_transformer: Optional[str] = None  # e.g., "date_iso", "strip_dashes"

@dataclasses.dataclass
class DocumentActionSchema:
    """Contract defining the target form requirements."""
    schema_id: str
    target_url_pattern: str
    fields: List[FieldTarget]
    submit_affordance_hint: str = "Submit"
    success_indicator_pattern: str = "Confirmation|Receipt|Success|Reference"

@dataclasses.dataclass
class CompiledActionStep:
    """An executable UI reflex action bound to a verified document citation."""
    action_index: int                    # Target [#N] from current AXTree
    verb: str                            # CLICK, FILL, TYPE, SELECT
    value: Optional[str]                 # Value to insert
    source_field: str                    # Name of the originating document field
    citation: CitationAnchor             # Proof anchor
    expected_state_change: bool = True

@dataclasses.dataclass
class VerificationReceipt:
    """Immutable transaction log proving verified document-to-action execution."""
    receipt_id: str
    document_name: str
    schema_id: str
    fields_submitted: Dict[str, Any]
    citations: List[CitationAnchor]
    submission_confirmation_code: Optional[str]
    state_changed: bool
    hamming_distance: int
    wall_time_ms: float
    solari_session_id: Optional[str]
    timestamp: str
```

---

## 4. Bridge Implementation (`src/arc_cua/index_bridge.py`)

The bridge connects `pageindex.client.PageIndexClient` with `arc_cua.browser_session.BrowserSession`.

```python
class ArcIndexBridge:
    """Orchestrates document tree reasoning and compiles actions into ARC reflex loops."""

    def __init__(self, pageindex_client: Any, browser_session: Any):
        self.pageindex = pageindex_client
        self.browser = browser_session

    def ingest_document(self, file_path: str, wait: bool = True) -> str:
        """Submit document to PageIndex and generate hierarchical tree index."""
        result = self.pageindex.submit_document(file_path, wait=wait)
        return result["doc_id"]

    def extract_action_fields(
        self,
        doc_id: str,
        schema: DocumentActionSchema,
    ) -> List[ExtractedField]:
        """Reason over the PageIndex document tree to extract fields with citations."""
        # 1. Inspect tree structure first (PageIndex tree reasoning)
        structure = self.pageindex.get_document_structure(doc_id)
        
        # 2. Formulate grounded extraction query
        prompt = (
            f"Extract the following fields according to schema '{schema.schema_id}':\n"
            + "\n".join(f"- {f.field_name} ({f.target_role}): hints={f.label_hints}" for f in schema.fields)
            + "\nFor every field, provide the exact value and cite the exact page and block."
        )
        response_text = self.pageindex.chat(prompt, doc_id=doc_id)
        
        # 3. Parse <cite doc=... page=... block=.../> tags and match to schema
        return self._parse_extracted_fields(response_text, schema)

    def bind_to_axtree(
        self,
        extracted_fields: List[ExtractedField],
        schema: DocumentActionSchema,
    ) -> List[CompiledActionStep]:
        """Inspect active ARC AXTree and map extracted fields to visible [#N] affordances."""
        inspect_result = self.browser.inspect()
        axtree_text = inspect_result["text"]
        action_map = self.browser.index_map  # Maps index N -> {role, name, backend_dom_id, etc.}
        
        compiled_steps: List[CompiledActionStep] = []
        for field in extracted_fields:
            target_spec = next((f for f in schema.fields if f.field_name == field.field_name), None)
            if not target_spec:
                continue
            
            # Find best matching [#N] affordance using label hints and role
            matched_idx = self._find_matching_affordance(target_spec, action_map)
            if matched_idx is None:
                if target_spec.required:
                    raise ValueError(f"Required form field '{field.field_name}' not found in active AXTree.")
                continue

            compiled_steps.append(CompiledActionStep(
                action_index=matched_idx,
                verb=target_spec.input_verb,
                value=str(field.value),
                source_field=field.field_name,
                citation=field.citation,
            ))
        return compiled_steps

    def execute_and_verify(
        self,
        steps: List[CompiledActionStep],
        schema: DocumentActionSchema,
    ) -> VerificationReceipt:
        """Dispatch compiled steps through ARC's sub-10ms reflex loop and verify end state."""
        results = []
        for step in steps:
            res = self.browser.act(
                action=step.verb,
                index=step.action_index,
                value=step.value,
                observe=True,  # Refresh AXTree on action return
            )
            results.append(res)
            if not res["state_changed"] and res["verb"] in ("FILL", "TYPE", "SELECT"):
                # Detect stall or ineffective action immediately
                pass

        # Submit form if indicated
        # Verify receipt code and state divergence
        return self._generate_receipt(schema, steps, results)
```

---

## 5. New MCP Tool Surface (`arc-cua-mcp`)

To expose ARC Index to coding agents (Claude Code, Oh My Pi, Cursor), `src/arc_cua/mcp_server.py` is extended with 3 complementary tools:

```yaml
# New MCP Tools in arc-cua-mcp
- arc_index_document:
    description: "Ingest a complex PDF/document into PageIndex and return its hierarchical tree index."
    parameters:
      file_path: { type: string, description: "Local path to document" }
      mode: { type: string, enum: ["local", "cloud"], default: "local" }

- arc_index_query:
    description: "Perform reasoning search over a document tree to extract structured values with block-level citations."
    parameters:
      doc_id: { type: string }
      query: { type: string }
      target_fields: { type: array, items: { type: string }, description: "Optional list of specific field names" }

- arc_doc_to_action:
    description: "Autonomous composite pipeline: extracts document fields via PageIndex, binds to current portal form, executes via reflex loop, and returns audit receipt."
    parameters:
      doc_id: { type: string }
      schema_id: { type: string, enum: ["healthcare_denial_appeal", "invoice_entry", "custom"] }
      auto_submit: { type: boolean, default: false }
```

---

## 6. Concrete 4-Phase Implementation Timeline (24–48 Hours)

```text
 HOURS 00:00 ──────────────────────────────────────────────► 48:00
 ┌─────────────────┬──────────────────┬─────────────────┬─────────────────┐
 │    PHASE 1      │     PHASE 2      │     PHASE 3     │     PHASE 4     │
 │  Core Bridge    │   MCP Server     │  RCM Denial &   │  Test Suite &   │
 │  & PageIndex    │   Tool Suite     │  Invoice Flows  │  Solari Live    │
 │  (Hours 00-08)  │  (Hours 08-16)   │  (Hours 16-30)  │  (Hours 30-48)  │
 └─────────────────┴──────────────────┴─────────────────┴─────────────────┘
```

### Phase 1: Core Bridge & Local Ingestion (Hours 00–08)
- Install and configure `pageindex` SDK in `solari-hybrid-cua`.
- Implement `src/arc_cua/index_bridge.py` dataclasses (`CitationAnchor`, `ExtractedField`, `DocumentActionSchema`).
- Build unit tests validating `_parse_extracted_fields` using synthetic PageIndex citation outputs.

### Phase 2: ARC MCP Tool Suite Expansion (Hours 08–16)
- Register `arc_index_document`, `arc_index_query`, and `arc_doc_to_action` in `src/arc_cua/mcp_server.py`.
- Thread-affine worker dispatch ensuring PageIndex calls and Playwright browser calls do not deadlock event loops.
- Add `--settle-ms` support for dynamic forms rendering post-input.

### Phase 3: Domain Workflow Implementations (Hours 16–30)
- **Pinetree Health RCM Template (`schemas/rcm_denial.json`):**
  - Input: Insurance Denial Letter PDF (Claim Number, Patient Name, Date of Service, Billed Amount, Denial Code `CO-16`, CPT `99214`, Appeal Ground).
  - Target: Mock/Live Payer Portal Appeal Form.
- **Financial Invoice Template (`schemas/invoice_ap.json`):**
  - Input: Vendor Invoice PDF (Vendor Name, Tax ID, Invoice Date, Line Items, Net 30 Terms).
  - Target: Accounts Payable Web Form.

### Phase 4: Test Suite, Benchmark Harness, and Solari Live Verification (Hours 30–48)
- End-to-end automated test in `tests/test_index_bridge.py`:
  - Ingests mock denial PDF fixture.
  - Launches local Chromium or Solari cloud browser.
  - Resolves target form and asserts `state_changed=True` on each field.
  - Checks `VerificationReceipt` contains exact page and block citations.
- Solari live cloud run (`SOLARI_LIVE_TESTS=1`) validating zero protocol leakage and sub-second execution.

---

## 7. Unit Economics & Latency Analysis

| Architecture | Document Cost (30-page PDF) | Form Actuation Cost | Total Cost / Task | Step Latency | Citation Auditability |
|---|---|---|---|---|---|
| **Monolithic VLM (GPT-4o)** | ~$0.30 (vision tokens) | ~$0.48 (20 steps @ $0.024/step) | **$0.7800** | 2,500+ ms | ❌ None (Opaque) |
| **Vector RAG + Selenium** | ~$0.02 (embeddings) | $0.00 (local scripts) | **$0.0200** | 1,200 ms | ⚠️ Fragile (Chunk boundary) |
| **ARC Index (PageIndex + ARC)** | **$0.0300** ($0.001/page tree index) | **$0.0015** (sub-10ms ARC reflex) | **$0.0315** | **2.31 ms** (reflex) | ✅ **100% Block-Level Citation** |

**Net Improvement:** **95.9% cost reduction** against frontier visual agents with **verifiable, audit-proof execution**.

---

## 8. Immediate Action Items
Status as of 2026-09-29:
1. ~~Commit `docs/ARC_INDEX_PLAN.md` to repository documentation index.~~ Done.
2. ~~Build runnable demonstration script `scripts/demo_arc_index_pipeline.py`.~~ Done.
3. ~~Verify local pipeline execution against simulated denial letter fixture.~~ Done, and run live on
   Solari with real PageIndex (Section 9).
4. **Open:** fix the grounding weakness (Section 10). This is the first task of the next session.

---

## 9. Live Test: Documents, Method, and Results (2026-09-29)

This section replaces an earlier draft that listed six documents as "verified". On checking,
one URL was dead (Medi-Cal RAD: the server returns an empty reply, and the old script wrote a
45-byte placeholder PDF and still printed PASS). The CMS ground truth listed values that are
not in the file (`140001`, `CO-45`, `CO-16`, `MA01`). The Patient Advocate Foundation file is a
1-page fill-in-the-blanks template (`[Member ID #]`), not a 4-page appeal. The SEC,
RVL-CDIP and EDGAR documents were never downloaded. None of those are used below.

### 9.1 Documents

| Case | Document | Why | Ground truth |
|---|---|---|---|
| `SYN-DENIAL-01` | `tests/fixtures/documents/synthetic_denial_letter.pdf`: 4-page fictitious denial notice rendered by `scripts/make_denial_fixture.py` | Facts spread across pages (claim p.1, CPT line p.2, CARC p.3, appeal ground p.4) with decoys: a second claim number and a paid CPT line on the same date | Known by construction: `synthetic_denial_letter.truth.json` |
| `MED-CMS-01` | CMS FISS Standard Paper Remittance Advice example (public, [cms.gov](https://www.cms.gov/Outreach-and-Education/Medicare-Learning-Network-MLN/MLNGenInfo/Downloads/FISS-SPR-Example.pdf)), fetched by `scripts/fetch_document_corpus.py` | Real payer layout: fixed-width columns under numbered headers; pypdf flattens the rows | Read off the file's own text: `cms_fiss_spr_example.truth.json` (SMITH J, MBI `1EG4TE5MK72`, 10/01–10/31/2018, RC 29/N211 and RC 16/MA18) |

### 9.2 Method (`scripts/run_arc_index_live.py`, `scripts/eval_extraction.py`)

1. **Index + extract**, two interchangeable indexes (`--index`):
   - `pageindex`: the real PageIndex SDK (`pageindex==0.2.20`) in local mode through
     `arc_cua.pageindex_adapter`. Flash builds the tree from the PDF layout; summaries and the
     answering agent run on Gemini 3.8 Flash via LiteLLM. No PageIndex account needed. Local
     documents have no layout blocks, so **citations are page-level**.
   - `blocks`: `arc_cua.local_doc_index`, pypdf text split into line blocks (`p2_b5`) and one
     Gemini call. Block-level citations, but no tree: only for documents that fit one prompt.
   Schema fields carry a `description` of how *documents* label the field (RC/REM columns,
   "the denied line item"), separate from the portal's `label_hints`. It contains no expected values.
2. **Grounding check.** `ArcIndexBridge.verify_grounding` asks the index to confirm each citation
   (`verify_citation`): the cited block, or the cited page, must contain the value. Anything else,
   and every `NOT_FOUND`, is rejected and never typed.
3. **Portal.** A small appeal form loaded with `page.set_content` (BrowserSession rejects `data:`
   URLs on purpose). On submit it renders a random confirmation code and echoes the form data it
   received; the run grades that echo, not what it meant to send.
4. **Fill + submit.** All fills go in one in-page call (`BrowserSession.fill_many`): native value
   setter + input/change events, each value read back; fields it cannot fill (no DOM path, e.g.
   shadow DOM) fall back to a per-field `act`. Submit is a real Playwright click.

### 9.3 Results

Result JSON in `artifacts/benchmarks/arc_index_live_*_20260929-*.json`.
Tests: `pytest tests` 257 passed (2 pre-existing failures in `test_phase1_remediation.py`);
`SOLARI_LIVE_TESTS=1 pytest tests/test_index_live.py` 2 passed.

**Extraction reliability** (`eval_extraction.py`, per field, correct / wrong / rejected):

| | `blocks`, before field descriptions | `blocks`, with descriptions (n=20) | `pageindex`, with descriptions (n=10) |
|---|---|---|---|
| `SYN-DENIAL-01`, 6 fields | not measured (6/6 in each single live run) | 20/0/0 each | 10/0/0 each |
| `MED-CMS-01` reason + remark code | 5/0/5 (n=10) | 20/0/0 | 10/0/0 |
| `MED-CMS-01`, other 4 fields | 10/0/0 | 20/0/0 | 10/0/0 |

One intermediate prompt (label hints only, no descriptions) made the synthetic letter's
`cpt_code` **wrong 7 times in 10**: the model picked 36415 from the paid line on the same date.
Those values were cited correctly, so grounding passed them. See 9.4.

**Full live run on Solari** (both cases, every field correct in the portal, confirmation matched):

| | `blocks` index | `pageindex` index |
|---|---|---|
| Indexing | ~0 s (pypdf) | 9.2–9.7 s per document |
| Extraction | 1.6–2.9 s, ~1.1k tokens | 6.4–9.2 s (agent navigates the tree) |
| 6–7 fills, batched | one ~380–420 ms call | same |
| Submit click | 2.1–2.5 s | same |
| Fill + submit total | ~6.3 s (was ~20.5 s with one `act` per field) | ~5.8 s |

Latency, measured on Solari from this machine: one CDP round trip is ~186 ms; a Playwright `fill`
is ~5 round trips (~920 ms) and a full `act` with its before/after snapshots ~2 s. Batching removes
that per field. On local Chromium a fill is 5–8 ms. The "sub-10 ms reflex" figure holds only when
the runner sits next to the browser; the in-VM runner in Section 2 does not exist yet, and the
2.31 ms and cost figures in Section 7 are not measured by these runs.

### 9.4 Limits and open work

- **Grounding proves presence, not correctness.** A value that appears in its cited source passes,
  even if it is the wrong row (the paid CPT line above). Field descriptions fixed that case; a
  general fix is planned in **Section 10** (next session).
- **Page-level citations are coarse.** With `pageindex`, `date_of_service` was verified against the
  first line on page 2 that contains the date, which is the paid line; the value is right but the
  snippet points at the wrong row. Block-level checks need PageIndex cloud (`get_block`); that
  path is not wired up or tested.
- The portal is a stand-in; no real payer portal was used.
- Invoice (`invoice_ap`) and regulatory cases have no documents with verified ground truth yet.
- `pageindex` is not a declared dependency: it upgrades `websockets` to 16.x, which other tools
  in this environment (lark-oapi, streamlit, frida-tools) pin at 13.1. It was run from a separate venv.

---

## 10. Next Session: Make Grounding Check *Correctness*, Not Just Presence

**Status: open. Start here next session.**

### 10.1 The weakness

`ArcIndexBridge.verify_grounding` accepts a field when the cited block (or page) *contains* the value.
That proves the value is in the document, not that it is the value the schema asked for. Two
observed failures:

1. **Wrong row, passed as grounded.** With a prompt that gave label hints but no field descriptions,
   `SYN-DENIAL-01`'s `cpt_code` came back `36415` in 7 of 10 runs. That is the *paid* line's code; the
   appeal is about the *denied* line (`99214`). The model cited page 2, page 2 contains `36415`, so the
   check passed it, and it would have been typed into the portal. Field descriptions ("CPT code of the
   denied line item") fixed this document (20/20), but that is a prompt fix for one case, not a guarantee.
2. **Right value, wrong evidence.** With page-level citations (local PageIndex), `date_of_service`
   was verified against the first line on page 2 containing `2026-06-14`, which is the paid line. The
   receipt's snippet then points at the wrong row, so the audit trail is misleading even when the value is right.

The invariant we want: **a value reaches the portal only if it is correct, or it does not reach it at
all.** Today the only guarantee is "only if it appears at the cited place".

### 10.2 Plan

1. **Reproduce first (red).** Add a `--no-descriptions` flag to `scripts/eval_extraction.py` that
   strips `description` from the schema, which restores the prompt that produced 7/10 wrong. Add
   decoy variants of the synthetic letter to `scripts/make_denial_fixture.py`:
   - paid and denied lines in swapped order;
   - two denied lines with different CPT codes (the schema must then say which one, or extraction must fail);
   - the decoy claim number moved to page 1 above the real one.
   Record the `wrong` counts per variant with `blocks` and `pageindex`. These are the baseline.
2. **Evidence quotes.** Require each field's answer to carry a verbatim `evidence` quote (the whole
   source row or sentence) alongside the value. Accept only if:
   - the value is inside the quote, and
   - the quote (whitespace-normalized) is inside the cited block or page.
   Store the quote as `CitationAnchor.text_snippet`. This fixes the misleading snippet (10.1, item 2)
   for page-level citations too, because the snippet becomes the row the model actually used.
3. **Schema evidence constraints.** Add optional per-field rules to `FieldTarget`, checked against the quote:
   - `evidence_must_match`: a regex, e.g. `cpt_code` evidence must match `(?i)denied`;
   - `same_row_as`: fields that must cite the same row, e.g. `cpt_code`, `billed_amount` and
     `date_of_service` all come from the denied line.
   A field that fails its rule is rejected, not typed.
4. **Cross-field consistency.** Where a document has redundancy, use it. For example, the
   billed amount on the cited row must equal `billed_amount`, and the claim number must not be one
   the document says is unaffected. Keep these as schema rules, not code special cases.
5. **Block-level PageIndex (stretch).** PageIndex cloud has layout blocks (`get_block`). Wire
   `PageIndexAdapter.verify_citation` to use them when a cloud key is present; this needs `PAGEINDEX_API_KEY`.

### 10.3 Acceptance criteria

- `eval_extraction.py -n 20` reports **`wrong=0` for every field, on every decoy variant, with and
  without `--no-descriptions`, for both `--index blocks` and `--index pageindex`**. Rejections are
  allowed: a missing field is safe, a wrong one is not.
- On the current fixtures with descriptions, correct stays at 20/20 (no regression from stricter checks).
- Every receipt snippet is the row the value came from (checked by a test on the `date_of_service` case).
- Unit tests cover each new rule (`evidence_must_match`, `same_row_as`, quote-not-on-page).
- The live Solari run (`SOLARI_LIVE_TESTS=1 pytest tests/test_index_live.py`) still passes.

### 10.4 Pointers

- Check: `src/arc_cua/index_bridge.py` → `verify_grounding`, `_parse_extracted_fields`, `extract_action_fields` (prompt).
- Indexes: `src/arc_cua/local_doc_index.py` (`verify_citation`, block level), `src/arc_cua/pageindex_adapter.py` (page level).
- Schemas: `schemas/rcm_denial.json`, `schemas/medicare_redetermination.json`.
- Measurement: `scripts/eval_extraction.py`; fixtures: `scripts/make_denial_fixture.py`, `tests/fixtures/documents/*.truth.json`.
- PageIndex runs need the separate venv (`pageindex==0.2.20` needs `websockets` 16; see 9.4).
