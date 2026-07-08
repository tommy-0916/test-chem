"""General web search + page reading for the research layer.

Engine chain (highest priority first, auto-skipped when unconfigured):

  1. Tavily        POST https://api.tavily.com/search          TAVILY_API_KEY
  2. Serper/Google POST https://google.serper.dev/search       SERPER_API_KEY
  3. Brave         GET  https://api.search.brave.com/...       BRAVE_API_KEY
  4. SearXNG       GET  {SEARXNG_BASE_URL}/search?format=json  self-hosted
  5. DuckDuckGo    GET  https://html.duckduckgo.com/html/      keyless fallback

Page reading: Jina Reader (https://r.jina.ai/<url>, optional JINA_API_KEY)
with a direct-fetch + tag-strip fallback.

Design rules (same as the scholarly client): stdlib urllib only, every
failure lands in ``last_errors`` and never propagates into planning, all
keys come from the environment. Web results are **candidates, not
evidence** — downstream ingestion tags them ``web_unverified``.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

DEFAULT_WEB_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_DDG_LINK_RE = re.compile(
    r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_DDG_SNIPPET_RE = re.compile(
    r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class WebSearchResult:
    """One web search hit (candidate lead, not evidence)."""

    title: str
    url: str
    snippet: str = ""
    content: str = ""
    score: float = 0.0
    engine: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


class WebSearchClient:
    """Multi-engine web search with graceful degradation."""

    ENGINE_PRIORITY = ["tavily", "serper", "brave", "searxng", "duckduckgo"]

    def __init__(
        self,
        *,
        tavily_api_key: str = "",
        serper_api_key: str = "",
        brave_api_key: str = "",
        searxng_base_url: str = "",
        jina_api_key: str = "",
        timeout_seconds: int = 20,
        user_agent: str = DEFAULT_WEB_USER_AGENT,
    ) -> None:
        self.tavily_api_key = tavily_api_key or os.getenv("TAVILY_API_KEY", "")
        self.serper_api_key = serper_api_key or os.getenv("SERPER_API_KEY", "")
        self.brave_api_key = brave_api_key or os.getenv("BRAVE_API_KEY", "")
        self.searxng_base_url = (
            searxng_base_url or os.getenv("SEARXNG_BASE_URL", "")
        ).rstrip("/")
        self.jina_api_key = jina_api_key or os.getenv("JINA_API_KEY", "")
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.last_errors: List[str] = []

    # ------------------------------------------------------------------
    # engine selection
    # ------------------------------------------------------------------

    def available_engines(self) -> List[str]:
        engines: List[str] = []
        if self.tavily_api_key:
            engines.append("tavily")
        if self.serper_api_key:
            engines.append("serper")
        if self.brave_api_key:
            engines.append("brave")
        if self.searxng_base_url:
            engines.append("searxng")
        engines.append("duckduckgo")  # keyless fallback, always available
        return engines

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        engine: str = "auto",
    ) -> List[WebSearchResult]:
        """Search the web; ``auto`` walks the engine chain until results."""
        self.last_errors = []
        cleaned = (query or "").strip()
        if not cleaned:
            return []
        engines = (
            self.available_engines() if engine == "auto" else [engine.strip().lower()]
        )
        for name in engines:
            handler = getattr(self, f"_search_{name}", None)
            if handler is None:
                self.last_errors.append(f"unknown web engine: {name}")
                continue
            try:
                results = handler(cleaned, max_results)
            except Exception as exc:
                self.last_errors.append(f"{name}: {type(exc).__name__}: {exc}")
                continue
            if results:
                return results[:max_results]
        return []

    # ------------------------------------------------------------------
    # engines
    # ------------------------------------------------------------------

    def _search_tavily(self, query: str, max_results: int) -> List[WebSearchResult]:
        if not self.tavily_api_key:
            raise ValueError("TAVILY_API_KEY not configured")
        payload = {
            "api_key": self.tavily_api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": False,
        }
        data = self._post_json("https://api.tavily.com/search", payload)
        results: List[WebSearchResult] = []
        for item in data.get("results", []) or []:
            if not isinstance(item, dict):
                continue
            results.append(
                WebSearchResult(
                    title=str(item.get("title", "")).strip(),
                    url=str(item.get("url", "")).strip(),
                    snippet=str(item.get("content", ""))[:500],
                    content=str(item.get("content", "")),
                    score=float(item.get("score", 0.0) or 0.0),
                    engine="tavily",
                )
            )
        return results

    def _search_serper(self, query: str, max_results: int) -> List[WebSearchResult]:
        if not self.serper_api_key:
            raise ValueError("SERPER_API_KEY not configured")
        data = self._serper_post("/search", query, num=max_results)
        results: List[WebSearchResult] = []
        for item in data.get("organic", []) or []:
            if not isinstance(item, dict):
                continue
            results.append(
                WebSearchResult(
                    title=str(item.get("title", "")).strip(),
                    url=str(item.get("link", "")).strip(),
                    snippet=str(item.get("snippet", ""))[:500],
                    engine="serper",
                    raw={"date": item.get("date", "")},
                )
            )
        return results

    def search_scholar(
        self,
        query: str,
        *,
        max_results: int = 5,
    ) -> List[Dict[str, Any]]:
        """Google Scholar via Serper /scholar (needs SERPER_API_KEY).

        Returns lead dicts; the scholarly client turns them into
        ExternalPaper candidates that still require DOI/arXiv verification.
        """
        if not self.serper_api_key:
            raise ValueError("SERPER_API_KEY not configured (google scholar)")
        data = self._serper_post("/scholar", query, num=max_results)
        leads: List[Dict[str, Any]] = []
        for item in data.get("organic", []) or []:
            if not isinstance(item, dict):
                continue
            leads.append(
                {
                    "title": str(item.get("title", "")).strip(),
                    "url": str(item.get("link", "")).strip(),
                    "snippet": str(item.get("snippet", "")).strip(),
                    "year": str(item.get("year", "") or ""),
                    "publication_info": str(item.get("publicationInfo", "") or ""),
                    "pdf_url": str(item.get("pdfUrl", "") or ""),
                }
            )
        return leads[:max_results]

    def _serper_post(self, endpoint: str, query: str, *, num: int) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"q": query, "num": num}
        if any("一" <= char <= "鿿" for char in query):
            payload.update({"gl": "cn", "hl": "zh-cn"})
        else:
            payload.update({"gl": "us", "hl": "en"})
        return self._post_json(
            f"https://google.serper.dev{endpoint}",
            payload,
            headers={"X-API-KEY": self.serper_api_key},
        )

    def _search_brave(self, query: str, max_results: int) -> List[WebSearchResult]:
        if not self.brave_api_key:
            raise ValueError("BRAVE_API_KEY not configured")
        params = urllib.parse.urlencode({"q": query, "count": max_results})
        data = json.loads(
            self._get_text(
                f"https://api.search.brave.com/res/v1/web/search?{params}",
                headers={
                    "X-Subscription-Token": self.brave_api_key,
                    "Accept": "application/json",
                },
            )
        )
        results: List[WebSearchResult] = []
        for item in ((data.get("web") or {}).get("results") or []):
            if not isinstance(item, dict):
                continue
            results.append(
                WebSearchResult(
                    title=str(item.get("title", "")).strip(),
                    url=str(item.get("url", "")).strip(),
                    snippet=self._strip_tags(str(item.get("description", "")))[:500],
                    engine="brave",
                )
            )
        return results

    def _search_searxng(self, query: str, max_results: int) -> List[WebSearchResult]:
        if not self.searxng_base_url:
            raise ValueError("SEARXNG_BASE_URL not configured")
        params = urllib.parse.urlencode({"q": query, "format": "json"})
        data = json.loads(self._get_text(f"{self.searxng_base_url}/search?{params}"))
        results: List[WebSearchResult] = []
        for item in data.get("results", []) or []:
            if not isinstance(item, dict):
                continue
            results.append(
                WebSearchResult(
                    title=str(item.get("title", "")).strip(),
                    url=str(item.get("url", "")).strip(),
                    snippet=str(item.get("content", ""))[:500],
                    engine="searxng",
                )
            )
        return results[:max_results]

    def _search_duckduckgo(self, query: str, max_results: int) -> List[WebSearchResult]:
        encoded = urllib.parse.quote_plus(query)
        html = self._get_text(f"https://html.duckduckgo.com/html/?q={encoded}")
        return self._parse_ddg_html(html, max_results)

    def _parse_ddg_html(self, html: str, max_results: int) -> List[WebSearchResult]:
        links = _DDG_LINK_RE.findall(html)
        snippets = _DDG_SNIPPET_RE.findall(html)
        results: List[WebSearchResult] = []
        for index, (url, title_html) in enumerate(links):
            title = self._strip_tags(title_html)
            snippet = (
                self._strip_tags(snippets[index]) if index < len(snippets) else ""
            )
            if "duckduckgo.com" in url:
                # unwrap //duckduckgo.com/l/?uddg=https%3A... redirects
                parsed = urllib.parse.urlparse(url)
                uddg = urllib.parse.parse_qs(parsed.query).get("uddg")
                if not uddg:
                    continue
                url = urllib.parse.unquote(uddg[0])
            if not url.startswith("http"):
                continue
            results.append(
                WebSearchResult(
                    title=title,
                    url=url,
                    snippet=snippet[:500],
                    engine="duckduckgo",
                )
            )
            if len(results) >= max_results:
                break
        return results

    # ------------------------------------------------------------------
    # page reading
    # ------------------------------------------------------------------

    def fetch_page(self, url: str, *, max_chars: int = 20000) -> str:
        """Readable page text: Jina Reader first, direct fetch fallback."""
        cleaned = (url or "").strip()
        if not cleaned.startswith("http"):
            return ""
        try:
            headers: Dict[str, str] = {}
            if self.jina_api_key:
                headers["Authorization"] = f"Bearer {self.jina_api_key}"
            text = self._get_text(f"https://r.jina.ai/{cleaned}", headers=headers)
            if text.strip():
                return text[:max_chars]
        except Exception as exc:
            self.last_errors.append(f"jina_reader: {type(exc).__name__}: {exc}")
        try:
            return self._fetch_direct(cleaned)[:max_chars]
        except Exception as exc:
            self.last_errors.append(f"direct_fetch: {type(exc).__name__}: {exc}")
            return ""

    def _fetch_direct(self, url: str) -> str:
        html = self._get_text(url)
        cleaned = _SCRIPT_STYLE_RE.sub(" ", html)
        text = self._strip_tags(cleaned)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    # ------------------------------------------------------------------
    # transport helpers (override in tests)
    # ------------------------------------------------------------------

    def _get_text(self, url: str, *, headers: Optional[Dict[str, str]] = None) -> str:
        resolved = {"User-Agent": self.user_agent}
        resolved.update(headers or {})
        request = urllib.request.Request(url, headers=resolved)
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return response.read().decode("utf-8", errors="replace")

    def _post_json(
        self,
        url: str,
        payload: Dict[str, Any],
        *,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        resolved = {
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
        }
        resolved.update(headers or {})
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=resolved,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))

    @staticmethod
    def _strip_tags(value: str) -> str:
        import html as html_module

        return re.sub(r"\s+", " ", _TAG_RE.sub(" ", html_module.unescape(value or ""))).strip()
