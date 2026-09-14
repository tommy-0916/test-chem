"""Issue 1 regression tests: literature search queries must be chemistry-only.

Task/device context (自动化/工作站/设备/机器人/实验室编号/下发任务…) must never
reach scholarly search, while the original user query stays untouched and
chemistry entities survive sanitization.
"""

from __future__ import annotations

import unittest

from reaserch_agent.tools.query_sanitizer import (
    removed_context_terms,
    sanitize_search_queries,
    sanitize_search_query,
)
from reaserch_agent.workflow import ResearchAgent

POLLUTED_QUERY = (
    "我希望基于现有自动化化学工作站，设计一个 NiFe 普鲁士蓝类似物（NiFe-PBA）"
    "的合成实验并下发到303实验室，返回任务ID"
)
BANNED_TOKENS = (
    "自动化",
    "工作站",
    "设备",
    "机器人",
    "303",
    "实验室",
    "下发",
    "任务ID",
    "workstation",
    "automation",
    "automated",
    "robot",
)


class QuerySanitizerTest(unittest.TestCase):
    def assert_clean(self, text: str) -> None:
        lowered = text.lower()
        for token in BANNED_TOKENS:
            self.assertNotIn(token.lower(), lowered, f"`{token}` leaked into: {text}")

    def test_polluted_chinese_query_keeps_chemistry_only(self) -> None:
        cleaned = sanitize_search_query(POLLUTED_QUERY)
        self.assert_clean(cleaned)
        self.assertIn("NiFe", cleaned)
        self.assertIn("普鲁士蓝类似物", cleaned)
        self.assertIn("NiFe-PBA", cleaned)

    def test_polluted_english_query_keeps_chemistry_only(self) -> None:
        cleaned = sanitize_search_query(
            "NiFe Prussian blue analogue synthesis automated chemistry "
            "workstation 303 laboratory robotic dispatch"
        )
        self.assert_clean(cleaned)
        self.assertIn("Prussian blue analogue synthesis", cleaned)

    def test_electrochemistry_survives_workstation_stripping(self) -> None:
        cleaned = sanitize_search_query("双工位电化学工作站上进行 NiFe-PBA 电化学活化 CV LSV 测试")
        self.assert_clean(cleaned)
        self.assertIn("电化学活化", cleaned)
        self.assertIn("CV", cleaned)

    def test_pure_filler_queries_are_dropped(self) -> None:
        kept = sanitize_search_queries(
            ["automated robotic experiment design", "请设计实验", "普鲁士蓝 水系钾离子电池"]
        )
        self.assertEqual(kept, ["普鲁士蓝 水系钾离子电池"])

    def test_clean_chemistry_query_is_untouched(self) -> None:
        query = "NiFe Prussian blue analogue aqueous coprecipitation synthesis"
        self.assertEqual(sanitize_search_query(query), query)

    def test_removed_terms_are_reported_for_auditing(self) -> None:
        removed = removed_context_terms(POLLUTED_QUERY)
        self.assertTrue(any("工作站" in term for term in removed))

    def test_task_dispatch_clause_removed_whole(self) -> None:
        """Issue 8 P0-2: the full dispatch clause must vanish — word-level
        removal alone left `请将实验 ，并返回该实验任务的 id。` behind."""
        query = (
            "为了探究 NiFe LDH 在碱性 OER 下的表面重构，请围绕 NiFe LDH 设计实验方案。"
            "请将实验下发至 303 实验室，并返回该实验任务的 id。"
        )
        cleaned = sanitize_search_query(query)
        for residue in ("请将实验", "返回该实验", "任务的 id", "任务的id"):
            self.assertNotIn(residue, cleaned)
        self.assertIn("NiFe LDH", cleaned)
        self.assertIn("OER", cleaned)

    def test_dispatch_clause_variants_removed(self) -> None:
        cases = [
            "合成 CoMo 硫化物并进行 HER 测试。请把该任务提交到自动化工作站。",
            "请在303实验室的自动化平台上执行 NiCo EOR 对照实验，返回任务编号",
        ]
        for query in cases:
            cleaned = sanitize_search_query(query)
            for residue in ("提交到", "下发", "返回任务", "任务编号", "实验室"):
                self.assertNotIn(residue, cleaned, f"`{residue}` in: {cleaned}")


class SurveyQueryPipelineTest(unittest.TestCase):
    """The heuristic planning path must emit sanitized queries end to end."""

    def test_heuristic_survey_queries_are_sanitized(self) -> None:
        agent = ResearchAgent(model=None, use_llm=False)
        state = agent.run(
            event_type="bootstrap",
            query=POLLUTED_QUERY,
            constraints={"device_context": {"workstations": [{"station_name": "Liquid_Handling_Station_1ml_V2"}]}},
        )

        self.assertTrue(state.survey_queries)
        for query in state.survey_queries:
            lowered = query.lower()
            for token in BANNED_TOKENS:
                self.assertNotIn(
                    token.lower(), lowered, f"`{token}` leaked into survey query: {query}"
                )
        # the original user query is preserved verbatim in state
        self.assertEqual(state.event.query, POLLUTED_QUERY)
        # sanitized queries are logged for auditability
        self.assertTrue(
            any("survey queries after sanitization" in log for log in state.logs)
        )


