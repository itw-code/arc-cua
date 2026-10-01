"""Unit tests for Hybrid System-1 / System-2 Decision Architecture.

Validates:
1. Local Laya (421M ModernBERT) fast path decisions & affordance matching (<20ms, $0.00).
2. Candidate list budgeting (>20 options) via hierarchical coarse-to-fine pruning.
3. System-2 Cortex Escalation to SGLang (/v1/systemone) on low confidence or local unavailability.
4. Administrative Organizer triage and confidence-gating (>0.70 vs Review Queue).
5. SimHash stall detection and Cortex recovery escalation.
"""

import json
import pathlib
import sys
import tempfile
from unittest.mock import MagicMock, patch

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


from arc_cua.executor_interface import ActionVerb
from arc_cua.index_bridge import (
    MAX_LAYA_CANDIDATE_OPTIONS,
    DecisionTier,
    FieldBinding,
    HybridDecisionClient,
    MockLayaAgent,
    TreeTraversalResult,
)
from arc_cua.schemas import ActionResult
from arc_cua.state_verifier import StateVerificationResult
from scripts.admin_organizer import (
    AdminTriageEngine,
    FileOrganizerWatcher,
)


# ==============================================================================
# 1. System-1 Local Laya Fast Path Tests
# ==============================================================================

def test_local_laya_tree_traversal():
    """Validates fast-path tree traversal with sub-20ms logit argmax."""
    client = HybridDecisionClient(prefer_local=True, mock_laya=MockLayaAgent(default_confidence=0.94))

    candidate_nodes = [
        {"node_id": "0001", "title": "Introduction and Payer Info", "summary": "Intro"},
        {"node_id": "0002", "title": "Service and Itemization", "summary": "CPT codes and pricing"},
        {"node_id": "0003", "title": "Adjudication and Denial Reasons", "summary": "Denial reason codes CO-16"},
        {"node_id": "0004", "title": "Signature and Authorization", "summary": "Signatures"},
    ]

    result = client.traverse_tree(
        query="Find the denial reason codes and explanation",
        candidate_nodes=candidate_nodes,
    )

    assert isinstance(result, TreeTraversalResult)
    assert result.tier == DecisionTier.LOCAL_LAYA
    assert result.node_id == "0003"
    assert result.confidence >= 0.70
    assert result.latency_ms < 50.0  # sub-50ms test boundary


def test_local_laya_form_affordance_matching():
    """Validates affordance binding directly to active [#N] AXTree form pins."""
    client = HybridDecisionClient(prefer_local=True, mock_laya=MockLayaAgent(default_confidence=0.91))

    extracted_fields = {
        "claim_id": "CLM-2026-9912",
        "denial_code": "CO-16",
        "cpt_code": "99214",
    }

    axtree = [
        {"index": 1, "role": "textbox", "name": "Patient Full Name", "css": "#patient_name"},
        {"index": 2, "role": "textbox", "name": "Insurance Claim ID", "css": "#claim_num"},
        {"index": 3, "role": "textbox", "name": "Payer Denial Reason Code", "css": "#denial_ref"},
        {"index": 4, "role": "textbox", "name": "CPT Procedure Code", "css": "#cpt"},
        {"index": 5, "role": "button", "name": "Submit Appeal Request", "css": "#btn_submit"},
    ]

    bindings = client.match_form_fields(extracted_fields, axtree)

    assert len(bindings) == 3
    claim_binding = next(b for b in bindings if b.field_name == "claim_id")
    assert claim_binding.action_index == 2
    assert claim_binding.verb == ActionVerb.FILL
    assert claim_binding.tier == DecisionTier.LOCAL_LAYA

    denial_binding = next(b for b in bindings if b.field_name == "denial_code")
    assert denial_binding.action_index == 3

    cpt_binding = next(b for b in bindings if b.field_name == "cpt_code")
    assert cpt_binding.action_index == 4


