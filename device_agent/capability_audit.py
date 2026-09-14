"""Deterministic capability audit for device workflows (issues #9 and #11).

Issue #9: the workstation truth source HAS a complete solid-weighing chain —
Solid_Sample_Transfer_Workstation_V1 (source vial → 料斗/hopper, SKILL line 19)
feeding Multi_Channel_Solid_Weighing_Workstation_V1 (recipe file: 加样量 [0,50] g
+ 料罐号 [1,30], SKILL lines 47-48) / Single_Channel (进样质量 (0,200) g) /
Multi_Channel V2 (料斗编号 [1,10] → 10ml耐压反应管, 加料样 (0,40] g) — yet the
Device LLM sometimes routes "称取 5.0/10.0 mg 粉末" into a MANUAL
offline_handoff. Across the archived eval runs the same case flips between
on-device and manual per attempt, so this is instability, not a capability gap.

Issue #11: the 45 workstations are physically interconnected; inter-station
material transfer never needs a human. Manual MATERIAL operations (人工拿取/
搬运/转移/称取/装载…) are forbidden outright — human *review* (人工审核) is not
a material operation and stays legal.

Both are enforced deterministically here, AFTER the LLM output and independent
of it, mirroring how workflow_validator.py enforces the SKILL schema: findings
feed the bounded self-repair loop, and what cannot be repaired is reported —
never silently passed.

The one TRUE gap stays protected: no weighing/transfer station targets
96位石英孔板 (quartz plate, muffle-furnace path — C01), so quartz-plate powder
loading is never flagged as automatable and weighing-related feasibility
constraints that cite it keep their hard classification.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# ----------------------------------------------------------------------
# Solid-chain capability table (from SKILL.md truth source; see module doc)
# ----------------------------------------------------------------------

# Containers a weighing station can dose powder INTO.
SOLID_WEIGHING_TARGETS = ("进样瓶", "西林瓶", "50ml耐热瓶", "10ml耐压反应管")
# Containers the transfer station can take powder FROM (→ 料斗).
SOLID_TRANSFER_INPUTS = ("96位塑料孔板", "50ml耐热瓶", "西林瓶", "进样瓶")
# TRUE capability gaps — no station loads powder into these; never flag.
SOLID_TRUE_GAP_MARKERS = ("石英孔板", "马弗炉", "muffle")

# station → inclusive-ish mass window in grams (per SKILL parameter tables).
MASS_RANGES_G: Dict[str, Tuple[float, float]] = {
    "Multi_Channel_Solid_Weighing_Workstation_V1": (0.0, 50.0),
    "Multi_Channel_Solid_Weighing_Workstation_V2": (0.0, 40.0),
    "Single_Channel_Solid_Weighing_Workstation_V1": (0.0, 200.0),
}

# The canonical on-device route, stated once so repair prompts and findings
# can quote it verbatim (SKILL-correct parameter shapes included — archived
# runs show the LLM inventing field names even when it picks the stations).
SOLID_WEIGHING_ROUTE_NOTE = (
    "固体定量称量的设备内规范链路：\n"
    "1) Solid_Sample_Transfer_Workstation_V1 / 固体样品转移：源容器"
    "（进样瓶/西林瓶/50ml耐热瓶/96位塑料孔板，无盖固体）→ 料斗；"
    "参数仅有 容器类型/容器数量/容器编号。\n"
    "2) Multi_Channel_Solid_Weighing_Workstation_V1 / 固体进样-文件传参-机器人："
    "目标容器（进样瓶/西林瓶/50ml耐热瓶，无盖）为参数，配方经 上传文件 CSV"
    "（每行：瓶号, 加样量(单位 g，例如 5 mg = 0.005 g，范围 [0,50]), 料罐号(1-30)）；"
    "料斗与料罐是同一进料接口的两种叫法。\n"
    "3) 或 Single_Channel_Solid_Weighing_Workstation_V1 / 固体进样："
    "进样瓶/50ml耐热瓶 直接定量，参数 进样质量(单位 g，范围 (0,200))。\n"
    "4) 10ml耐压反应管 定量装粉走 Multi_Channel_Solid_Weighing_Workstation_V2 / "
    "固体称量（料斗编号 1-10，--加料样 单位 g，范围 (0,40]）。\n"
    "只有 96位石英孔板 的定量装粉是真实能力缺口（可保留 handoff 或判 hard）。"
)

# ----------------------------------------------------------------------
# Text probes
# ----------------------------------------------------------------------

_WEIGHING_VERB_RE = re.compile(r"称取|称量|称样|分样|分装|定量装|定量取|各取")
_MASS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(mg|毫克|g|克)(?![a-z])", re.IGNORECASE)
_CONTAINER_RE = re.compile(
    r"石英孔板|96位塑料孔板|10ml耐压反应管|50ml耐热瓶|进样瓶|西林瓶|XRD瓶"
)
# Manual MATERIAL verbs (issue #11). Review verbs are deliberately absent.
_MANUAL_MATERIAL_RE = re.compile(
    r"(?:人工|手动)\s*(?:拿取|搬运|转移|称取|称量|装载|装入|装瓶|重新装载|移出|放入|分装|分样|取样|加入)"
)
# Review/confirmation wording that legitimately mentions 人工 — never flagged.
_REVIEW_CONTEXT_RE = re.compile(r"(?:人工|手动)\s*(?:审核|复核|确认|审查|判读|评审)")

# Feasibility constraints CLAIMING a weighing-capability gap (issue #9 guard).
_WEIGHING_GAP_CLAIM_RE = re.compile(
    r"(?:缺少|缺失|没有|不存在|无)[^，。;；]{0,12}称(?:量|取|样)"
    r"|无法[^，。;；]{0,10}称(?:量|取|样)"
    r"|称(?:量|取|样)[^，。;；]{0,10}(?:无法|不能|不支持)"
)

# Ordinary sample vessels are part of the platform's connected transfer fabric.
# This table is intentionally narrow: rigid carriers, plates, and powder hoppers
# have different physical handling contracts and are excluded below.
_ORDINARY_CONNECTED_CONTAINER_RE = re.compile(
    r"10\s*ml耐压反应管|耐压反应管|反应管|50\s*ml耐热瓶|耐热瓶|"
    r"色谱进样瓶|进样瓶|西林瓶|留样瓶",
    re.IGNORECASE,
)
_SPECIAL_CONTAINER_OR_BOUNDARY_RE = re.compile(
    r"石英孔板|塑料孔板|孔板|料斗|料罐|hopper|碳纸|镍泡沫|刚性载体|"
    r"基底片|XRD\s*(?:载体|基底)|马弗炉",
    re.IGNORECASE,
)
_OBSERVATION_BOUNDARY_RE = re.compile(
    r"observation|data[_ ]?return|数据回传|图谱|谱图|判读|观测|观察结果|"
    r"XRD|PXRD|SEM|TEM|Raman|XAS",
    re.IGNORECASE,
)
_NO_PATH_HANDOFF_RE = re.compile(
    r"no[_ -]?supported[_ -]?container[_ -]?path|无(?:支持的)?容器路径|"
    r"没有.{0,20}(?:转移|容器)(?:路径|链路)|无法.{0,20}(?:转移|换瓶)",
    re.IGNORECASE,
)
_TRANSFER_REASON_CODES = frozenset(
    {
        "operation_input_incompatibility",
        "capacity_split",
        "aliquot",
        "pooling",
        "solid_hopper",
        "xrd_carrier",
        "product_output",
        "minimal_required_transfer",
        "unnecessary_transfer",
    }
)
_STRUCTURED_SPLIT_REASON_CODES = frozenset(
    {"capacity_split", "aliquot", "pooling"}
)


def _container_type_and_id(value: Any) -> Optional[Tuple[str, str]]:
    """Return a conservative (type, identity) tuple for an explicit vessel.

    An identity is mandatory.  A bare ``进样瓶`` cannot prove a transfer cycle,
    so it intentionally returns ``None`` instead of guessing from nearby text.
    """
    if isinstance(value, dict):
        container_type = next(
            (
                value.get(key)
                for key in ("container_type", "容器类型", "type")
                if value.get(key) not in (None, "")
            ),
            None,
        )
        container_id = next(
            (
                value.get(key)
                for key in ("container_id", "容器编号", "id")
                if value.get(key) not in (None, "")
            ),
            None,
        )
        if container_type is None or container_id is None:
            return None
        type_text = _canonical_ordinary_container_type(container_type)
        id_text = re.sub(r"\s+", "", str(container_id)).lower()
        return (type_text, id_text) if type_text and id_text else None

    text = re.sub(r"\s+", "", str(value or ""))
    type_match = _ORDINARY_CONNECTED_CONTAINER_RE.search(text)
    if not type_match:
        return None
    container_type = _canonical_ordinary_container_type(type_match.group(0))
    remainder = text[: type_match.start()] + text[type_match.end() :]
    # Only a compact, unambiguous scalar identity is accepted here.  Ranges and
    # comma-separated sets do not establish a single-sample transfer trace.
    identity_match = re.search(r"(?:编号)?[:：#-]?([A-Za-z]*\d+)$", remainder)
    if not identity_match:
        identity_match = re.match(r"^([A-Za-z]*\d+)(?:，|,|$)", remainder)
    if not identity_match:
        return None
    return container_type, identity_match.group(1).lower()


def _canonical_ordinary_container_type(value: Any) -> str:
    """Collapse truth-source aliases before comparing lineage identities."""
    text = re.sub(r"\s+", "", str(value or "")).lower()
    if "耐压反应管" in text or text == "反应管":
        return "10ml耐压反应管"
    if "耐热瓶" in text:
        return "50ml耐热瓶"
    if "色谱进样瓶" in text:
        return "色谱进样瓶"
    if "进样瓶" in text:
        return "进样瓶"
    if "西林瓶" in text:
        return "西林瓶"
    if "留样瓶" in text:
        return "留样瓶"
    return text


def _lineage_transfer(step: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract one fully declared transfer edge; never infer missing lineage."""
    lineage = step.get("sample_lineage")
    metadata = lineage if isinstance(lineage, dict) else step
    if metadata.get("trace_complete") is not True and metadata.get(
        "lineage_complete"
    ) is not True:
        return None
    sample_id = metadata.get("sample_id") or step.get("sample_id")
    if not isinstance(sample_id, str) or not sample_id.strip():
        return None
    source = _container_type_and_id(metadata.get("source_container"))
    destination = _container_type_and_id(metadata.get("destination_container"))
    if source is None or destination is None or source == destination:
        return None
    reason = str(
        metadata.get("transfer_reason")
        or step.get("transfer_reason")
        or ""
    )
    raw_refs = (
        metadata.get("justification_evidence_refs")
        or metadata.get("evidence_refs")
        or step.get("justification_evidence_refs")
        or []
    )
    if isinstance(raw_refs, str):
        raw_refs = [raw_refs]
    evidence_refs = [
        str(item).strip()
        for item in raw_refs
        if str(item).strip()
    ] if isinstance(raw_refs, list) else []
    return {
        "sample_id": sample_id.strip(),
        "source": source,
        "destination": destination,
        "reason": reason,
        "justification_evidence_refs": evidence_refs,
        "step_number": step.get("plan_step", step.get("step_number", "?")),
    }


