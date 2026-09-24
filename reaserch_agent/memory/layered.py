"""Chem-agent two-layer memory facade."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .base import ChemMemoryLayer, ChemMemoryStore


class LayeredChemMemory:
    """Two-layer memory: experimental history and literature knowledge."""

    def __init__(self, root_dir: str | Path | None = None) -> None:
        self.store = ChemMemoryStore(root_dir=root_dir)

    @staticmethod
    def default_root() -> Path:
        return ChemMemoryStore.default_root()

    def add_experiment(
        self,
        content: Any,
        *,
        metadata: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = "chem-agent",
        run_id: Optional[str] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        merged_metadata = dict(metadata or {})
        merged_metadata.setdefault("memory_kind", "experiment")
        return self.store.add(
            content,
            layer=ChemMemoryLayer.EXPERIMENT,
            metadata=merged_metadata,
            user_id=user_id,
            agent_id=agent_id,
            run_id=run_id,
            infer=False,
        )

    def add_literature(
        self,
        content: Any,
        *,
        metadata: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = "chem-agent",
        run_id: Optional[str] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        merged_metadata = dict(metadata or {})
        merged_metadata.setdefault("memory_kind", "literature")
        return self.store.add(
            content,
            layer=ChemMemoryLayer.LITERATURE,
            metadata=merged_metadata,
            user_id=user_id,
            agent_id=agent_id,
            run_id=run_id,
            infer=False,
        )

    def remember_experiment_state(
        self,
        state_payload: Dict[str, Any],
        *,
        run_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Persist a compact experiment memory from a research-agent state."""

        event = state_payload.get("event", {}) if isinstance(state_payload, dict) else {}
        query = event.get("query", "") if isinstance(event, dict) else ""
        macro_plan = state_payload.get("macro_plan", [])
        observations = state_payload.get("observations", [])
        stage = state_payload.get("current_stage", "")
        summary = {
            "query": query,
            "current_stage": stage,
            "macro_plan": macro_plan,
            "latest_observation": state_payload.get("latest_observation", {}),
            "stage_progress": state_payload.get("stage_progress", {}),
            "post_observation_repair_path": state_payload.get("post_observation_repair_path", ""),
        }
        text = json.dumps(summary, ensure_ascii=False, indent=2)
        metadata = {
            "title": query or stage or "chem-agent experiment memory",
            "query": query,
            "current_stage": stage,
            "observation_count": len(observations) if isinstance(observations, list) else 0,
            "status": state_payload.get("status", ""),
        }
        return self.add_experiment(
            text,
            metadata=metadata,
            user_id=user_id,
            run_id=run_id,
        )

    def add_literature_protocol(
        self,
        *,
        title: str,
        protocol_summary: str,
        source_file: str = "",
        steps: Optional[List[Dict[str, Any]]] = None,
        performance: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        content_parts = [title.strip(), protocol_summary.strip()]
        if steps:
            content_parts.append(json.dumps(steps, ensure_ascii=False, indent=2))
        if performance:
            content_parts.append(json.dumps(performance, ensure_ascii=False, indent=2))
        merged_metadata = dict(metadata or {})
        merged_metadata.update(
            {
                "title": title,
                "source_path": source_file,
                "steps": steps or [],
                "performance": performance or [],
            }
        )
        return self.add_literature(
            "\n\n".join(part for part in content_parts if part),
            metadata=merged_metadata,
        )

    def search(
        self,
        query: str,
        *,
        layers: Optional[Sequence[str]] = None,
        top_k: int = 10,
        filters: Optional[Dict[str, Any]] = None,
        threshold: float = 0.1,
    ) -> Dict[str, List[Dict[str, Any]]]:
        return self.store.search(
            query,
            layers=layers,
            top_k=top_k,
            filters=filters,
            threshold=threshold,
        )

    def search_experiments(
        self,
        query: str,
        *,
        top_k: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        return self.search(
            query,
            layers=[ChemMemoryLayer.EXPERIMENT],
            top_k=top_k,
            filters=filters,
        )

    def search_literature(
        self,
        query: str,
        *,
        top_k: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        return self.search(
            query,
            layers=[ChemMemoryLayer.LITERATURE],
            top_k=top_k,
            filters=filters,
        )

    def get_all(
        self,
        *,
        layers: Optional[Sequence[str]] = None,
        filters: Optional[Dict[str, Any]] = None,
        top_k: int = 20,
    ) -> Dict[str, List[Dict[str, Any]]]:
        return self.store.get_all(filters=filters, layers=layers, top_k=top_k)

    def get(self, memory_id: str) -> Dict[str, Any]:
        return self.store.get(memory_id)

    def update(
        self,
        memory_id: str,
        *,
        data: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        return self.store.update(memory_id, data=data, metadata=metadata)

    def delete(self, memory_id: str) -> Dict[str, str]:
        return self.store.delete(memory_id)

    def history(self, memory_id: str) -> List[Dict[str, Any]]:
        return self.store.history(memory_id)

    def count(self, layers: Optional[Sequence[str]] = None) -> int:
        return self.store.count(layers=layers)