# ==============================================================================
# 2. Candidate List Budgeting (>20 items) Tests
# ==============================================================================

def test_candidate_list_budgeting_hierarchical_split():
    """Validates that candidate sets > 20 trigger coarse-to-fine hierarchical pruning."""
    client = HybridDecisionClient(prefer_local=True, mock_laya=MockLayaAgent(default_confidence=0.95))

    # Generate 35 candidate nodes exceeding the 20-candidate head budget
    candidates = []
    for i in range(35):
        nid = f"{i:04d}"
        title = "Payer Denial Adjudication Codes" if nid == "0003" else f"General Document Section {i}"
        candidates.append({
            "node_id": nid,
            "title": title,
            "summary": f"Summary details for section {i}.",
        })

    assert len(candidates) > MAX_LAYA_CANDIDATE_OPTIONS

    result = client.traverse_tree(
        query="Payer Denial Adjudication Codes",
        candidate_nodes=candidates,
    )

    assert result.node_id == "0003"
    assert result.tier == DecisionTier.LOCAL_LAYA
    assert result.confidence >= 0.70


# ==============================================================================
# 3. System-2 Cortex Escalation (SGLang) Tests
# ==============================================================================

def test_sglang_escalation_on_low_confidence():
    """Validates escalation to Colab SGLang when local Laya confidence < 0.70."""
    # Local agent with low confidence 0.45 (< 0.70)
    low_conf_laya = MockLayaAgent(default_confidence=0.45)
    client = HybridDecisionClient(
        confidence_threshold=0.70,
        mock_laya=low_conf_laya,
    )

    # Mock response from Colab SGLang endpoint
    mock_sglang_response = {
        "answers": {
            "section_choice": {
                "choice": "[0002] High-Confidence Section via SGLang",
                "confidence": 0.98,
                "probabilities": {"[0002] High-Confidence Section via SGLang": 0.98},
            }
        }
    }

    with patch.object(client, "_post_remote_systemone", return_value=mock_sglang_response) as mock_post:
        candidates = [
            {"node_id": "0001", "title": "Section 1", "summary": "s1"},
            {"node_id": "0002", "title": "High-Confidence Section via SGLang", "summary": "s2"},
        ]
        res = client.traverse_tree("Query", candidates)

        # Must have escalated to SGLang
        assert mock_post.called
        assert res.tier == DecisionTier.REMOTE_SGLANG
        assert res.confidence == 0.98
        assert res.node_id == "0002"


def test_sglang_escalation_when_local_unavailable():
    """Validates automatic escalation to Colab SGLang when local Laya is None."""
    client = HybridDecisionClient(
        prefer_local=False,
        mock_laya=None,
    )
    assert client.laya_agent is None

    mock_sglang_response = {
        "answers": {
            "section_choice": {
                "choice": "[0005] SGLang Escalated Node",
                "confidence": 0.96,
            }
        }
    }

    with patch.object(client, "_post_remote_systemone", return_value=mock_sglang_response) as mock_post:
        candidates = [{"node_id": "0005", "title": "SGLang Escalated Node", "summary": "s"}]
        res = client.traverse_tree("Locate node", candidates)

        assert mock_post.called
        assert res.tier == DecisionTier.REMOTE_SGLANG
        assert res.node_id == "0005"


# ==============================================================================
# 4. Administrative File Organizer Triage & Gating Tests
# ==============================================================================

