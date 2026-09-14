"""Offline behavior tests for the single task-oriented online skill."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from pydantic import ValidationError

from reaserch_agent.tools.online_research import OnlineResearchService


CAPABILITIES = {
    "tier": "experiment", "skill": "experiment-capabilities", "projection_version": "1.0",
    "source_digest_sha256": "test-source", "workstations": [],
    "capabilities": [{"id": "xrd", "name": "XRD", "support_status": "supported", "currently_usable": True}],
}


class OnlineResearchSkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.records = [{"paper_id": "p1", "title": "XRD paper", "doi": "10.1000/test", "source": "crossref",
                         "verification_status": "doi_verified", "full_text_status": "metadata_only", "corpus_files": []}]
        self.acquisition = Mock()
        self.acquisition.registry.all.return_value = self.records
        self.acquisition.acquire_for_bootstrap.return_value = {
            "seeds": [], "candidates_log": [{"title": "XRD paper", "doi": "10.1000/test", "kept": True}],
            "written_files": [], "errors": [], "retrieval_status": "success", "actual_scholarly_queries": ["XRD"],
        }
        self.acquisition.acquire_for_repair.return_value = {"kept": 0, "written_files": [], "errors": []}
        self.executor = Mock()
        self.executor.kb_dir = Path(self.temp.name)
        self.executor.enable_paper_download = False
        self.executor.execute.return_value = {"status": "error", "error": "disabled"}
        self.factory = Mock(return_value=self.acquisition)
        self.service = OnlineResearchService(self.executor, CAPABILITIES, self.factory)

    def test_schema_exposes_one_task_not_internal_operations(self):
        tool = self.service.as_tool()
        self.assertEqual(tool.name, "online_research")
        properties = tool.args_schema.model_json_schema()["properties"]
        self.assertEqual(set(properties), {"query", "objective", "references", "evidence_depth"})
        with self.assertRaises(ValidationError):
            tool.invoke({"query": "XRD", "device_context": {"supports_everything": True}})
        with self.assertRaises(ValidationError):
            tool.invoke({"query": "XRD", "action": "paper_search"})

    def test_shared_service_returns_evidence_not_just_archive_counts(self):
        result = self.service.as_tool().invoke({"query": "XRD protocol"})
        self.assertEqual(result["results"][0]["doi"], "10.1000/test")
        self.assertEqual(result["results"][0]["capability_assessment"]["status"], "unknown")
        self.assertEqual(result["device_capability_snapshot"], "test-source")
        self.assertEqual(result["actual_scholarly_queries"], ["XRD"])

    def test_explicit_download_depth_cannot_override_disabled_policy(self):
        result = self.service.run("XRD", evidence_depth="full_text")
        self.assertFalse(self.acquisition.download_pdfs)
        self.assertEqual(result["download_status"], "disabled")
        self.executor.execute.assert_not_called()

    def test_download_depth_and_permission_use_existing_oa_executor(self):
        self.executor.enable_paper_download = True
        corpus = str(Path(self.temp.name) / "new_protocol.json")
        pdf = str(Path(self.temp.name) / "paper.pdf")
        self.executor.execute.return_value = {"status": "ok", "full_text_status": "parsed", "archived": True,
                                             "corpus_file": corpus, "pdf_file": pdf}
        result = self.service.run("XRD", evidence_depth="full_text")
        self.assertTrue(self.acquisition.download_pdfs)
        self.assertEqual(self.executor.execute.call_args.args[0]["tool"], "paper_download")
        self.assertEqual(result["results"][0]["full_text_status"], "parsed")
        self.assertEqual(result["results"][0]["corpus_files"][0], corpus)
        self.assertIn(pdf, result["results"][0]["pdf_files"])
        self.assertIn(corpus, result["written_files"])

    def test_repair_uses_same_service_and_sanitized_queries(self):
        self.service.run("XRD", survey_queries=["XRD phase analysis"], mode="repair", stage="phase")
        self.acquisition.acquire_for_repair.assert_called_once()
        self.assertEqual(self.acquisition.acquire_for_repair.call_args.kwargs["stage"], "phase")
        self.acquisition.acquire_for_bootstrap.assert_not_called()

    def test_public_url_read_uses_safe_existing_executor(self):
        self.service.as_tool().invoke({"query": "XRD", "references": ["https://example.test/paper"]})
        self.executor.execute.assert_called_once_with({"tool": "web_read", "url": "https://example.test/paper"})

    def test_model_cannot_request_local_file_access(self):
        with self.assertRaises(ValueError):
            self.service.as_tool().invoke({"query": "XRD", "references": ["/etc/hosts"]})
        self.factory.assert_not_called()

    def test_query_planning_receives_high_level_capabilities(self):
        model = Mock()
        model.bind_tools.return_value = model
        model.invoke.side_effect = [SimpleNamespace(content='{"queries":["XRD phase analysis"]}'), SimpleNamespace(content='{"assessments":[]}')]
        service = OnlineResearchService(self.executor, CAPABILITIES, self.factory, query_model=model)
        service.run("material characterization")
        self.assertIn('"id": "xrd"', model.invoke.call_args_list[0].args[0][1].content)
        self.assertEqual(self.acquisition.acquire_for_bootstrap.call_args.kwargs["survey_queries"], ["XRD phase analysis"])

    def test_unsupported_reference_is_retained_and_cannot_fake_compatible(self):
        self.records.append({**self.records[0], "paper_id": "p2", "title": "Other paper", "doi": "10.1000/other"})
        self.acquisition.acquire_for_bootstrap.return_value["candidates_log"].append({"title": "Other paper", "kept": True})
        model = Mock()
        model.bind_tools.return_value = model
        model.invoke.return_value = SimpleNamespace(content='{"assessments":['
            '{"paper_id":"p1","status":"out_of_scope","required_capability_ids":[],"missing_capabilities":["in situ XPS"],"evidence_quote":"requires in situ XPS","reason":"not declared"},'
            '{"paper_id":"p2","status":"compatible","required_capability_ids":["invented-device"],"missing_capabilities":[],"evidence_quote":"requires in situ XPS"}]}')
        service = OnlineResearchService(self.executor, CAPABILITIES, self.factory, query_model=model)
        original = service._record_result

        def with_excerpt(record):
            result = original(record)
            result["evidence_excerpt"] = "This protocol requires in situ XPS."
            return result

        service._record_result = with_excerpt
        result = service.run("XRD", survey_queries=["XRD"])
        assessments = {r["paper_id"]: r["capability_assessment"] for r in result["results"]}
        self.assertEqual(len(assessments), 2)
        self.assertEqual(assessments["p1"]["status"], "out_of_scope")
        self.assertEqual(assessments["p2"]["status"], "unknown")

    def test_provider_failure_not_reported_as_empty_search(self):
        self.acquisition.acquire_for_bootstrap.return_value = {"retrieval_status": "provider_failure", "errors": ["timeout"], "written_files": []}
        result = self.service.run("XRD")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["retrieval_status"], "provider_failure")

    def test_repair_provider_failure_is_not_empty_even_for_legacy_summary(self):
        self.acquisition.acquire_for_repair.return_value = {"kept": 0, "errors": ["timeout"], "written_files": []}
        result = self.service.run("XRD", mode="repair", survey_queries=["XRD"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["retrieval_status"], "provider_failure")

    def test_repair_cache_hit_is_returned_without_new_files(self):
        self.acquisition.acquire_for_repair.return_value = {
            "kept": 1, "written_files": [], "errors": [],
            "candidates_log": [{"title": "XRD paper", "doi": "10.1000/test", "kept": True}],
        }
        result = self.service.run("XRD", mode="repair", survey_queries=["XRD"])
        self.assertEqual(result["results"][0]["doi"], "10.1000/test")
        self.assertFalse(result["archived"])


if __name__ == "__main__":
    unittest.main()
