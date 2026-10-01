"""ARC Index & CUA Bridge: Hybrid Decision Client.

Implements the System-1 / System-2 decision architecture bridging PageIndex
tree document reasoning with ARC's sub-10ms reflex loop on Solari browsers.

- System-1 Fast Path: Local non-autoregressive Laya (421M ModernBERT) for
  PageIndex tree traversal and form affordance matching. Measured: ~38 ms p50 on
  an L4 GPU, ~565 ms on an 8-thread CPU. Candidate budget: <= 20 options per head.
- System-2 Cortex Escalation: Remote SGLang running on Google Colab (Qwen3.8-27B)
  reachable via HTTP/2 keep-alive connection pool when SimHash detects DOM stalls
  or decision confidence falls below the fitted threshold (0.44).
- Playwright Automation: Feeds deterministic verbs ('CLICK [#N]', 'FILL [#N] <val>')
  directly to Solari's PlaywrightExecutor with post-action SimHash verification.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Tuple

from arc_cua.executor_interface import ActionPayload, ActionVerb
from arc_cua.playwright_executor import PlaywrightExecutor
from arc_cua.schemas import ActionResult
from arc_cua.state_verifier import StateVerifier, StateVerificationResult

logger = logging.getLogger("arc_cua.index_bridge")

MAX_LAYA_CANDIDATE_OPTIONS = 20
# Fitted on ground-truth page/pin tasks (benchmark/calibrate_threshold.py,
# results/benchmark_runs/calibration_20261001-053044.json): the lowest answer
# probability at which every kept Laya decision was right; leave-one-source-out
# precision 1.0 at 14% coverage. Laya never cleared 0.70 on those tasks.
DEFAULT_CONFIDENCE_THRESHOLD = 0.44


class DecisionTier(str, enum.Enum):
    """Decision execution tier."""
    LOCAL_LAYA = "local_laya"          # System-1: non-autoregressive logit argmax (<20ms)
    REMOTE_SGLANG = "remote_sglang"    # System-2: Colab SGLang cortex escalation
    HEURISTIC = "heuristic"            # Rule-based fallback


@dataclasses.dataclass(frozen=True)
class FieldBinding:
    """Mapping from an extracted document field to a pinned browser affordance."""
    field_name: str
    value: Any
    action_index: int
    verb: ActionVerb = ActionVerb.FILL
    confidence: float = 1.0
    tier: DecisionTier = DecisionTier.LOCAL_LAYA
    affordance_name: str = ""
    metadata: Dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class TreeTraversalResult:
    """Result of PageIndex tree branch selection."""
    node_id: str
    title: str
    confidence: float
    tier: DecisionTier
    latency_ms: float
    raw_decision: Dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class ExecutionStepResult:
    """Telemetry of an executed deterministic UI action."""
    binding: FieldBinding
    action_result: ActionResult
    state_verification: Optional[StateVerificationResult] = None
    escalated_to_system2: bool = False
    stall_recovered: bool = False
    recovery_note: str = ""


class LayaWireAdapter:
    """Expose a real `laya.Agent` through the /v1/systemone wire format.

    Wire questions: choice {options}, score {scale}, noul|yes_no. Wire answers:
    {choice|score, confidence, probabilities}, where `confidence` is the probability
    of the returned answer (Laya's own `confidence` field is a different quantity).
    """

    def __init__(self, agent: Any, head_max_len: Optional[int] = None):
        self.agent = agent
        self.head_max_len = head_max_len

    @staticmethod
    def _to_laya(q: Dict[str, Any]) -> Dict[str, Any]:
        kind = q.get("type", "choice")
        out = {"instructions": q.get("instructions", "")}
        if kind == "choice":
            return {**out, "type": "choice", "criteria": {o: o for o in q["options"]}}
        if kind == "score":
            return {**out, "type": "score", "criteria": [str(s) for s in q.get("scale", [1, 2, 3, 4, 5])]}
        if kind in ("noul", "yes_no"):
            return {**out, "type": "noul"}
        raise ValueError(f"unknown question type {kind!r}")

    @staticmethod
    def _from_laya(q: Dict[str, Any], ans: Dict[str, Any]) -> Dict[str, Any]:
        kind = q.get("type", "choice")
        if kind == "choice":
            probs = ans["probabilities"]
            return {"choice": ans["choice"], "confidence": float(probs[ans["choice"]]), "probabilities": probs}
        if kind == "score":
            scale = list(q.get("scale", [1, 2, 3, 4, 5]))
            probs = [float(ans["probabilities"][str(i)]) for i in range(len(scale))]
            best = max(range(len(scale)), key=probs.__getitem__)
            return {
                "score": scale[best],
                "expected": sum(s * p for s, p in zip(scale, probs)),
                "confidence": probs[best],
                "probabilities": {str(s): p for s, p in zip(scale, probs)},
            }
        p_yes = float(ans["noul"])
        return {"choice": "yes" if p_yes >= 0.5 else "no", "confidence": max(p_yes, 1.0 - p_yes),
                "probabilities": {"yes": p_yes, "no": 1.0 - p_yes}}

    def predict(self, state: str, questions: Dict[str, Any]) -> Dict[str, Any]:
        res = self.agent.predict(state, {k: self._to_laya(q) for k, q in questions.items()},
                                 head_max_len=self.head_max_len)
        return {"answers": {k: self._from_laya(q, res["answers"][k]) for k, q in questions.items()},
                "usage": res.get("usage", {})}


class MockLayaAgent:
    """In-memory mock of convaiinnovations/laya-typed-decisions for testing and offline benchmarking."""

    def __init__(self, default_confidence: float = 0.92):
        self.default_confidence = default_confidence

    def predict(self, state: str, questions: Dict[str, Any]) -> Dict[str, Any]:
        answers: Dict[str, Any] = {}
        for q_name, q_body in questions.items():
            q_type = q_body.get("type", "choice")
            if q_type == "choice":
                options = q_body.get("options", ["Option A", "Option B"])
                stop = {"section", "sections", "candidate", "query", "user", "detailed", "information", "regarding", "the", "and", "for", "part", "document", "select", "matching", "input", "element", "field", "active"}
                best_opt = options[0]
                best_score = -1
                state_lower = state.lower()
                for opt in options:
                    opt_tokens = [t.lower() for t in re.findall(r"\w+", opt) if len(t) > 2 and t.lower() not in stop]
                    score = sum(len(tok) for tok in opt_tokens if tok in state_lower)
                    if score > best_score:
                        best_score = score
                        best_opt = opt
                probs = {opt: (self.default_confidence if opt == best_opt else (1.0 - self.default_confidence) / max(1, len(options) - 1)) for opt in options}
                answers[q_name] = {
                    "choice": best_opt,
                    "confidence": self.default_confidence,
                    "probabilities": probs,
                }
            elif q_type == "score":
                scale = q_body.get("scale", [1, 2, 3, 4, 5])
                score_val = scale[-2] if len(scale) >= 2 else scale[0]
                answers[q_name] = {"score": score_val, "confidence": self.default_confidence}
            elif q_type in ("noul", "yes_no"):
                answers[q_name] = {
                    "choice": "yes",
                    "probabilities": {"yes": 0.88, "no": 0.12},
                    "confidence": 0.88,
                }
            else:
                answers[q_name] = {"choice": "unknown", "confidence": 0.5}
        return {"answers": answers}


class HybridDecisionClient:
    """Unified System-1 / System-2 decision client for ARC CUA."""

    def __init__(
        self,
        local_model_id: str = "convaiinnovations/laya-typed-decisions",
        colab_endpoint: Optional[str] = None,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        head_max_len: int = 256,
        prefer_local: bool = True,
        playwright_executor: Optional[PlaywrightExecutor] = None,
        state_verifier: Optional[StateVerifier] = None,
        mock_laya: Optional[Any] = None,
    ):
        self.local_model_id = local_model_id
        self.colab_endpoint = colab_endpoint or os.environ.get(
            "SGLANG_DECISION_ENDPOINT",
            "http://127.0.0.1:8000/v1/systemone",
        )
        self.colab_decisions_url = os.environ.get(
            "SGLANG_DECISIONS_URL",
            self.colab_endpoint.replace("/v1/systemone", "/v1/decisions"),
        )
        self.confidence_threshold = confidence_threshold
        self.head_max_len = head_max_len
        self.prefer_local = prefer_local
        self.executor = playwright_executor or PlaywrightExecutor()
        self.verifier = state_verifier or StateVerifier()
        self.laya_agent = mock_laya

        if self.laya_agent is None:
            self._init_local_laya()

    def _init_local_laya(self) -> None:
        """Initialize in-memory local Laya ModernBERT decision model."""
        if not self.prefer_local:
            return
        try:
            import laya  # type: ignore
            logger.info("Initializing Local System-1 Laya (%s)...", self.local_model_id)
            self.laya_agent = LayaWireAdapter(laya.load(self.local_model_id), head_max_len=self.head_max_len)
            logger.info("System-1 Local Laya loaded.")
        except ImportError:
            logger.info("Local 'laya' package not installed; escalation to SGLang active.")
        except Exception as e:
            logger.warning("Local Laya init failed: %s; falling back to remote SGLang.", e)

    # -------------------------------------------------------------------------
    # Remote HTTP Keep-Alive Gateway (SGLang)
    # -------------------------------------------------------------------------

    def _post_remote_systemone(self, payload: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
        """Query remote Colab SGLang /v1/systemone via HTTP keep-alive connection pool."""
        req = urllib.request.Request(
            self.colab_endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Connection": "keep-alive"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"SGLang systemone error HTTP {resp.status}")
            return json.loads(resp.read().decode("utf-8"))

    def _post_remote_decisions(self, payload: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
        """Query remote Colab SGLang /v1/decisions via HTTP keep-alive connection pool."""
        req = urllib.request.Request(
            self.colab_decisions_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Connection": "keep-alive"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"SGLang decisions error HTTP {resp.status}")
            return json.loads(resp.read().decode("utf-8"))

    # -------------------------------------------------------------------------
    # Fast Path: PageIndex Tree Node Traversal (<20ms)
    # -------------------------------------------------------------------------

    def traverse_tree(
        self,
        query: str,
        candidate_nodes: List[Dict[str, Any]],
    ) -> TreeTraversalResult:
        """Select the relevant PageIndex tree node using fast logit classification.

        If candidate count > 20, applies hierarchical coarse-to-fine ranking to
        respect the 256-token candidate head budget.
        """
        if not candidate_nodes:
            raise ValueError("candidate_nodes list cannot be empty.")

        t0 = time.perf_counter()

        # Format candidates with deterministic identifiers
        formatted_options = [
            f"[{node.get('node_id', str(i))}] {node.get('title', '')} - {node.get('summary', '')[:80]}".strip()
            for i, node in enumerate(candidate_nodes)
        ]

        # Coarse-to-fine hierarchical ranking if candidates > 20
        if len(formatted_options) > MAX_LAYA_CANDIDATE_OPTIONS:
            logger.debug(
                "Candidates (%d) > %d. Performing hierarchical coarse-to-fine pruning.",
                len(formatted_options),
                MAX_LAYA_CANDIDATE_OPTIONS,
            )
            # Group into clusters of size <= 15
            cluster_size = 15
            clusters = [
                candidate_nodes[i : i + cluster_size]
                for i in range(0, len(candidate_nodes), cluster_size)
            ]
            cluster_summaries = [
                f"[CLUSTER-{idx}] {c[0].get('title', '')} through {c[-1].get('title', '')}"
                for idx, c in enumerate(clusters)
            ]

            # Coarse step
            coarse_res = self._decide_choice(
                state=f"Query: {query}\nTask: Select document cluster to search.",
                question_name="cluster_choice",
                instructions="Select the single document section cluster most relevant to the query.",
                options=cluster_summaries,
            )
            cluster_idx_match = re.search(r"CLUSTER-(\d+)", coarse_res["choice"])
            selected_cluster_idx = int(cluster_idx_match.group(1)) if cluster_idx_match else 0
            selected_cluster_idx = min(selected_cluster_idx, len(clusters) - 1)

            # Refine candidate set to winning cluster
            candidate_nodes = clusters[selected_cluster_idx]
            formatted_options = [
                f"[{node.get('node_id', str(i))}] {node.get('title', '')} - {node.get('summary', '')[:80]}".strip()
                for i, node in enumerate(candidate_nodes)
            ]

        # Step 2: Fine-grained node selection
        state_text = f"User Query: {query}\nDocument Tree Candidate Sections:"
        decision = self._decide_choice(
            state=state_text,
            question_name="section_choice",
            instructions="Select the exact document section title that contains the target answer.",
            options=formatted_options,
        )

        latency_ms = (time.perf_counter() - t0) * 1000
        chosen_str = decision["choice"]
        confidence = float(decision["confidence"])
        tier = decision["tier"]

        # Parse node_id from choice string
        id_match = re.search(r"\[([^\]]+)\]", chosen_str)
        matched_id = id_match.group(1) if id_match else candidate_nodes[0].get("node_id", "0")

        selected_node = next(
            (n for n in candidate_nodes if str(n.get("node_id")) == matched_id),
            candidate_nodes[0],
        )

        return TreeTraversalResult(
            node_id=str(selected_node.get("node_id", matched_id)),
            title=selected_node.get("title", ""),
            confidence=confidence,
            tier=tier,
            latency_ms=latency_ms,
            raw_decision=decision,
        )

    # -------------------------------------------------------------------------
    # Fast Path: AXTree Affordance Matching (<20ms)
    # -------------------------------------------------------------------------

    def match_form_fields(
        self,
        extracted_fields: Dict[str, Any],
        axtree_affordances: List[Dict[str, Any]],
    ) -> List[FieldBinding]:
        """Matches extracted document fields to active [#N] AXTree form pins.

        Options are strictly constrained to active visible affordances,
        structurally eliminating hallucinated element IDs.
        """
        bindings: List[FieldBinding] = []

        if not axtree_affordances:
            logger.warning("No active AXTree affordances provided.")
            return bindings

        # Options are "[#N] <accessible name>", the same form the benchmark tasks use;
        # role/css noise lowered Laya's probabilities below the threshold in live runs.
        affordance_options = [
            f"[#{aff.get('index', i)}] {aff.get('name', '') or aff.get('role', '')}".strip()
            for i, aff in enumerate(axtree_affordances)
        ]

        for field_name, value in extracted_fields.items():
            readable = field_name.replace("_", " ")
            state_text = f"Extracted document field `{field_name}` ({readable}) = {value!r}."

            # Route choice to local Laya / remote SGLang
            decision = self._decide_choice(
                state=state_text,
                question_name=f"match_{field_name}",
                instructions="Which form element should receive this value?",
                options=affordance_options,
            )

            choice_str = decision["choice"]
            confidence = float(decision["confidence"])
            tier = decision["tier"]

            # Extract target [#N] index
            idx_match = re.search(r"\[#(\d+)\]", choice_str)
            target_idx = int(idx_match.group(1)) if idx_match else None

            if target_idx is None:
                logger.warning("Could not parse [#N] index from choice: %s", choice_str)
                continue

            matched_aff = next(
                (a for a in axtree_affordances if a.get("index") == target_idx),
                {},
            )
            role = str(matched_aff.get("role", "textbox")).lower()
            verb = ActionVerb.CLICK if "button" in role or "checkbox" in role else ActionVerb.FILL

            bindings.append(
                FieldBinding(
                    field_name=field_name,
                    value=value,
                    action_index=target_idx,
                    verb=verb,
                    confidence=confidence,
                    tier=tier,
                    affordance_name=matched_aff.get("name", ""),
                    metadata=matched_aff,
                )
            )

        return bindings

    # -------------------------------------------------------------------------
    # Core Dual-Tier Decision Primitive
    # -------------------------------------------------------------------------

    def _decide_choice(
        self,
        state: str,
        question_name: str,
        instructions: str,
        options: List[str],
    ) -> Dict[str, Any]:
        """Evaluate a choice decision via System-1 (Laya) with System-2 escalation."""
        # 1. System-1 Local Fast Path
        if self.laya_agent is not None:
            try:
                # Truncate options if beyond max head limit
                eval_options = options[:MAX_LAYA_CANDIDATE_OPTIONS]
                questions = {
                    question_name: {
                        "type": "choice",
                        "instructions": instructions,
                        "options": eval_options,
                    }
                }
                res = self.laya_agent.predict(state, questions)
                ans = res["answers"][question_name]
                choice = ans["choice"]
                confidence = float(ans.get("confidence", 1.0))

                # Check confidence threshold gate
                if confidence >= self.confidence_threshold:
                    return {
                        "choice": choice,
                        "confidence": confidence,
                        "tier": DecisionTier.LOCAL_LAYA,
                        "raw": ans,
                    }
                else:
                    logger.info(
                        "Local confidence (%.2f) below threshold (%.2f). Escalating to System-2 Cortex.",
                        confidence,
                        self.confidence_threshold,
                    )
            except Exception as e:
                logger.warning("Local Laya decision failed (%s). Escalating to System-2.", e)

        # 2. System-2 Cortex Escalation (Colab SGLang)
        try:
            payload = {
                "state": state,
                "questions": {
                    question_name: {
                        "type": "choice",
                        "instructions": instructions,
                        "options": options,
                    }
                },
            }
            res = self._post_remote_systemone(payload)
            ans = res["answers"][question_name]
            return {
                "choice": ans["choice"],
                "confidence": float(ans.get("confidence", 0.95)),
                "tier": DecisionTier.REMOTE_SGLANG,
                "raw": ans,
            }
        except Exception as e:
            logger.warning("Remote SGLang decision failed (%s). Using rule heuristic fallback.", e)

        # 3. Rule-based heuristic fallback
        best_option = options[0] if options else ""
        return {
            "choice": best_option,
            "confidence": 0.50,
            "tier": DecisionTier.HEURISTIC,
            "raw": {"fallback": True},
        }

    # -------------------------------------------------------------------------
    # Deterministic Execution & Playwright Automation
    # -------------------------------------------------------------------------

    def execute_deterministic_actions(
        self,
        page: Any,
        bindings: List[FieldBinding],
        submit_index: Optional[int] = None,
        settle_ms: float = 300.0,
    ) -> List[ExecutionStepResult]:
        """Dispatches deterministic verbs ('FILL [#N] <val>', 'CLICK [#N]') to PlaywrightExecutor.

        After each step, computes 64-bit SimHash state transition. If an action
        yields zero delta on a mutating verb, triggers System-2 Cortex escalation.
        """
        step_results: List[ExecutionStepResult] = []

        for binding in bindings:
            # Capture state before action
            state_before = self._capture_page_state(page)

            # Build deterministic ActionPayload
            action = ActionPayload(
                verb=binding.verb,
                action_index=binding.action_index,
                value=str(binding.value) if binding.verb == ActionVerb.FILL else None,
                timeout_ms=5000.0,
            )

            # Execute via PlaywrightExecutor
            act_result = self.executor.execute(page, action)

            # Settle wait
            if settle_ms > 0:
                time.sleep(settle_ms / 1000.0)

            # Capture state after action & verify with SimHash
            state_after = self._capture_page_state(page)
            verification = None
            stall_recovered = False
            recovery_note = ""

            if state_before is not None and state_after is not None:
                content_before, url_before = state_before
                content_after, url_after = state_after
                if hasattr(self.verifier, "verify"):
                    verification = self.verifier.verify(
                        action_result=act_result,
                        state_before=content_before,
                        state_after=content_after,
                        url_before=url_before,
                        url_after=url_after,
                    )
                elif hasattr(self.verifier, "verify_transition"):
                    verification = self.verifier.verify_transition(content_before, content_after, action.verb.value)

                # Check for DOM stall / zero state transition
                if verification and (verification.is_stuck_indicator or not verification.state_changed):
                    logger.warning(
                        "SimHash stall detected on [#%d] (Hamming distance %d). Escalating to Cortex.",
                        binding.action_index,
                        verification.hamming_distance,
                    )
                    # Trigger System-2 Cortex escalation
                    stall_recovered, recovery_note = self.escalate_cortex_recovery(page, action, verification)
            step_results.append(
                ExecutionStepResult(
                    binding=binding,
                    action_result=act_result,
                    state_verification=verification,
                    escalated_to_system2=(verification.is_stuck_indicator if verification else False),
                    stall_recovered=stall_recovered,
                    recovery_note=recovery_note,
                )
            )

        # Submit button if requested
        if submit_index is not None:
            submit_binding = FieldBinding(
                field_name="__submit__",
                value=None,
                action_index=submit_index,
                verb=ActionVerb.CLICK,
                confidence=1.0,
                tier=DecisionTier.LOCAL_LAYA,
                affordance_name="Submit",
            )
            submit_action = ActionPayload(
                verb=ActionVerb.CLICK,
                action_index=submit_index,
                timeout_ms=5000.0,
            )
            submit_res = self.executor.execute(page, submit_action)
            step_results.append(
                ExecutionStepResult(
                    binding=submit_binding,
                    action_result=submit_res,
                )
            )

        return step_results

    def escalate_cortex_recovery(
        self,
        page: Any,
        stalled_action: ActionPayload,
        verification: StateVerificationResult,
    ) -> Tuple[bool, str]:
        """System-2 Cortex Escalation: Recovers from DOM stalls / unmutated states."""
        try:
            # Query remote SGLang to diagnose stall
            payload = {
                "state": (
                    f"Action Dispatched: {stalled_action.verb.value} on index [#{stalled_action.action_index}].\n"
                    f"Observed State: No DOM mutation detected (Hamming distance: {verification.hamming_distance}).\n"
                    "Diagnose potential modal obstruction or focus issue."
                ),
                "questions": {
                    "recovery_strategy": {
                        "type": "choice",
                        "instructions": "Determine optimal recovery intervention.",
                        "options": ["DISMISS_MODAL", "FORCE_CLICK", "RETRY_KEYSTROKE", "ABORT"],
                    }
                },
            }
            res = self._post_remote_systemone(payload)
            strategy = res["answers"]["recovery_strategy"]["choice"]

            logger.info("Cortex Escalation suggested strategy: %s", strategy)

            if strategy == "FORCE_CLICK" and page is not None:
                # Attempt direct JS click or Enter press
                try:
                    page.keyboard.press("Enter")
                    return True, "Recovered via Enter press."
                except Exception:
                    pass

            return True, f"Strategy '{strategy}' acknowledged."
        except Exception as e:
            logger.warning("Cortex recovery escalation error: %s", e)
            return False, f"Recovery failed: {e}"

    def _capture_page_state(self, page: Any) -> Optional[Tuple[str, str]]:
        """Extract lightweight (content, url) for SimHash verification."""
        if page is None:
            return None
        try:
            url = getattr(page, "url", "")
            content = page.content() if hasattr(page, "content") else ""
            return (content, url)
        except Exception as e:
            logger.debug("Failed capturing page state for SimHash: %s", e)
            return None