class AcquisitionOutboundQueryTest(unittest.TestCase):
    """Queries actually sent to paper databases must be sanitized + logged."""

    def test_bootstrap_keyword_line_receives_sanitized_query(self) -> None:
        from reaserch_agent.tools.literature_acquisition import LiteratureAcquisition

        sent_all: list = []

        class StubClient:
            last_errors: list = []

            def search(self, query, sources=(), max_results=5):
                sent_all.append(query)
                return []

            def fetch_references(self, seed, limit=0):
                return []

            def fetch_citations(self, seed, limit=0):
                return []

        acquisition = LiteratureAcquisition.__new__(LiteratureAcquisition)
        acquisition.client = StubClient()
        acquisition.errors = []
        acquisition.keyword_sources = ["semantic_scholar"]
        acquisition.max_keyword_results = 3
        acquisition.max_snowball_per_seed = 0
        acquisition.web_client = None
        acquisition.enable_web = False
        # dll_main branch: scholarly search must be on to reach the keyword line
        acquisition.enable_scholarly_search = True
        acquisition._resolve_seeds = lambda refs: []
        acquisition._filter_candidates = lambda candidates, anchor, seeds: []
        acquisition._archive_paper = lambda paper, role="", stage="": ([], False)
        acquisition._acquire_web_knowledge = lambda *args, **kwargs: {}
        acquisition._safe = lambda fn, label: fn()
        acquisition._paper_summary = lambda paper: {}

        summary = acquisition.acquire_for_bootstrap(POLLUTED_QUERY, [])

        # every outbound query (incl. zero-hit retries) must be sanitized
        self.assertTrue(sent_all)
        for outbound in sent_all:
            lowered = outbound.lower()
            for token in BANNED_TOKENS:
                self.assertNotIn(token.lower(), lowered)
        self.assertIn("NiFe-PBA", sent_all[0])
        # all outbound queries are recorded for auditing
        self.assertEqual(summary["actual_scholarly_queries"], sent_all)
        self.assertIn(sent_all[0], summary["keyword_query"])

    def test_bootstrap_consumes_survey_queries_not_long_query(self) -> None:
        """Issue 8 P0-1: when survey_queries exist, the scholarly line must
        send THOSE short queries — never the (long) sanitized user query."""
        from reaserch_agent.tools.literature_acquisition import LiteratureAcquisition

        sent: list = []

        class StubClient:
            last_errors: list = []

            def search(self, query, sources=(), max_results=5):
                sent.append(query)
                return []

            def fetch_references(self, seed, limit=0):
                return []

            def fetch_citations(self, seed, limit=0):
                return []

        acquisition = LiteratureAcquisition.__new__(LiteratureAcquisition)
        acquisition.client = StubClient()
        acquisition.errors = []
        acquisition.keyword_sources = ["semantic_scholar"]
        acquisition.max_keyword_results = 8
        acquisition.max_snowball_per_seed = 0
        acquisition.web_client = None
        acquisition.enable_web = False
        acquisition.enable_scholarly_search = True
        acquisition._resolve_seeds = lambda refs: []
        acquisition._filter_candidates = lambda candidates, anchor, seeds: []
        acquisition._archive_paper = lambda paper, role="", stage="": ([], False)
        acquisition._acquire_web_knowledge = lambda *args, **kwargs: {}
        acquisition._safe = lambda fn, label: fn()
        acquisition._paper_summary = lambda paper: {}

        survey = [
            "NiFe LDH 共沉淀合成 Fe配位环境 碱性OER",
            "NiFe 催化剂 XRD XPS 表面价态",
        ]
        summary = acquisition.acquire_for_bootstrap(
            POLLUTED_QUERY, [], survey_queries=survey
        )

        # the survey queries went out first, individually; the long query
        # never went out. Zero-hit retries may follow (bounded, recorded).
        self.assertEqual(sent[: len(survey)], survey)
        self.assertEqual(summary["actual_scholarly_queries"], sent)
        self.assertEqual(summary["generated_survey_queries"], survey)
        for outbound in sent:
            self.assertLess(len(outbound), 90)
            self.assertNotIn("请将实验", outbound)
            self.assertNotIn("工作站", outbound)

    def test_zero_hit_triggers_bounded_retry_and_status(self) -> None:
        """Issue 8 P1: zero-keep first pass → at most 2 deterministic retry
        rounds, all queries recorded; clean zero result (no provider errors)
        is reported as no_relevant_papers, never provider_failure."""
        from reaserch_agent.tools.literature_acquisition import LiteratureAcquisition

        sent: list = []

        class EmptyClient:
            last_errors: list = []

            def search(self, query, sources=(), max_results=5):
                sent.append(query)
                return []

            def fetch_references(self, seed, limit=0):
                return []

            def fetch_citations(self, seed, limit=0):
                return []

        acquisition = LiteratureAcquisition.__new__(LiteratureAcquisition)
        acquisition.client = EmptyClient()
        acquisition.errors = []
        acquisition.keyword_sources = ["semantic_scholar"]
        acquisition.max_keyword_results = 6
        acquisition.max_snowball_per_seed = 0
        acquisition.web_client = None
        acquisition.enable_web = False
        acquisition.enable_scholarly_search = True
        acquisition._resolve_seeds = lambda refs: []
        acquisition._filter_candidates = lambda candidates, anchor, seeds: []
        acquisition._archive_paper = lambda paper, role="", stage="": ([], False)
        acquisition._acquire_web_knowledge = lambda *args, **kwargs: {}
        acquisition._safe = lambda fn, label: fn()
        acquisition._paper_summary = lambda paper: {}

        survey = ["NiFe LDH 共沉淀 XRD 碱性OER"]
        summary = acquisition.acquire_for_bootstrap(
            POLLUTED_QUERY, [], survey_queries=survey
        )

        # retry rounds happened, bounded, and were recorded
        self.assertTrue(summary["zero_hit_retry_rounds"])
        self.assertLessEqual(len(summary["zero_hit_retry_rounds"]), 2)
        self.assertGreater(len(summary["actual_scholarly_queries"]), len(survey))
        # round 1 strips characterization terms (XRD gone, chemistry kept)
        round1 = summary["zero_hit_retry_rounds"][0]
        self.assertEqual(round1["round"], "strip_characterization")
        for retry_query in round1["queries"]:
            self.assertNotIn("XRD", retry_query)
        # clean zero result → no_relevant_papers (provider was healthy)
        self.assertEqual(summary["retrieval_status"], "no_relevant_papers")

    def test_provider_failure_status_when_errors_present(self) -> None:
        from reaserch_agent.tools.literature_acquisition import LiteratureAcquisition

        class BrokenClient:
            last_errors = ["semantic_scholar: HTTP 429 too many requests"]

            def search(self, query, sources=(), max_results=5):
                return []

            def fetch_references(self, seed, limit=0):
                return []

            def fetch_citations(self, seed, limit=0):
                return []

        acquisition = LiteratureAcquisition.__new__(LiteratureAcquisition)
        acquisition.client = BrokenClient()
        acquisition.errors = []
        acquisition.keyword_sources = ["semantic_scholar"]
        acquisition.max_keyword_results = 6
        acquisition.max_snowball_per_seed = 0
        acquisition.web_client = None
        acquisition.enable_web = False
        acquisition.enable_scholarly_search = True
        acquisition._resolve_seeds = lambda refs: []
        acquisition._filter_candidates = lambda candidates, anchor, seeds: []
        acquisition._archive_paper = lambda paper, role="", stage="": ([], False)
        acquisition._acquire_web_knowledge = lambda *args, **kwargs: {}
        acquisition._safe = lambda fn, label: fn()
        acquisition._paper_summary = lambda paper: {}

        summary = acquisition.acquire_for_bootstrap(
            POLLUTED_QUERY, [], survey_queries=["NiFe OER 电催化"]
        )
        self.assertEqual(summary["retrieval_status"], "provider_failure")

    def test_provider_failure_status_accepts_urllib_http_error_wording(self) -> None:
        """urllib reports ``HTTP Error 429``, not the shorter ``HTTP 429``."""
        from reaserch_agent.tools.literature_acquisition import LiteratureAcquisition

        class BrokenClient:
            last_errors = ["semantic_scholar: HTTPError: HTTP Error 429: Too Many Requests"]

            def search(self, query, sources=(), max_results=5):
                return []

            def fetch_references(self, seed, limit=0):
                return []

            def fetch_citations(self, seed, limit=0):
                return []

        acquisition = LiteratureAcquisition.__new__(LiteratureAcquisition)
        acquisition.client = BrokenClient()
        acquisition.errors = []
        acquisition.keyword_sources = ["semantic_scholar"]
        acquisition.max_keyword_results = 6
        acquisition.max_snowball_per_seed = 0
        acquisition.web_client = None
        acquisition.enable_web = False
        acquisition.enable_scholarly_search = True
        acquisition._resolve_seeds = lambda refs: []
        acquisition._filter_candidates = lambda candidates, anchor, seeds: []
        acquisition._archive_paper = lambda paper, role="", stage="": ([], False)
        acquisition._acquire_web_knowledge = lambda *args, **kwargs: {}
        acquisition._safe = lambda fn, label: fn()
        acquisition._paper_summary = lambda paper: {}

        summary = acquisition.acquire_for_bootstrap(
            POLLUTED_QUERY, [], survey_queries=["NiFe OER 电催化"]
        )
        self.assertEqual(summary["retrieval_status"], "provider_failure")


if __name__ == "__main__":
    unittest.main()
