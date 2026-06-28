"""Adapters between chem memory records and existing research-agent hits."""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List

from ..state import SearchHit


def memory_result_to_search_hit(result: Dict[str, Any]) -> SearchHit:
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    layer = str(result.get("layer") or metadata.get("layer") or "memory")
    title = (
        metadata.get("title")
        or metadata.get("source_title")
        or metadata.get("paper_title")
        or metadata.get("query")
        or _short_title(result.get("memory", ""))
    )
    source_path = (
        metadata.get("source_path")
        or metadata.get("file_path")
        or f"chem-memory://{layer}/{result.get('id', '')}"
    )
    problem = (
        metadata.get("problem")
        or metadata.get("objective")
        or metadata.get("hypothesis")
        or metadata.get("query")
        or ""
    )
    memory_text = str(result.get("memory", ""))
    details = metadata.get("experiment_details") or metadata.get("protocol_summary") or memory_text
    steps = metadata.get("steps") if isinstance(metadata.get("steps"), list) else []
    performance = (
        metadata.get("performance")
        if isinstance(metadata.get("performance"), list)
        else []
    )
    matched_terms = (
        result.get("matched_terms")
        if isinstance(result.get("matched_terms"), list)
        else []
    )

    return SearchHit(
        title=str(title),
        file_path=str(source_path),
        score=float(result.get("score") or 0.0),
        problem=str(problem),
        synthesis_summary=memory_text,
        experiment_details=str(details),
        steps=steps,
        performance=performance,
        matched_terms=[str(term) for term in matched_terms],
    )


def format_memory_results_for_prompt(hits: Iterable[SearchHit]) -> str:
    blocks: List[str] = []
    for index, hit in enumerate(hits, start=1):
        block = [
            f"[{index}] {hit.title}",
            f"score: {hit.score:.3f}",
            f"source: {hit.file_path}",
        ]
        if hit.problem:
            block.append(f"problem/objective: {hit.problem}")
        if hit.synthesis_summary:
            block.append(f"memory: {_truncate(hit.synthesis_summary, 1600)}")
        if hit.experiment_details and hit.experiment_details != hit.synthesis_summary:
            block.append(f"details: {_truncate(hit.experiment_details, 1600)}")
        if hit.steps:
            block.append("steps: " + _truncate(json.dumps(hit.steps[:6], ensure_ascii=False), 1200))
        if hit.performance:
            block.append(
                "performance: "
                + _truncate(json.dumps(hit.performance[:5], ensure_ascii=False), 1000)
            )
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


def _short_title(text: Any, max_len: int = 80) -> str:
    normalized = " ".join(str(text or "").split())
    return normalized[:max_len] or "chem memory"


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."
