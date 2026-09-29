"""ARC Index Bridge: Vectorless Document-to-Action Engine.

Bridges VectifyAI/PageIndex hierarchical tree document reasoning with ARC's
sub-10ms in-VM reflex execution loop on Solari cloud browsers.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("arc_cua.index_bridge")


@dataclasses.dataclass(frozen=True)
class CitationAnchor:
    """Verifiable proof linking an extracted value back to source document bytes."""
    document: str
    page: int
    block_id: Optional[str] = None
    text_snippet: Optional[str] = None
    confidence: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "document": self.document,
            "page": self.page,
            "block_id": self.block_id,
            "text_snippet": self.text_snippet,
            "confidence": self.confidence,
        }


@dataclasses.dataclass
class ExtractedField:
    """A single semantic value extracted via PageIndex tree reasoning."""
    field_name: str
    value: Any
    citation: CitationAnchor
    normalized_str: str = ""
    validation_status: bool = True
    error_message: Optional[str] = None

    def __post_init__(self):
        if not self.normalized_str:
            self.normalized_str = str(self.value).strip()


@dataclasses.dataclass
class FieldTarget:
    """Specification of how a document field binds to a portal form affordance."""
    field_name: str
    target_role: str                     # e.g., "textbox", "combobox", "radio", "button"
    label_hints: List[str]               # e.g., ["Claim ID", "Claim #", "Reference Number"]
    input_verb: str = "FILL"             # "FILL", "TYPE", "SELECT", "CLICK"
    required: bool = True
    format_transformer: Optional[str] = None
    # How source documents label this field, for the extraction prompt (form label_hints
    # describe the portal, which often words it differently from the document).
    description: Optional[str] = None


@dataclasses.dataclass
class DocumentActionSchema:
    """Contract defining the target form requirements."""
    schema_id: str
    target_url_pattern: str
    fields: List[FieldTarget]
    submit_affordance_hint: str = "Submit"
    success_indicator_pattern: str = "Confirmation|Receipt|Success|Reference|Accepted"


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
    citations: List[Dict[str, Any]]
    submission_confirmation_code: Optional[str]
    state_changed: bool
    hamming_distance: int
    wall_time_ms: float
    solari_session_id: Optional[str]
    timestamp: str
    step_log: List[Dict[str, Any]] = dataclasses.field(default_factory=list)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(dataclasses.asdict(self), indent=indent, default=str)


class ArcIndexBridge:
    """Orchestrates document tree reasoning and compiles actions into ARC reflex loops."""

    def __init__(self, pageindex_client: Optional[Any] = None, browser_session: Optional[Any] = None):
        self.pageindex = pageindex_client
        self.browser = browser_session

    @staticmethod
    def parse_citations(text: str, default_doc: str = "document.pdf") -> List[CitationAnchor]:
        """Extract <cite doc=... page=... block=.../> or <doc=...;page=...;block=...> tags."""
        found: List[CitationAnchor] = []
        # Pattern 1: <cite doc="..." page="..." block="..."/>
        cite_tag_re = re.compile(r"""<cite\s+([^>]*)/?>""")
        attr_re = re.compile(r"""\b(\w+)=["']([^"']*)["']""")
        for m in cite_tag_re.finditer(text):
            attrs = dict(attr_re.findall(m.group(1)))
            doc = attrs.get("doc", default_doc)
            page_val = int(attrs.get("page", 1)) if attrs.get("page", "").isdigit() else 1
            block_val = attrs.get("block")
            found.append(CitationAnchor(document=doc, page=page_val, block_id=block_val))

        # Pattern 2: <doc=...;page=...;block=...>
        old_tag_re = re.compile(r"""<doc=([^;<>]+);page=(\d+)(?:;block=([^;<>]+))?>""")
        for m in old_tag_re.finditer(text):
            found.append(CitationAnchor(
                document=m.group(1),
                page=int(m.group(2)),
                block_id=m.group(3) if m.group(3) else None,
            ))
        return found

    # ---- PageIndex tier -------------------------------------------------

    def ingest_document(self, file_path: str, wait: bool = True) -> str:
        """Submit a document to PageIndex and return its doc_id."""
        if self.pageindex is None:
            raise RuntimeError("PageIndex client required for ingestion (pip install pageindex).")
        result = self.pageindex.submit_document(file_path, wait=wait)
        return result["doc_id"]

    def extract_action_fields(
        self,
        doc_id: str,
        schema: DocumentActionSchema,
        document_name: Optional[str] = None,
    ) -> List[ExtractedField]:
        """Reason over the PageIndex tree and return schema fields with block-level citations."""
        if self.pageindex is None:
            raise RuntimeError("PageIndex client required for extraction.")
        blocks = getattr(self.pageindex, "cites_blocks", True)
        cite = '<cite doc=".." page=".." block=".."/>' if blocks else '<cite doc=".." page=".."/>'
        prompt = (
            f"Extract the following fields for schema '{schema.schema_id}'. "
            f"Answer with one line per field as `field_name: value {cite}`, citing where the value is. "
            "If a field is not in the document, write `field_name: NOT_FOUND`.\n"
            + "\n".join(f"- {f.field_name}: {f.description or ', '.join(f.label_hints)}" for f in schema.fields)
        )
        response_text = self.pageindex.chat(prompt, doc_id=doc_id)
        fields = self._parse_extracted_fields(response_text, schema, document_name or doc_id)
        if not blocks:
            # A page-level index cannot back a block id; don't let the receipt carry one.
            for f in fields:
                f.citation = dataclasses.replace(f.citation, block_id=None)
        return self.verify_grounding(doc_id, fields)

    def verify_grounding(self, doc_id: str, fields: List[ExtractedField]) -> List[ExtractedField]:
        """Reject fields whose cited source does not contain the value.

        The client's `verify_citation(doc_id, citation, value)` returns the supporting text
        (block or page, depending on the index) or None. A client without it cannot be
        checked: its citations are taken on trust and fields pass through unchanged.
        """
        check = getattr(self.pageindex, "verify_citation", None)
        for f in fields:
            if f.normalized_str.upper() == "NOT_FOUND":
                f.validation_status, f.error_message = False, "not found in document"
                continue
            if check is None or not f.validation_status:
                continue
            snippet = check(doc_id, f.citation, f.normalized_str)
            if snippet:
                f.citation = dataclasses.replace(f.citation, text_snippet=snippet)
            else:
                where = f"block {f.citation.block_id}" if f.citation.block_id else f"page {f.citation.page}"
                f.validation_status = False
                f.error_message = f"value not in cited {where}"
        return fields

    @staticmethod
    def _parse_extracted_fields(
        response_text: str,
        schema: DocumentActionSchema,
        default_doc: str = "document.pdf",
    ) -> List[ExtractedField]:
        """Parse `field_name: value <cite .../>` lines. Fields without a citation are rejected
        (validation_status=False) so ungrounded values never reach the browser."""
        fields: List[ExtractedField] = []
        by_name = {f.field_name: f for f in schema.fields}
        for line in response_text.splitlines():
            m = re.match(r"\s*[-*]?\s*`?([A-Za-z_][\w]*)`?\s*[:=]\s*(.+)", line)
            if not m or m.group(1) not in by_name:
                continue
            name, rest = m.group(1), m.group(2)
            cites = ArcIndexBridge.parse_citations(rest, default_doc)
            value = re.sub(r"<cite\s[^>]*/?>|<doc=[^<>]*>", "", rest).strip().strip("`\"'").strip()
            if cites:
                fields.append(ExtractedField(name, value, cites[0]))
            else:
                fields.append(ExtractedField(
                    name, value, CitationAnchor(default_doc, 0, confidence=0.0),
                    validation_status=False, error_message="no citation anchor",
                ))
        return fields

    @staticmethod
    def _scrape_confirmation(tree_text: str, schema: DocumentActionSchema) -> Optional[str]:
        """Find a confirmation code (must contain a digit) near a success label."""
        label = f"(?i:{schema.success_indicator_pattern}|Confirmation|Reference|Receipt)"
        for m in re.finditer(label + r"[^A-Za-z0-9]{0,20}\s*([A-Z0-9]+(?:-[A-Z0-9]+)+|[A-Z0-9]{6,20})", tree_text):
            if re.search(r"\d", m.group(1)):
                return m.group(1)
        return None

    def bind_to_axtree(
        self,
        extracted_fields: List[ExtractedField],
        schema: DocumentActionSchema,
        axtree_text: str,
        action_map: Dict[int, Dict[str, Any]],
    ) -> List[CompiledActionStep]:
        """Maps extracted document values to visible [#N] affordances in the AXTree."""
        compiled_steps: List[CompiledActionStep] = []

        for field in extracted_fields:
            if not field.validation_status:
                logger.warning(f"Skipping ungrounded field '{field.field_name}': {field.error_message}")
                continue
            target_spec = next((f for f in schema.fields if f.field_name == field.field_name), None)
            if not target_spec:
                continue

            matched_idx = self._find_matching_affordance(target_spec, action_map)
            if matched_idx is None:
                if target_spec.required:
                    logger.warning(f"Required field '{field.field_name}' not matched in active AXTree.")
                continue

            compiled_steps.append(CompiledActionStep(
                action_index=matched_idx,
                verb=target_spec.input_verb,
                value=field.normalized_str,
                source_field=field.field_name,
                citation=field.citation,
            ))

        return compiled_steps

    def _find_matching_affordance(
        self,
        target_spec: FieldTarget,
        action_map: Dict[int, Dict[str, Any]],
    ) -> Optional[int]:
        """Heuristic affordance resolver matching label hints and role to [#N] nodes."""
        target_hints = [h.lower() for h in target_spec.label_hints]

        # Priority 1: Exact label or attribute match
        for idx, node in action_map.items():
            name = (node.get("name") or "").lower()
            role = (node.get("role") or "").lower()
            aria = (node.get("aria_label") or "").lower()
            css = (node.get("css") or "").lower()

            # Check role compatibility
            role_match = (
                (target_spec.target_role == "textbox" and ("box" in role or "input" in role or role == "textbox"))
                or (target_spec.target_role == "button" and ("button" in role or role == "button"))
                or (target_spec.target_role in role)
            )

            # Check hint presence
            for hint in target_hints:
                if hint in name or hint in aria or hint in css:
                    return idx

        # Priority 2: Role match if unique or high confidence
        for idx, node in action_map.items():
            role = (node.get("role") or "").lower()
            for hint in target_hints:
                name = (node.get("name") or "").lower()
                if hint in name:
                    return idx

        return None

    def execute_and_verify(
        self,
        steps: List[CompiledActionStep],
        schema: DocumentActionSchema,
        submit: bool = True,
        document_name: str = "document.pdf",
        settle_ms: float = 1000.0,
        batch_fills: bool = True,
    ) -> VerificationReceipt:
        """Executes compiled reflex steps through the browser session and verifies state change.

        Fills run without re-observing, so every [#N] resolves against the inspect the steps were
        bound to. settle_ms caps how long each inspect waits for the page to stop changing.
        batch_fills: send all FILL/TYPE steps in one in-page call when the session supports
        it (BrowserSession.fill_many); any field it cannot fill falls back to a per-field act.
        """
        if not self.browser:
            raise RuntimeError("BrowserSession required for execution.")

        t_start = time.perf_counter()
        fields_submitted: Dict[str, Any] = {}
        citations: List[Dict[str, Any]] = []
        total_hamming = 0
        step_log: List[Dict[str, Any]] = []

        def record(step: CompiledActionStep, res: Dict[str, Any]) -> None:
            nonlocal total_hamming
            total_hamming += res.get("hamming_distance") or 0
            step_log.append({"field": step.source_field, "verb": step.verb, "index": step.action_index,
                             **{k: res.get(k) for k in ("success", "state_changed", "hamming_distance",
                                                        "action_latency_ms", "error", "batched")}})
            if res.get("success", True):
                fields_submitted[step.source_field] = step.value
                citations.append(step.citation.to_dict())

        remaining = list(steps)
        fill_many = getattr(self.browser, "fill_many", None)
        batchable = [s for s in steps if s.verb.upper() in ("FILL", "TYPE")]
        if batch_fills and fill_many and len(batchable) > 1:
            batch = fill_many([(s.action_index, s.value or "") for s in batchable])
            by_index = {r["index"]: r for r in batch["results"]}
            total_hamming += batch["hamming_distance"] or 0
            for s in batchable:
                r = by_index.get(s.action_index)
                if r and r.get("ok"):
                    # Hamming is counted once for the whole batch above, not per field.
                    record(s, {"success": True, "state_changed": r["state_changed"], "hamming_distance": 0,
                               "action_latency_ms": batch["latency_ms"], "batched": True})
                    remaining.remove(s)
                elif r:
                    logger.info(f"Batch fill of [#{s.action_index}] failed ({r.get('error')}); using act.")

        for step in remaining:
            logger.info(f"Executing step: {step.verb} on [#{step.action_index}] with value='{step.value}'")
            res = self.browser.act(
                action=step.verb.lower(),
                index=step.action_index,
                value=step.value,
                settle_ms=settle_ms,
            )
            record(step, res)

        # Find submit button if configured
        inspect_post = self.browser.inspect(settle_ms=settle_ms)
        action_map = inspect_post.get("action_index_map") or getattr(self.browser, "_index_map", {})
        submit_idx = None
        for idx, node in action_map.items():
            name = (node.get("name") or "").lower()
            role = (node.get("role") or "").lower()
            if "button" in role and schema.submit_affordance_hint.lower() in name:
                submit_idx = idx
                break

        confirmation_code: Optional[str] = None
        final_state_changed = False

        if submit and submit_idx is not None:
            sub_res = self.browser.act(action="click", index=submit_idx, settle_ms=settle_ms)
            final_state_changed = sub_res.get("state_changed", False)
            step_log.append({"field": "<submit>", "verb": "CLICK", "index": submit_idx,
                             **{k: sub_res.get(k) for k in ("success", "state_changed", "hamming_distance",
                                                            "action_latency_ms", "error")}})
            total_hamming += sub_res.get("hamming_distance", 0)

            # Look for confirmation text in returned tree
            post_tree = self.browser.inspect(settle_ms=settle_ms)
            tree_text = post_tree.get("text", "")
            confirmation_code = self._scrape_confirmation(tree_text, schema)

        wall_ms = (time.perf_counter() - t_start) * 1000.0

        return VerificationReceipt(
            receipt_id=f"rcpt_{uuid.uuid4().hex[:12]}",
            document_name=document_name,
            schema_id=schema.schema_id,
            fields_submitted=fields_submitted,
            citations=citations,
            submission_confirmation_code=confirmation_code,
            state_changed=final_state_changed or total_hamming > 0,
            hamming_distance=total_hamming,
            wall_time_ms=round(wall_ms, 2),
            solari_session_id=getattr(getattr(self.browser, "_solari_session", None), "session_id", None),
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            step_log=step_log,
        )


def load_schema(path_or_id: str) -> DocumentActionSchema:
    """Load a DocumentActionSchema from a JSON file, or a built-in by id (schemas/*.json)."""
    import pathlib
    p = pathlib.Path(path_or_id)
    if not p.exists():
        builtin = {"healthcare_denial_appeal": "rcm_denial.json", "invoice_entry": "invoice_ap.json",
                   "medicare_redetermination": "medicare_redetermination.json"}
        name = builtin.get(path_or_id)
        if name is None:
            raise ValueError(f"Unknown schema '{path_or_id}'. Built-ins: {sorted(builtin)} or pass a JSON path.")
        p = pathlib.Path(__file__).resolve().parents[2] / "schemas" / name
    data = json.loads(p.read_text(encoding="utf-8"))
    fields = [FieldTarget(**f) for f in data.pop("fields")]
    return DocumentActionSchema(fields=fields, **data)
