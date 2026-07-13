"""General web search + page reading for the research layer.

Engine chain (highest priority first, auto-skipped when unconfigured):

  1. Tavily        POST https://api.tavily.com/search          TAVILY_API_KEY
                   (one key or an ordered, comma-separated key list)
  2. Serper/Google POST https://google.serper.dev/search       SERPER_API_KEY
  3. Brave         GET  https://api.search.brave.com/...       BRAVE_API_KEY
  4. SearXNG       GET  {SEARXNG_BASE_URL}/search?format=json  self-hosted
  5. DuckDuckGo    GET  https://html.duckduckgo.com/html/      keyless fallback

Page reading: Jina Reader (https://r.jina.ai/<url>, optional JINA_API_KEY)
with a direct-fetch + tag-strip fallback.

Design rules (same as the scholarly client): stdlib urllib only, every
failure lands in ``last_errors`` and ``last_attempts`` and never propagates
into planning, all keys come from the environment. Web results are
**candidates, not evidence** — downstream ingestion tags them
``web_unverified``.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .network_safety import (
    UrlValidator,
    build_safe_opener,
    validate_public_http_url,
)

DEFAULT_WEB_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_MAX_WEB_RESPONSE_BYTES = 4 * 1024 * 1024

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

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable representation of the result."""
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "content": self.content,
            "score": self.score,
            "engine": self.engine,
            "raw": dict(self.raw),
        }


