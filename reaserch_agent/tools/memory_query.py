"""Memory query tool for retrieving similar historical experiments."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List, Sequence

from ..memory import ChemMemoryLayer, LayeredChemMemory
from ..memory.adapters import (
    format_memory_results_for_prompt,
    memory_result_to_search_hit,
)
from ..state import SearchHit
from .corpus_search import LocalExperimentCorpus


class MemoryQuery:
    """Search chem-agent's experiment memory with corpus fallback."""

    def __init__(self, corpus_dir: str | Path | None = None, top_k: int = 3) -> None:
        self._memory = LayeredChemMemory(root_dir=self._resolve_memory_root(corpus_dir))
        self._corpus = LocalExperimentCorpus(corpus_dir=corpus_dir)
        self._top_k = top_k

    def refresh(self) -> None:
        """Pick up corpus files ingested after construction."""
        self._corpus.refresh()

    @property
    def memory(self) -> LayeredChemMemory:
        """Underlying layered store (campaign trajectory writes/recall)."""
        return self._memory

    def search(self, queries: Sequence[str], top_k: int | None = None) -> List[SearchHit]:
        resolved_top_k = top_k or self._top_k
        query_text = "\n".join(query.strip() for query in queries if query and query.strip())
        memory_hits: List[SearchHit] = []
        if query_text:
            memory_results = self._memory.search(
                query_text,
                layers=[ChemMemoryLayer.EXPERIMENT],
                top_k=resolved_top_k,
            ).get("results", [])
            memory_hits = [
                memory_result_to_search_hit(result)
                for result in memory_results
            ]

        corpus_hits = self._corpus.search(queries, top_k=resolved_top_k)
        return self._merge_hits(memory_hits, corpus_hits, top_k=resolved_top_k)

    def format_context(self, hits: Iterable[SearchHit]) -> str:
        return format_memory_results_for_prompt(hits)

    def _resolve_memory_root(self, corpus_dir: str | Path | None) -> Path:
        explicit_store_dir = os.getenv("RESEARCH_MEMORY_STORE_DIR")
        if explicit_store_dir:
            return Path(explicit_store_dir).expanduser()

        if corpus_dir is None:
            return LayeredChemMemory.default_root()

        path = Path(corpus_dir).expanduser()
        if path.is_dir() and path.name == "chem_memory":
            return path
        if path.is_dir() and (path / "chem_memory.sqlite").exists():
            return path
        if path.is_dir() and not self._looks_like_corpus_dir(path):
            return path
        return LayeredChemMemory.default_root()

    def _looks_like_corpus_dir(self, path: Path) -> bool:
        if not path.is_dir():
            return False
        return any(
            child.is_file() and child.suffix.lower() in {".json", ".pdf"}
            for child in path.iterdir()
        )

    def _merge_hits(
        self,
        primary_hits: Sequence[SearchHit],
        fallback_hits: Sequence[SearchHit],
        *,
        top_k: int,
    ) -> List[SearchHit]:
        merged: List[SearchHit] = []
        seen = set()
        for hit in list(primary_hits) + list(fallback_hits):
            key = (hit.title, hit.file_path)
            if key in seen:
                continue
            seen.add(key)
            merged.append(hit)
            if len(merged) >= top_k:
                break
        return merged
