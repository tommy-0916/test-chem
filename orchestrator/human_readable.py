"""Build the unified user-readable result (issue 2: human_readable_result.json).

This is EXTRACTION ONLY: it reorganizes what the research state, device
package, and ledger already say into one file a person can read without
opening five JSONs. It never rewrites the original query, never invents plan
content, and never overwrites the source artifacts.

Stable IDs: stages S01…, macro steps M01… (from 步骤序号), device steps D01…
(from step_number); device steps point back to their macro step via
``source_macro_step_id`` and to the observation hierarchy via
``macro_action_id`` / ``observation_point_id`` (issue 6).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

HUMAN_READABLE_FILENAME = "human_readable_result.json"

_AGENT_FILL_MARK = "agent补全"


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _stage_id(index: int) -> str:
    return f"S{index:02d}"


def _macro_step_id(step: Dict[str, Any], fallback_index: int) -> str:
    number = step.get("步骤序号")
    if isinstance(number, int) and number > 0:
        return f"M{number:02d}"
    return f"M{fallback_index:02d}"


def _evidence(step: Dict[str, Any]) -> Dict[str, Any]:
    """Structured provenance per issue 2's agent-fill recommendation."""
    source = str(step.get("来源", "") or "").strip()
    if not source:
        return {
            "source_type": "unspecified",
            "reference": "",
            "requires_review": True,
        }
    if _AGENT_FILL_MARK in source:
        return {
            "source_type": "agent_generated",
            "reference": source,
            "reason": "文献/知识库未提供该步骤完整参数，由 agent 依据化学常识补全",
            "requires_review": True,
        }
    return {
        "source_type": "paper_protocol",
        "reference": source,
        "requires_review": False,
    }


def _paper_entries(research_state: Dict[str, Any]) -> List[Dict[str, Any]]:
    macro_sources = " ".join(
        str(step.get("来源", ""))
        for step in _as_list(research_state.get("macro_plan"))
        if isinstance(step, dict)
    )
    papers: List[Dict[str, Any]] = []
    seen_titles = set()

    for seed in _as_list(research_state.get("seed_papers")):
        seed = _as_dict(seed)
        title = str(seed.get("title", "")).strip()
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        papers.append(
            {
                "title": title,
                "doi": str(seed.get("doi", "")),
                "source": "online",
                "relevance": "用户提供/在线解析的种子文献",
                "used_in_plan": title[:24] in macro_sources,
            }
        )

    for hit in _as_list(research_state.get("knowledge_hits")):
        hit = _as_dict(hit)
        title = str(hit.get("title", "")).strip()
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        matched = [str(term) for term in _as_list(hit.get("matched_terms"))][:6]
        papers.append(
            {
                "title": title,
                "doi": "",
                "source": "local_memory",
                "relevance": (
                    f"知识库命中（匹配词：{'、'.join(matched)}）" if matched else "知识库命中"
                ),
                "used_in_plan": title[:24] in macro_sources,
            }
        )
    return papers


def _run_status(research_state: Dict[str, Any], device_package: Optional[Dict[str, Any]]) -> str:
    research_status = str(research_state.get("status", "")).strip()
    if research_status == "manual_required":
        return "manual_required"
    package = _as_dict(device_package)
    device_status = str(package.get("status", "")).strip()
    if device_status == "feasibility_error":
        return "feasibility_error"
    if device_status == "failed":
        return "device_error"
    if device_status == "success":
        return "completed"
    return research_status or "unknown"


def _failure_reasons(
    research_state: Dict[str, Any],
    device_package: Optional[Dict[str, Any]],
) -> List[str]:
    reasons: List[str] = []
    manual = str(research_state.get("manual_handoff", "")).strip()
    if manual:
        reasons.append(f"研究层转人工：{manual}")
    package = _as_dict(device_package)
    error_package = _as_dict(package.get("error_package"))
    for constraint in _as_list(error_package.get("blocking_constraints")):
        text = str(constraint).strip()
        if text:
            reasons.append(text)
    message = str(error_package.get("message", "")).strip()
    if message and message not in reasons:
        reasons.append(message)
    if package.get("status") == "failed":
        validation = _as_dict(package.get("dispatch_validation"))
        for error in _as_list(validation.get("errors"))[:6]:
            reasons.append(str(error))
    return reasons


def _final_plan_text(
    research_state: Dict[str, Any],
    device_package: Optional[Dict[str, Any]],
) -> str:
    package = _as_dict(device_package)
    workflow_txt = str(package.get("workflow_txt", "")).strip()
    if package.get("status") == "success" and workflow_txt:
        return workflow_txt
    plan = str(research_state.get("current_stage_plan", "")).strip()
    lines = [plan] if plan else []
    for index, step in enumerate(_as_list(research_state.get("macro_plan")), start=1):
        if not isinstance(step, dict):
            continue
        lines.append(
            f"{step.get('步骤序号', index)}. {step.get('操作', '')}"
            f"（{step.get('试剂/对象', '')}）：{step.get('参数', '')}"
        )
    return "\n".join(lines)


