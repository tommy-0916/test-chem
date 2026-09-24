"""Redundant, observable, and bounded open-access PDF downloading.

The downloader resolves one paper through independent providers in order and
stops at the first payload that passes PDF validation. Provider failures are
returned to callers instead of being hidden or raised into the agent workflow.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .network_safety import (
    UrlValidator,
    build_safe_opener,
    validate_public_http_url,
)
from .web_search import WebSearchClient


DEFAULT_PDF_USER_AGENT = "chemagent-paper-downloader/0.1"
DEFAULT_MAX_PDF_BYTES = 50 * 1024 * 1024
_ARXIV_ID_RE = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf)/|^)(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)",
    re.IGNORECASE,
)
_PDF_CONTENT_TYPES = {
    "application/pdf",
    "application/x-pdf",
    "application/octet-stream",
    "binary/octet-stream",
}
_METADATA_PDF_KEYS = {
    "downloadurl",
    "download_url",
    "fulltexturl",
    "full_text_url",
    "oa_pdf_url",
    "pdf",
    "pdf_url",
    "sourcefulltexturls",
    "source_fulltext_urls",
    "url_for_pdf",
}


@dataclass
class PdfDownloadAttempt:
    """One provider resolution or download outcome."""

    provider: str
    status: str
    url: str = ""
    error: str = ""
    bytes_written: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "url": self.url,
            "error": self.error,
            "bytes_written": self.bytes_written,
        }


@dataclass
class PdfDownloadResult:
    """Final download result plus the complete provider attempt trail."""

    paper_title: str
    path: Optional[Path] = None
    attempts: List[PdfDownloadAttempt] = field(default_factory=list)

    @property
    def downloaded(self) -> bool:
        return self.path is not None

    @property
    def successful_attempt(self) -> Optional[PdfDownloadAttempt]:
        for attempt in reversed(self.attempts):
            if attempt.status == "downloaded":
                return attempt
        return None

    @property
    def errors(self) -> List[str]:
        errors: List[str] = []
        for attempt in self.attempts:
            if attempt.status in {"downloaded", "no_candidate"}:
                continue
            detail = attempt.error or attempt.status
            errors.append(f"{attempt.provider}: {detail}")
        return errors

    def to_dict(self) -> Dict[str, Any]:
        success = self.successful_attempt
        return {
            "downloaded": self.downloaded,
            "path": str(self.path) if self.path else "",
            "provider": success.provider if success else "",
            "url": success.url if success else "",
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "errors": self.errors,
        }


class OpenAccessPdfDownloader:
    """Resolve and download an OA PDF with provider-level failover.

    Resolution order is deterministic: arXiv direct URL, Semantic Scholar
    ``openAccessPdf``, Unpaywall, CORE, PDF-looking metadata URLs, then an open
    web PDF lead. Network access uses stdlib ``urllib`` only.
    """

    def __init__(
        self,
        *,
        semantic_scholar_api_key: str = "",
        unpaywall_email: str = "",
        core_api_key: str = "",
        user_agent: str = DEFAULT_PDF_USER_AGENT,
        timeout_seconds: int = 30,
        max_pdf_bytes: int = DEFAULT_MAX_PDF_BYTES,
        opener: Optional[Callable[..., Any]] = None,
        web_search_client: Optional[WebSearchClient] = None,
        url_validator: Optional[UrlValidator] = None,
        max_candidates_per_provider: int = 5,
        max_total_download_attempts: int = 20,
        total_timeout_seconds: int = 120,
    ) -> None:
        self.semantic_scholar_api_key = (
            semantic_scholar_api_key.strip()
            or os.getenv("S2_API_KEY", "").strip()
            or os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip()
        )
        self.unpaywall_email = (
            unpaywall_email.strip()
            or os.getenv("UNPAYWALL_EMAIL", "").strip()
            or os.getenv("CROSSREF_MAILTO", "").strip()
        )
        self.core_api_key = core_api_key.strip() or os.getenv("CORE_API_KEY", "").strip()
        self.user_agent = user_agent
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.max_pdf_bytes = max(1024, int(max_pdf_bytes))
        self._url_validator = url_validator or validate_public_http_url
        self._opener = opener or build_safe_opener(self._url_validator)
        self.web_search_client = web_search_client
        self.max_candidates_per_provider = max(1, int(max_candidates_per_provider))
        self.max_total_download_attempts = max(1, int(max_total_download_attempts))
        self.total_timeout_seconds = max(1, int(total_timeout_seconds))
        self._deadline_state = threading.local()

    def download(
        self,
        paper: Any,
        output_dir: str | Path,
        *,
        candidate_validator: Optional[Callable[[Path], Any]] = None,
    ) -> PdfDownloadResult:
        """Download ``paper`` to ``output_dir`` and retain every failed attempt."""
        result = PdfDownloadResult(paper_title=str(getattr(paper, "title", "")))
        destination = Path(output_dir).expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        target = self._reserve_unique_path(
            destination / self._paper_filename(paper, result.paper_title)
        )

        previous_deadline = getattr(self._deadline_state, "value", None)
        self._deadline_state.value = time.monotonic() + self.total_timeout_seconds
        try:
            return self._download_channels(
                paper,
                target,
                result,
                candidate_validator=candidate_validator,
            )
        finally:
            if result.path is None:
                target.unlink(missing_ok=True)
            if previous_deadline is None:
                try:
                    del self._deadline_state.value
                except AttributeError:
                    pass
            else:
                self._deadline_state.value = previous_deadline

    def _download_channels(
        self,
        paper: Any,
        target: Path,
        result: PdfDownloadResult,
        *,
        candidate_validator: Optional[Callable[[Path], Any]],
    ) -> PdfDownloadResult:
        channels = [
            ("arxiv_direct", self._resolve_arxiv),
            ("semantic_scholar", self._resolve_semantic_scholar),
            ("unpaywall", self._resolve_unpaywall),
            ("core", self._resolve_core),
            ("metadata", self._resolve_metadata_urls),
            ("web_search", self._resolve_web_search),
        ]
        seen_urls = set()
        download_attempt_count = 0
        for provider, resolver in channels:
            if self._deadline_exceeded():
                result.attempts.append(self._deadline_attempt())
                break
            try:
                urls = self._unique_urls(resolver(paper))
            except Exception as exc:
                result.attempts.append(
                    PdfDownloadAttempt(
                        provider=provider,
                        status="resolution_failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            if len(urls) > self.max_candidates_per_provider:
                result.attempts.append(
                    PdfDownloadAttempt(
                        provider=provider,
                        status="candidate_cap_reached",
                        error=(
                            f"kept first {self.max_candidates_per_provider} of "
                            f"{len(urls)} candidates"
                        ),
                    )
                )
                urls = urls[: self.max_candidates_per_provider]
            if not urls:
                result.attempts.append(
                    PdfDownloadAttempt(provider=provider, status="no_candidate")
                )
                continue
            for url in urls:
                if url in seen_urls:
                    continue
                if download_attempt_count >= self.max_total_download_attempts:
                    result.attempts.append(
                        PdfDownloadAttempt(
                            provider="downloader",
                            status="candidate_cap_reached",
                            error=(
                                "global download attempt cap reached "
                                f"({self.max_total_download_attempts})"
                            ),
                        )
                    )
                    return result
                if self._deadline_exceeded():
                    result.attempts.append(self._deadline_attempt())
                    return result
                seen_urls.add(url)
                attempt = self._download_candidate(provider, url, target)
                download_attempt_count += 1
                result.attempts.append(attempt)
                if attempt.status == "downloaded":
                    if candidate_validator is not None:
                        try:
                            accepted = candidate_validator(target)
                            if accepted is False:
                                raise ValueError("candidate validator returned false")
                        except Exception as exc:
                            self._reset_reservation(target)
                            attempt.status = "parse_failed"
                            attempt.error = (
                                "downloaded PDF failed content validation: "
                                f"{type(exc).__name__}: {exc}"
                            )
                            continue
                    if self._deadline_exceeded():
                        attempt.status = "deadline_exceeded"
                        attempt.error = (
                            f"total download deadline exceeded "
                            f"({self.total_timeout_seconds}s)"
                        )
                        return result
                    result.path = target
                    return result
        return result

    def _deadline_exceeded(self) -> bool:
        deadline = getattr(self._deadline_state, "value", None)
        return deadline is not None and time.monotonic() >= deadline

    def _deadline_attempt(self) -> PdfDownloadAttempt:
        return PdfDownloadAttempt(
            provider="downloader",
            status="deadline_exceeded",
            error=(
                f"total download deadline exceeded ({self.total_timeout_seconds}s)"
            ),
        )

    def _network_timeout(self) -> float:
        deadline = getattr(self._deadline_state, "value", None)
        if deadline is None:
            return float(self.timeout_seconds)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"total download deadline exceeded ({self.total_timeout_seconds}s)"
            )
        return max(0.05, min(float(self.timeout_seconds), remaining))

    def _resolve_arxiv(self, paper: Any) -> List[str]:
        arxiv_id = self._arxiv_id(paper)
        if not arxiv_id:
            return []
        encoded = urllib.parse.quote(arxiv_id.removesuffix(".pdf"), safe="/")
        return [f"https://arxiv.org/pdf/{encoded}.pdf"]

    def _resolve_semantic_scholar(self, paper: Any) -> List[str]:
        if getattr(paper, "source", "") == "semantic_scholar" and getattr(
            paper, "pdf_url", ""
        ):
            return [str(paper.pdf_url)]
        identifier = self._s2_identifier(paper)
        if not identifier:
            return []
        fields = urllib.parse.urlencode({"fields": "openAccessPdf"})
        quoted = urllib.parse.quote(identifier, safe=":")
        url = f"https://api.semanticscholar.org/graph/v1/paper/{quoted}?{fields}"
        headers = {"Accept": "application/json"}
        if self.semantic_scholar_api_key:
            headers["x-api-key"] = self.semantic_scholar_api_key
        try:
            payload = self._get_json(url, headers=headers)
        except urllib.error.HTTPError as exc:
            if not self.semantic_scholar_api_key or exc.code not in {401, 403}:
                raise
            payload = self._get_json(url, headers={"Accept": "application/json"})
        open_pdf = payload.get("openAccessPdf") or {}
        if not isinstance(open_pdf, dict):
            return []
        candidate = str(open_pdf.get("url") or "").strip()
        return [candidate] if candidate else []

    def _resolve_unpaywall(self, paper: Any) -> List[str]:
        doi = self._doi(paper)
        if not doi:
            return []
        if not self.unpaywall_email:
            raise ValueError("UNPAYWALL_EMAIL or CROSSREF_MAILTO is required")
        encoded = urllib.parse.quote(doi, safe="")
        query = urllib.parse.urlencode({"email": self.unpaywall_email})
        payload = self._get_json(
            f"https://api.unpaywall.org/v2/{encoded}?{query}",
            headers={"Accept": "application/json"},
        )
        if not payload.get("is_oa"):
            return []
        locations: List[Any] = [payload.get("best_oa_location")]
        locations.extend(payload.get("oa_locations") or [])
        urls: List[str] = []
        for location in locations:
            if not isinstance(location, dict):
                continue
            urls.extend(
                [
                    str(location.get("url_for_pdf") or "").strip(),
                    str(location.get("url") or "").strip(),
                ]
            )
        return [url for url in urls if url]

    def _resolve_core(self, paper: Any) -> List[str]:
        doi = self._doi(paper)
        title = str(getattr(paper, "title", "") or "").strip()
        if not doi and not title:
            return []
        headers = {"Accept": "application/json"}
        if self.core_api_key:
            headers["Authorization"] = f"Bearer {self.core_api_key}"

        payloads: List[Dict[str, Any]] = []
        errors: List[str] = []
        if doi:
            encoded = urllib.parse.quote(doi, safe="")
            try:
                payloads.append(
                    self._get_json(
                        f"https://api.core.ac.uk/v3/outputs/doi/{encoded}",
                        headers=headers,
                    )
                )
            except Exception as exc:
                errors.append(f"DOI lookup: {type(exc).__name__}: {exc}")
        if title and not self._urls_from_core_payloads(payloads):
            query = urllib.parse.urlencode({"q": title, "limit": 3})
            try:
                payloads.append(
                    self._get_json(
                        f"https://api.core.ac.uk/v3/search/works?{query}",
                        headers=headers,
                    )
                )
            except Exception as exc:
                errors.append(f"title lookup: {type(exc).__name__}: {exc}")
        urls = self._urls_from_core_payloads(payloads)
        if not urls and errors:
            raise RuntimeError("; ".join(errors))
        return urls

    def _resolve_metadata_urls(self, paper: Any) -> List[str]:
        urls: List[str] = []
        pdf_url = str(getattr(paper, "pdf_url", "") or "").strip()
        if pdf_url:
            urls.append(pdf_url)
        landing_url = str(getattr(paper, "url", "") or "").strip()
        if self._looks_like_pdf_url(landing_url):
            urls.append(landing_url)

        raw = getattr(paper, "raw", {}) or {}

        def collect(value: Any, key: str = "") -> None:
            if isinstance(value, str):
                candidate = value.strip()
                if candidate and (
                    key.lower() in _METADATA_PDF_KEYS
                    or self._looks_like_pdf_url(candidate)
                ):
                    urls.append(candidate)
            elif isinstance(value, list):
                for item in value:
                    collect(item, key)
            elif isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    collect(nested_value, str(nested_key))

        collect(raw)
        return urls

    def _resolve_web_search(self, paper: Any) -> List[str]:
        """Find a final PDF lead on the open web after academic OA sources fail."""
        title = str(getattr(paper, "title", "") or "").strip()
        if not title:
            return []
        if self.web_search_client is None:
            self.web_search_client = WebSearchClient(
                timeout_seconds=self.timeout_seconds
            )
        original_timeout = getattr(self.web_search_client, "timeout_seconds", None)
        if original_timeout is not None and hasattr(
            self.web_search_client, "available_engines"
        ):
            remaining = self._network_timeout()
            engines = self.web_search_client.available_engines()
            request_budget = len(engines)
            if "tavily" in engines:
                request_budget += max(
                    0,
                    len(getattr(self.web_search_client, "tavily_api_keys", [])) - 1,
                )
            self.web_search_client.timeout_seconds = max(
                0.05,
                min(float(original_timeout), remaining / max(1, request_budget)),
            )
        try:
            results = self.web_search_client.search(
                f'"{title[:180]}" filetype:pdf',
                max_results=5,
            )
        finally:
            if original_timeout is not None:
                self.web_search_client.timeout_seconds = original_timeout
        urls: List[str] = []
        for result in results:
            candidate = str(result.url or "").strip()
            if self._looks_like_pdf_url(candidate):
                urls.append(candidate)
                continue
            arxiv_match = _ARXIV_ID_RE.search(candidate)
            if arxiv_match:
                arxiv_id = arxiv_match.group(1).removesuffix(".pdf")
                encoded = urllib.parse.quote(arxiv_id, safe="/")
                urls.append(f"https://arxiv.org/pdf/{encoded}.pdf")
        if not urls and self.web_search_client.last_errors:
            raise RuntimeError("; ".join(self.web_search_client.last_errors[:5]))
        return urls

    def _download_candidate(
        self,
        provider: str,
        url: str,
        target: Path,
    ) -> PdfDownloadAttempt:
        temp_path: Optional[Path] = None
        try:
            self._validate_remote_url(url)
            request = urllib.request.Request(
                url,
                headers={"User-Agent": self.user_agent, "Accept": "application/pdf"},
            )
            with self._opener(request, timeout=self._network_timeout()) as response:
                final_url = str(
                    response.geturl() if hasattr(response, "geturl") else url
                )
                self._url_validator(final_url or url)
                content_type = self._response_header(response, "Content-Type")
                media_type = content_type.split(";", 1)[0].strip().lower()
                if media_type not in _PDF_CONTENT_TYPES:
                    raise ValueError(
                        f"unexpected Content-Type {content_type or '<missing>'}"
                    )
                content_length = self._response_header(response, "Content-Length")
                if content_length:
                    try:
                        declared_size = int(content_length)
                    except ValueError as exc:
                        raise ValueError("invalid Content-Length") from exc
                    if declared_size > self.max_pdf_bytes:
                        raise ValueError(
                            f"PDF exceeds {self.max_pdf_bytes} byte limit"
                        )

                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=f".{target.stem}.",
                    suffix=".part",
                    dir=target.parent,
                    delete=False,
                ) as handle:
                    temp_path = Path(handle.name)
                    prefix = b""
                    total = 0
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > self.max_pdf_bytes:
                            raise ValueError(
                                f"PDF exceeds {self.max_pdf_bytes} byte limit"
                            )
                        if len(prefix) < 1024:
                            prefix += chunk[: 1024 - len(prefix)]
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())

                if b"%PDF-" not in prefix:
                    raise ValueError("payload is missing the PDF magic header")
                os.replace(temp_path, target)
                temp_path = None
                return PdfDownloadAttempt(
                    provider=provider,
                    status="downloaded",
                    url=url,
                    bytes_written=total,
                )
        except Exception as exc:
            return PdfDownloadAttempt(
                provider=provider,
                status="download_failed",
                url=url,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _get_json(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        resolved_headers = {"User-Agent": self.user_agent}
        resolved_headers.update(headers or {})
        request = urllib.request.Request(url, headers=resolved_headers)
        with self._opener(request, timeout=self._network_timeout()) as response:
            payload = response.read(4 * 1024 * 1024 + 1)
        if len(payload) > 4 * 1024 * 1024:
            raise ValueError("metadata response exceeds 4 MiB")
        parsed = json.loads(payload.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("metadata response is not a JSON object")
        return parsed

    def _s2_identifier(self, paper: Any) -> str:
        if getattr(paper, "source", "") == "semantic_scholar" and getattr(
            paper, "source_id", ""
        ):
            return str(paper.source_id)
        doi = self._doi(paper)
        if doi:
            return f"DOI:{doi}"
        arxiv_id = self._arxiv_id(paper)
        if arxiv_id:
            normalized = re.sub(r"v\d+$", "", arxiv_id, flags=re.IGNORECASE)
            return f"ARXIV:{normalized}"
        return ""

    def _arxiv_id(self, paper: Any) -> str:
        candidates = [
            str(getattr(paper, "arxiv_id", "") or ""),
            str(getattr(paper, "source_id", "") or ""),
            str(getattr(paper, "url", "") or ""),
            str(getattr(paper, "pdf_url", "") or ""),
        ]
        raw = getattr(paper, "raw", {}) or {}
        external_ids = raw.get("external_ids") if isinstance(raw, dict) else None
        if isinstance(external_ids, dict):
            candidates.extend(
                [
                    str(external_ids.get("ArXiv") or ""),
                    str(external_ids.get("ARXIV") or ""),
                ]
            )
        doi = self._doi(paper)
        if doi.lower().startswith("10.48550/arxiv."):
            candidates.append(doi.split("arxiv.", 1)[-1])
        for candidate in candidates:
            match = _ARXIV_ID_RE.search(candidate)
            if match:
                return match.group(1)
        return ""

    @staticmethod
    def _doi(paper: Any) -> str:
        doi = str(getattr(paper, "doi", "") or "").strip()
        doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
        return doi.rstrip(" .")

    @staticmethod
    def _urls_from_core_payloads(payloads: Iterable[Dict[str, Any]]) -> List[str]:
        urls: List[str] = []
        for payload in payloads:
            rows: List[Any] = [payload]
            results = payload.get("results")
            if isinstance(results, list):
                rows.extend(results)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                for key in ["downloadUrl", "fullTextLink"]:
                    value = str(row.get(key) or "").strip()
                    if value:
                        urls.append(value)
                for value in row.get("sourceFulltextUrls") or []:
                    candidate = str(value or "").strip()
                    if candidate:
                        urls.append(candidate)
        return urls

    @staticmethod
    def _response_header(response: Any, name: str) -> str:
        headers = getattr(response, "headers", {})
        value = headers.get(name, "") if hasattr(headers, "get") else ""
        return str(value or "")

    @staticmethod
    def _looks_like_pdf_url(url: str) -> bool:
        if not url:
            return False
        parsed = urllib.parse.urlparse(url)
        return parsed.path.lower().endswith(".pdf") or "pdf" in urllib.parse.parse_qs(
            parsed.query
        )

    def _validate_remote_url(self, url: str) -> None:
        self._url_validator(url)

    @staticmethod
    def _unique_urls(urls: Iterable[str]) -> List[str]:
        unique: List[str] = []
        seen = set()
        for raw in urls:
            url = str(raw or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            unique.append(url)
        return unique

    @staticmethod
    def _reserve_unique_path(path: Path) -> Path:
        for index in range(1, 1000):
            candidate = (
                path
                if index == 1
                else path.with_name(f"{path.stem}_{index}{path.suffix}")
            )
            try:
                descriptor = os.open(
                    candidate,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o644,
                )
            except FileExistsError:
                continue
            os.close(descriptor)
            return candidate
        raise RuntimeError(f"could not allocate unique output path for {path}")

    @staticmethod
    def _reset_reservation(path: Path) -> None:
        with path.open("wb") as handle:
            handle.flush()
            os.fsync(handle.fileno())

    def _paper_filename(self, paper: Any, title: str) -> str:
        identity = (
            self._doi(paper)
            or self._arxiv_id(paper)
            or str(getattr(paper, "source_id", "") or "").strip()
            or str(getattr(paper, "url", "") or "").strip()
        )
        suffix = ""
        if identity:
            suffix = "_" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:10]
        return f"{self._slugify(title or 'paper', max_length=80)}{suffix}.pdf"

    @staticmethod
    def _slugify(text: str, max_length: int = 96) -> str:
        cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", text.strip(), flags=re.UNICODE)
        cleaned = re.sub(r"_+", "_", cleaned).strip("._")
        return (cleaned or "paper")[:max_length]
