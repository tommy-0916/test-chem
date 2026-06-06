"""Knowledge query tool backed by the local benchmark corpus."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Sequence

from ..state import SearchHit
from .corpus_search import LocalExperimentCorpus


class KnowledgeQuery:
    """Search the local chem_kb corpus as a bootstrap knowledge base."""

    def __init__(self, corpus_dir: str | Path | None = None, top_k: int = 5) -> None:
        self._corpus = LocalExperimentCorpus(corpus_dir=corpus_dir)
        self._top_k = top_k

    def search(self, queries: Sequence[str], top_k: int | None = None) -> List[SearchHit]:
        return self._corpus.search(queries, top_k=top_k or self._top_k)

    def format_context(self, hits: Iterable[SearchHit]) -> str:
        return self._corpus.format_hits_for_prompt(hits)