def build_human_readable_result(
    research_state: Dict[str, Any],
    device_package: Optional[Dict[str, Any]] = None,
    *,
    campaign_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the issue-2 structure from existing artifacts (read-only)."""
    research_state = _as_dict(research_state)
    package = _as_dict(device_package)
    event = _as_dict(research_state.get("event"))
    macro_action = _as_dict(research_state.get("macro_action"))

    original_query = str(event.get("query", "") or research_state.get("query", ""))

    stages: List[Dict[str, Any]] = []
    current_stage = str(research_state.get("current_stage", ""))
    for index, name in enumerate(_as_list(research_state.get("stage_route")), start=1):
        stage_name = str(name)
        entry = {
            "stage_id": _stage_id(index),
            "name": stage_name,
            "goal": "",
            "is_current": stage_name == current_stage,
        }
        if entry["is_current"]:
            entry["goal"] = str(
                macro_action.get("completion_condition", "")
                or research_state.get("current_stage_plan", "")
            )[:200]
        stages.append(entry)

    macro_plan: List[Dict[str, Any]] = []
    for index, step in enumerate(_as_list(research_state.get("macro_plan")), start=1):
        if not isinstance(step, dict):
            continue
        macro_plan.append(
            {
                "macro_step_id": _macro_step_id(step, index),
                "operation": str(step.get("操作", "")),
                "reagents_or_objects": str(step.get("试剂/对象", "")),
                "parameters": str(step.get("参数", "")),
                "evidence": _evidence(step),
                "macro_action_id": str(step.get("macro_action_id", "")),
                "observation_point_id": str(step.get("observation_point_id", "")),
            }
        )

    workflow_json = _as_dict(package.get("workflow_json"))
    device_plan: List[Dict[str, Any]] = []
    for index, step in enumerate(_as_list(workflow_json.get("steps")), start=1):
        if not isinstance(step, dict):
            continue
        source = step.get("source_macro_step")
        source_id = f"M{int(source):02d}" if isinstance(source, int) and source > 0 else ""
        device_plan.append(
            {
                "device_step_id": f"D{step.get('step_number', index):02d}",
                "source_macro_step_id": source_id,
                "workstation": str(step.get("workstation", "")),
                "operation": str(step.get("operation", "")),
                "parameters": _as_dict(step.get("parameters")),
                "macro_action_id": str(step.get("macro_action_id", "")),
                "observation_point_id": str(step.get("observation_point_id", "")),
            }
        )

    # issue 7: the strict platform-form dispatch layer, distinct from the
    # planning-format device_plan above (device_plan 面向阅读，dispatch_plan
    # 为按平台字段严格转换后的下发形式).
    dispatch_payload = _as_dict(package.get("dispatch_payload"))
    dispatch_formatting = _as_dict(package.get("dispatch_formatting"))
    dispatch_steps: List[Dict[str, Any]] = []
    for index, step in enumerate(
        _as_list(_as_dict(dispatch_payload.get("experiment_steps")).get("steps")),
        start=1,
    ):
        if not isinstance(step, dict):
            continue
        dispatch_steps.append(
            {
                "device_step_id": f"D{step.get('step_number', index):02d}",
                "workstation": str(step.get("workstation", "")),
                "workstation_id": step.get("id", ""),
                "operation": str(step.get("operation", "")),
                "parameters": _as_dict(step.get("parameters")),
            }
        )

    result: Dict[str, Any] = {
        "original_query": original_query,
        "run_status": _run_status(research_state, device_package),
        "failure_category": str(research_state.get("failure_category", "")),
        "requires_scientific_review": bool(package.get("requires_scientific_review")),
        "papers": _paper_entries(research_state),
        "stages": stages,
        "macro_action": {
            "macro_action_id": str(macro_action.get("macro_action_id", "")),
            "observation_point": str(macro_action.get("observation_point", "")),
            "objective": str(macro_action.get("objective", "")),
            "completion_condition": str(macro_action.get("completion_condition", "")),
        },
        "macro_plan": macro_plan,
        "device_plan": device_plan,
        "dispatch_plan": {
            "note": "严格按平台字段转换后的下发形式；device_plan 为规划格式",
            "steps": dispatch_steps,
            "mapped_steps": dispatch_formatting.get("mapped_steps", 0),
            "unmapped_steps": dispatch_formatting.get("unmapped_steps", 0),
            "warnings": _as_list(dispatch_formatting.get("warnings")),
        },
        "offline_handoffs": _as_list(workflow_json.get("offline_handoffs")),
        "temporal_adaptations": _as_list(package.get("temporal_adaptations")),
        "blocking_constraints": _failure_reasons(research_state, device_package),
        "final_experiment_plan": _final_plan_text(research_state, device_package),
        "source_files": {
            "note": "本文件为只读抽取结果，原始产物未被修改",
            "research_state": "research_state.json",
            "device_package": "device_package.json" if device_package else "",
        },
    }
    if campaign_meta:
        result["campaign"] = dict(campaign_meta)
    return result


def write_human_readable_result(
    directory: Path,
    research_state: Dict[str, Any],
    device_package: Optional[Dict[str, Any]] = None,
    *,
    campaign_meta: Optional[Dict[str, Any]] = None,
) -> Path:
    result = build_human_readable_result(
        research_state,
        device_package,
        campaign_meta=campaign_meta,
    )
    path = Path(directory) / HUMAN_READABLE_FILENAME
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path
