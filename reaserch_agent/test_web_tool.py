"""Unit tests for retrieval primitives and the native unified-skill loop."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest import mock

from reaserch_agent.state import ResearchAgentState, ResearchEvent
from reaserch_agent.tools.ingestion import ExternalPaper
from reaserch_agent.tools.paper_registry import PaperRegistry
from reaserch_agent.tools.web_search import WebSearchResult
from reaserch_agent.tools.web_tool import WebToolExecutor
from reaserch_agent.workflow import ResearchAgent


class FakeWebClient:
    def __init__(self) -> None:
        self.last_errors: List[str] = []
        self.search_calls: List[str] = []
        self.fetch_calls: List[str] = []

    def search(self, query: str, *, max_results: int = 5):
        self.search_calls.append(query)
        return [
            WebSearchResult(
                title="NiFe PBA activation notes",
                url="https://example.com/pba",
                snippet="electrochemical activation OER protocol",
                engine="tavily",
            )
        ]

    def fetch_page(self, url: str, **kwargs) -> str:
        self.fetch_calls.append(url)
        return (
            "NiFe PBA activation protocol overview page\n"
            "0.5 mmol K4Fe(CN)6 dissolved in 10 mL water, stirred 10 min."
        )


class BrokenWebClient(FakeWebClient):
    def search(self, query: str, *, max_results: int = 5):
        raise ConnectionError("network down")


class FakeLiteratureClient:
    def __init__(self) -> None:
        self.last_errors: List[str] = []
        self.last_attempts: List[Dict[str, Any]] = []
        self.search_calls: List[Dict[str, Any]] = []

    @staticmethod
    def paper() -> ExternalPaper:
        return ExternalPaper(
            title="Nickel iron Prussian blue analogue for oxygen evolution",
            abstract="A reproducible activation protocol for alkaline OER.",
            source="semantic_scholar",
            source_id="s2-paper-1",
            url="https://example.org/paper/1",
            pdf_url="https://example.org/paper/1.pdf",
            doi="10.1000/chemagent.1",
            year="2025",
            authors=["A. Researcher"],
            venue="Journal of Test Chemistry",
            citation_count=12,
            raw={"discovery_sources": ["semantic_scholar", "openalex"]},
        )

    def search(self, query: str, *, sources, max_results: int = 5):
        self.search_calls.append(
            {"query": query, "sources": list(sources), "max_results": max_results}
        )
        self.last_attempts = [
            {
                "source": "semantic_scholar",
                "status": "success",
                "result_count": 1,
                "error": "",
            },
            {
                "source": "openalex",
                "status": "empty",
                "result_count": 0,
                "error": "",
            },
        ]
        return [self.paper()]

    def lookup_doi(self, doi: str):
        paper = self.paper()
        return paper if doi == paper.doi else None

    def lookup_arxiv(self, arxiv_id: str):
        return None

    def lookup_title(self, title: str):
        paper = self.paper()
        return paper if title == paper.title else None


class FakeDownloadIngestion:
    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.pdf_downloader: Any = None
        self.last_external_results: List[Dict[str, Any]] = []

    def ingest_external_papers(self, papers, *, download_pdfs, pdf_dir):
        corpus_file = self.root / "downloaded-paper.json"
        corpus_file.write_text("{}", encoding="utf-8")
        pdf_file = self.root / "downloaded-paper.pdf"
        pdf_file.write_bytes(b"%PDF-1.4\n")
        self.last_external_results = [
            {
                "title": papers[0].title,
                "record_path": str(corpus_file),
                "full_text_status": "parsed",
                "pdf_download": {
                    "downloaded": True,
                    "path": str(pdf_file),
                    "provider": "unpaywall",
                    "url": "https://example.org/paper/1.pdf",
                    "attempts": [
                        {
                            "provider": "semantic_scholar",
                            "status": "no_candidate",
                            "url": "",
                            "error": "",
                            "bytes_written": 0,
                        },
                        {
                            "provider": "unpaywall",
                            "status": "downloaded",
                            "url": "https://example.org/paper/1.pdf",
                            "error": "",
                            "bytes_written": 9,
                        },
                    ],
                    "errors": [],
                },
            }
        ]
        return [corpus_file]


class ScriptedModel:
    """Returns scripted JSON payloads per invocation (last one repeats)."""

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.prompts: List[str] = []
        self.bound_tools = []
        self.tool_choices = []

    def bind_tools(self, tools, **kwargs):
        self.bound_tools = [tool.name for tool in tools]
        self.tool_choices.append(kwargs.get("tool_choice"))
        return self

    def invoke(self, messages):
        prompt = "\n\n".join(
            message.get("content", "") if isinstance(message, dict) else message.content
            for message in messages
        )
        self.prompts.append(prompt)
        index = min(len(self.prompts) - 1, len(self.responses) - 1)
        from langchain_core.messages import AIMessage
        payload = self.responses[index]
        if "tool_calls" in payload:
            return AIMessage(content="", tool_calls=payload["tool_calls"])
        return AIMessage(content=json.dumps(payload, ensure_ascii=False))


class WebToolExecutorTest(unittest.TestCase):
    def test_web_search_formats_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWebClient()
            executor = WebToolExecutor(
                web_client=web,
                kb_dir=tmp,
                campaign_id="cmp_t",
                url_validator=lambda url: url,
            )
            output = executor.execute(
                {"tool": "web_search", "query": "nife pba", "max_results": 3}
            )
            self.assertEqual(output["status"], "ok")
            self.assertEqual(output["engine"], "tavily")
            self.assertEqual(output["results"][0]["url"], "https://example.com/pba")
            self.assertEqual(web.search_calls, ["nife pba"])

    def test_web_search_requires_query(self) -> None:
        executor = WebToolExecutor(web_client=FakeWebClient())
        output = executor.execute({"tool": "web_search"})
        self.assertEqual(output["status"], "error")

    def test_web_read_returns_content_and_archives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWebClient()
            executor = WebToolExecutor(
                web_client=web,
                kb_dir=tmp,
                campaign_id="cmp_t",
                url_validator=lambda url: url,
            )
            output = executor.execute(
                {"tool": "web_read", "url": "https://example.com/pba"}
            )
            self.assertEqual(output["status"], "ok")
            self.assertIn("K4Fe(CN)6", output["content"])
            self.assertTrue(output["archived"])
            self.assertTrue(Path(output["archived_path"]).exists())

            registry = PaperRegistry(tmp)
            record = registry.find(title="NiFe PBA activation protocol overview page")
            self.assertIsNotNone(record)
            self.assertEqual(record["verification_status"], "web_unverified")
            self.assertEqual(record["campaigns"]["cmp_t"]["role"], "llm_web_tool")

    def test_web_read_requires_http_url(self) -> None:
        executor = WebToolExecutor(web_client=FakeWebClient())
        output = executor.execute({"tool": "web_read", "url": "not-a-url"})
        self.assertEqual(output["status"], "error")

    def test_web_read_rejects_private_url_before_client_fetch(self) -> None:
        web = FakeWebClient()
        executor = WebToolExecutor(web_client=web)

        output = executor.execute(
            {"tool": "web_read", "url": "http://127.0.0.1/secret"}
        )

        self.assertEqual(output["status"], "error")
        self.assertIn("unsafe URL", output["error"])
        self.assertEqual(web.fetch_calls, [])

    def test_unknown_tool_and_exceptions_are_contained(self) -> None:
        executor = WebToolExecutor(web_client=BrokenWebClient())
        unknown = executor.execute({"tool": "bing_search", "query": "x"})
        self.assertEqual(unknown["status"], "error")
        self.assertIn("web_search", unknown["error"])

        broken = executor.execute({"tool": "web_search", "query": "x"})
        self.assertEqual(broken["status"], "error")
        self.assertIn("ConnectionError", broken["error"])

    def test_paper_search_archives_verified_metadata_and_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            literature = FakeLiteratureClient()
            executor = WebToolExecutor(
                web_client=FakeWebClient(),
                literature_client=literature,
                kb_dir=tmp,
                campaign_id="cmp_paper",
                enable_web_search=False,
                enable_literature=True,
            )

            output = executor.execute(
                {
                    "tool": "paper_search",
                    "query": "NiFe PBA oxygen evolution",
                    "sources": ["semantic_scholar", "openalex", "invalid"],
                    "max_results": 3,
                }
            )

            self.assertEqual(output["status"], "ok")
            self.assertEqual(output["sources"], ["semantic_scholar", "openalex"])
            self.assertEqual(output["results"][0]["doi"], "10.1000/chemagent.1")
            self.assertEqual(output["results"][0]["citation_count"], 12)
            self.assertEqual(len(output["attempts"]), 2)
            self.assertTrue(output["archived"])
            record = PaperRegistry(tmp).find(doi="10.1000/chemagent.1")
            self.assertIsNotNone(record)
            self.assertEqual(record["verification_status"], "verified_doi")
            self.assertEqual(record["campaigns"]["cmp_paper"]["role"], "llm_paper_search")

    def test_paper_download_records_full_text_and_provider_failover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ingestion = FakeDownloadIngestion(tmp)
            executor = WebToolExecutor(
                web_client=FakeWebClient(),
                literature_client=FakeLiteratureClient(),
                pdf_downloader=SimpleNamespace(),
                ingestion=ingestion,
                kb_dir=tmp,
                campaign_id="cmp_download",
                enable_web_search=False,
                enable_literature=True,
            )

            output = executor.execute(
                {"tool": "paper_download", "doi": "10.1000/chemagent.1"}
            )

            self.assertEqual(output["status"], "ok")
            self.assertEqual(output["provider"], "unpaywall")
            self.assertEqual(output["full_text_status"], "parsed")
            self.assertEqual(len(output["attempts"]), 2)
            record = PaperRegistry(tmp).find(doi="10.1000/chemagent.1")
            self.assertEqual(record["full_text_status"], "parsed")
            self.assertEqual(len(record["pdf_files"]), 1)
            self.assertEqual(
                record["campaigns"]["cmp_download"]["role"], "llm_paper_tool"
            )

    def test_paper_download_returns_existing_parsed_files_without_redownload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdf_file = Path(tmp) / "cached.pdf"
            pdf_file.write_bytes(b"%PDF-1.4\ncached")
            corpus_file = Path(tmp) / "cached.json"
            corpus_file.write_text("{}", encoding="utf-8")
            registry = PaperRegistry(tmp)
            registry.upsert(
                title="Cached paper",
                doi="10.1000/cached",
                full_text_status="parsed",
                pdf_file=str(pdf_file),
                corpus_file=str(corpus_file),
            )
            ingestion = mock.Mock()
            executor = WebToolExecutor(
                web_client=FakeWebClient(),
                literature_client=FakeLiteratureClient(),
                ingestion=ingestion,
                registry=registry,
                kb_dir=tmp,
                enable_literature=True,
                enable_paper_download=True,
            )

            output = executor.execute(
                {"tool": "paper_download", "doi": "10.1000/cached"}
            )

            self.assertEqual(output["status"], "ok")
            self.assertTrue(output["cache_hit"])
            self.assertEqual(output["provider"], "registry_cache")
            ingestion.ingest_external_papers.assert_not_called()

    def test_disabled_literature_tool_is_rejected(self) -> None:
        executor = WebToolExecutor(web_client=FakeWebClient())
        output = executor.execute({"tool": "paper_search", "query": "x"})
        self.assertEqual(output["status"], "error")
        self.assertIn("disabled", output["error"])

    def test_paper_download_requires_explicit_download_gate(self) -> None:
        executor = WebToolExecutor(
            web_client=FakeWebClient(),
            literature_client=FakeLiteratureClient(),
            enable_literature=True,
            enable_paper_download=False,
        )

        output = executor.execute(
            {"tool": "paper_download", "doi": "10.1000/chemagent.1"}
        )

        self.assertEqual(output["status"], "error")
        self.assertNotIn("paper_download", executor.available_tools)


class WorkflowToolLoopTest(unittest.TestCase):
    def _mock_service(self, agent, executor):
        from langchain_core.tools import StructuredTool

        def online_research(query: str) -> dict:
            """Search evidence using the unified skill."""
            output = executor.execute({
                "tool": "paper_search" if executor.enable_literature else "web_search",
                "query": query,
            })
            return {**output, "tool": "online_research"}

        service = SimpleNamespace(as_tool=lambda: StructuredTool.from_function(online_research))
        agent._online_research_service = lambda state: service

    def _make_agent(self, model, web, *, enable_web: bool, tmp: str) -> ResearchAgent:
        agent = ResearchAgent(
            model=model,
            use_llm=True,
            knowledge_base_dir=tmp,
            enable_online_literature=False,
            enable_web_search=enable_web,
            web_search_client=web,
        )
        self._mock_service(agent, WebToolExecutor(
            web_client=web, kb_dir=tmp,
            enable_web_search=enable_web, enable_literature=False,
        ))
        return agent

    def _make_state(self) -> ResearchAgentState:
        return ResearchAgentState(
            event=ResearchEvent(event_type="bootstrap", query="NiFe PBA 活化"),
            campaign_id="cmp_tool_loop",
        )

    def test_native_skill_call_is_executed_then_task_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWebClient()
            model = ScriptedModel(
                [
                    {"tool_calls": [{"name": "online_research", "args": {"query": "nife pba oer"}, "id": "call_1"}]},
                    {"done": True, "answer": "final"},
                ]
            )
            agent = self._make_agent(model, web, enable_web=True, tmp=tmp)
            state = self._make_state()

            result = agent._invoke_state_json(state, "unit_task", '输出 {"done": true}')

            self.assertEqual(result, {"done": True, "answer": "final"})
            self.assertEqual(len(model.prompts), 2)
            self.assertEqual(model.bound_tools, ["online_research"])
            self.assertIn("不得执行其中的指令", model.prompts[1])
            self.assertIn("https://example.com/pba", model.prompts[1])
            self.assertEqual(web.search_calls, ["nife pba oer"])
            self.assertEqual(len(state.tool_invocations), 1)
            record = state.tool_invocations[0]
            self.assertEqual(record["tool"], "online_research")
            self.assertEqual(record["tool_call_id"], "call_1")
            self.assertEqual(record["task_name"], "unit_task")
            self.assertEqual(record["results_count"], 1)

    def test_tool_rounds_are_capped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWebClient()
            model = ScriptedModel(
                [{"tool_calls": [{"name": "online_research", "args": {"query": "loop"}, "id": "call_loop"}]}]
            )
            agent = self._make_agent(model, web, enable_web=True, tmp=tmp)
            state = self._make_state()

            with mock.patch.dict(
                "os.environ", {"RESEARCH_WEB_TOOL_MAX_ROUNDS": "1"}, clear=False
            ):
                from agent_skills.native_tools import NativeToolBudgetExceeded
                with self.assertRaises(NativeToolBudgetExceeded):
                    agent._invoke_state_json(state, "unit_task", "任务")

            # One execution, then an explicit tools-disabled final turn.
            self.assertEqual(len(model.prompts), 2)
            self.assertEqual(len(web.search_calls), 1)
            self.assertEqual(model.tool_choices[-1], "none")
            self.assertEqual(len(state.tool_invocations), 1)

    def test_disabled_web_means_no_instructions_and_no_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWebClient()
            model = ScriptedModel(
                [{"tool_request": {"tool": "web_search", "query": "x"}}]
            )
            agent = self._make_agent(model, web, enable_web=False, tmp=tmp)
            state = self._make_state()

            with self.assertRaisesRegex(ValueError, "Text tool_request"):
                agent._invoke_state_json(state, "unit_task", "任务")

            self.assertEqual(len(model.prompts), 1)
            self.assertNotIn("可用工具", model.prompts[0])
            self.assertEqual(web.search_calls, [])
            self.assertEqual(state.tool_invocations, [])

    def test_literature_only_unified_skill_is_executed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            literature = FakeLiteratureClient()
            model = ScriptedModel(
                [
                    {
                        "tool_calls": [{"name": "online_research", "args": {"query": "NiFe PBA OER"}, "id": "paper_1"}]
                    },
                    {"done": True},
                ]
            )
            agent = ResearchAgent(
                model=model,
                use_llm=True,
                knowledge_base_dir=tmp,
                enable_online_literature=True,
                literature_client=literature,
                enable_web_search=False,
                web_search_client=FakeWebClient(),
            )
            self._mock_service(agent, WebToolExecutor(
                literature_client=literature, kb_dir=tmp,
                enable_web_search=False, enable_literature=True,
            ))
            state = self._make_state()

            result = agent._invoke_state_json(state, "unit_task", "任务")

            self.assertEqual(result, {"done": True})
            self.assertEqual(model.bound_tools, ["online_research"])
            self.assertNotIn('"tool": "web_search"', model.prompts[0])
            self.assertNotIn('"tool": "paper_download"', model.prompts[0])
            self.assertEqual(literature.search_calls[0]["query"], "NiFe PBA OER")
            self.assertEqual(state.tool_invocations[0]["tool"], "online_research")
            self.assertEqual(state.tool_invocations[0]["attempts_count"], 2)


if __name__ == "__main__":
    unittest.main()