def test_admin_organizer_high_confidence_sorting():
    """Validates triage and file movement to organized directory when confidence > 0.70."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        inbox = pathlib.Path(tmp_dir) / "Inbox"
        organized = pathlib.Path(tmp_dir) / "Organized"
        review = inbox / "Review_Queue"
        inbox.mkdir(parents=True)

        sample_file = inbox / "invoice_test.txt"
        sample_file.write_text(
            "INVOICE #9021. Total Due: $450.00. Vendor: CloudTech Inc. Payment due in 30 days.",
            encoding="utf-8",
        )

        engine = AdminTriageEngine(prefer_local=True)
        # Inject mock laya into engine
        engine.laya_agent = MockLayaAgent(default_confidence=0.92)

        watcher = FileOrganizerWatcher(
            inbox_dir=inbox,
            organized_dir=organized,
            review_dir=review,
            confidence_threshold=0.70,
            engine=engine,
        )

        dest = watcher.process_file(sample_file)

        assert dest is not None
        assert dest.exists()
        assert not sample_file.exists()
        # Verify sidecar audit JSON exists
        sidecar = dest.with_suffix(dest.suffix + ".triage.json")
        assert sidecar.exists()
        with open(sidecar, "r", encoding="utf-8") as f:
            data = json.load(f)
            assert data["triage"]["confidence"] >= 0.70
            assert "Finance/Invoices_and_Receipts" in data["triage"]["target_folder"]


def test_admin_organizer_low_confidence_review_queue():
    """Validates that low-confidence documents (<=0.70) are safely quarantined to Review_Queue."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        inbox = pathlib.Path(tmp_dir) / "Inbox"
        organized = pathlib.Path(tmp_dir) / "Organized"
        review = inbox / "Review_Queue"
        inbox.mkdir(parents=True)

        ambiguous_file = inbox / "random_notes.txt"
        ambiguous_file.write_text("Ambiguous scribbles without clear operational context.", encoding="utf-8")

        engine = AdminTriageEngine(prefer_local=True)
        engine.laya_agent = MockLayaAgent(default_confidence=0.55)  # < 0.70 threshold

        watcher = FileOrganizerWatcher(
            inbox_dir=inbox,
            organized_dir=organized,
            review_dir=review,
            confidence_threshold=0.70,
            engine=engine,
        )

        dest = watcher.process_file(ambiguous_file)

        assert dest is not None
        assert dest.exists()
        assert review in dest.parents or dest.parent == review
        assert "Review_Queue" in str(dest)


# ==============================================================================
# 5. SimHash Stall Detection & Cortex Recovery Tests
# ==============================================================================

def test_simhash_stall_detection_and_escalation():
    """Validates that an action yielding zero DOM mutation triggers System-2 Cortex escalation."""
    mock_laya = MockLayaAgent(default_confidence=0.92)
    client = HybridDecisionClient(prefer_local=True, mock_laya=mock_laya)

    # Mock page object
    mock_page = MagicMock()
    mock_page.url = "https://payer.example.com/appeal"
    mock_page.title.return_value = "Payer Portal"
    mock_page.content.return_value = "<html><body><form></form></body></html>"

    # Mock executor returning successful mechanical delivery
    mock_executor = MagicMock()
    mock_executor.execute.return_value = ActionResult(
        success=True,
        verb="FILL",
        latency_ms=5.0,
    )
    client.executor = mock_executor

    # Mock verifier indicating stuck / zero Hamming distance transition
    mock_verifier = MagicMock()
    verification_res = StateVerificationResult(
        state_changed=False,
        simhash_before=0x12345678,
        simhash_after=0x12345678,
        hamming_distance=0,
        is_stuck_indicator=True,
    )
    mock_verifier.verify.return_value = verification_res
    mock_verifier.verify_transition.return_value = verification_res
    client.verifier = mock_verifier

    bindings = [
        FieldBinding(
            field_name="claim_id",
            value="CLM-9912",
            action_index=2,
            verb=ActionVerb.FILL,
            confidence=0.92,
        )
    ]

    with patch.object(
        client,
        "escalate_cortex_recovery",
        return_value=(True, "Recovered via Enter press."),
    ) as mock_escalate:
        results = client.execute_deterministic_actions(page=mock_page, bindings=bindings, settle_ms=0)

        assert len(results) == 1
        assert results[0].escalated_to_system2 is True
        assert results[0].stall_recovered is True
        assert mock_escalate.called
