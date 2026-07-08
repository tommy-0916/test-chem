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
from .paper_registry import PaperRegistry
from .web_search import WebSearchClient, WebSearchResult
from .web_tool import WebToolExecutor

__all__ = [
    "ExternalKnowledgeClient",
    "ExternalPaper",
    "KnowledgeIngestion",
    "KnowledgeQuery",
    "LiteratureAcquisition",
    "MemoryQuery",
    "PaperRegistry",
    "WebSearchClient",
    "WebSearchResult",
    "WebToolExecutor",
    "classify_reference",
    "ensure_device_context",
    "load_device_context",
]