def _structured_necessary_transfer_evidence(
    payload: Dict[str, Any],
    edge: Dict[str, Any],
) -> bool:
    """Validate a necessary-transfer label against structured sidecars.

    Free text such as ``capacity_split`` or ``XRD prep`` must not suppress a
    round-trip finding.  Capacity/split/aliquot/merge claims are accepted only
    when the lineage edge references a matching quantity adjustment id; other
    necessity classes remain unverified until an equally deterministic truth
    reference is available.
    """
    reason = str(edge.get("reason", "") or "").strip()
    refs = set(edge.get("justification_evidence_refs", []) or [])
    if reason not in _TRANSFER_REASON_CODES or not refs:
        return False
    if reason not in _STRUCTURED_SPLIT_REASON_CODES:
        # The LLM supplies the semantic class; deterministic code checks only
        # that it is an exact enum and cites evidence.  It never reclassifies
        # free prose with keyword regular expressions.
        return True
    allowed_kinds = {
        "split_transfer",
        "split_batch",
        "capacity_split",
        "aliquot",
        "merge",
        "pool",
    }
    for adjustment in payload.get("quantity_adjustments", []) or []:
        if not isinstance(adjustment, dict):
            continue
        adjustment_id = str(adjustment.get("adjustment_id", "") or "").strip()
        kind = str(adjustment.get("kind", "") or "").strip().lower()
        if adjustment_id in refs and kind in allowed_kinds:
            return True
    return False


