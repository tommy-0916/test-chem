"""Repeat-run comparison for research states (issue 3: repeatability).

Given two saved research states produced from the same query, report what
changed and attribute each change to its source:

- ``papers_or_network`` — different knowledge/literature hits (upstream
  databases or network availability changed);
- ``agent_generated``  — macro steps whose provenance is agent-fill differ
  (LLM filled gaps differently);
- ``plan``             — stage route / paper-backed plan steps differ.

Deterministic and read-only; used by the repeat-run acceptance test and as a
CLI-friendly helper for manual comparisons.
"""

from __future__ import annotations

from typing import Any, Dict, List

_AGENT_FILL_MARK = "agent补全"


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _titles(state: Dict[str, Any]) -> List[str]:
    titles = []
    for hit in _as_list(state.get("knowledge_hits")):
        if isinstance(hit, dict):
            title = str(hit.get("title", "")).strip()
            if title:
                titles.append(title)
    return sorted(set(titles))


def _step_core(step: Dict[str, Any]) -> str:
    return (
        f"{step.get('操作', '')}|{step.get('试剂/对象', '')}|{step.get('参数', '')}"
    )


def _is_agent_fill(step: Dict[str, Any]) -> bool:
    return _AGENT_FILL_MARK in str(step.get("来源", ""))


def compare_research_states(
    state_a: Dict[str, Any],
    state_b: Dict[str, Any],
) -> Dict[str, Any]:
    """Categorized diff of two research states from the same query."""
    changes: List[Dict[str, str]] = []

    titles_a, titles_b = _titles(state_a), _titles(state_b)
    papers_changed = titles_a != titles_b
    if papers_changed:
        only_a = sorted(set(titles_a) - set(titles_b))
        only_b = sorted(set(titles_b) - set(titles_a))
        changes.append(
            {
                "source": "papers_or_network",
                "detail": (
                    f"论文命中不同：仅第一次命中 {only_a[:3]}；仅第二次命中 {only_b[:3]}"
                ),
            }
        )

    acq_a = state_a.get("literature_acquisition") or {}
    acq_b = state_b.get("literature_acquisition") or {}
    if str(acq_a.get("keyword_query", "")) != str(acq_b.get("keyword_query", "")):
        changes.append(
            {
                "source": "papers_or_network",
                "detail": (
                    "实际检索词不同："
                    f"`{acq_a.get('keyword_query', '')}` vs `{acq_b.get('keyword_query', '')}`"
                ),
            }
        )

    route_a = [str(s) for s in _as_list(state_a.get("stage_route"))]
    route_b = [str(s) for s in _as_list(state_b.get("stage_route"))]
    stages_changed = route_a != route_b
    if stages_changed:
        changes.append(
            {
                "source": "plan",
                "detail": f"stage 路线不同：{route_a} vs {route_b}",
            }
        )

    plan_a = [s for s in _as_list(state_a.get("macro_plan")) if isinstance(s, dict)]
    plan_b = [s for s in _as_list(state_b.get("macro_plan")) if isinstance(s, dict)]
    macro_changed = False
    for index in range(max(len(plan_a), len(plan_b))):
        step_a = plan_a[index] if index < len(plan_a) else {}
        step_b = plan_b[index] if index < len(plan_b) else {}
        if _step_core(step_a) == _step_core(step_b):
            continue
        macro_changed = True
        agent_fill = _is_agent_fill(step_a) or _is_agent_fill(step_b)
        changes.append(
            {
                "source": "agent_generated" if agent_fill else "plan",
                "detail": (
                    f"第 {index + 1} 步不同："
                    f"`{_step_core(step_a)[:80]}` vs `{_step_core(step_b)[:80]}`"
                    + ("（该步骤为 agent 补全，属于生成随机性）" if agent_fill else "")
                ),
            }
        )

    return {
        "identical": not changes,
        "papers_changed": papers_changed,
        "stages_changed": stages_changed,
        "macro_plan_changed": macro_changed,
        "changes": changes,
        "summary": (
            "两次运行结果一致"
            if not changes
            else "；".join(f"[{c['source']}] {c['detail']}" for c in changes[:6])
        ),
    }
