"""Classify device feasibility feedback before it becomes a terminal verdict.

The device agent's LLM may return a ``device_feasibility_error`` for three
very different reasons, and only one of them is a real physical blocker:

- ``hard``          — the truth source genuinely lacks a capability
                      (missing station/equipment, no legal transfer path,
                      incompatible containers, volume over a hard limit,
                      station offline, an explicitly non-interruptible
                      continuous feed).
- ``adaptable``     — the complaint is about process phrasing/timing
                      (e.g. "边滴入边搅拌" cannot run atomically on one
                      station) which the device layer can preserve with an
                      interleaved batch schedule.
- ``unverifiable``  — a macro parameter has no matching dispatch field so
                      the device cannot *prove* it is satisfied (e.g. an
                      XRD radiation source).  This must go to human review,
                      not be reported as physically infeasible.

The historical bug was treating everything as ``hard`` (physical_infeasible)
unless a temporal keyword matched.  Here the default is ``unverifiable``:
claiming a hard blocker requires hard evidence in the constraint text.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List


# ---------------------------------------------------------------------------
# temporal addition-while-stirring detection (adaptable process semantics)
# ---------------------------------------------------------------------------

TEMPORAL_ADDITION_STIRRING_MARKERS = (
    "边搅拌边加入",
    "边搅拌边滴加",
    "边滴入边搅拌",
    "边滴加边搅拌",
    "持续磁力搅拌下加入",
    "持续磁力搅拌条件下加入",
    "同步搅拌加液",
    "同步加液",
    "滴加过程中搅拌",
    "滴入过程中搅拌",
    "缓慢滴加",
    "控速滴加",
    "dropwise addition",
    "addition while stirring",
)

# An explicitly non-interruptible feed is a genuine capability requirement.
HARD_TEMPORAL_FEED_RE = re.compile(
    r"(?:连续流|恒定流速|不可中断|不可拆分|微流控|泵控).{0,24}(?:加液|滴加|滴入|进料)"
    r"|(?:加液|滴加|滴入|进料).{0,24}(?:连续流|恒定流速|不可中断|不可拆分|微流控|泵控)",
    re.IGNORECASE,
)

# Hard capability-gap evidence.  These phrases talk about equipment or legal
# paths, not about parameter fields or timing.
HARD_DEVICE_BLOCKER_MARKERS = (
    "没有液体进样",
    "缺少液体进样",
    "没有磁力搅拌",
    "缺少磁力搅拌",
    "工作站不可用",
    "设备不可用",
    "status=offline",
    "工作站离线",
    "设备离线",
    "维修",
    "故障",
    "无法转移",
    "没有转移路径",
    "无合法转移",
    "容器不兼容",
    "容器类型不连续",
    "体积超过",
    "体积超限",
    "超出上限",
    "容量不足",
    "配平失败",
    "无法配平",
    "安全限制",
)

# "<station-ish noun> ... <missing/offline>" — tempered so that parameter/
# field complaints ("工作站参数中没有该字段") do NOT count as hard evidence.
HARD_DEVICE_BLOCKER_RE = re.compile(
    r"(?:液体进样|移液|磁力搅拌|搅拌|工作站|设备)"
    r"(?:(?!参数|字段|下发|schema)[^，。;；]){0,10}"
    r"(?:缺失|不存在|没有|不可用|离线|维修|故障|不兼容)",
    re.IGNORECASE,
)

# Epistemic hedges: the model is reasoning about whether an EXISTING candidate
# station/capability qualifies, not asserting definitive absence.  "Cannot
# confirm / no rule proves equivalence" is an unprovable condition that must go
# to human review, per the reviewer's principle — not physical_infeasible.
# This takes precedence over the equipment-gap-by-name hard rule below.
CONFIRMATION_HEDGE_RE = re.compile(
    r"未提供可被确认"
    r"|可被确认等价|无法确认等价|不能确认等价|无法证明.{0,6}等价|不能证明.{0,6}等价"
    r"|没有规则(?:证明|证实|说明|支持)|无规则(?:证明|证实)"
    r"|(?:无法|不能|难以|未能)(?:确认|证明|证实|判定|保证)"
    r"|(?:是否|能否).{0,10}(?:等价|满足|支持).{0,6}(?:不确定|无法确认|存疑)",
    re.IGNORECASE,
)
EQUIPMENT_NOUNS = (
    "反应釜|高压釜|马弗炉|工作站|反应器|离心机|烘干机|色谱|光谱仪|衍射仪"
    "|电化学工作站|超声|电极|机械臂|机器人|平台|仪器|设备|装置|模块"
)
EQUIPMENT_GAP_PREFIX_RE = re.compile(
    rf"(?:缺少|缺失|没有|不存在|不具备|未配置|未安装)"
    rf"(?:(?!参数|字段|schema|接口|下发)[^，。;；]){{0,14}}(?:{EQUIPMENT_NOUNS})"
)
EQUIPMENT_GAP_SUFFIX_RE = re.compile(
    rf"(?:{EQUIPMENT_NOUNS})"
    rf"(?:(?!参数|字段|schema|接口|下发)[^，。;；]){{0,6}}(?:缺失|不存在|未配置|未安装)"
)

# Parameter/field-absence complaints — the "no identically named dispatch
# field" case that must become human review, never physical_infeasible.
PARAM_ABSENCE_RE = re.compile(
    r"(?:可下发|下发|schema|参数|字段|接口)[^，。;；]{0,16}"
    r"(?:不含|不包含|只包含|仅包含|没有|缺少|缺失|未包含|未列出|不存在"
    r"|无法(?:设置|指定|下发|配置|保证))"
    r"|(?:无法|不能|难以)(?:保证|证明|验证|确认)"
    r"|没有(?:同名|对应)(?:字段|参数)"
    r"|无(?:同名|对应)(?:字段|参数)",
    re.IGNORECASE,
)

# Generic atomicity / parallel-execution complaints (adaptable).
ATOMICITY_RE = re.compile(
    r"(?:无法|不能|不可)(?:在)?(?:同一|单个|单一)?(?:工作站|站|设备)?"
    r"[^，。;；]{0,10}(?:同时|同步|并行|原子化)"
    r"|(?:同时|同步|并行|原子化)[^，。;；]{0,12}(?:执行|完成|进行)"
    r"[^，。;；]{0,10}(?:不支持|无法|不能)"
    r"|原子化.{0,10}(?:并行|执行|动作)",
    re.IGNORECASE,
)
DROPWISE_LIMIT_RE = re.compile(
    r"(?:缓慢|控速|逐滴|分批)?滴加[^，。;；]{0,16}(?:无法|不支持|不能)"
    r"|(?:无法|不支持|不能)[^，。;；]{0,12}(?:缓慢|控速|连续|逐滴)?滴加",
    re.IGNORECASE,
)

# LLM-declared categories in unsupported_items (see the task prompt).  The
# LLM may soften a constraint, but hardening requires textual evidence.
LLM_CATEGORY_MAP = {
    "hard_capability_gap": "hard",
    "physical_infeasible": "hard",
    "process_semantics_adaptable": "adaptable",
    "temporal_adaptation": "adaptable",
    "unverifiable_condition": "unverifiable",
    "needs_human_review": "unverifiable",
}


def json_text(value: Any) -> str:
    """Flatten nested model output for conservative semantic checks."""
    try:
        return json.dumps(value, ensure_ascii=False).lower()
    except (TypeError, ValueError):
        return str(value or "").lower()


def has_temporal_addition_stirring(text: Any) -> bool:
    normalized = json_text(text)
    if any(marker.lower() in normalized for marker in TEMPORAL_ADDITION_STIRRING_MARKERS):
        return True
    return bool(
        re.search(
            r"(?:边|持续|同步).{0,12}(?:搅拌|磁力搅拌).{0,20}(?:加入|加液|滴加|滴入)",
            normalized,
        )
        or re.search(
            r"(?:加入|加液|滴加|滴入).{0,20}(?:边|持续|同步).{0,12}(?:搅拌|磁力搅拌)",
            normalized,
        )
        or re.search(r"(?:滴加|滴入).{0,12}(?:过程中|同时).{0,12}搅拌", normalized)
    )


def classify_constraint_text(text: Any) -> str:
    """Classify one blocking-constraint string: hard / adaptable / unverifiable."""
    normalized = json_text(text)
    if not normalized.strip("\"' "):
        return "unverifiable"
    if HARD_TEMPORAL_FEED_RE.search(normalized):
        return "hard"
    if any(marker in normalized for marker in HARD_DEVICE_BLOCKER_MARKERS):
        return "hard"
    # An explicit inability to CONFIRM/prove equivalence of an existing
    # candidate station is an unprovable condition (human review), and takes
    # precedence over the "no X station" equipment-gap wording it often
    # accompanies.  Definitive physical facts above (offline, no transfer
    # path, incompatible container, volume over limit) already returned hard.
    if CONFIRMATION_HEDGE_RE.search(normalized):
        return "unverifiable"
    if EQUIPMENT_GAP_PREFIX_RE.search(normalized) or EQUIPMENT_GAP_SUFFIX_RE.search(normalized):
        return "hard"
    if HARD_DEVICE_BLOCKER_RE.search(normalized) and not PARAM_ABSENCE_RE.search(normalized):
        return "hard"
    if PARAM_ABSENCE_RE.search(normalized):
        return "unverifiable"
    if (
        has_temporal_addition_stirring(normalized)
        or ATOMICITY_RE.search(normalized)
        or DROPWISE_LIMIT_RE.search(normalized)
    ):
        return "adaptable"
    # KEY CHANGE: without hard evidence the constraint is at most a condition
    # the device cannot prove — a human-review case, not physical_infeasible.
    return "unverifiable"


def _is_error_result(result: Dict[str, Any]) -> bool:
    status = str(result.get("status", "")).strip().lower()
    feedback_type = str(result.get("feedback_type", "")).strip().lower()
    return (
        status in {"feasibility_error", "unsupported", "not_feasible"}
        or feedback_type == "device_feasibility_error"
    )


def classify_feasibility_result(
    result: Dict[str, Any],
    handoff: Dict[str, Any],
) -> Dict[str, Any]:
    """Bucket every blocking constraint / unsupported item of an error result.

    Returns ``{"is_error", "overall", "hard", "adaptable", "unverifiable",
    "temporal_signal"}`` where ``overall`` is ``hard`` if any hard evidence
    exists, else ``adaptable`` if any process-semantics complaint exists,
    else ``unverifiable``.
    """
    classification: Dict[str, Any] = {
        "is_error": _is_error_result(result),
        "overall": "",
        "hard": [],
        "adaptable": [],
        "unverifiable": [],
        "temporal_signal": has_temporal_addition_stirring(handoff)
        or has_temporal_addition_stirring(result),
    }
    if not classification["is_error"]:
        return classification

    feasibility = result.get("feasibility") if isinstance(result.get("feasibility"), dict) else {}

    texts: List[Any] = []
    for source in (
        feasibility.get("blocking_constraints", []),
        result.get("blocking_constraints", []),
    ):
        if isinstance(source, list):
            texts.extend(source)
        elif source:
            texts.append(source)

    items = feasibility.get("unsupported_items", [])
    if not isinstance(items, list):
        items = []
    for item in items:
        if not isinstance(item, dict):
            texts.append(item)
            continue
        blob = " ".join(
            str(item.get(key, "") or "")
            for key in ("requirement", "reason", "missing_device_capability")
        )
        evidence_class = classify_constraint_text(blob)
        missing = str(item.get("missing_device_capability", "") or "")
        if evidence_class != "hard" and missing:
            if re.search(rf"(?:{EQUIPMENT_NOUNS})", missing) and not re.search(
                r"参数|字段|schema|源|气氛|湿度|范围|精度|波长", missing
            ):
                evidence_class = "hard"
        llm_class = LLM_CATEGORY_MAP.get(
            str(item.get("constraint_category", "") or "").strip().lower(), ""
        )
        if llm_class and llm_class != "hard" and evidence_class != "hard":
            evidence_class = llm_class
        classification[evidence_class].append(blob.strip() or json_text(item))

    for text in texts:
        bucket = classify_constraint_text(text)
        classification[bucket].append(str(text).strip())

    if not (classification["hard"] or classification["adaptable"] or classification["unverifiable"]):
        classification["unverifiable"].append(
            "device agent 未返回具体阻塞原因，默认按不可证明条件进入人工审核。"
        )

    if classification["hard"]:
        classification["overall"] = "hard"
    elif classification["adaptable"]:
        classification["overall"] = "adaptable"
    else:
        classification["overall"] = "unverifiable"
    return classification


def soft_temporal_mapping_error(result: Dict[str, Any], handoff: Dict[str, Any]) -> bool:
    """A retryable non-atomic timing complaint, not a hard blocker."""
    classification = classify_feasibility_result(result, handoff)
    if not classification["is_error"] or classification["overall"] == "hard":
        return False
    evidence_text = json_text(
        {
            "adaptable": classification["adaptable"],
            "unverifiable": classification["unverifiable"],
        }
    )
    if has_temporal_addition_stirring(evidence_text):
        return True
    return bool(
        classification["adaptable"]
        and classification["temporal_signal"]
        and re.search(r"同时|同步|原子化|同一时间|不能在.*搅拌", evidence_text)
    )
