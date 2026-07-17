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

        captured = {}

        class StubClient:
            last_errors: list = []

            def search(self, query, sources=(), max_results=5):
                captured["query"] = query
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

        lowered = captured["query"].lower()
        for token in BANNED_TOKENS:
            self.assertNotIn(token.lower(), lowered)
        self.assertIn("NiFe-PBA", captured["query"])
        # the outbound query is recorded in the acquisition summary
        self.assertEqual(summary["keyword_query"], captured["query"])


if __name__ == "__main__":
    unittest.main()