def audit_connected_sample_container_chain(payload: Any) -> List[Dict[str, Any]]:
    """Proof-only audit for connected ordinary-vessel paths and transfer loops.

    Two classes are detected:

    * an explicit ordinary-vessel transfer mislabeled as a no-path
      ``offline_handoff``; and
    * A→B→A or repeated same-type vessel changes for one sample, but only
      when every edge explicitly declares a complete lineage trace.

    Findings are Device-local by contract.  Missing lineage, ambiguous vessel
    sets, or unknown handling capability produces no deterministic verdict.
    """
    findings: List[Dict[str, Any]] = []
    if not isinstance(payload, dict):
        return findings

    for index, handoff in enumerate(payload.get("offline_handoffs") or [], start=1):
        if not isinstance(handoff, dict):
            continue
        text = _handoff_text(handoff) + " " + " ".join(
            str(handoff.get(key, "") or "")
            for key in (
                "handoff_type",
                "source_container",
                "destination_container",
                "lineage_mapping",
                "reason",
                "required_state_on_return",
            )
        )
        handoff_type = str(handoff.get("handoff_type", "") or "")
        semantic_class = str(
            handoff.get("semantic_classification", "") or ""
        ).strip()
        source_text = str(handoff.get("source_container", "") or "")
        destination_text = str(handoff.get("destination_container", "") or "")
        no_path_claim = (
            semantic_class == "unsupported_external_operation"
            if semantic_class
            else bool(_NO_PATH_HANDOFF_RE.search(handoff_type + " " + text))
        )
        ordinary_endpoints = bool(
            _ORDINARY_CONNECTED_CONTAINER_RE.search(source_text)
            and _ORDINARY_CONNECTED_CONTAINER_RE.search(destination_text)
        )
        # An observation boundary is exempt only when it is not simultaneously
        # claiming that an explicit ordinary-vessel transfer has no path.
        # Concatenating both labels must not hide a real sample movement.
        is_observation = (
            semantic_class == "observation_data_return"
            if semantic_class
            else bool(
                _OBSERVATION_BOUNDARY_RE.search(handoff_type)
                or _OBSERVATION_BOUNDARY_RE.search(text)
            )
        )
        if is_observation and not (no_path_claim and ordinary_endpoints):
            continue
        if not no_path_claim:
            continue
        # Do not trust a free-form ``contains_sample_handling=false`` flag, or
        # a mention of a later carrier/hopper/XRD step in ``reason``.  Explicit
        # ordinary source+destination vessels prove that this record itself is
        # a sample transfer.  True special boundaries fail the ordinary-vessel
        # test naturally because one endpoint is the carrier/plate/hopper.
        if not ordinary_endpoints:
            continue
        has_complete_lineage = bool(
            str(handoff.get("sample", "") or "").strip()
            and str(handoff.get("lineage_mapping", "") or "").strip()
        )
        findings.append(
            {
                "type": (
                    "ordinary_transfer_misclassified_offline"
                    if has_complete_lineage
                    else "ordinary_transfer_path_requires_lineage_evidence"
                ),
                "handoff_index": index,
                "handoff_name": str(handoff.get("name", ""))[:80],
                "feedback_route": "device",
                "failure_scope": "device_workflow",
                "requires_research_replan": False,
                "evidence": text[:240],
                "message": (
                    f"offline_handoff[{index}]「{str(handoff.get('name', ''))[:40]}」"
                    + (
                        "把具有明确样品谱系的普通容器转移伪装成无路径离线交接。"
                        if has_complete_lineage
                        else "声称普通容器无转移路径，但未提供完整 sample/lineage_mapping。"
                    )
                    +
                    "平台默认容器/物料转移链联通；请在 Device 内改写为一次最短"
                    "在线转移，或补充非普通容器/刚性载体能力缺口的确定证据。"
                ),
            }
        )

    edges_by_sample: Dict[str, List[Dict[str, Any]]] = {}
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        raw_steps = payload.get("device_plan")
    for step in raw_steps or []:
        if not isinstance(step, dict):
            continue
        edge = _lineage_transfer(step)
        if edge is not None:
            edges_by_sample.setdefault(edge["sample_id"], []).append(edge)

    for sample_id, edges in edges_by_sample.items():
        for first, second in zip(edges, edges[1:]):
            if first["destination"] != second["source"]:
                continue  # incomplete/non-contiguous trace: no inference
            claimed_necessary = [
                edge
                for edge in (first, second)
                if edge["reason"] in _TRANSFER_REASON_CODES
                and edge["reason"] not in {
                    "minimal_required_transfer",
                    "unnecessary_transfer",
                }
            ]
            if claimed_necessary:
                if any(
                    _structured_necessary_transfer_evidence(payload, edge)
                    for edge in claimed_necessary
                ):
                    continue
                findings.append(
                    {
                        "type": "unverified_transfer_justification",
                        "sample_id": sample_id,
                        "step_numbers": [
                            first["step_number"],
                            second["step_number"],
                        ],
                        "feedback_route": "device",
                        "failure_scope": "device_workflow",
                        "requires_research_replan": False,
                        "evidence": (
                            first["reason"] + " | " + second["reason"]
                        )[:240],
                        "message": (
                            f"样品 {sample_id} 的容器循环声称属于必要转移，"
                            "但 sample_lineage.justification_evidence_refs 未"
                            "指向匹配的结构化 quantity_adjustments 记录。请在 Device"
                            " 内补齐证据或删除无意义换瓶；不得返回 Research。"
                        ),
                    }
                )
                continue
            is_round_trip = second["destination"] == first["source"]
            is_repeated_same_type = (
                first["source"][0]
                == first["destination"][0]
                == second["destination"][0]
            )
            if not is_round_trip and not is_repeated_same_type:
                continue
            chain = (
                f"{first['source'][0]}#{first['source'][1]} → "
                f"{first['destination'][0]}#{first['destination'][1]} → "
                f"{second['destination'][0]}#{second['destination'][1]}"
            )
            findings.append(
                {
                    "type": (
                        "redundant_container_round_trip"
                        if is_round_trip
                        else "redundant_same_type_container_changes"
                    ),
                    "sample_id": sample_id,
                    "step_numbers": [first["step_number"], second["step_number"]],
                    "feedback_route": "device",
                    "failure_scope": "device_workflow",
                    "requires_research_replan": False,
                    "evidence": chain,
                    "message": (
                        f"样品 {sample_id} 在完整谱系记录中出现无必要的容器链 "
                        f"{chain}（步骤 {first['step_number']}, "
                        f"{second['step_number']}）。请在 Device 内沿用原容器"
                        "或压缩为一次必要转移；不得返回 Research。"
                    ),
                }
            )
    return findings


