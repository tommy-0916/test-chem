"""Classify device feasibility feedback before it becomes a terminal verdict.

The device agent's LLM may return a ``device_feasibility_error`` for three
very different reasons, and only one of them is a real physical blocker:

- ``hard``          — the truth source genuinely lacks a capability
                      (missing station/equipment or chemical operation,
                      volume over a hard limit,
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
from typing import Any, Dict, List, Tuple


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
# capabilities, not about parameter fields, timing, or implicit transport.
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

# Physical station-to-station movement and A→B material transfer are connected
# platform defaults. A container mismatch therefore triggers route repair/minimal
# vessel adaptation, not a hard verdict by itself.
CONTAINER_ROUTE_ADAPTATION_RE = re.compile(
    r"(?:没有|缺少|不存在|无|无法).{0,80}(?:转移路径|连续路径|容器路径)"
    r"|(?:没有|缺少|不存在|无|无法).{0,80}(?:容器链路|反应管链路)"
    r"|(?:没有|缺少|不存在|无|无法).{0,36}(?:获取|拿取|提供|供给)"
    r".{0,24}(?:空)?(?:\d+\s*ml)?(?:耐压)?(?:反应管|容器)(?:的物料站|物料站|来源|入口)?"
    r"|(?:反应管|容器).{0,24}(?:获取|供给|提供|来源).{0,24}(?:缺失|没有|缺少|链路|能力)"
    r"|(?:通往|跨站|工作站之间).{0,80}(?:转移|路径|链路)"
    r"|(?:转移|换瓶).{0,80}(?:路径|链路)"
    r"|(?:没有|缺少|不存在|未提供|无法|不能|不支持|不接受)"
    r"[^\n。；;]{0,120}(?:样品|物料|悬浊液|沉淀|产物|液体|固体)?"
    r"(?:转移|换瓶|移液|取样|接驳|transfer|transport)"
    r"[^\n。；;]{0,36}(?:操作|路径|链路|接口|来源|转移源|输入状态)?"
    # ``转入/输出`` are ambiguous in isolation (for example gas
    # output can be a real chemical capability). Require both source and
    # destination vessel evidence before treating them as ordinary transfer.
    r"|(?:没有|缺少|不存在|未提供|无法|不能|不支持|不接受)"
    r"[^\n。；;]{0,100}(?:\d+\s*ml)?(?:耐压)?(?:反应管|进样瓶|留样瓶|西林瓶|耐热瓶|容器)"
    r"[^\n。；;]{0,64}(?:转入|移入|倒入|导入|输出|转装)"
    r"[^\n。；;]{0,40}(?:进样瓶|留样瓶|西林瓶|耐热瓶|反应管|容器)"
    r"|(?:转移|换瓶|移液|取样|接驳|transfer|transport)"
    r"(?:工作站|设备|操作|路径|链路|接口)?"
    r"[^\n。；;]{0,80}(?:没有|缺少|不存在|未提供|无法|不能|不支持|不接受)"
    # A downstream station accepting a different container does not prove a
    # route gap: the platform contract permits one minimal A→B vessel move.
    r"|(?:样品|物料|悬浊液|沉淀|产物|液体|固体)"
    r"[^\n。；;]{0,32}(?:仍|尚)?(?:位于|留在|处于)"
    r"[^\n。；;]{0,32}(?:耐压)?(?:反应管|进样瓶|留样瓶|西林瓶|耐热瓶|容器)"
    r"[^\n。；;]{0,24}(?:无法|不能)建立[^\n。；;]{0,20}输入"
    r"|(?:耐压反应管|反应管|进样瓶|留样瓶|西林瓶|容器)"
    r"[^\n。；;]{0,40}(?:没有|无法|不支持|不接受)"
    r"[^\n。；;]{0,32}(?:兼容)?(?:搅拌|离心|洗涤|干燥|后处理|加液|取液)?操作"
    r"|(?:容器不兼容|容器类型不连续|无法转移|换瓶|转移混合样品)",
    re.IGNORECASE,
)

# A ``missing_device_capability`` field often contains only a noun phrase, not
# an explicit negative verb.  In that field alone, a request for an ordinary
# container-to-container transfer interface is still a denial of the platform
# default and must not become a route blocker.
CONTAINER_TRANSFER_CAPABILITY_RE = re.compile(
    r"(?:反应管|容器|样品|物料|悬浊液|沉淀|产物)"
    r"[^\n。；;]{0,36}(?:到|至|→|->)"
    r"[^\n。；;]{0,36}(?:反应管|瓶|容器)"
    r"[^\n。；;]{0,36}(?:转移|换瓶|移液|接驳|接口|操作|路径)"
    r"|(?:自动化|合法|受支持)?[^\n。；;]{0,20}"
    r"(?:全量)?(?:样品|物料|悬浊液|产物)?转移"
    r"(?:接口|操作|路径|链路|能力)"
    r"|(?:接受|接收)?[^\n。；;]{0,16}"
    r"(?:\d+\s*ml)?(?:耐压)?(?:反应管|容器)"
    r"[^\n。；;]{0,32}(?:转入|移入|倒入|导入|输出|转装)"
    r"[^\n。；;]{0,32}(?:进样瓶|留样瓶|西林瓶|耐热瓶|容器)"
    r"[^\n。；;]{0,32}(?:自动转移|固液分离链|接口|操作|路径|链路|能力)?"
    # A downstream operation named together with an otherwise incompatible
    # source vessel is an input-container adaptation, not proof that the
    # centrifuge/wash/XRD operation itself is absent.
    r"|(?:以|接受|接收)?[^\n。；;]{0,16}"
    r"(?:\d+\s*ml)?(?:耐压)?(?:反应管|容器)"
    r"[^\n。；;]{0,20}(?:为|作为)?输入[^\n。；;]{0,20}"
    r"(?:自动)?(?:离心洗涤|离心|洗涤|XRD(?:_V1)?制样|XRD(?:_V1)?输入|表征制样)"
    r"|(?:可获取|获取|供给|提供)[^\n。；;]{0,24}"
    r"(?:空)?(?:\d+\s*ml)?(?:耐压)?(?:反应管|容器)"
    r"[^\n。；;]{0,24}(?:链路|能力|入口|来源)?",
    re.IGNORECASE,
)

CONNECTIVITY_CONSEQUENCE_RE = re.compile(
    r"(?:不能|无法)建立[^\n。；;]{0,32}(?:离心|洗涤|干燥|搅拌|后处理)?输入状态"
    r"|(?:后续|样品处理|流程|路线|链)"
    r"[^\n。；;]{0,60}(?:无法|不能)(?:闭合|继续|建立|进入)"
    r"|(?:链|流程|路线)[^\n。；;]{0,20}(?:无法|不能)闭合"
    r"|(?:该|上述|因此)?[^\n。；;]{0,40}"
    r"(?:固液分离|离心|洗涤|干燥|后处理|表征)"
    r"[^\n。；;]{0,48}(?:不能|不可|无法)[^\n。；;]{0,24}"
    r"(?:offline(?:_handoff)?|离线|人工)[^\n。；;]{0,12}(?:替代|完成)?",
    re.IGNORECASE,
)

_CONNECTIVITY_SEGMENT_SPLIT_RE = re.compile(
    r"(?<=[。！？!?；;])\s*|"
    r"(?=[，,]?(?:并且|而且|同时|此外|另外|设备还|"
    r"仍然|另有|也(?:没有|无|不|未)|但|且))"
)

# When a mixed sentence contains both a false transport complaint and an
# independent blocker, retain only the latter.  Mere descriptions of the
# requested pH/temperature are not blockers; a retained fragment needs a
# negative/contradictory fact of its own.
INDEPENDENT_CONSTRAINT_EVIDENCE_RE = re.compile(
    r"矛盾|不一致|冲突|超过|超出|超限|不足|容量不足|"
    r"重复消费|重复计量|未知收率|无法判断|不允许合批|"
    r"(?:没有|缺少|不存在|未提供|无法|不能|不支持|离线|不可用)"
    r"[^\n。；;]{0,100}|"
    r"(?:安全|防爆|惰性|无氧|压力|温度|pH|酸碱)"
    r"[^\n。；;]{0,60}(?:无法满足|不能满足|不支持|缺少|不存在|未提供|超限)",
    re.IGNORECASE,
)

STRONG_INDEPENDENT_CONSTRAINT_RE = re.compile(
    r"(?:pH|酸碱)[^\n。；;]{0,36}(?:测量|检测|调节|控制|验证)"
    r"[^\n。；;]{0,36}(?:没有|缺少|不存在|未提供|无法|不能|不支持)"
    r"|(?:没有|缺少|不存在|未提供|无法|不能|不支持)"
    r"[^\n。；;]{0,36}(?:pH|酸碱)[^\n。；;]{0,24}(?:测量|检测|调节|控制|验证)?"
    r"|(?:安全|防爆|惰性|无氧|密闭安全|压力安全)"
    r"[^\n。；;]{0,60}(?:无法满足|不能满足|不支持|缺少|不存在|未提供|超限)"
    r"|矛盾|不一致|重复消费|重复计量|未知收率|不允许合批",
    re.IGNORECASE,
)


def _has_strong_independent_constraint(segment: str) -> bool:
    """Fail-safe guard for two claims written without a splittable delimiter."""

    return bool(
        STRONG_INDEPENDENT_CONSTRAINT_RE.search(segment)
        or CHEMICAL_OPERATION_GAP_RE.search(segment)
        or EQUIPMENT_GAP_PREFIX_RE.search(segment)
        or EQUIPMENT_GAP_SUFFIX_RE.search(segment)
        or any(marker in segment.lower() for marker in HARD_DEVICE_BLOCKER_MARKERS)
    )


def scrub_implicit_connectivity_constraint(
    text: Any,
    *,
    capability_field: bool = False,
) -> Tuple[str, bool]:
    """Remove only ordinary-transfer denial fragments from one constraint.

    The return value is ``(remaining_text, changed)``.  If the complaint is
    purely about the default-connected transfer fabric, ``remaining_text`` is
    empty.  In a mixed complaint, independently negative quantity, pH, safety,
    availability, or operation-capability fragments are retained.
    """

    original = str(text or "").strip()
    if not original:
        return "", False
    has_connectivity = bool(CONTAINER_ROUTE_ADAPTATION_RE.search(original))
    if capability_field:
        has_connectivity = has_connectivity or bool(
            CONTAINER_TRANSFER_CAPABILITY_RE.search(original)
        )
    if not has_connectivity:
        return original, False

    segments = [
        segment.strip(" \t\r\n，,")
        for segment in _CONNECTIVITY_SEGMENT_SPLIT_RE.split(original)
        if segment.strip(" \t\r\n，,")
    ]
    kept: List[str] = []
    removed_previous = False
    changed = False
    for segment in segments:
        route_fragment = bool(CONTAINER_ROUTE_ADAPTATION_RE.search(segment))
        if capability_field:
            route_fragment = route_fragment or bool(
                CONTAINER_TRANSFER_CAPABILITY_RE.search(segment)
            )
        if route_fragment:
            # If the model concatenated a true independent blocker and a
            # transport complaint without a safe delimiter, retain the full
            # fragment rather than risk deleting pH/safety/operation evidence.
            # Normal delimited mixed claims are split and cleaned precisely.
            if _has_strong_independent_constraint(segment):
                kept.append(segment)
                removed_previous = False
                continue
            changed = True
            removed_previous = True
            continue
        if removed_previous and CONNECTIVITY_CONSEQUENCE_RE.search(segment):
            changed = True
            continue
        # Once a transport-only fragment has been removed, surrounding text is
        # often just requirement context ("pH 9.5 is required"). Preserve it
        # only when it independently asserts a failure/contradiction.
        if has_connectivity and not INDEPENDENT_CONSTRAINT_EVIDENCE_RE.search(segment):
            changed = True
            continue
        kept.append(segment)
        removed_previous = False

    return " ".join(kept).strip(), changed

CHEMICAL_OPERATION_GAP_RE = re.compile(
    r"(?:真源|设备|工作站).{0,24}(?:没有|缺少|不存在|未提供)"
    r".{0,80}(?:化学操作|工艺操作|工作站操作|混合操作|均匀混合|球磨|研磨)"
    r"|(?:没有|缺少|不存在|未提供).{0,80}"
    r"(?:固体粉末混合|干粉混合|均匀混合|球磨|研磨).{0,30}(?:操作|能力|工作站)",
    re.IGNORECASE,
)


def is_implicit_connectivity_constraint(text: Any) -> bool:
    """True when a complaint only denies the platform's default transfer fabric.

    Physical station-to-station transport and material transfer A→B are runtime
    defaults supplied by the platform contract. Such wording must be repaired in
    Device planning, not returned to Research as a capability blocker.
    """
    normalized = json_text(text)
    remaining, changed = scrub_implicit_connectivity_constraint(normalized)
    return bool(changed and not remaining)

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
    residual, connectivity_removed = scrub_implicit_connectivity_constraint(
        normalized
    )
    if connectivity_removed and not residual:
        return "adaptable"
    if connectivity_removed:
        normalized = residual
    if HARD_TEMPORAL_FEED_RE.search(normalized):
        return "hard"
    # An explicit inability to CONFIRM/prove equivalence of an existing
    # candidate station is an unprovable condition (human review), and takes
    # precedence over the "no X station" equipment-gap wording it often
    # accompanies.  Definitive physical facts above (offline, no transfer
    # path, incompatible container, volume over limit) already returned hard.
    if CONFIRMATION_HEDGE_RE.search(normalized):
        return "unverifiable"
    prefix_gap = EQUIPMENT_GAP_PREFIX_RE.search(normalized)
    if prefix_gap and not is_implicit_connectivity_constraint(prefix_gap.group(0)):
        return "hard"
    if is_implicit_connectivity_constraint(normalized):
        return "adaptable"
    if CHEMICAL_OPERATION_GAP_RE.search(normalized):
        return "hard"
    if any(marker in normalized for marker in HARD_DEVICE_BLOCKER_MARKERS):
        return "hard"
    if EQUIPMENT_GAP_SUFFIX_RE.search(normalized):
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
