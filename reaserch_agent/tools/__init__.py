"""Tooling exports for the research agent."""

from .knowledge_query import KnowledgeQuery
from .memory_query import MemoryQuery
from .device_context import ensure_device_context, load_device_context
from .ingestion import (
    ExternalKnowledgeClient,
    ExternalPaper,
    KnowledgeIngestion,
    classify_reference,
)
from .literature_acquisition import LiteratureAcquisition
from .paper_download import (
    OpenAccessPdfDownloader,
    PdfDownloadAttempt,
    PdfDownloadResult,
)
from .paper_registry import PaperRegistry
from .web_search import WebSearchAttempt, WebSearchClient, WebSearchResult
from .web_tool import WebToolExecutor

__all__ = [
    "ExternalKnowledgeClient",
    "ExternalPaper",
    "KnowledgeIngestion",
    "KnowledgeQuery",
    "LiteratureAcquisition",
    "MemoryQuery",
    "OpenAccessPdfDownloader",
    "PaperRegistry",
    "PdfDownloadAttempt",
    "PdfDownloadResult",
    "WebSearchAttempt",
    "WebSearchClient",
    "WebSearchResult",
    "WebToolExecutor",
    "classify_reference",
    "ensure_device_context",
    "load_device_context",
]