def _mass_to_grams(value: float, unit: str) -> float:
    unit_lower = unit.lower()
    if unit_lower in ("mg",) or unit == "毫克":
        return value / 1000.0
    return value


def _handoff_text(handoff: Dict[str, Any]) -> str:
    """Concatenate every prose field a handoff may carry (shapes vary by run)."""
    pieces: List[str] = []
    for key in (
        "name", "sample", "procedure", "required_action", "resume_condition",
        "notes", "description",
    ):
        value = handoff.get(key)
        if isinstance(value, str):
            pieces.append(value)
    for key in ("instructions", "required_actions", "required_return_data"):
        value = handoff.get(key)
        if isinstance(value, list):
            pieces.extend(str(item) for item in value)
    return " ".join(pieces)


def _dosable_mass(masses_g: List[float]) -> bool:
    """True when every extracted mass fits at least one weighing station."""
    if not masses_g:
        return False
    for mass in masses_g:
        if not any(low < mass <= high for low, high in MASS_RANGES_G.values()):
            return False
    return True


# ----------------------------------------------------------------------
# 1. offline_handoffs reverse lookup (issue #9 P0)
# ----------------------------------------------------------------------

def audit_offline_handoffs(workflow_json: Any) -> List[Dict[str, Any]]:
    """Flag handoffs the solid-weighing chain could do on-device.

    Conservative by construction: a handoff is flagged ONLY when it (a) uses a
    weighing/aliquoting verb, (b) states at least one concrete mass that fits
    a weighing station's range, (c) targets (or omits, defaulting to vials)
    a container the chain supports, and (d) does NOT touch a true-gap marker
    (石英孔板/马弗炉). Observation-data returns (XRD 图谱 etc.) carry no mass
    dosing semantics and are never flagged.
    """
    findings: List[Dict[str, Any]] = []
    if not isinstance(workflow_json, dict):
        return findings
    handoffs = workflow_json.get("offline_handoffs")
    if not isinstance(handoffs, list):
        return findings

    for index, handoff in enumerate(handoffs, start=1):
        if not isinstance(handoff, dict):
            continue
        semantic_class = str(
            handoff.get("semantic_classification", "") or ""
        ).strip()
        if semantic_class:
            if semantic_class != "unsupported_external_operation":
                continue
            operation_kind = str(
                handoff.get("material_operation_kind", "") or ""
            ).strip()
            if operation_kind != "solid_dosing":
                continue
            requested_quantity = handoff.get("requested_quantity")
            if not isinstance(requested_quantity, dict):
                continue
            try:
                value = float(requested_quantity.get("value"))
            except (TypeError, ValueError):
                continue
            unit = str(requested_quantity.get("unit") or "")
            masses = [_mass_to_grams(value, unit)]
            destination = str(handoff.get("destination_container") or "")
            if (
                _dosable_mass(masses)
                and any(target in destination for target in SOLID_WEIGHING_TARGETS)
                and not any(marker.lower() in destination.lower() for marker in SOLID_TRUE_GAP_MARKERS)
            ):
                findings.append(
                    {
                        "type": "unnecessary_offline_handoff",
                        "handoff_index": index,
                        "handoff_name": str(handoff.get("name", ""))[:80],
                        "evidence": {
                            "semantic_classification": semantic_class,
                            "material_operation_kind": operation_kind,
                            "requested_quantity": requested_quantity,
                            "destination_container": destination,
                        },
                        "message": (
                            f"offline_handoff[{index}] 经 LLM 语义分类为定量固体加样，"
                            "且结构化质量/目标容器落在现有称量链能力范围内；必须改为设备步骤。"
                        ),
                        "route": SOLID_WEIGHING_ROUTE_NOTE,
                    }
                )
            continue
        text = _handoff_text(handoff)
        if not text or not _WEIGHING_VERB_RE.search(text):
            continue
        if any(marker in text.lower() for marker in SOLID_TRUE_GAP_MARKERS):
            continue  # true gap — the handoff is legitimate
        masses = [
            _mass_to_grams(float(match.group(1)), match.group(2))
            for match in _MASS_RE.finditer(text)
        ]
        if not _dosable_mass(masses):
            continue  # no concrete dosable mass — cannot prove automatable
        containers = set(_CONTAINER_RE.findall(text))
        containers.discard("XRD瓶")  # XRD瓶 is prose for 进样瓶
        if containers and not containers.issubset(set(SOLID_WEIGHING_TARGETS) | set(SOLID_TRANSFER_INPUTS)):
            continue  # mentions an unsupported container — stay conservative
        findings.append(
            {
                "type": "unnecessary_offline_handoff",
                "handoff_index": index,
                "handoff_name": str(handoff.get("name", ""))[:80],
                "evidence": text[:160],
                "message": (
                    f"offline_handoff[{index}]「{str(handoff.get('name', ''))[:40]}」"
                    "是 mg 级固体定量称量/分装——设备真源存在完整自动化链路，"
                    "禁止交给人工，必须映射为设备步骤。"
                ),
                "route": SOLID_WEIGHING_ROUTE_NOTE,
            }
        )
    return findings


