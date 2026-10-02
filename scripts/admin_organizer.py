#!/usr/bin/env python3
"""Autonomous Administrative File Organizer (System-1 / System-2 Hybrid Triage).

Monitors an incoming directory (~/Downloads/Inbox), extracts document preview metadata,
evaluates target categories, operational urgency (1-5), and action requirement via
local non-autoregressive Laya (421M ModernBERT), with automatic HTTP/2 keep-alive
fallback to remote SGLang /v1/systemone (Google Colab).

Enforces confidence gating (>0.70). Items below threshold route to Inbox/Review_Queue.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import urllib.request
import urllib.error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("admin_organizer")

DEFAULT_CATEGORIES = [
    "Finance/Invoices_and_Receipts",
    "Finance/Payroll_and_Compensation",
    "Finance/Tax_Filings_and_Audits",
    "Legal/Contracts_and_NDAs",
    "Legal/Compliance_and_Governance",
    "HR/Leave_and_Personnel",
    "HR/Recruitment_and_Offers",
    "Engineering/Architecture_and_RFCs",
    "Engineering/Incident_Postmortems",
    "Operations/Vendor_Agreements",
    "Operations/Facility_and_Logistics",
    "Archive/Junk_and_Temporary",
]

DEFAULT_INBOX = Path.home() / "Downloads" / "Inbox"
DEFAULT_ORGANIZED = Path.home() / "Documents" / "Organized"
DEFAULT_REVIEW_QUEUE = DEFAULT_INBOX / "Review_Queue"
DEFAULT_CONFIDENCE_THRESHOLD = 0.70
MAX_LAYA_CANDIDATES = 20


@dataclass
class TriageResult:
    target_folder: str
    confidence: float
    urgency: int
    requires_action: bool
    action_probability: float
    engine_used: str
    raw_response: Dict[str, Any]


class AdminTriageEngine:
    """Hybrid triage engine supporting Local Laya (421M) with Colab SGLang fallback."""

    def __init__(
        self,
        categories: Optional[List[str]] = None,
        colab_endpoint: Optional[str] = None,
        prefer_local: bool = True,
        head_max_len: int = 256,
    ):
        self.categories = categories or DEFAULT_CATEGORIES
        self.decision_spec = None
        try:
            from arc_cua.decision_endpoint import DecisionEndpointResolver

            self.decision_spec = DecisionEndpointResolver(endpoint=colab_endpoint).discover()
            self.colab_endpoint = self.decision_spec.systemone_url
            if not self.decision_spec.is_healthy:
                logger.warning(
                    "System-2 endpoint %s is not healthy (%s); triage will use the heuristic fallback.",
                    self.decision_spec.base_url,
                    self.decision_spec.health.get("error"),
                )
            else:
                logger.info("System-2 endpoint resolved via %s: %s", self.decision_spec.source, self.decision_spec.base_url)
        except Exception as e:
            from arc_cua.decision_endpoint import DEFAULT_GATEWAY_PORT

            logger.warning("Could not resolve System-2 endpoint (%s); using loopback default.", e)
            self.colab_endpoint = f"http://127.0.0.1:{DEFAULT_GATEWAY_PORT}/v1/systemone"
        self.prefer_local = prefer_local
        self.head_max_len = head_max_len
        self.laya_agent = None
        self._init_local_laya()

    def _init_local_laya(self) -> None:
        """Attempt to load convaiinnovations/laya-typed-decisions into memory."""
        if not self.prefer_local:
            return
        try:
            import laya  # type: ignore
            from arc_cua.index_bridge import LayaWireAdapter

            model_id = "convaiinnovations/laya-typed-decisions"
            logger.info("Initializing Local System-1 Laya (%s)...", model_id)
            self.laya_agent = LayaWireAdapter(laya.load(model_id), head_max_len=self.head_max_len)
            logger.info("Local Laya loaded.")
        except ImportError:
            logger.info(
                "Local 'laya' package not installed. Will route to remote SGLang /v1/systemone endpoint."
            )
        except Exception as e:
            logger.warning("Failed to initialize local Laya: %s. Using Colab fallback.", e)

    def extract_preview(self, file_path: Path, max_chars: int = 1500) -> str:
        """Extract lightweight text preview from file (PDF, TXT, MD, JSON, CSV)."""
        suffix = file_path.suffix.lower()
        stat = file_path.stat()
        meta_header = f"Filename: {file_path.name}\nSize: {stat.st_size} bytes\nSuffix: {suffix}\n"

        # Attempt PDF extraction if pypdf is present
        if suffix == ".pdf":
            try:
                import pypdf

                reader = pypdf.PdfReader(str(file_path))
                text = ""
                for page in reader.pages[:3]:
                    text += page.extract_text() or ""
                    if len(text) >= max_chars:
                        break
                return meta_header + "Content Preview:\n" + text[:max_chars].strip()
            except Exception as e:
                logger.debug("PDF reader fallback for %s: %s", file_path.name, e)

        # Plain text / fallback
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read(max_chars)
            return meta_header + "Content Preview:\n" + content.strip()
        except Exception as e:
            return meta_header + f"Preview unavailable: {e}"

    def _hierarchical_category_split(self, categories: List[str]) -> Dict[str, List[str]]:
        """Split categories into coarse top-level domains for budget management."""
        tree: Dict[str, List[str]] = {}
        for cat in categories:
            parts = cat.split("/", 1)
            top = parts[0]
            tree.setdefault(top, []).append(cat)
        return tree

    def predict_local(self, state: str, candidate_options: List[str]) -> Dict[str, Any]:
        """Execute decision via local Laya agent."""
        if self.laya_agent is None:
            raise RuntimeError("Local Laya agent not available.")

        # If candidates exceed head budget (>20), apply coarse-to-fine hierarchical ranking
        if len(candidate_options) > MAX_LAYA_CANDIDATES:
            logger.info(
                "Candidate set (%d) > %d. Applying coarse-to-fine hierarchical routing.",
                len(candidate_options),
                MAX_LAYA_CANDIDATES,
            )
            tree = self._hierarchical_category_split(candidate_options)
            coarse_options = list(tree.keys())

            # Step 1: Coarse domain classification
            coarse_q = {
                "top_domain": {
                    "type": "choice",
                    "instructions": "Determine high-level operational domain.",
                    "options": coarse_options,
                }
            }
            coarse_res = self.laya_agent.predict(state, coarse_q)
            chosen_top = coarse_res["answers"]["top_domain"]["choice"]
            candidate_options = tree.get(chosen_top, candidate_options[:MAX_LAYA_CANDIDATES])

        questions = {
            "target_folder": {
                "type": "choice",
                "instructions": "Determine the single most appropriate target directory for this document.",
                "options": candidate_options,
            },
            "urgency": {
                "type": "score",
                "instructions": "Rate operational urgency (1=low, 5=critical).",
                "scale": [1, 2, 3, 4, 5],
            },
            "requires_action": {
                "type": "noul",
                "instructions": "Does this document require signature, payment, approval, or manual review?",
            },
        }

        # Predict with local ModernBERT non-autoregressive forward pass
        return self.laya_agent.predict(state, questions)

    def predict_remote(self, state: str, candidate_options: List[str]) -> Dict[str, Any]:
        """Execute decision via remote SGLang /v1/systemone endpoint over HTTP keep-alive."""
        payload = {
            "state": state,
            "questions": {
                "target_folder": {
                    "type": "choice",
                    "instructions": "Determine the single most appropriate target directory for this document.",
                    "options": candidate_options,
                },
                "urgency": {
                    "type": "score",
                    "instructions": "Rate operational urgency (1=low, 5=critical).",
                    "scale": [1, 2, 3, 4, 5],
                },
                "requires_action": {
                    "type": "noul",
                    "instructions": "Does this document require signature, payment, approval, or manual review?",
                },
            },
        }

        req = urllib.request.Request(
            self.colab_endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Connection": "keep-alive"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12.0) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Colab decision endpoint error HTTP {resp.status}")
            return json.loads(resp.read().decode("utf-8"))

    def triage_file(self, file_path: Path) -> TriageResult:
        """Triage an incoming file using Local Laya with SGLang fallback."""
        preview = self.extract_preview(file_path)
        engine_used = "local_laya"
        res = None

        if self.laya_agent is not None:
            try:
                res = self.predict_local(preview, self.categories)
            except Exception as e:
                logger.warning("Local Laya triage failed (%s). Falling back to Colab SGLang.", e)
                res = None

        if res is None:
            engine_used = "remote_sglang"
            try:
                res = self.predict_remote(preview, self.categories)
            except Exception as e:
                logger.error("Both Local Laya and Colab SGLang failed: %s", e)
                # Graceful heuristic fallback
                return TriageResult(
                    target_folder="Archive/Junk_and_Temporary",
                    confidence=0.0,
                    urgency=1,
                    requires_action=False,
                    action_probability=0.0,
                    engine_used="heuristic_failure",
                    raw_response={"error": str(e)},
                )

        answers = res.get("answers", {})
        tf = answers.get("target_folder", {})
        urg = answers.get("urgency", {})
        act = answers.get("requires_action", {})

        target = tf.get("choice", self.categories[0])
        confidence = float(tf.get("confidence", 0.0))
        urgency_score = int(urg.get("score", 1))

        # noul/yes_no handling
        act_probs = act.get("probabilities", {})
        yes_prob = float(act_probs.get("yes", 0.0) if isinstance(act_probs, dict) else 0.0)
        requires_action = yes_prob >= 0.5 or str(act.get("choice", "")).lower() == "yes"

        return TriageResult(
            target_folder=target,
            confidence=confidence,
            urgency=urgency_score,
            requires_action=requires_action,
            action_probability=yes_prob,
            engine_used=engine_used,
            raw_response=res,
        )


class FileOrganizerWatcher:
    """Watches inbox directory and dispatches files according to confidence gating."""

    def __init__(
        self,
        inbox_dir: Path = DEFAULT_INBOX,
        organized_dir: Path = DEFAULT_ORGANIZED,
        review_dir: Path = DEFAULT_REVIEW_QUEUE,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        engine: Optional[AdminTriageEngine] = None,
        dry_run: bool = False,
    ):
        self.inbox_dir = inbox_dir
        self.organized_dir = organized_dir
        self.review_dir = review_dir
        self.confidence_threshold = confidence_threshold
        self.engine = engine or AdminTriageEngine()
        self.dry_run = dry_run

    def process_file(self, file_path: Path) -> Optional[Path]:
        """Triage single file and move based on confidence gate."""
        if not file_path.is_file():
            return None
        # Ignore hidden files, temporary downloads, or files already in review queue
        if file_path.name.startswith(".") or file_path.name.endswith(".crdownload"):
            return None
        if self.review_dir in file_path.parents or file_path.parent == self.review_dir:
            return None

        logger.info("Processing '%s' (%.1f KB)...", file_path.name, file_path.stat().st_size / 1024)
        t0 = time.perf_counter()
        result = self.engine.triage_file(file_path)
        latency_ms = (time.perf_counter() - t0) * 1000

        logger.info(
            "[%s] -> %s (Conf: %.2f, Urgency: %d, ActionReq: %.2f) [%s in %.1fms]",
            file_path.name,
            result.target_folder,
            result.confidence,
            result.urgency,
            result.action_probability,
            result.engine_used,
            latency_ms,
        )

        # Confidence Gating
        if result.confidence >= self.confidence_threshold:
            dest_dir = self.organized_dir / result.target_folder
            dest_path = dest_dir / file_path.name
            action_label = "ORGANIZED"
        else:
            dest_dir = self.review_dir
            dest_path = dest_dir / file_path.name
            action_label = "REVIEW_QUEUE"
            logger.warning(
                "Low confidence (%.2f < %.2f) for '%s' -> Routed to Review Queue.",
                result.confidence,
                self.confidence_threshold,
                file_path.name,
            )

        if not self.dry_run:
            dest_dir.mkdir(parents=True, exist_ok=True)
            # Write sidecar triage audit metadata
            sidecar_path = dest_path.with_suffix(dest_path.suffix + ".triage.json")
            with open(sidecar_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "source_file": str(file_path),
                        "triage": asdict(result),
                        "latency_ms": latency_ms,
                        "timestamp": time.time(),
                    },
                    f,
                    indent=2,
                )
            shutil.move(str(file_path), str(dest_path))
            logger.info("Moved [%s] -> %s", action_label, dest_path)
        else:
            logger.info("[DRY RUN] Would move [%s] -> %s", action_label, dest_path)

        return dest_path

    def run_once(self) -> int:
        """Scan inbox once and process pending files."""
        if not self.inbox_dir.exists():
            logger.info("Inbox directory does not exist: %s. Creating...", self.inbox_dir)
            self.inbox_dir.mkdir(parents=True, exist_ok=True)
            return 0

        count = 0
        for item in sorted(self.inbox_dir.iterdir()):
            if item.is_file() and not item.name.endswith(".triage.json"):
                self.process_file(item)
                count += 1
        return count

    def run_watch(self, poll_interval: float = 3.0) -> None:
        """Continuous polling watcher loop."""
        logger.info(
            "Starting directory watcher on '%s' (poll interval: %.1fs)...",
            self.inbox_dir,
            poll_interval,
        )
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.organized_dir.mkdir(parents=True, exist_ok=True)
        self.review_dir.mkdir(parents=True, exist_ok=True)

        try:
            while True:
                self.run_once()
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            logger.info("Directory watcher stopped by user.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Administrative File Organizer via System-1/2 Hybrid Decision Engine")
    parser.add_argument("--inbox", type=Path, default=DEFAULT_INBOX, help="Incoming inbox folder to watch")
    parser.add_argument("--organized", type=Path, default=DEFAULT_ORGANIZED, help="Root destination folder")
    parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW_QUEUE, help="Low-confidence review queue folder")
    parser.add_argument("--threshold", type=float, default=DEFAULT_CONFIDENCE_THRESHOLD, help="Confidence threshold gate")
    parser.add_argument("--endpoint", type=str, default=None, help="Remote SGLang decision endpoint URL")
    parser.add_argument("--watch", action="store_true", help="Run continuous directory watcher loop")
    parser.add_argument("--dry-run", action="store_true", help="Simulate routing decisions without moving files")
    args = parser.parse_args()

    engine = AdminTriageEngine(colab_endpoint=args.endpoint)
    watcher = FileOrganizerWatcher(
        inbox_dir=args.inbox,
        organized_dir=args.organized,
        review_dir=args.review,
        confidence_threshold=args.threshold,
        engine=engine,
        dry_run=args.dry_run,
    )

    if args.watch:
        watcher.run_watch()
    else:
        processed = watcher.run_once()
        logger.info("Batch triage complete. Processed %d file(s).", processed)


if __name__ == "__main__":
    main()
