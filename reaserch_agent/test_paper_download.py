"""Zero-network tests for redundant paper PDF acquisition."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from reaserch_agent.tools.ingestion import ExternalPaper, KnowledgeIngestion
from reaserch_agent.tools.literature_acquisition import LiteratureAcquisition
from reaserch_agent.tools.paper_download import (
    OpenAccessPdfDownloader,
    PdfDownloadAttempt,
    PdfDownloadResult,
)
from reaserch_agent.tools.paper_registry import PaperRegistry
from reaserch_agent.tools.web_search import WebSearchResult


class FakeResponse:
    def __init__(self, data: bytes, content_type: str) -> None:
        self.data = data
        self.offset = 0
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(data)),
        }

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.data) - self.offset
        chunk = self.data[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class RedirectingResponse(FakeResponse):
    def __init__(self, data: bytes, content_type: str, final_url: str) -> None:
        super().__init__(data, content_type)
        self.final_url = final_url

    def geturl(self) -> str:
        return self.final_url


class RoutingOpener:
    def __init__(self) -> None:
        self.urls: List[str] = []

    def __call__(self, request, *, timeout: int):
        url = request.full_url
        self.urls.append(url)
        if url == "https://arxiv.org/pdf/2403.02310.pdf":
            return FakeResponse(b"<html>blocked</html>", "text/html")
        if "api.semanticscholar.org/graph/v1/paper/DOI:" in url:
            payload = {"openAccessPdf": {"url": "https://oa.example/paper.pdf"}}
            return FakeResponse(json.dumps(payload).encode(), "application/json")
        if url == "https://oa.example/paper.pdf":
            return FakeResponse(b"%PDF-1.4\nbody\n%%EOF", "application/pdf")
        raise ConnectionError(f"unavailable: {url}")


class ParseFallbackOpener:
    def __init__(self) -> None:
        self.urls: List[str] = []

    def __call__(self, request, *, timeout: int):
        url = request.full_url
        self.urls.append(url)
        if url == "https://arxiv.org/pdf/2403.02310.pdf":
            return FakeResponse(
                b"%PDF-1.4\nnot-a-real-document",
                "application/pdf",
            )
        if "api.semanticscholar.org/graph/v1/paper/DOI:" in url:
            payload = {"openAccessPdf": {"url": "https://oa.example/good.pdf"}}
            return FakeResponse(json.dumps(payload).encode(), "application/json")
        if url == "https://oa.example/good.pdf":
            return FakeResponse(
                b"%PDF-1.7\nsecond-provider-candidate\n%%EOF",
                "application/pdf",
            )
        raise ConnectionError(f"unavailable: {url}")


class EmptyWebClient:
    last_errors: List[str] = []

    def search(self, query: str, *, max_results: int = 5):
        return []


class PdfWebClient(EmptyWebClient):
    def search(self, query: str, *, max_results: int = 5):
        return [
            WebSearchResult(
                title="Web PDF",
                url="https://papers.example/discovered.pdf",
                engine="duckduckgo",
            )
        ]


class PaperDownloadTest(unittest.TestCase):
    def test_arxiv_failure_falls_back_to_semantic_scholar_oa(self) -> None:
        paper = ExternalPaper(
            title="Fallback paper",
            doi="10.1000/paper",
            arxiv_id="2403.02310",
        )
        opener = RoutingOpener()
        downloader = OpenAccessPdfDownloader(
            opener=opener,
            web_search_client=EmptyWebClient(),
            url_validator=lambda url: None,
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = downloader.download(paper, tmp)

            self.assertTrue(result.downloaded)
            self.assertEqual(result.successful_attempt.provider, "semantic_scholar")
            self.assertEqual(
                [(attempt.provider, attempt.status) for attempt in result.attempts[:2]],
                [
                    ("arxiv_direct", "download_failed"),
                    ("semantic_scholar", "downloaded"),
                ],
            )
            self.assertTrue(result.path.read_bytes().startswith(b"%PDF-"))
            self.assertFalse(list(Path(tmp).glob("*.part")))

    def test_private_metadata_url_is_rejected_without_network_access(self) -> None:
        paper = ExternalPaper(title="", pdf_url="http://127.0.0.1/private.pdf")
        opener = mock.Mock(side_effect=AssertionError("network must not be used"))
        downloader = OpenAccessPdfDownloader(
            opener=opener,
            web_search_client=EmptyWebClient(),
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = downloader.download(paper, tmp)

        self.assertFalse(result.downloaded)
        self.assertFalse(opener.called)
        metadata_attempt = next(
            attempt for attempt in result.attempts if attempt.provider == "metadata"
        )
        self.assertIn("private or local", metadata_attempt.error)

    def test_web_search_is_the_last_pdf_discovery_fallback(self) -> None:
        paper = ExternalPaper(title="Hard to find paper")

        def opener(request, *, timeout: int):
            if request.full_url == "https://papers.example/discovered.pdf":
                return FakeResponse(b"%PDF-1.7\nweb\n%%EOF", "application/pdf")
            raise ConnectionError("academic resolver unavailable")

        downloader = OpenAccessPdfDownloader(
            opener=opener,
            web_search_client=PdfWebClient(),
            url_validator=lambda url: None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = downloader.download(paper, tmp)

        self.assertTrue(result.downloaded)
        self.assertEqual(result.successful_attempt.provider, "web_search")
        self.assertTrue(
            any(attempt.provider == "core" for attempt in result.attempts)
        )

    def test_semantic_scholar_retries_anonymously_when_key_is_rejected(self) -> None:
        paper = ExternalPaper(title="S2 retry", doi="10.1000/retry")
        downloader = OpenAccessPdfDownloader(semantic_scholar_api_key="rejected")
        forbidden = urllib.error.HTTPError(
            "https://api.semanticscholar.org",
            403,
            "Forbidden",
            None,
            None,
        )
        with mock.patch.object(
            downloader,
            "_get_json",
            side_effect=[
                forbidden,
                {"openAccessPdf": {"url": "https://oa.example/retry.pdf"}},
            ],
        ) as get_json:
            urls = downloader._resolve_semantic_scholar(paper)

        self.assertEqual(urls, ["https://oa.example/retry.pdf"])
        self.assertIn("x-api-key", get_json.call_args_list[0].kwargs["headers"])
        self.assertNotIn("x-api-key", get_json.call_args_list[1].kwargs["headers"])

    def test_pdf_redirect_to_private_address_is_rejected(self) -> None:
        paper = ExternalPaper(
            title="",
            pdf_url="https://93.184.216.34/paper.pdf",
        )

        def opener(request, *, timeout: int):
            return RedirectingResponse(
                b"%PDF-1.7\nbody\n%%EOF",
                "application/pdf",
                "http://127.0.0.1/private.pdf",
            )

        downloader = OpenAccessPdfDownloader(
            opener=opener,
            web_search_client=EmptyWebClient(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = downloader.download(paper, tmp)

        self.assertFalse(result.downloaded)
        metadata_attempt = next(
            attempt for attempt in result.attempts if attempt.provider == "metadata"
        )
        self.assertIn("private or local", metadata_attempt.error)

    def test_metadata_candidate_list_is_bounded_per_provider(self) -> None:
        paper = ExternalPaper(
            title="",
            raw={
                "source_fulltext_urls": [
                    f"https://papers.example/{index}.pdf" for index in range(1000)
                ]
            },
        )
        opener = mock.Mock(
            side_effect=lambda request, timeout: FakeResponse(
                b"not a pdf",
                "text/html",
            )
        )
        downloader = OpenAccessPdfDownloader(
            opener=opener,
            web_search_client=EmptyWebClient(),
            url_validator=lambda url: None,
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = downloader.download(paper, tmp)

        self.assertEqual(opener.call_count, 5)
        cap = next(
            attempt
            for attempt in result.attempts
            if attempt.status == "candidate_cap_reached"
        )
        self.assertEqual(cap.provider, "metadata")
        self.assertIn("first 5 of 1000", cap.error)

    def test_global_download_attempt_cap_stops_remaining_candidates(self) -> None:
        paper = ExternalPaper(
            title="",
            raw={
                "source_fulltext_urls": [
                    f"https://papers.example/{index}.pdf" for index in range(10)
                ]
            },
        )
        opener = mock.Mock(
            side_effect=lambda request, timeout: FakeResponse(
                b"not a pdf",
                "text/html",
            )
        )
        downloader = OpenAccessPdfDownloader(
            opener=opener,
            web_search_client=EmptyWebClient(),
            url_validator=lambda url: None,
            max_candidates_per_provider=10,
            max_total_download_attempts=3,
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = downloader.download(paper, tmp)

        self.assertEqual(opener.call_count, 3)
        self.assertTrue(
            any(
                attempt.provider == "downloader"
                and attempt.status == "candidate_cap_reached"
                for attempt in result.attempts
            )
        )

    def test_concurrent_same_title_downloads_reserve_distinct_paths(self) -> None:
        barrier = threading.Barrier(2)

        def run(marker: str, output_dir: str):
            def opener(request, *, timeout: float):
                barrier.wait(timeout=2)
                return FakeResponse(
                    f"%PDF-1.7\n{marker}\n%%EOF".encode(),
                    "application/pdf",
                )

            downloader = OpenAccessPdfDownloader(
                opener=opener,
                web_search_client=EmptyWebClient(),
                url_validator=lambda url: None,
            )
            return downloader.download(
                ExternalPaper(
                    title="",
                    pdf_url="https://papers.example/shared.pdf",
                ),
                output_dir,
            )

        with tempfile.TemporaryDirectory() as tmp:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda marker: run(marker, tmp), ["A", "B"]))

            paths = [result.path for result in results]
            self.assertTrue(all(path is not None for path in paths))
            self.assertEqual(len(set(paths)), 2)
            self.assertEqual(
                {path.read_bytes().splitlines()[1].decode() for path in paths},
                {"A", "B"},
            )


class FailedDownloader:
    def download(
        self,
        paper: ExternalPaper,
        output_dir: str | Path,
        *,
        candidate_validator=None,
    ) -> PdfDownloadResult:
        return PdfDownloadResult(
            paper_title=paper.title,
            attempts=[
                PdfDownloadAttempt(
                    provider="arxiv_direct",
                    status="download_failed",
                    error="HTTPError: unavailable",
                )
            ],
        )


class InvalidPdfDownloader:
    def download(
        self,
        paper: ExternalPaper,
        output_dir: str | Path,
        *,
        candidate_validator=None,
    ) -> PdfDownloadResult:
        path = Path(output_dir) / "invalid.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4\nnot-a-real-pdf")
        return PdfDownloadResult(
            paper_title=paper.title,
            path=path,
            attempts=[
                PdfDownloadAttempt(
                    provider="metadata",
                    status="downloaded",
                    url="https://example.org/invalid.pdf",
                    bytes_written=path.stat().st_size,
                )
            ],
        )


class DownloadStatusIntegrationTest(unittest.TestCase):
    def test_failed_download_keeps_metadata_and_is_not_marked_parsed(self) -> None:
        paper = ExternalPaper(
            title="Metadata survives download failure",
            source="arxiv",
            source_id="2403.02310",
            arxiv_id="2403.02310",
        )
        with tempfile.TemporaryDirectory() as tmp:
            ingestion = KnowledgeIngestion(
                tmp,
                pdf_downloader=FailedDownloader(),  # type: ignore[arg-type]
            )
            written = ingestion.ingest_external_papers(
                [paper],
                download_pdfs=True,
                pdf_dir=Path(tmp) / "pdfs",
            )

            payload = json.loads(written[0].read_text(encoding="utf-8"))
            metadata = payload["_ingestion_metadata"]
            self.assertEqual(metadata["full_text_status"], "download_failed")
            self.assertFalse(metadata["pdf_download"]["downloaded"])

            acquisition = LiteratureAcquisition(
                kb_dir=tmp,
                campaign_id="cmp_download",
                ingestion=ingestion,
                download_pdfs=True,
                client=mock.MagicMock(last_errors=[]),
            )
            acquisition._archive_paper(paper, role="seed")
            record = PaperRegistry(tmp).find(arxiv_id="2403.02310")
            self.assertIsNotNone(record)
            self.assertEqual(record["full_text_status"], "download_failed")
            self.assertTrue(record["download_attempts"])

    def test_downloaded_pdf_without_extractable_text_is_parse_failed(self) -> None:
        paper = ExternalPaper(title="Invalid PDF metadata", doi="10.1000/invalid")
        with tempfile.TemporaryDirectory() as tmp:
            ingestion = KnowledgeIngestion(
                tmp,
                pdf_downloader=InvalidPdfDownloader(),  # type: ignore[arg-type]
            )
            written = ingestion.ingest_external_papers(
                [paper],
                download_pdfs=True,
                pdf_dir=Path(tmp) / "pdfs",
            )

            payload = json.loads(written[0].read_text(encoding="utf-8"))
            metadata = payload["_ingestion_metadata"]
            self.assertEqual(metadata["full_text_status"], "parse_failed")
            attempts = metadata["pdf_download"]["attempts"]
            self.assertEqual(attempts[-1]["provider"], "pdf_parser")
            self.assertEqual(attempts[-1]["status"], "parse_failed")

    def test_unparseable_pdf_falls_through_to_next_provider(self) -> None:
        paper = ExternalPaper(
            title="Parse-aware fallback",
            doi="10.1000/parse-fallback",
            arxiv_id="2403.02310",
        )
        opener = ParseFallbackOpener()
        downloader = OpenAccessPdfDownloader(
            opener=opener,
            web_search_client=EmptyWebClient(),
            url_validator=lambda url: None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            ingestion = KnowledgeIngestion(tmp, pdf_downloader=downloader)

            def parse_candidate(path: str | Path):
                payload = Path(path).read_bytes()
                if b"not-a-real-document" in payload:
                    return None
                return ingestion.record_from_text(
                    title=paper.title,
                    text=(
                        "A valid chemistry paper describes synthesis, washing, "
                        "drying, characterization, and electrochemical testing. "
                    )
                    * 5,
                    source_path=str(path),
                    source_type="pdf",
                )

            with mock.patch.object(
                ingestion,
                "record_from_file",
                side_effect=parse_candidate,
            ):
                written = ingestion.ingest_external_papers(
                    [paper],
                    download_pdfs=True,
                    pdf_dir=Path(tmp) / "pdfs",
                )

            metadata = json.loads(written[0].read_text(encoding="utf-8"))[
                "_ingestion_metadata"
            ]
            attempts = metadata["pdf_download"]["attempts"]
            self.assertEqual(metadata["full_text_status"], "parsed")
            self.assertEqual(attempts[0]["provider"], "arxiv_direct")
            self.assertEqual(attempts[0]["status"], "parse_failed")
            self.assertEqual(attempts[1]["provider"], "semantic_scholar")
            self.assertEqual(attempts[1]["status"], "downloaded")
            self.assertTrue(
                any("api.semanticscholar.org" in url for url in opener.urls)
            )


if __name__ == "__main__":
    unittest.main()
