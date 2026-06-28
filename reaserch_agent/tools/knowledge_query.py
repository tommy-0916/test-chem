"""Knowledge query tool backed by literature memory and local corpus."""

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


class KnowledgeQuery:
    """Search the literature memory layer plus the local chem_kb corpus."""

    def __init__(self, corpus_dir: str | Path | None = None, top_k: int = 5) -> None:
        self._memory = LayeredChemMemory(root_dir=self._resolve_memory_root())
        self._corpus = LocalExperimentCorpus(corpus_dir=corpus_dir)
        self._top_k = top_k

    def search(self, queries: Sequence[str], top_k: int | None = None) -> List[SearchHit]:
        resolved_top_k = top_k or self._top_k
        query_text = "\n".join(query.strip() for query in queries if query and query.strip())
        memory_hits: List[SearchHit] = []
        if query_text:
            memory_results = self._memory.search(
                query_text,
                layers=[ChemMemoryLayer.LITERATURE],
                top_k=resolved_top_k,
            ).get("results", [])
            memory_hits = [
                memory_result_to_search_hit(result)
                for result in memory_results
            ]

        corpus_hits = self._corpus.search(queries, top_k=resolved_top_k)
        return self._merge_hits(memory_hits, corpus_hits, top_k=resolved_top_k)

    def format_context(self, hits: Iterable[SearchHit]) -> str:
        materialized_hits = list(hits)
        memory_only = [
            hit for hit in materialized_hits if hit.file_path.startswith("chem-memory://")
        ]
        if len(memory_only) == len(materialized_hits):
            return format_memory_results_for_prompt(materialized_hits)
        return self._corpus.format_hits_for_prompt(materialized_hits)

    def _resolve_memory_root(self) -> Path:
        explicit_store_dir = os.getenv("RESEARCH_MEMORY_STORE_DIR")
        if explicit_store_dir:
            return Path(explicit_store_dir).expanduser()
        return LayeredChemMemory.default_root()

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
