"""Three-layer campaign recall aligned with planning granularity.

Layer A (always): campaign path summary — stage route + completed-stage
rollups + turn count. Layer B (rule): last K trajectory nodes of the
current stage in full detail. Layer C (signal): cross-campaign similar
cases, retrieved only for abnormal / feasibility / safety signals, with
freshness labels. Each layer has a hard character budget so the prompt
context stays bounded no matter how long the campaign runs.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from .base import ChemMemoryLayer
from .layered import LayeredChemMemory

PATH_SUMMARY_BUDGET = 2000
RECENT_TURNS_BUDGET = 3000
CROSS_CAMPAIGN_BUDGET = 1500

TURN_NODE_TYPES = ["bootstrap", "observation_turn", "manual_handoff"]


def build_campaign_memory_context(
    memory: LayeredChemMemory,
    *,
    campaign_id: str,
    current_stage: str,
    stage_route: Sequence[str],
    signal_text: str = "",
    include_cross_campaign: bool = False,
    recent_k: int = 3,
) -> Dict[str, Any]:
    if not campaign_id:
        return {}

    turns = _experiment_records(
        memory,
        filters={
            "campaign_id": campaign_id,
            "node_type": {"in": TURN_NODE_TYPES},
        },
        top_k=100,
    )
    stage_summaries = _experiment_records(
        memory,
        filters={"campaign_id": campaign_id, "node_type": "stage_summary"},
        top_k=20,
    )

    context: Dict[str, Any] = {
        "path_summary": _truncate_text(
            json.dumps(
                {
                    "stage_route": list(stage_route),
                    "current_stage": current_stage,
                    "total_turns": len(turns),
                    "completed_stage_summaries": [
                        {
                            "stage": _metadata(record).get("stage", ""),
                            "summary": str(record.get("memory", ""))[:300],
                        }
                        for record in stage_summaries
                    ],
                },
                ensure_ascii=False,
            ),
            PATH_SUMMARY_BUDGET,
        )
    }

    stage_turns = [
        record
        for record in turns
        if _metadata(record).get("stage", "") == current_stage
    ]
    recent = stage_turns[-recent_k:] if stage_turns else turns[-recent_k:]
    context["recent_stage_turns"] = _truncate_text(
        json.dumps(
            [
                {
                    "turn": _metadata(record).get("turn_index"),
                    "node_type": _metadata(record).get("node_type", ""),
                    "stage": _metadata(record).get("stage", ""),
                    "recorded_at": record.get("created_at", ""),
                    "detail": str(record.get("memory", ""))[:800],
                }
                for record in recent
            ],
            ensure_ascii=False,
        ),
        RECENT_TURNS_BUDGET,
    )

    if include_cross_campaign and signal_text.strip():
        context["cross_campaign_cases"] = _truncate_text(
            json.dumps(
                _cross_campaign_cases(memory, campaign_id, signal_text),
                ensure_ascii=False,
            ),
            CROSS_CAMPAIGN_BUDGET,
        )

    return context


def _cross_campaign_cases(
    memory: LayeredChemMemory,
    campaign_id: str,
    signal_text: str,
    *,
    top_k: int = 3,
) -> List[Dict[str, Any]]:
    try:
        results = memory.search_experiments(signal_text, top_k=top_k * 3).get(
            "results", []
        )
    except Exception:
        return []
    cases: List[Dict[str, Any]] = []
    for result in results:
        metadata = _metadata(result)
        if metadata.get("campaign_id", "") == campaign_id:
            continue
        cases.append(
            {
                "campaign_id": metadata.get("campaign_id", ""),
                "stage": metadata.get("stage", ""),
                "node_type": metadata.get("node_type", ""),
                "freshness": result.get("created_at", ""),
                "score": result.get("score", 0.0),
                "detail": str(result.get("memory", ""))[:500],
            }
        )
        if len(cases) >= top_k:
            break
    return cases


def _experiment_records(
    memory: LayeredChemMemory,
    *,
    filters: Dict[str, Any],
    top_k: int,
) -> List[Dict[str, Any]]:
    try:
        payload = memory.get_all(
            layers=[ChemMemoryLayer.EXPERIMENT],
            filters=filters,
            top_k=top_k,
        )
    except Exception:
        return []
    results = payload.get("results", []) if isinstance(payload, dict) else []
    records = [result for result in results if isinstance(result, dict)]
    records.sort(key=lambda record: str(record.get("created_at", "")))
    return records


def _metadata(record: Dict[str, Any]) -> Dict[str, Any]:
    metadata = record.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _truncate_text(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    return text[: budget - 3] + "..."