@dataclass(frozen=True)
class WebSearchAttempt:
    """One provider attempt, without retaining credential material."""

    engine: str
    status: str
    result_count: int = 0
    error: str = ""
    credential_index: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable health record for logs and APIs."""
        return {
            "engine": self.engine,
            "status": self.status,
            "result_count": self.result_count,
            "error": self.error,
            "credential_index": self.credential_index,
        }


class WebSearchClient:
    """Multi-engine web search with graceful degradation."""

    ENGINE_PRIORITY = ["tavily", "serper", "brave", "searxng", "duckduckgo"]

    def __init__(
        self,
        *,
        tavily_api_key: str = "",
        tavily_api_keys: Optional[Sequence[str]] = None,
        serper_api_key: str = "",
        brave_api_key: str = "",
        searxng_base_url: str = "",
        jina_api_key: str = "",
        timeout_seconds: int = 20,
        user_agent: str = DEFAULT_WEB_USER_AGENT,
        max_response_bytes: int = DEFAULT_MAX_WEB_RESPONSE_BYTES,
        opener: Optional[Callable[..., Any]] = None,
        url_validator: Optional[UrlValidator] = None,
        total_timeout_seconds: int = 60,
        page_timeout_seconds: int = 30,
    ) -> None:
        explicit_tavily_keys: List[str] = []
        if tavily_api_key:
            explicit_tavily_keys.append(tavily_api_key)
        if isinstance(tavily_api_keys, str):
            explicit_tavily_keys.append(tavily_api_keys)
        elif tavily_api_keys:
            explicit_tavily_keys.extend(tavily_api_keys)
        key_sources = explicit_tavily_keys or [os.getenv("TAVILY_API_KEY", "")]
        self.tavily_api_keys = self._parse_api_keys(key_sources)
        # Keep the original attribute for callers that inspect configuration.
        self.tavily_api_key = self.tavily_api_keys[0] if self.tavily_api_keys else ""
        self.serper_api_key = serper_api_key or os.getenv("SERPER_API_KEY", "")
        self.brave_api_key = brave_api_key or os.getenv("BRAVE_API_KEY", "")
        self.searxng_base_url = (
            searxng_base_url or os.getenv("SEARXNG_BASE_URL", "")
        ).rstrip("/")
        self.jina_api_key = jina_api_key or os.getenv("JINA_API_KEY", "")
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.max_response_bytes = max(1024, int(max_response_bytes))
        self._url_validator = url_validator or validate_public_http_url
        self._opener = opener or build_safe_opener(self._url_validator)
        self.total_timeout_seconds = max(1, int(total_timeout_seconds))
        self.page_timeout_seconds = max(1, int(page_timeout_seconds))
        self._deadline_state = threading.local()
        self.last_errors: List[str] = []
        self.last_attempts: List[WebSearchAttempt] = []

    # ------------------------------------------------------------------
    # engine selection
    # ------------------------------------------------------------------

    def available_engines(self) -> List[str]:
        engines: List[str] = []
        if self.tavily_api_keys:
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
        self.last_attempts = []
        cleaned = (query or "").strip()
        if not cleaned:
            return []
        engines = (
            self.available_engines() if engine == "auto" else [engine.strip().lower()]
        )
        previous_deadline = getattr(self._deadline_state, "value", None)
        previous_slice = getattr(self._deadline_state, "slice_timeout", None)
        self._deadline_state.value = time.monotonic() + self.total_timeout_seconds
        try:
            return self._search_engines(cleaned, engines, max_results)
        finally:
            for attribute, previous in (
                ("value", previous_deadline),
                ("slice_timeout", previous_slice),
            ):
                if previous is None:
                    try:
                        delattr(self._deadline_state, attribute)
                    except AttributeError:
                        pass
                else:
                    setattr(self._deadline_state, attribute, previous)

    def _search_engines(
        self,
        query: str,
        engines: Sequence[str],
        max_results: int,
    ) -> List[WebSearchResult]:
        request_budget = sum(
            len(self.tavily_api_keys)
            if name == "tavily" and self.tavily_api_keys
            else 1
            for name in engines
        )
        completed_requests = 0
        for name in engines:
            handler = getattr(self, f"_search_{name}", None)
            if handler is None:
                error = f"unknown web engine: {name}"
                self.last_errors.append(error)
                self.last_attempts.append(
                    WebSearchAttempt(engine=name, status="error", error=error)
                )
                continue
            credentials: List[Tuple[Optional[int], Optional[str]]] = [(None, None)]
            if name == "tavily" and self.tavily_api_keys:
                credentials = list(enumerate(self.tavily_api_keys, start=1))
            for credential_index, credential in credentials:
                remaining = self._remaining_seconds()
                if remaining <= 0:
                    detail = (
                        f"total search deadline exceeded "
                        f"({self.total_timeout_seconds}s)"
                    )
                    self.last_errors.append(f"{name}: {detail}")
                    self.last_attempts.append(
                        WebSearchAttempt(engine=name, status="error", error=detail)
                    )
                    return []
                remaining_requests = max(1, request_budget - completed_requests)
                self._deadline_state.slice_timeout = remaining / remaining_requests
                try:
                    if name == "tavily":
                        results = handler(
                            query,
                            max_results,
                            api_key=credential,
                        )
                    else:
                        results = handler(query, max_results)
                except Exception as exc:
                    label = (
                        f"{name}[credential#{credential_index}]"
                        if credential_index is not None
                        else name
                    )
                    detail = self._redact_secrets(
                        f"{type(exc).__name__}: {exc}"
                    )
                    error = f"{label}: {detail}"
                    self.last_errors.append(error)
                    self.last_attempts.append(
                        WebSearchAttempt(
                            engine=name,
                            status="error",
                            error=detail,
                            credential_index=credential_index,
                        )
                    )
                    completed_requests += 1
                    continue
                completed_requests += 1
                deduplicated = self._deduplicate_results(results, max_results)
                self.last_attempts.append(
                    WebSearchAttempt(
                        engine=name,
                        status="success" if deduplicated else "empty",
                        result_count=len(deduplicated),
                        credential_index=credential_index,
                    )
                )
                if deduplicated:
                    return deduplicated
        return []

    def _remaining_seconds(self) -> float:
        deadline = getattr(self._deadline_state, "value", None)
        if deadline is None:
            return float(self.timeout_seconds)
        return deadline - time.monotonic()

    def _network_timeout(self) -> float:
        remaining = self._remaining_seconds()
        if remaining <= 0:
            raise TimeoutError(
                f"total search deadline exceeded ({self.total_timeout_seconds}s)"
            )
        sliced = getattr(self._deadline_state, "slice_timeout", remaining)
        return max(0.05, min(float(self.timeout_seconds), remaining, float(sliced)))

    # ------------------------------------------------------------------
    # engines
    # ------------------------------------------------------------------

    def _search_tavily(
        self,
        query: str,
        max_results: int,
        *,
        api_key: Optional[str] = None,
    ) -> List[WebSearchResult]:
        resolved_api_key = api_key or self.tavily_api_key
        if not resolved_api_key:
            raise ValueError("TAVILY_API_KEY not configured")
        payload = {
            "api_key": resolved_api_key,
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
        return results

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
        return results

    # ------------------------------------------------------------------
    # result and diagnostic normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_api_keys(values: Sequence[str]) -> Tuple[str, ...]:
        """Parse comma-separated keys, preserving order and removing duplicates."""
        keys: List[str] = []
        seen = set()
        for value in values:
            for candidate in str(value or "").split(","):
                cleaned = candidate.strip()
                if cleaned and cleaned not in seen:
                    keys.append(cleaned)
                    seen.add(cleaned)
        return tuple(keys)

    @staticmethod
    def _deduplicate_results(
        results: Sequence[WebSearchResult],
        max_results: int,
    ) -> List[WebSearchResult]:
        """Drop invalid and duplicate URLs while retaining provider ordering."""
        unique: List[WebSearchResult] = []
        seen = set()
        limit = max(0, int(max_results))
        if limit == 0:
            return unique
        for result in results:
            cleaned_url = str(result.url or "").strip()
            parsed = urllib.parse.urlsplit(cleaned_url)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                continue
            dedupe_key = urllib.parse.urlunsplit(
                (
                    parsed.scheme.lower(),
                    parsed.netloc.lower(),
                    parsed.path.rstrip("/"),
                    parsed.query,
                    "",
                )
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            unique.append(result)
            if len(unique) >= limit:
                break
        return unique

    def _redact_secrets(self, value: str) -> str:
        """Ensure provider exceptions cannot echo configured Tavily keys."""
        redacted = str(value or "")
        for api_key in self.tavily_api_keys:
            redacted = redacted.replace(api_key, "[REDACTED]")
        return redacted

    # ------------------------------------------------------------------
    # page reading
    # ------------------------------------------------------------------

    def fetch_page(self, url: str, *, max_chars: int = 20000) -> str:
        """Readable page text: Jina Reader first, direct fetch fallback."""
        previous_deadline = getattr(self._deadline_state, "value", None)
        previous_slice = getattr(self._deadline_state, "slice_timeout", None)
        self._deadline_state.value = time.monotonic() + self.page_timeout_seconds
        self._deadline_state.slice_timeout = self.page_timeout_seconds / 2
        try:
            return self._fetch_page_bounded(url, max_chars=max_chars)
        finally:
            for attribute, previous in (
                ("value", previous_deadline),
                ("slice_timeout", previous_slice),
            ):
                if previous is None:
                    try:
                        delattr(self._deadline_state, attribute)
                    except AttributeError:
                        pass
                else:
                    setattr(self._deadline_state, attribute, previous)

    def _fetch_page_bounded(self, url: str, *, max_chars: int) -> str:
        cleaned = (url or "").strip()
        try:
            self._url_validator(cleaned)
        except Exception as exc:
            self.last_errors.append(f"unsafe_url: {type(exc).__name__}: {exc}")
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
        with self._opener(request, timeout=self._network_timeout()) as response:
            return self._read_response(response).decode("utf-8", errors="replace")

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
        with self._opener(request, timeout=self._network_timeout()) as response:
            return json.loads(
                self._read_response(response).decode("utf-8", errors="replace")
            )

    def _read_response(self, response: Any) -> bytes:
        payload = response.read(self.max_response_bytes + 1)
        if len(payload) > self.max_response_bytes:
            raise ValueError(
                f"response exceeds {self.max_response_bytes} byte limit"
            )
        return payload

    @staticmethod
    def _strip_tags(value: str) -> str:
        import html as html_module

        return re.sub(r"\s+", " ", _TAG_RE.sub(" ", html_module.unescape(value or ""))).strip()