# ----------------------------------------------------------------------
# 2. manual material-operation scan (issue #11)
# ----------------------------------------------------------------------

def scan_manual_material_operations(
    workflow_json: Any,
    workflow_txt: str = "",
) -> List[Dict[str, Any]]:
    """Flag any manual MATERIAL verb in steps, handoffs, or workflow_txt.

    The 45 stations are physically interconnected — inter-station transfer
    never needs a human. Review wording (人工审核/复核/确认…) is whitelisted.
    """
    findings: List[Dict[str, Any]] = []

    def _scan(text: str, where: str) -> None:
        if not text:
            return
        for match in _MANUAL_MATERIAL_RE.finditer(text):
            start = max(0, match.start() - 8)
            window = text[start : match.end() + 12]
            if _REVIEW_CONTEXT_RE.search(window):
                continue
            findings.append(
                {
                    "type": "forbidden_manual_material_operation",
                    "where": where,
                    "evidence": window.strip()[:120],
                    "message": (
                        f"{where} 出现人工物料操作「{match.group(0)}」——45 个工作站"
                        "物理联通，物料/样品转移不需要人类参与，禁止人工物料步骤。"
                    ),
                }
            )

    if isinstance(workflow_json, dict):
        for step in workflow_json.get("steps") or []:
            if isinstance(step, dict):
                _scan(
                    str(step.get("operation", "")) + " " + str(step.get("notes", "")),
                    f"第 {step.get('step_number', '?')} 步",
                )
        for index, handoff in enumerate(workflow_json.get("offline_handoffs") or [], start=1):
            if isinstance(handoff, dict):
                _scan(_handoff_text(handoff), f"offline_handoff[{index}]")
    _scan(str(workflow_txt or ""), "workflow_txt")
    return findings


# ----------------------------------------------------------------------
# 3. weighing false-hard guard (issue #9, feasibility side)
# ----------------------------------------------------------------------

def weighing_false_hard_guard(constraint_text: str) -> bool:
    """True when a hard-classified constraint claims a weighing gap that the
    truth source disproves (weighing chain exists) — such a claim must be
    downgraded to unverifiable/needs_human_review, never physical_infeasible.

    Constraints citing the true gap (石英孔板/马弗炉 container path) are left
    untouched: those are genuinely hard (C01 across all archived runs).
    """
    text = str(constraint_text or "")
    if not text:
        return False
    lowered = text.lower()
    if any(marker in lowered for marker in SOLID_TRUE_GAP_MARKERS):
        return False
    return bool(_WEIGHING_GAP_CLAIM_RE.search(text))
