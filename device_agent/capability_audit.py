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
from typing import Any, Dict, List, Tuple

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
