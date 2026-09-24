from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.validate_evaluation import CASE_IDS, main


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class ValidateEvaluationTest(unittest.TestCase):
    def build_complete_run(self, root: Path) -> Path:
        run = root / "chem-agent-eval-test"
        evaluation = run / "evaluation"
        write_json(
            run / "suite_manifest.json",
            {
                "case_ids": CASE_IDS,
                "online_literature": True,
                "real_device_dispatch": False,
                "dry_run": False,
                "campaign_model": "real-model",
                "campaign_endpoint": "https://provider.invalid/v1",
                "campaign_wire_api": "codex_responses",
                "schema_llm_review": True,
            },
        )

        summaries = []
        audits = []
        reviews = []
        verdicts = []
        matrix = []
        for case_id in CASE_IDS:
            query = f"exact query {case_id}"
            digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
            case_dir = run / case_id
            write_json(
                case_dir / "input.json",
                {"case_id": case_id, "query": query, "query_sha256": digest},
            )
            (case_dir / "query.sha256").write_text(digest + "\n", encoding="utf-8")
            write_json(case_dir / "case_summary.json", {"case_id": case_id})
            write_json(
                case_dir / "blackbox" / f"{case_id}-test" / "research_state.json",
                {"event": {"query": query}, "macro_plan": [{"step": 1}]},
            )
            summary = {
                "case_id": case_id,
                "query_sha256": digest,
                "query_exact_match": True,
                "online_literature_requested": True,
                "boundary_status": "goal_reached",
                "research_states": ["blackbox/research_state.json"],
                "workflow_packages": [],
            }
            summaries.append(summary)
            audits.append(
                {
                    "case_id": case_id,
                    "checked_workflows": 0,
                    "checked_steps": 0,
                    "dispatch_schema_match": "not_evaluable",
                }
            )
            reviews.append(
                {
                    "case_id": case_id,
                    "review_status": "not_evaluable",
                    "checked_workflow_count": 0,
                    "checked_step_count": 0,
                    "verdict": "not_evaluable",
                }
            )
            verdicts.append(
                {
                    "case_id": case_id,
                    "evaluation_complete": True,
                    "dispatch_schema_match": "not_evaluable",
                }
            )
            case_eval = {
                "case_id": case_id,
                "process_completion": "yes",
                "paper_quality_summary": "mixed",
                "plan_workstation_match": "not_evaluable",
                "dispatch_schema_match": "not_evaluable",
            }
            write_json(evaluation / case_id / "evaluation.json", case_eval)
            (evaluation / case_id / "evaluation.md").write_text(
                f"# {case_id}\n", encoding="utf-8"
            )
            matrix.append(case_eval)

        write_json(run / "raw_summary.json", summaries)
        write_json(evaluation / "workstation_schema_audit.json", {"cases": audits})
        write_json(evaluation / "workstation_schema_llm_review.json", {"cases": reviews})
        write_json(
            evaluation / "workstation_schema_verdict.json",
            {"evaluation_complete": True, "cases": verdicts},
        )
        write_json(evaluation / "verdict_matrix.json", {"cases": matrix})
        (evaluation / "overall_report.md").write_text("# Overall\n", encoding="utf-8")
        (evaluation / "workstation_direct_acceptance.md").write_text(
            "# Direct acceptance\n", encoding="utf-8"
        )
        return run

    def test_complete_run_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run = self.build_complete_run(Path(temp))
            with patch.object(sys, "argv", ["validate_evaluation.py", "--run", str(run)]):
                self.assertEqual(main(), 0)
            result = json.loads(
                (run / "evaluation/completion_check.json").read_text(encoding="utf-8")
            )
            self.assertTrue(result["complete"])

    def test_recorded_llm_failure_blocks_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run = self.build_complete_run(Path(temp))
            failure = run / "A01/blackbox/A01-test/device_state.json"
            write_json(failure, {"log": "LLM call failed after retries"})
            with patch.object(sys, "argv", ["validate_evaluation.py", "--run", str(run)]):
                self.assertEqual(main(), 1)
            result = json.loads(
                (run / "evaluation/completion_check.json").read_text(encoding="utf-8")
            )
            self.assertFalse(result["complete"])
            self.assertGreater(result["llm_failure_count"], 0)


if __name__ == "__main__":
    unittest.main()
