from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.run_suite import run_blackbox, run_case


class RunSuiteBlackBoxTest(unittest.TestCase):
    def test_case_invokes_only_public_campaign_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "run_campaign.py").write_text("# public entrypoint\n", encoding="utf-8")
            run_root = root / "result"
            query = "保持完全不变的测试 Query"
            case = {
                "case_id": "A01",
                "title": "A01 test",
                "query": query,
                "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            }
            args = argparse.Namespace(
                case_timeout=10,
                poll_seconds=0.1,
                keep_waiting_for_observation=False,
                dry_run=True,
            )
            summary = run_case(
                case,
                repo=repo,
                run_root=run_root,
                run_stamp="20260719-000000",
                python=Path(sys.executable),
                args=args,
                base_env={},
                api_key="test-secret-not-for-log",
                campaign_model="test-model",
                campaign_endpoint="https://provider.invalid/v1",
                campaign_wire_api="codex_responses",
                campaign_reasoning_effort="xhigh",
            )
            log = (run_root / "A01" / "chem_agent.log").read_text(encoding="utf-8")
            self.assertIn("run_campaign.py", log)
            self.assertIn(query, log)
            self.assertIn("--online-literature", log)
            self.assertNotIn("run_research_agent.py", log)
            self.assertNotIn("run_from_research_state.py", log)
            self.assertNotIn("--execution-adapter", log)
            self.assertIn("--model-name test-model", log)
            self.assertIn("--base-url https://provider.invalid/v1", log)
            self.assertIn("--wire-api codex_responses", log)
            self.assertIn("--reasoning-effort xhigh", log)
            self.assertNotIn("test-secret-not-for-log", log)
            self.assertEqual(summary["blackbox_input_contract"]["online_literature"], True)

    def test_awaiting_observation_is_an_external_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            campaign_dir = root / "campaign"
            marker = campaign_dir / "iteration_01" / "AWAITING_OBSERVATION.md"
            code = (
                "from pathlib import Path; import time; "
                f"p=Path({str(marker)!r}); p.parent.mkdir(parents=True); "
                "p.write_text('waiting', encoding='utf-8'); time.sleep(30)"
            )
            result = run_blackbox(
                [sys.executable, "-c", code],
                cwd=root,
                env=dict(os.environ),
                log_path=root / "chem_agent.log",
                campaign_dir=campaign_dir,
                timeout=10,
                poll_seconds=0.1,
                stop_at_awaiting_observation=True,
                dry_run=False,
            )
            self.assertEqual(result["boundary_status"], "awaiting_observation")
            self.assertEqual(Path(result["awaiting_observation_marker"]), marker.resolve())
            self.assertFalse(result["timed_out"])


if __name__ == "__main__":
    unittest.main()
