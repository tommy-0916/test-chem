"""LLM-facing web tools: bounded web_search / web_read with campaign archiving.

The research LLM stays text-in / JSON-out (no native function calling — the
codex_responses backend cannot do it). Instead, any planning step may reply
with ``{"tool_request": {"tool": "web_search", "query": "..."}}``; the
workflow executes the request here, appends the output to the prompt, and
re-invokes the same step (bounded rounds). Pages read through ``web_read``
are archived into the campaign KB as ``web_unverified`` leads so later
retrieval and provenance stay consistent with the acquisition web line.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .ingestion import KnowledgeIngestion
from .paper_registry import PaperRegistry
from .web_search import WebSearchClient

TOOL_PROTOCOL_INSTRUCTIONS = """## 可用工具（可选，仅在确实缺少外部信息时使用）
当且仅当缺少完成当前任务所需的外部信息时，你可以先请求一次工具调用——输出且只输出：
{"tool_request": {"tool": "web_search", "query": "<检索词>", "max_results": 5}}
或
{"tool_request": {"tool": "web_read", "url": "https://..."}}
系统会执行工具，把结果以「工具调用结果」段落追加回来，然后你再输出该任务要求的最终 JSON。
规则：
- 每个任务最多 {max_rounds} 次工具调用；能直接完成任务就不要请求工具
- web 结果只是未验证线索（web_unverified），不能当作已验证文献证据引用
- 最终输出必须是任务要求的 JSON，不得再包含 tool_request"""


class WebToolExecutor:
    """Execute model-issued web tool requests; never raises into planning."""

    KNOWN_TOOLS = ("web_search", "web_read")

    def __init__(
        self,
        *,
        web_client: Optional[WebSearchClient] = None,
        kb_dir: str | Path | None = None,
        campaign_id: str = "",
        ingestion: Optional[KnowledgeIngestion] = None,
        registry: Optional[PaperRegistry] = None,
        max_results: int = 5,
        max_page_chars: int = 6000,
    ) -> None:
        self.web_client = web_client or WebSearchClient()
        self.kb_dir = (
            Path(kb_dir).expanduser().resolve()
            if kb_dir
            else Path(__file__).resolve().parents[1] / "chem_kb"
        )
        self.campaign_id = campaign_id
        self._ingestion = ingestion
        self._registry = registry
        self.max_results = max_results
        self.max_page_chars = max_page_chars

    @staticmethod
    def protocol_instructions(max_rounds: int) -> str:
        return TOOL_PROTOCOL_INSTRUCTIONS.replace("{max_rounds}", str(max_rounds))

    def execute(self, request: Dict[str, Any]) -> Dict[str, Any]:
        tool = str((request or {}).get("tool", "")).strip().lower()
        try:
            if tool == "web_search":
                return self._web_search(request)
            if tool == "web_read":
                return self._web_read(request)
            return {
                "tool": tool or "(missing)",
                "status": "error",
                "error": f"unknown tool; available: {', '.join(self.KNOWN_TOOLS)}",
            }
        except Exception as exc:  # tool failures must not break planning
            return {
                "tool": tool,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }

    # ------------------------------------------------------------------
    # tools
    # ------------------------------------------------------------------

    def _web_search(self, request: Dict[str, Any]) -> Dict[str, Any]:
        query = str(request.get("query", "")).strip()[:200]
        if not query:
            return {"tool": "web_search", "status": "error", "error": "query is required"}
        try:
            max_results = int(request.get("max_results", self.max_results) or self.max_results)
        except (TypeError, ValueError):
            max_results = self.max_results
        max_results = max(1, min(max_results, 8))

        results = self.web_client.search(query, max_results=max_results)
        return {
            "tool": "web_search",
            "status": "ok",
            "query": query,
            "engine": results[0].engine if results else "",
            "results": [
                {
                    "title": result.title,
                    "url": result.url,
                    "snippet": result.snippet,
                }
                for result in results
            ],
            "errors": list(getattr(self.web_client, "last_errors", []) or []),
            "note": "web 结果为未验证线索；需要正文时用 web_read 读取具体 url",
        }

    def _web_read(self, request: Dict[str, Any]) -> Dict[str, Any]:
        url = str(request.get("url", "")).strip()
        if not url.startswith("http"):
            return {"tool": "web_read", "status": "error", "error": "a http(s) url is required"}

        content = self.web_client.fetch_page(url, max_chars=self.max_page_chars)
        if not str(content or "").strip():
            return {
                "tool": "web_read",
                "status": "error",
                "url": url,
                "error": "page fetch returned no readable text",
                "errors": list(getattr(self.web_client, "last_errors", []) or []),
            }

        archived_path = self._archive_page(url, str(content))
        return {
            "tool": "web_read",
            "status": "ok",
            "url": url,
            "content": str(content)[: self.max_page_chars],
            "archived": bool(archived_path),
            "archived_path": archived_path,
            "note": "内容已按 web_unverified 归档，仅作线索，不作参数级证据",
        }

    # ------------------------------------------------------------------
    # archiving (consistent with the acquisition web line)
    # ------------------------------------------------------------------

    def _archive_page(self, url: str, content: str) -> str:
        try:
            ingestion = self._ingestion or KnowledgeIngestion(self.kb_dir)
            registry = self._registry or PaperRegistry(self.kb_dir)
            title = self._title_from_content(url, content)
            record = ingestion.record_from_text(
                title=title,
                text=content,
                source_path=url,
                source_type="web",
            )
            path = str(ingestion.write_record(record))
            registry.upsert(
                title=title,
                source="web:llm_tool",
                url=url,
                verification_status="web_unverified",
                full_text_status="parsed",
                corpus_file=path,
                campaign_id=self.campaign_id,
                role="llm_web_tool",
            )
            return path
        except Exception:
            return ""

    @staticmethod
    def _title_from_content(url: str, content: str) -> str:
        for line in content.splitlines()[:10]:
            cleaned = re.sub(r"\s+", " ", line).strip(" #*")
            if 10 <= len(cleaned) <= 160:
                return cleaned
        return url[:160]
