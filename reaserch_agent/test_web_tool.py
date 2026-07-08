"""Unit tests for the LLM-facing web tool protocol (tool_request loop)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest import mock

from reaserch_agent.state import ResearchAgentState, ResearchEvent
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


class ScriptedModel:
    """Returns scripted JSON payloads per invocation (last one repeats)."""

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.prompts: List[str] = []

    def invoke(self, messages):
        prompt = "\n\n".join(
            getattr(message, "content", None) or message.get("content", "")
            for message in messages
        )
        self.prompts.append(prompt)
        index = min(len(self.prompts) - 1, len(self.responses) - 1)
        return SimpleNamespace(
            content=json.dumps(self.responses[index], ensure_ascii=False)
        )


class WebToolExecutorTest(unittest.TestCase):
    def test_web_search_formats_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWebClient()
            executor = WebToolExecutor(web_client=web, kb_dir=tmp, campaign_id="cmp_t")
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
            executor = WebToolExecutor(web_client=web, kb_dir=tmp, campaign_id="cmp_t")
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

    def test_unknown_tool_and_exceptions_are_contained(self) -> None:
        executor = WebToolExecutor(web_client=BrokenWebClient())
        unknown = executor.execute({"tool": "bing_search", "query": "x"})
        self.assertEqual(unknown["status"], "error")
        self.assertIn("web_search", unknown["error"])

        broken = executor.execute({"tool": "web_search", "query": "x"})
        self.assertEqual(broken["status"], "error")
        self.assertIn("ConnectionError", broken["error"])


class WorkflowToolLoopTest(unittest.TestCase):
    def _make_agent(self, model, web, *, enable_web: bool, tmp: str) -> ResearchAgent:
        return ResearchAgent(
            model=model,
            use_llm=True,
            knowledge_base_dir=tmp,
            enable_web_search=enable_web,
            web_search_client=web,
        )

    def _make_state(self) -> ResearchAgentState:
        return ResearchAgentState(
            event=ResearchEvent(event_type="bootstrap", query="NiFe PBA 活化"),
            campaign_id="cmp_tool_loop",
        )

    def test_tool_request_is_executed_then_task_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWebClient()
            model = ScriptedModel(
                [
                    {"tool_request": {"tool": "web_search", "query": "nife pba oer"}},
                    {"done": True, "answer": "final"},
                ]
            )
            agent = self._make_agent(model, web, enable_web=True, tmp=tmp)
            state = self._make_state()

            result = agent._invoke_state_json(state, "unit_task", '输出 {"done": true}')

            self.assertEqual(result, {"done": True, "answer": "final"})
            self.assertEqual(len(model.prompts), 2)
            self.assertIn("可用工具", model.prompts[0])
            self.assertIn("工具调用结果 1", model.prompts[1])
            self.assertIn("https://example.com/pba", model.prompts[1])
            self.assertEqual(web.search_calls, ["nife pba oer"])
            self.assertEqual(len(state.tool_invocations), 1)
            record = state.tool_invocations[0]
            self.assertEqual(record["tool"], "web_search")
            self.assertEqual(record["task_name"], "unit_task")
            self.assertEqual(record["results_count"], 1)

    def test_tool_rounds_are_capped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWebClient()
            model = ScriptedModel(
                [{"tool_request": {"tool": "web_search", "query": "loop"}}]
            )
            agent = self._make_agent(model, web, enable_web=True, tmp=tmp)
            state = self._make_state()

            with mock.patch.dict(
                "os.environ", {"RESEARCH_WEB_TOOL_MAX_ROUNDS": "1"}, clear=False
            ):
                result = agent._invoke_state_json(state, "unit_task", "任务")

            # 1 tool round -> 2 LLM invocations, then loop exits with last result
            self.assertEqual(len(model.prompts), 2)
            self.assertEqual(len(web.search_calls), 1)
            self.assertIn("tool_request", result)
            self.assertEqual(len(state.tool_invocations), 1)

    def test_disabled_web_means_no_instructions_and_no_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWebClient()
            model = ScriptedModel(
                [{"tool_request": {"tool": "web_search", "query": "x"}}]
            )
            agent = self._make_agent(model, web, enable_web=False, tmp=tmp)
            state = self._make_state()

            result = agent._invoke_state_json(state, "unit_task", "任务")

            self.assertEqual(len(model.prompts), 1)
            self.assertNotIn("可用工具", model.prompts[0])
            self.assertEqual(web.search_calls, [])
            self.assertIn("tool_request", result)
            self.assertEqual(state.tool_invocations, [])


if __name__ == "__main__":
    unittest.main()
