"""Unit tests for multi-source data access (P7).

Engines (Tavily/Serper/Brave/SearXNG/DuckDuckGo), page reading (Jina/direct),
new scholarly sources (OpenAlex/PubMed/Google Scholar), and the acquisition
web line — all with stubbed transports, zero real network.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from reaserch_agent.tools.ingestion import ExternalKnowledgeClient
from reaserch_agent.tools.literature_acquisition import (
    LiteratureAcquisition,
    default_keyword_sources,
)
from reaserch_agent.tools.paper_registry import PaperRegistry
from reaserch_agent.tools.web_search import WebSearchClient, WebSearchResult


class StubWebClient(WebSearchClient):
    """WebSearchClient with canned transports keyed by URL substring."""

    def __init__(self, *, get_map=None, post_map=None, **kwargs) -> None:
        kwargs.setdefault("url_validator", lambda url: None)
        super().__init__(**kwargs)
        self.get_map = get_map or {}
        self.post_map = post_map or {}
        self.posted: List[tuple] = []
        self.fetched: List[str] = []

    def _get_text(self, url: str, *, headers=None) -> str:
        self.fetched.append(url)
        for key, value in self.get_map.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unexpected GET {url}")

    def _post_json(self, url: str, payload, *, headers=None) -> Dict[str, Any]:
        self.posted.append((url, payload, headers))
        for key, value in self.post_map.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unexpected POST {url}")


DDG_HTML = (
    '<a class="result__a" href="//duckduckgo.com/l/'
    '?uddg=https%3A%2F%2Fexample.com%2Fpba&rut=abc">NiFe <b>PBA</b> page</a>'
    '<a class="result__snippet">activation <b>protocol</b> details</a>'
)


class WebEngineParsingTest(unittest.TestCase):
    def test_tavily_parsing(self) -> None:
        client = StubWebClient(
            tavily_api_key="tvly-test",
            post_map={
                "api.tavily.com": {
                    "results": [
                        {
                            "title": "T1",
                            "url": "https://a.com",
                            "content": "long content",
                            "score": 0.9,
                        }
                    ]
                }
            },
        )
        results = client.search("nife pba", max_results=3)
        self.assertEqual(results[0].engine, "tavily")
        self.assertEqual(results[0].url, "https://a.com")
        self.assertEqual(results[0].content, "long content")

    def test_serper_parsing_and_chinese_locale(self) -> None:
        client = StubWebClient(
            serper_api_key="sk",
            post_map={
                "google.serper.dev/search": {
                    "organic": [
                        {"title": "S1", "link": "https://b.com", "snippet": "snip"}
                    ]
                }
            },
        )
        results = client.search("普鲁士蓝 电化学活化", max_results=3, engine="serper")
        self.assertEqual(results[0].engine, "serper")
        _, payload, headers = client.posted[0]
        self.assertEqual(payload["gl"], "cn")
        self.assertEqual(headers["X-API-KEY"], "sk")

    def test_brave_and_searxng_parsing(self) -> None:
        client = StubWebClient(
            brave_api_key="bk",
            searxng_base_url="https://searx.local",
            get_map={
                "api.search.brave.com": json.dumps(
                    {"web": {"results": [{"title": "B", "url": "https://c.com", "description": "<b>d</b>"}]}}
                ),
                "searx.local": json.dumps(
                    {"results": [{"title": "X", "url": "https://d.com", "content": "cx"}]}
                ),
            },
        )
        brave = client.search("q", engine="brave")
        self.assertEqual(brave[0].engine, "brave")
        self.assertEqual(brave[0].snippet, "d")
        searx = client.search("q", engine="searxng")
        self.assertEqual(searx[0].url, "https://d.com")

    def test_duckduckgo_parse_unwraps_redirect(self) -> None:
        client = StubWebClient(get_map={"duckduckgo.com/html": DDG_HTML})
        results = client.search("nife pba", max_results=3)
        self.assertEqual(results[0].engine, "duckduckgo")
        self.assertEqual(results[0].url, "https://example.com/pba")
        self.assertEqual(results[0].title, "NiFe PBA page")
        self.assertIn("activation", results[0].snippet)

    def test_engine_chain_falls_back_on_failure(self) -> None:
        client = StubWebClient(
            tavily_api_key="tvly-test",
            post_map={"api.tavily.com": ConnectionError("tavily down")},
            get_map={"duckduckgo.com/html": DDG_HTML},
        )
        results = client.search("nife pba")
        self.assertEqual(results[0].engine, "duckduckgo")
        self.assertTrue(any("tavily" in err for err in client.last_errors))

    def test_available_engines_ordering(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "TAVILY_API_KEY": "",
                "SERPER_API_KEY": "",
                "BRAVE_API_KEY": "",
                "SEARXNG_BASE_URL": "",
            },
            clear=False,
        ):
            client = StubWebClient(serper_api_key="sk", brave_api_key="bk")
            self.assertEqual(
                client.available_engines(), ["serper", "brave", "duckduckgo"]
            )
            keyless = StubWebClient()
            self.assertEqual(keyless.available_engines(), ["duckduckgo"])


class PageReadingTest(unittest.TestCase):
    def test_fetch_page_prefers_jina(self) -> None:
        client = StubWebClient(get_map={"r.jina.ai": "readable page text"})
        self.assertEqual(client.fetch_page("https://example.com"), "readable page text")

    def test_fetch_page_falls_back_to_direct_strip(self) -> None:
        client = StubWebClient(
            get_map={
                "r.jina.ai": ConnectionError("jina down"),
                "example.com": "<html><script>x()</script><body><p>0.5 mmol in 10 mL</p></body></html>",
            }
        )
        text = client.fetch_page("https://example.com")
        self.assertIn("0.5 mmol in 10 mL", text)
        self.assertNotIn("<p>", text)
        self.assertNotIn("x()", text)


class ScholarlySourcesTest(unittest.TestCase):
    def _client(self, get_map: Dict[str, Any]) -> ExternalKnowledgeClient:
        client = ExternalKnowledgeClient()

        def fake_get(url: str, *, headers=None) -> str:
            for key, value in get_map.items():
                if key in url:
                    return value
            raise AssertionError(f"unexpected GET {url}")

        client._get_text = fake_get  # type: ignore[method-assign]
        return client

    def test_openalex_parse_and_abstract_reconstruction(self) -> None:
        payload = {
            "results": [
                {
                    "id": "https://openalex.org/W123",
                    "title": "NiFe PBA OER activation",
                    "publication_year": 2024,
                    "doi": "https://doi.org/10.1000/oa1",
                    "ids": {"openalex": "https://openalex.org/W123"},
                    "abstract_inverted_index": {"NiFe": [0], "PBA": [1], "works": [2]},
                    "authorships": [{"author": {"display_name": "A. Author"}}],
                    "primary_location": {"pdf_url": "https://pdf.oa"},
                    "open_access": {"oa_url": "https://oa.url"},
                }
            ]
        }
        client = self._client({"api.openalex.org": json.dumps(payload)})
        papers = client.search_openalex("nife pba")
        self.assertEqual(papers[0].doi, "10.1000/oa1")
        self.assertEqual(papers[0].abstract, "NiFe PBA works")
        self.assertEqual(papers[0].pdf_url, "https://pdf.oa")
        self.assertEqual(papers[0].source, "openalex")
        self.assertEqual(papers[0].authors, ["A. Author"])

    def test_pubmed_esearch_then_esummary(self) -> None:
        client = self._client(
            {
                "esearch.fcgi": json.dumps({"esearchresult": {"idlist": ["11111"]}}),
                "esummary.fcgi": json.dumps(
                    {
                        "result": {
                            "11111": {
                                "title": "PubMed paper",
                                "pubdate": "2023 Jan",
                                "authors": [{"name": "B. Bio"}],
                                "articleids": [{"idtype": "doi", "value": "10.1000/pm1"}],
                            }
                        }
                    }
                ),
            }
        )
        papers = client.search_pubmed("prussian blue biomedical")
        self.assertEqual(papers[0].source, "pubmed")
        self.assertEqual(papers[0].doi, "10.1000/pm1")
        self.assertEqual(papers[0].year, "2023")
        self.assertEqual(papers[0].url, "https://pubmed.ncbi.nlm.nih.gov/11111/")

    def test_google_scholar_via_serper(self) -> None:
        class FakeWSC:
            def __init__(self, **kwargs) -> None:
                pass

            def search_scholar(self, query, *, max_results=5):
                return [
                    {
                        "title": "Scholar paper",
                        "url": "https://scholar.x/p1",
                        "snippet": "activation study",
                        "year": "2024",
                        "publication_info": "Nature",
                        "pdf_url": "https://pdf.x/p1.pdf",
                    }
                ]

        with mock.patch(
            "reaserch_agent.tools.web_search.WebSearchClient", FakeWSC
        ):
            client = ExternalKnowledgeClient()
            papers = client.search_google_scholar("nife pba")
        self.assertEqual(papers[0].source, "google_scholar")
        self.assertEqual(papers[0].pdf_url, "https://pdf.x/p1.pdf")
        self.assertEqual(papers[0].raw["publication_info"], "Nature")

    def test_search_dispatcher_accepts_new_sources(self) -> None:
        client = self._client({"api.openalex.org": json.dumps({"results": []})})
        client.search("q", sources=("openalex",), max_results=2)
        self.assertEqual(client.last_errors, [])
        # unknown source still lands in last_errors, not an exception
        client.search("q", sources=("bing",))
        self.assertTrue(any("bing" in err for err in client.last_errors))

    def test_default_keyword_sources_gated_by_serper_key(self) -> None:
        with mock.patch.dict("os.environ", {"SERPER_API_KEY": ""}, clear=False):
            self.assertEqual(
                default_keyword_sources(),
                ["semantic_scholar", "crossref", "arxiv", "openalex"],
            )
        with mock.patch.dict("os.environ", {"SERPER_API_KEY": "sk"}, clear=False):
            self.assertIn("google_scholar", default_keyword_sources())


class FakeAcquisitionWebClient:
    def __init__(self) -> None:
        self.last_errors: List[str] = []
        self.search_calls: List[str] = []
        self.fetch_calls: List[str] = []

    def search(self, query: str, *, max_results: int = 5):
        self.search_calls.append(query)
        return [
            WebSearchResult(
                title="NiFe Prussian blue analogue activation protocol notes",
                url="https://example.com/pba",
                snippet="NiFe Prussian blue analogue electrochemical activation OER protocol",
                engine="tavily",
            ),
            WebSearchResult(
                title="Best pasta recipes",
                url="https://example.com/food",
                snippet="pasta pizza carbonara dinner ideas",
                engine="tavily",
            ),
        ]

    def fetch_page(self, url: str, **kwargs) -> str:
        self.fetch_calls.append(url)
        return (
            "Protocol: 0.5 mmol K4Fe(CN)6 dissolved in 10 mL water, "
            "stirred for 10 min, then aged 12 h at room temperature."
        )


class AcquisitionWebLineTest(unittest.TestCase):
    QUERY = "NiFe-PBA electrochemical activation OER"

    def test_web_line_ingests_relevant_page_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeAcquisitionWebClient()
            acquisition = LiteratureAcquisition(
                kb_dir=tmp,
                campaign_id="cmp_web",
                client=mock.MagicMock(last_errors=[]),
                web_client=web,
                enable_web_search=True,
                max_keyword_results=0,  # isolate the web line
            )
            summary = acquisition.acquire_for_bootstrap(self.QUERY, [])

            self.assertEqual(summary["web_kept"], 1)
            self.assertEqual(summary["web_engine"], "tavily")
            self.assertEqual(web.fetch_calls, ["https://example.com/pba"])
            written = summary["written_files"]
            self.assertEqual(len(written), 1)
            record = json.loads(Path(written[0]).read_text(encoding="utf-8"))
            self.assertIn("K4Fe(CN)6", json.dumps(record, ensure_ascii=False))
            self.assertEqual(
                record["_ingestion_metadata"]["source_type"], "web"
            )

            registry = PaperRegistry(tmp)
            record = registry.find(title="NiFe Prussian blue analogue activation protocol notes")
            self.assertIsNotNone(record)
            self.assertEqual(record["verification_status"], "web_unverified")
            self.assertEqual(record["source"], "web:tavily")
            self.assertEqual(record["campaigns"]["cmp_web"]["role"], "web")

    def test_web_line_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeAcquisitionWebClient()
            acquisition = LiteratureAcquisition(
                kb_dir=tmp,
                campaign_id="cmp_noweb",
                client=mock.MagicMock(last_errors=[]),
                web_client=web,
                max_keyword_results=0,
            )
            summary = acquisition.acquire_for_bootstrap(self.QUERY, [])
            self.assertEqual(web.search_calls, [])
            self.assertEqual(summary.get("web_kept", 0), 0)

    def test_repair_round_includes_bounded_web(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeAcquisitionWebClient()
            acquisition = LiteratureAcquisition(
                kb_dir=tmp,
                campaign_id="cmp_repair_web",
                client=mock.MagicMock(last_errors=[], search=lambda *a, **k: []),
                web_client=web,
                enable_web_search=True,
            )
            summary = acquisition.acquire_for_repair(
                ["NiFe PBA activation anomaly"],
                stage="活化 stage",
            )
            self.assertEqual(summary["web_kept"], 1)
            registry = PaperRegistry(tmp)
            record = registry.find(
                title="NiFe Prussian blue analogue activation protocol notes"
            )
            self.assertEqual(
                record["campaigns"]["cmp_repair_web"]["role"], "b2_repair_web"
            )
            self.assertEqual(
                record["campaigns"]["cmp_repair_web"]["stage"], "活化 stage"
            )


class WorkflowWebIntegrationTest(unittest.TestCase):
    def test_b1_runs_web_line_when_enabled_without_references(self) -> None:
        from reaserch_agent.workflow import ResearchAgent

        with tempfile.TemporaryDirectory() as tmp:
            kb = Path(tmp) / "kb"
            kb.mkdir()
            web = FakeAcquisitionWebClient()

            class QuietScholarly:
                last_errors: List[str] = []

                def search(self, *args, **kwargs):
                    return []

            agent = ResearchAgent(
                model=None,
                use_llm=False,
                knowledge_base_dir=str(kb),
                literature_client=QuietScholarly(),
                enable_web_search=True,
                web_search_client=web,
            )
            state = agent.run(
                event_type="bootstrap",
                query="NiFe-PBA electrochemical activation OER 优化",
                campaign_id="cmp_web_b1",
            )

            self.assertEqual(state.status, "completed")
            self.assertTrue(web.search_calls)
            self.assertTrue(
                any("web_kept=1" in log for log in state.logs),
                [log for log in state.logs if "literature acquisition" in log],
            )


if __name__ == "__main__":
    unittest.main()
