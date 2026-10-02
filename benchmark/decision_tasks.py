"""Ground-truth decision tasks for System-1 / System-2 evaluation.

Every answer comes from a fixture, never from a model:
- page tasks: `tests/fixtures/documents/<doc>.truth.json` gives each field's page;
  the state is the PDF's own text (pypdf), the options are its pages.
- pin tasks: which [#N] form pin takes a field. Truth is the portal's own labels
  (`scripts/tools/demo_arc_index_pipeline.py`, `tests/test_index_bridge.py`); fields the
  portal has no input for map to NONE_PIN. A 20-pin portal adds hand-written
  distractors (address, NPI, ...) whose labels match none of the fields.
Seeds only shuffle option order, which measures order sensitivity; they never pick outcomes.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "tests" / "fixtures" / "documents"
PAGE_DOCS = [
    "synthetic_denial_letter",
    "synthetic_denial_letter_decoy_first",
    "synthetic_denial_letter_swapped",
    "synthetic_denial_letter_two_denied",
]
NONE_PIN = "NONE: no field on this form takes it"

FIELD_TEXT = {
    "claim_id": "the insurance claim ID",
    "patient_name": "the patient's name",
    "date_of_service": "the date of service",
    "billed_amount": "the billed amount",
    "cpt_code": "the CPT procedure code",
    "denial_code": "the payer's denial reason code",
}

# portal name -> (pins, field -> truth pin)
PORTALS: Dict[str, tuple] = {
    "demo_portal": (
        ["[#1] Insurance Claim ID", "[#2] Payer Denial Reason Code", "[#3] CPT Procedure Code",
         "[#4] Appeal Justification Notes", "[#5] Submit Appeal Request"],
        {"claim_id": "[#1] Insurance Claim ID", "denial_code": "[#2] Payer Denial Reason Code",
         "cpt_code": "[#3] CPT Procedure Code"},
    ),
    "bridge_test_portal": (
        ["[#1] Patient Full Name", "[#2] Insurance Claim ID", "[#3] Denial Code (e.g. CO-16)",
         "[#4] CPT Procedure Code", "[#5] Submit Appeal Request"],
        {"patient_name": "[#1] Patient Full Name", "claim_id": "[#2] Insurance Claim ID",
         "denial_code": "[#3] Denial Code (e.g. CO-16)", "cpt_code": "[#4] CPT Procedure Code"},
    ),
    "wide_portal": (
        ["[#1] Provider NPI", "[#2] Provider Tax ID", "[#3] Street Address", "[#4] City", "[#5] ZIP Code",
         "[#6] Contact Phone", "[#7] Contact Email", "[#8] Patient Full Name", "[#9] Patient Date of Birth",
         "[#10] Member ID", "[#11] Insurance Claim ID", "[#12] Date of Service", "[#13] Billed Amount (USD)",
         "[#14] CPT Procedure Code", "[#15] Diagnosis Code (ICD-10)", "[#16] Payer Denial Reason Code",
         "[#17] Appeal Justification Notes", "[#18] Upload Attachment", "[#19] Save Draft", "[#20] Submit Appeal"],
        {"patient_name": "[#8] Patient Full Name", "claim_id": "[#11] Insurance Claim ID",
         "date_of_service": "[#12] Date of Service", "billed_amount": "[#13] Billed Amount (USD)",
         "cpt_code": "[#14] CPT Procedure Code", "denial_code": "[#16] Payer Denial Reason Code"},
    ),
    # Labels share no content word with the field descriptions: keyword matching cannot solve it.
    "paraphrase_portal": (
        ["[#1] Reference #", "[#2] Member", "[#3] Visit on", "[#4] Charges", "[#5] Service line",
         "[#6] Reason", "[#7] Why should we reconsider?", "[#8] Send"],
        {"claim_id": "[#1] Reference #", "patient_name": "[#2] Member", "date_of_service": "[#3] Visit on",
         "billed_amount": "[#4] Charges", "cpt_code": "[#5] Service line", "denial_code": "[#6] Reason"},
    ),
}


@dataclass(frozen=True)
class DecisionTask:
    task_id: str
    kind: str  # "page" | "pin"
    state: str
    instructions: str
    options: List[str]
    truth: str
    source: str

    def question(self) -> Dict[str, object]:
        return {"type": "choice", "instructions": self.instructions, "options": list(self.options)}


def _pages(doc: str) -> List[str]:
    import pypdf

    return [p.extract_text() or "" for p in pypdf.PdfReader(str(DOCS / f"{doc}.pdf")).pages]


def page_tasks() -> List[DecisionTask]:
    tasks = []
    for doc in PAGE_DOCS:
        truth = json.loads((DOCS / f"{doc}.truth.json").read_text(encoding="utf-8"))
        pages = _pages(doc)
        state = "\n\n".join(f"=== Page {i} ===\n{t.strip()}" for i, t in enumerate(pages, 1))
        opts = [f"Page {i}" for i in range(1, len(pages) + 1)]
        for field, spec in truth.items():
            if field.startswith("_") or field not in FIELD_TEXT:
                continue
            tasks.append(DecisionTask(
                task_id=f"page:{doc}:{field}", kind="page", state=state,
                instructions=f"Which page of this letter states {FIELD_TEXT[field]}?",
                options=opts, truth=f"Page {spec['page']}", source=f"{doc}.truth.json",
            ))
    return tasks


def pin_tasks() -> List[DecisionTask]:
    truth = json.loads((DOCS / "synthetic_denial_letter.truth.json").read_text(encoding="utf-8"))
    tasks = []
    for portal, (pins, mapping) in PORTALS.items():
        for field in FIELD_TEXT:
            value = truth[field]["value"]
            tasks.append(DecisionTask(
                task_id=f"pin:{portal}:{field}", kind="pin",
                state=f"Extracted document field `{field}` ({FIELD_TEXT[field]}) = {value!r}.",
                instructions="Which form element should receive this value?",
                options=pins + [NONE_PIN], truth=mapping.get(field, NONE_PIN), source=portal,
            ))
    return tasks


def all_tasks(seed: Optional[int] = None) -> List[DecisionTask]:
    tasks = page_tasks() + pin_tasks()
    if seed is None:
        return tasks
    rng = random.Random(seed)
    out = []
    for t in tasks:
        opts = list(t.options)
        rng.shuffle(opts)
        out.append(DecisionTask(t.task_id, t.kind, t.state, t.instructions, opts, t.truth, t.source))
    return out
