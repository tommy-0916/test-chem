"""Single-agent device mapper.

This is the simplified device layer used after research-agent planning:
research macro actions + device truth -> workstation workflow.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import re
import uuid
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from langchain_core.messages import HumanMessage, SystemMessage
from agent_skills.responses_diagnostics import (
    format_responses_failure,
    get_responses_diagnostics,
)
from agent_skills.llm_retry import (
    LogicalCallDeadlineExceeded,
    NonRetryableGatewayError,
    RetryableGatewayError,
    call_with_gateway_retry,
    is_retryable_gateway_error,
    is_terminal_gateway_error,
    safe_gateway_error_code,
    safe_gateway_error_metadata,
)
from agent_skills.llm_timing import measure_llm_request
try:
    from .feasibility_fragments import (
        FeasibilityFragmentError,
        build_fragment_instruction,
        build_fragment_request_context,
        merge_fragment,
    )
except ImportError:
    from feasibility_fragments import (
        FeasibilityFragmentError,
        build_fragment_instruction,
        build_fragment_request_context,
        merge_fragment,
    )
try:
    from .checkpoints import (
        DeviceCheckpointStore,
        digest as checkpoint_digest,
        file_digest as checkpoint_file_digest,
        implementation_digest,
    )
except ImportError:
    from checkpoints import (
        DeviceCheckpointStore,
        digest as checkpoint_digest,
        file_digest as checkpoint_file_digest,
        implementation_digest,
    )

from utils.paths import chem_resources_root, format_reference_path, workstation_dir
from utils.workstation_loader import WorkstationLoader

from workflow_normalizer import SkillContractEngine
try:
    from .skill_loading import WorkstationSkillSession, WorkstationTruthChangedError, invoke_with_tools
except ImportError:
    from skill_loading import WorkstationSkillSession, WorkstationTruthChangedError, invoke_with_tools

from feasibility_rules import (
    classify_feasibility_result,
    has_temporal_addition_stirring as _has_temporal_addition_stirring,
    json_text as _json_text,
    scrub_implicit_connectivity_constraint,
    soft_temporal_mapping_error as _soft_temporal_mapping_error,
)
from dispatch_formatter import (
    CURATED_OPERATION_ALIASES,
    DispatchCatalog,
    complete_required_fields,
    format_dispatch_payload,
)
try:  # package import (orchestrator / run_from_research_state)
    from .human_quantity_approval import (
        ValidatedHumanQuantityApprovalBundle,
        approved_quantity_contract_digest,
        consume_validated_human_quantity_approval_bundle,
    )
except ImportError:  # direct script / legacy tests
    from human_quantity_approval import (
        ValidatedHumanQuantityApprovalBundle,
        approved_quantity_contract_digest,
        consume_validated_human_quantity_approval_bundle,
    )
from capability_audit import (
    SOLID_WEIGHING_ROUTE_NOTE,
    audit_connected_sample_container_chain,
    audit_offline_handoffs,
    scan_manual_material_operations,
    weighing_false_hard_guard,
)
from workflow_validator import WorkflowValidator, structure_validation_errors
from recipe_materializer import (
    RecipeMaterializationError,
    materialize_workflow_recipe_files,
)
try:
    from .v2_validation import (
        build_validation_issues_v2,
        chunk_hashes,
        device_step_hashes,
        locked_chunk_violations,
        merge_scoped_device_step_repair,
    )
except ImportError:
    from v2_validation import (
        build_validation_issues_v2,
        chunk_hashes,
        device_step_hashes,
        locked_chunk_violations,
        merge_scoped_device_step_repair,
    )

logger = logging.getLogger(__name__)


def _is_gateway_failure(exc: BaseException) -> bool:
    _, http_status = safe_gateway_error_metadata(exc)
    return bool(
        get_responses_diagnostics(exc) is not None
        or isinstance(
            exc,
            (
                LogicalCallDeadlineExceeded,
                NonRetryableGatewayError,
                RetryableGatewayError,
            ),
        )
        or http_status is not None
        or is_retryable_gateway_error(exc)
        or is_terminal_gateway_error(exc)
    )


def _safe_model_failure_text(exc: BaseException) -> str:
    """Format model failures without persisting SDK bodies or request URLs."""

    responses_text = format_responses_failure(exc)
    if responses_text:
        return responses_text
    error_type, http_status = safe_gateway_error_metadata(exc)
    if _is_gateway_failure(exc):
        classification = (
            "terminal"
            if is_terminal_gateway_error(exc)
            else "retryable"
            if is_retryable_gateway_error(exc)
            else "non_retryable"
        )
        status = f" http_status={http_status}" if http_status is not None else ""
        return (
            f"Gateway request failed: {error_type}{status} "
            f"classification={classification}"
        )
    return str(exc)


DEFAULT_WORKFLOW_REPAIR_LIMIT = 8
DEFAULT_STAGE1_PLAN_REPAIR_LIMIT = 3
DEFAULT_JSON_FORMAT_RETRY_LIMIT = 2
DEFAULT_SEMANTIC_CONTRACT_REPAIR_LIMIT = 2
FEASIBILITY_CERTIFICATE_VERSION = "2.2"


class DeviceConfigurationError(RuntimeError):
    """Explicit configuration cannot safely support the Device workflow."""

SEMANTIC_ANALYSIS_SYSTEM_PROMPT = """
你是 Device 层的独立语义审查器。你的职责不是生成设备计划，而是阅读完整用户目标、
Research macro action、前后步骤和完整工作站摘要，把自然语言语义转换成结构化合同。

必须基于完整上下文判断，禁止只看单个关键词。尤其要正确处理否定、历史状态名、下游用途、
复合操作和同名材料。你负责判断：
1. 每个 macro step 真正需要的设备能力类别；
2. 它是否执行核心化学、是否只是 observation；
3. 每个 quantity_requirement 的语义；
4. Research 物料身份及可供 Device 引用的稳定 identity_id；
5. 明确的温度+气氛等联合能力要求。

你只输出一个 JSON object，不输出设备计划、工作流、Markdown 或解释性前后缀。
""".strip()

SEMANTIC_ANALYSIS_TASK_PROMPT = """
请根据完整上下文输出以下结构。每个 Research macro step 必须且只能出现一次。

## Research handoff
{research_handoff_json}

## 完整工作站能力摘要
{workstation_descriptions}

## 输出结构
{{
  "status": "semantic_analysis",
  "macro_step_assessments": [
    {{
      "source_macro_step": "Research 步骤标量 ID",
      "reason": "结合前后步骤的总体判断",
      "evidence_refs": ["macro_action_steps[i].操作", "macro_action_steps[i].参数"],
      "required_capabilities": [
        {{
          "category": "reaction | drying | calcination | purification | liquid_handling | stirring | liquid_pouring | ultrasonic_liquid_handling | ultrasonic_dispersion"
        }}
      ],
      "core_chemistry": {{"value": true}},
      "observation_only": {{"value": false}},
      "quantity_semantics": [
        {{
          "requirement_index": 0,
          "kind": "scientific_input_setpoint | target_dose | whole_batch | runtime_measured_inventory",
          "material_identity_id": "稳定物料 ID"
        }}
      ],
      "material_identities": [
        {{
          "identity_id": "稳定且简短的 ID",
          "canonical_name": "完整物料/试剂身份",
          "role": "reagent | sample | product | solvent | carrier | mixture",
          "aliases": ["原文中的等价写法"]
        }}
      ],
      "joint_requirements": [
        {{
          "kind": "temperature_atmosphere",
          "process": "hydrogen_reduction | oxidation | inert_heat_treatment | other",
          "temperature_c": 450,
          "atmosphere": "H2/Ar",
          "required": true
        }}
      ]
    }}
  ]
}}

规则：
- `required_capabilities` 可为空，但不得用空数组逃避原文明确要求的动作；复合操作逐项列出。
- “不加入/不干燥/避免煅烧”不是正向要求；“干燥粉末/反应液”等状态名也不是动作。
- quantity_semantics 必须逐项覆盖现有 quantity_requirements；不得漏项或增加不存在的索引。
- 同一物料跨步骤使用相同 identity_id；不同物料不得共享 identity_id。
- canonical_name 可以保留各步骤原文中的名称变体；稳定 identity_id 才是跨步骤绑定主键。
- core_chemistry 与 observation_only 不能同时为 true。
- 每个 macro step 只要求顶层 reason 非空；它覆盖本步骤内的全部语义判断。
  子项可在确有额外依据时补充自己的 reason/evidence_refs，但不是重复必填字段。
- 顶层 evidence_refs 可选；Device 会根据 source_macro_step 机械绑定到冻结 Research step，
  不要求 LLM 重复抄写可确定推导的路径。quantity_requirement 的 requirement_index 本身
  也是到冻结 Research 数量项的结构化引用。
""".strip()

_SEMANTIC_CAPABILITY_CATEGORIES = frozenset(
    {
        "reaction",
        "drying",
        "calcination",
        "purification",
        "liquid_handling",
        "stirring",
        "liquid_pouring",
        "ultrasonic_liquid_handling",
        "ultrasonic_dispersion",
    }
)

_SEMANTIC_QUANTITY_KINDS = frozenset(
    {
        "scientific_input_setpoint",
        "target_dose",
        "whole_batch",
        "runtime_measured_inventory",
    }
)

_JSON_FORMAT_RETRY_INSTRUCTION = """
上一响应因 JSON 格式不唯一或不完整已被丢弃。请重新生成同一个候选，且只输出一个
完整 JSON object：不要输出 Markdown code fence、解释性前后缀、第二个备选/修正版
JSON、workflow_txt 的重复副本或任何尾随结构化片段。不得借格式重试改变 Research
macro plan、样品/对照矩阵、试剂身份与顺序、observation point 或设备真源结论。
原始任务与全部证据已在本请求中；不要声称看不到上一响应，直接从原始输入重算。
第一个非空白字符必须是 `{`，最后一个非空白字符必须是 `}`。这只是
Device 同层格式重试，不是新的计划候选或 workflow 修改轮次。
""".strip()

_JSON_FORMAT_FINAL_RETRY_INSTRUCTION = """
这是最后一次 Device 同层序列化重试。请从原始请求重新计算，输出紧凑但完整的
单一 JSON object。所有契约字段、完整 device plan/workflow、数量与样品谱系都必须
保留；只缩短 notes、reason 和 self-check 的自然语言，禁止重复复述 Research 方案。
不要输出道歉、拒绝、格式说明、Markdown、数组或第二个顶层值。
""".strip()

_SCIENTIFIC_REVIEW_ADJUSTMENT_KINDS = {
    "replicate_batch",
    "scale_out_for_minimum",
    "concentration_change",
    "molar_ratio_change",
    "amount_change",
    "parameter_change",
}
_MECHANICAL_ADJUSTMENT_KINDS = {
    "unit_conversion",
    "split_transfer",
    "split_batch",
    "capacity_split",
    "container_change",
    "slot_reallocation",
}
_ALLOWED_ADJUSTMENT_KINDS = (
    _SCIENTIFIC_REVIEW_ADJUSTMENT_KINDS
    | _MECHANICAL_ADJUSTMENT_KINDS
    | {"device_operational"}
)

# Exact truth-source identifiers only.  This intentionally is not a keyword
# matcher: a plan step is forced to declare material lineage only when its
# workstation is one of the controlled processing stations below, or when the
# step itself explicitly declares a material event.  The Chinese aliases are
# the display names present in workstation_capability_index.json.
_CONTROLLED_MATERIAL_PROCESSING_WORKSTATIONS = frozenset(
    {
        "Centrifuge_V1",
        "离心机_V1",
        "Purification_Workstation_V1",
        "纯化工作站_V1",
        "Drying_Oven_V1",
        "烘干机_V1",
        "Muffle_Furnace_V1",
        "马弗炉_V1",
        "High_Temperature_High_Pressure_Microreaction_Platform_V1",
        "高温高压微反应平台_V1",
        "Post_Reaction_Processing_Platform_V1",
        "反应后处理平台_V1",
        "Photocatalysis_Workstation_V1",
        "光催化工作站_V1",
        "Photocatalysis_Workstation_V2",
        "光催化工作站_V2",
    }
)
_ALLOWED_MATERIAL_EVENT_KINDS = frozenset(
    {
        "state_change",
        "split_same_material",
        "replicate_same_material",
        "process_same_material",
    }
)
_CONDITIONAL_REACTION_PROCESSING_WORKSTATIONS = frozenset(
    {
        "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
        "常温磁力搅拌工作站_V1",
        "Heating_Magnetic_Stirring_Workstation_V1",
        "加热磁力搅拌工作站_V1",
    }
)
_REACTION_STATE_CHANGE_WORKSTATIONS = frozenset(
    set(_CONDITIONAL_REACTION_PROCESSING_WORKSTATIONS)
    | {
        "High_Temperature_High_Pressure_Microreaction_Platform_V1",
        "高温高压微反应平台_V1",
        "Photocatalysis_Workstation_V1",
        "光催化工作站_V1",
        "Photocatalysis_Workstation_V2",
        "光催化工作站_V2",
    }
)
_DRYING_STATE_CHANGE_WORKSTATIONS = frozenset(
    {
        "Drying_Oven_V1",
        "烘干机_V1",
    }
)
_CALCINATION_STATE_CHANGE_WORKSTATIONS = frozenset(
    {
        "Muffle_Furnace_V1",
        "马弗炉_V1",
    }
)
_PURIFICATION_STATE_CHANGE_WORKSTATIONS = frozenset(
    {
        "Centrifuge_V1",
        "离心机_V1",
        "Purification_Workstation_V1",
        "纯化工作站_V1",
    }
)
_LIQUID_HANDLING_WORKSTATIONS = frozenset(
    {
        "Cleaning_and_Dispensing_Workstation_V1",
        "Liquid_Handling_Station_1ml_V1",
        "Liquid_Handling_Station_1ml_V2",
        "Liquid_Handling_Station_5ml_V1",
        "Liquid_Handling_Station_5ml_V2",
        "Liquid_Handling_Station_5ml_V3",
        "Liquid_Handling_Station_4Channel_V1",
        "Ultrasonic_Liquid_Handling_Workstation_V1",
    }
)
_LIQUID_POURING_WORKSTATIONS = frozenset(
    {"Liquid_Pouring_Workstation_V1"}
)
_ULTRASONIC_LIQUID_HANDLING_WORKSTATIONS = frozenset(
    {"Ultrasonic_Liquid_Handling_Workstation_V1"}
)
_ULTRASONIC_DISPERSION_WORKSTATIONS = frozenset(
    {"Ultrasonic_Disperser_V1", "Ultrasonic_Disperser_V2"}
)
_STIRRING_PROCESS_WORKSTATIONS = frozenset(
    {
        "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
        "Heating_Magnetic_Stirring_Workstation_V1",
    }
)
_ALL_STATE_CHANGE_WORKSTATIONS = frozenset(
    set(_REACTION_STATE_CHANGE_WORKSTATIONS)
    | set(_DRYING_STATE_CHANGE_WORKSTATIONS)
    | set(_CALCINATION_STATE_CHANGE_WORKSTATIONS)
    | set(_PURIFICATION_STATE_CHANGE_WORKSTATIONS)
)
_FROZEN_REACTION_OPERATION_PATTERN = re.compile(
    r"反应|生成|合成|共沉淀|熟化|浸渍|水热|聚合|氧化|还原|"
    r"reaction|synthesi[sz]e|precipitat|aging|impregnat|"
    r"polymeri[sz]e|oxid|reduc",
    re.I,
)
_FROZEN_DRYING_OPERATION_PATTERN = re.compile(r"干燥|烘干|dry", re.I)
_FROZEN_CALCINATION_OPERATION_PATTERN = re.compile(
    r"煅烧|热解|焙烧|calcina|pyroly|roast", re.I
)
_FROZEN_PURIFICATION_OPERATION_PATTERN = re.compile(
    r"纯化|洗涤|离心|分离|purif|wash|centrif|separat", re.I
)
_FROZEN_LIQUID_HANDLING_PATTERN = re.compile(
    r"配制|配液|分装|加液|移液|滴加|定容|aliquot|dispens|pipett|"
    r"prepare\s+(?:a\s+)?(?:stock\s+)?solution",
    re.I,
)
_FROZEN_STIRRING_PATTERN = re.compile(r"搅拌|stirr?", re.I)
_FROZEN_LIQUID_POURING_PATTERN = re.compile(
    r"倾倒|倒出|去上清|pour|decant", re.I
)
_FROZEN_ULTRASONIC_LIQUID_PATTERN = re.compile(
    r"超声(?:加液|移液)|ultrasonic.{0,16}(?:dispens|pipett)", re.I
)
_FROZEN_ULTRASONIC_DISPERSION_PATTERN = re.compile(
    r"超声(?:分散|重悬|清洗)|ultrasonic.{0,16}(?:dispers|resuspend|clean)",
    re.I,
)
_GENERIC_PREPARATION_ARTIFACT_PATTERN = re.compile(
    r"XRD|表征|沉积样品|扫描样品|测试样品|浆液|墨水|电极|原液|"
    r"反应液|悬浊液|溶液|对照|基底|sample|slurry|ink|electrode|stock\s+solution",
    re.I,
)
_NEGATED_PROCESSING_TERM_PATTERN = re.compile(
    r"(?:"
    r"(?:避免|禁止|严禁)(?:进行|采用|使用)?(?:高温)?\s*|"
    r"(?:无需|无须|不需要|不进行|不要进行|不应进行|不得进行|"
    r"不采用|不使用)(?:高温)?\s*|"
    r"(?:avoid(?:ing)?|without|do\s+not|must\s+not|shall\s+not)\s+"
    r")"
    r"(反应|生成|合成|共沉淀|沉淀|熟化|浸渍|水热|聚合|氧化|还原|"
    r"煅烧|热解|焙烧|纯化|洗涤|离心|分离|干燥|烘干|搅拌|"
    r"reaction|synthesi[sz]e|precipitat|aging|impregnat|polymeri[sz]e|"
    r"oxid|reduc|calcina|pyroly|roast|purif|wash|centrif|separat|dry|stirr?)",
    re.I,
)
_NEGATED_PARAMETER_ACTION_PATTERN = re.compile(
    r"\*{0,2}\s*(?:不|无需|无须|不要|不得|禁止|避免)"
    r"(?:再|进行|采用|使用)?\s*"
    r"(?:加入|滴加|分批加入|移取|分装|混合|搅拌|"
    r"超声(?:分散|重悬|清洗|加液|移液)|倾倒|倒出|去除上清|弃去上清)"
    r"\s*\*{0,2}",
    re.I,
)
_FROZEN_SYNTHESIS_OPERATION_PATTERN = re.compile(
    r"反应|生成|制备|合成|共沉淀|熟化|浸渍|水热|聚合|氧化|还原|"
    r"煅烧|热解|纯化|洗涤|干燥|reaction|synthesi[sz]e|precipitat|aging|"
    r"impregnat|purif|wash|dry|oxid|reduc|calcina|pyroly|roast|"
    r"polymeri[sz]e|centrif|separat",
    re.I,
)
_FROZEN_EXTERNAL_MATERIAL_PATTERN = re.compile(
    r"外部预配|外部预制|预先制备|已制备|预制|预配|直接采购|商业购买|"
    r"external(?:ly)?\s+prepared|pre[- ]?made|pre[- ]?prepared|"
    r"commercial(?:ly)?\s+(?:available|purchased)|as[- ]received",
    re.I,
)
_FROZEN_HARD_STATE_CHANGE_OPERATION_PATTERN = re.compile(
    r"反应|生成|合成|共沉淀|熟化|浸渍|水热|聚合|氧化|还原|"
    r"煅烧|热解|纯化|洗涤|干燥|reaction|synthesi[sz]e|precipitat|"
    r"aging|impregnat|purif|wash|dry|oxid|reduc|calcina|pyroly|roast|"
    r"polymeri[sz]e|centrif|separat",
    re.I,
)
_FROZEN_EXTERNAL_DISTRIBUTION_OPERATION_PATTERN = re.compile(
    r"拿取|取用|分配|装载|上料|移取|dispens|load|retrieve|aliquot",
    re.I,
)
_DEVICE_STATE_CHANGE_INTENT_PATTERN = re.compile(
    r"反应|共沉淀|沉淀|沉积|浸渍|熟化|生成|合成|聚合|水热|"
    r"氧化|还原|煅烧|热解|reaction|precipitat|deposit|"
    r"impregnat|aging|synthesi[sz]e|polymeri[sz]e",
    re.I,
)


def _required_state_change_workstation_categories(
    operation: str,
) -> Dict[str, frozenset[str]]:
    """Return every independently required frozen processing category.

    The operation text is frozen Research input; Device-supplied objective or
    intent never expands these exact truth-source sets.  Composite operations
    retain every category (for example reaction *and* calcination), rather
    than accepting one station from a flattened union.  Generic ``制备`` maps
    to one generic category whose station set is the union of known
    state-changing stations; passive material/container/transfer stations are
    never members.
    """

    requirements: Dict[str, frozenset[str]] = {}
    if _FROZEN_REACTION_OPERATION_PATTERN.search(operation):
        requirements["reaction"] = _REACTION_STATE_CHANGE_WORKSTATIONS
    if _FROZEN_DRYING_OPERATION_PATTERN.search(operation):
        requirements["drying"] = _DRYING_STATE_CHANGE_WORKSTATIONS
    if _FROZEN_CALCINATION_OPERATION_PATTERN.search(operation):
        requirements["calcination"] = _CALCINATION_STATE_CHANGE_WORKSTATIONS
    if _FROZEN_PURIFICATION_OPERATION_PATTERN.search(operation):
        requirements["purification"] = _PURIFICATION_STATE_CHANGE_WORKSTATIONS
    if (
        not requirements
        and re.search(r"制备|fabricat|prepare", operation, re.I)
        and not _GENERIC_PREPARATION_ARTIFACT_PATTERN.search(operation)
    ):
        requirements["generic_state_change"] = _ALL_STATE_CHANGE_WORKSTATIONS
    return requirements


def _frozen_required_processing_text(macro: Dict[str, Any]) -> str:
    """Frozen positive processing semantics from Research operation+params.

    Parameters often contain mandatory stages omitted from the short operation
    label (for example A02's 60 °C drying between XRD deposition layers).  A
    narrow negation scrub removes explicitly forbidden processing such as
    ``避免高温煅烧`` without treating ``不得省略干燥`` as a negation of
    drying itself.
    """

    operation = str(macro.get("操作", macro.get("operation", "")) or "")
    operation = _NEGATED_PROCESSING_TERM_PATTERN.sub(" ", operation)
    # Material/state nouns are not execution predicates.  In particular the
    # real A01/A02 plans use these phrases in sample names and retrospective
    # clauses; treating them as actions creates bogus reaction/drying stages.
    operation = re.sub(r"未反应", "未处理", operation)
    operation = re.sub(r"(?:前驱体)?反应液", "前驱体液", operation)
    operation = re.sub(
        r"(?:水热)?反应(?:结束)?后|(?:水热)?反应所得",
        "处理后",
        operation,
    )
    operation = re.sub(r"干燥(?:催化剂)?粉末", "粉末", operation)
    passive_external_distribution = bool(
        _FROZEN_EXTERNAL_MATERIAL_PATTERN.search(operation)
        and _FROZEN_EXTERNAL_DISTRIBUTION_OPERATION_PATTERN.search(operation)
        and not _FROZEN_HARD_STATE_CHANGE_OPERATION_PATTERN.search(operation)
    )

    # A passive root-material distribution macro may quote the downstream
    # consumer schedule in its parameters (for example "每次 3 mL，重复洗涤
    # 3 次").  Those words describe use of the externally prepared stock, not
    # a purification state transition performed by this macro.  Preserve the
    # physical liquid-dispensing requirement when the operation itself names
    # a liquid, but do not promote downstream parameter prose into state
    # categories.
    if passive_external_distribution:
        liquid_marker = (
            " 分装"
            if re.search(r"洗液|原液|溶液|液体|solution|stock", operation, re.I)
            else ""
        )
        return (operation + liquid_marker).strip()

    parameters = _json_text(
        macro.get("参数", macro.get("parameters", ""))
    )
    parameters = _NEGATED_PROCESSING_TERM_PATTERN.sub(" ", parameters)
    # Action-level negation must be removed before positive marker matching.
    # The real A02 handoff contains Markdown-emphasised ``**不加入**泡沫镍``;
    # that prohibition cannot become a liquid-handling requirement.
    parameters = _NEGATED_PARAMETER_ACTION_PATTERN.sub(" ", parameters)
    parameters = re.sub(r"未反应", "未处理", parameters)
    parameters = re.sub(r"(?:前驱体)?反应液", "前驱体液", parameters)
    parameters = re.sub(
        r"(?:水热)?反应(?:结束)?后|(?:水热)?反应所得",
        "处理后",
        parameters,
    )
    parameters = re.sub(r"干燥(?:催化剂)?粉末", "粉末", parameters)

    # Parameters contribute only positive action-shaped evidence.  Bare
    # mentions (future use, material state, analytical description) are not
    # copied into the classification text.
    action_markers: List[str] = []
    if re.search(
        r"(?:在|于)?\s*\d+(?:\.\d+)?\s*(?:°\s*C|°C|℃).{0,12}"
        r"(?:干燥|烘干)\s*\d|(?:干燥|烘干)\s*\d+(?:\.\d+)?\s*"
        r"(?:min|h|分钟|小时)",
        parameters,
        re.I,
    ):
        action_markers.append("干燥")
    if re.search(
        r"(?:在|于)?\s*\d+(?:\.\d+)?\s*(?:°\s*C|°C|℃).{0,12}"
        r"(?:煅烧|热解|焙烧)\s*\d|(?:煅烧|热解|焙烧)\s*\d+(?:\.\d+)?\s*"
        r"(?:min|h|分钟|小时)",
        parameters,
        re.I,
    ):
        action_markers.append("煅烧")
    if re.search(
        r"\d+(?:\.\d+)?\s*(?:×\s*g|rpm).{0,16}离心|"
        r"离心\s*\d+(?:\.\d+)?|(?:洗涤|纯化)\s*\d+\s*次|"
        r"重复洗涤\s*\d*\s*次?",
        parameters,
        re.I,
    ):
        action_markers.append("洗涤离心")
    if re.search(
        r"加入|滴加|分批加入|移取|分装|依次混合|"
        r"制成.{0,16}(?:浆液|溶液|悬浊液)|"
        r"(?:取|用)\s*\d+(?:\.\d+)?\s*(?:mL|μL|uL).{0,48}混合",
        parameters,
        re.I,
    ):
        action_markers.append("加液")
    if re.search(
        r"\d+(?:\.\d+)?\s*rpm.{0,24}搅拌|"
        r"(?:继续|室温.{0,16})搅拌|搅拌(?:\s*\d|至|下|并|和|熟化)",
        parameters,
        re.I,
    ):
        action_markers.append("搅拌")
    if re.search(r"熟化\s*\d+(?:\.\d+)?", parameters, re.I):
        action_markers.append("熟化")
    if re.search(r"弃去上清|去除上清|倾倒|倒出|decant", parameters, re.I):
        action_markers.append("倾倒去上清")
    if re.search(r"超声(?:分散|重悬|清洗)\s*\d", parameters, re.I):
        action_markers.append("超声分散")
    if re.search(
        r"超声(?:加液|移液)|ultrasonic.{0,16}(?:dispens|pipett)",
        parameters,
        re.I,
    ):
        action_markers.append("超声加液")
    return " ".join([operation, *action_markers]).strip()


def _required_non_state_workstation_categories(
    frozen_processing_text: str,
) -> Dict[str, frozenset[str]]:
    """Exact workstation categories for frozen liquid/mechanical actions."""

    requirements: Dict[str, frozenset[str]] = {}
    if _FROZEN_LIQUID_HANDLING_PATTERN.search(frozen_processing_text):
        requirements["liquid_handling"] = _LIQUID_HANDLING_WORKSTATIONS
    if _FROZEN_STIRRING_PATTERN.search(frozen_processing_text):
        requirements["stirring"] = _STIRRING_PROCESS_WORKSTATIONS
    if _FROZEN_LIQUID_POURING_PATTERN.search(frozen_processing_text):
        requirements["liquid_pouring"] = _LIQUID_POURING_WORKSTATIONS
    if _FROZEN_ULTRASONIC_LIQUID_PATTERN.search(frozen_processing_text):
        requirements[
            "ultrasonic_liquid_handling"
        ] = _ULTRASONIC_LIQUID_HANDLING_WORKSTATIONS
    if _FROZEN_ULTRASONIC_DISPERSION_PATTERN.search(
        frozen_processing_text
    ):
        requirements[
            "ultrasonic_dispersion"
        ] = _ULTRASONIC_DISPERSION_WORKSTATIONS
    return requirements


def _allowed_state_change_workstations_for_frozen_operation(
    operation: str,
) -> frozenset[str]:
    """Compatibility union; coverage gates use per-category requirements."""

    requirements = _required_state_change_workstation_categories(operation)
    return frozenset(
        station
        for allowed_stations in requirements.values()
        for station in allowed_stations
    )


def _needs_temporal_mapping_retry(result: Dict[str, Any], handoff: Dict[str, Any]) -> bool:
    """Plan-stage result whose self-check still complains about addition timing."""
    if str(result.get("status", "")).strip().lower() not in {"success", "device_plan"}:
        return False
    if not _has_temporal_addition_stirring(handoff):
        return False
    self_check = result.get("device_self_check", {})
    return bool(
        isinstance(self_check, dict)
        and re.search(r"fail|失败|不通过|未通过", _json_text(self_check))
        and re.search(r"addition|加液|滴加|搅拌|节拍|同步", _json_text(self_check))
    )


FEASIBILITY_PLAN_SYSTEM_PROMPT = """
你是化学自动化平台的 device feasibility & planning agent（设备两段流程的第一段）。
你只做一件事：判断 research agent 的化学语义 macro action 能否由当前设备完成，并产出
**比 workflow_json 更高一级的 device_plan**（设备级计划）。你不写 workflow_json——
那是第二段 translation agent 的职责；你保证的是"每一步都有真实工作站能做、容器/体积/
配平/时序在设备上成立"。

职责边界：
1. 输入包含 query、stage、macro_action_steps、设备/器材真源。
2. research agent 已经根据设备和器材信息尽量规划可做的化学路线，但它仍只输出 macro action，不负责工作站细节。
3. 你负责选择具体工作站、容器策略、试剂策略、开盖/关盖时机、分瓶/配平、重复洗涤、干燥和离线 handoff 的**计划级决策**；具体参数字段名和 JSON 结构交给第二段。
   Research macro step 的 material_inputs/material_outputs、container_requirements 和
   intermediate_returns 是上游物料、逻辑容器和回传边界合同，必须逐项保留并实现。
   container_requirements.logical_container_id 不是实体瓶号或托盘槽位；在 container_plan
   中保留 logical_container_id，并另行分配实体容器与 batch/slot 对应。不得把逻辑编号
   直接写入机器参数，也不得把没有声明测量能力的中间返回当作可用实测库存。
4. 只有当当前设备真源无法实现某个必要化学动作或强制科学条件时，才返回 device_feasibility_error。
   先把 macro action 的每个实验参数分类，再决定它是不是阻塞：
   - device_dispatch_field（设备下发字段）：真源参数表中存在同义字段，写入该步骤的 关键参数；
   - fixed_device_capability（固定设备能力）：设备固有且无需下发的能力（如 XRD 的辐射源、
     环境常温），在 parameter_disposition 中说明依据，不作为阻塞；
   - derived_process_constraint（可派生流程约束）：可以用多个受支持步骤组合实现的过程语义
     （如“边滴入边搅拌”→分批加液+批次间搅拌，“缓慢滴加”→小份多次加液），必须派生实现并说明；
   - offline_condition（离线条件）：仅限两类——(a) 观测数据回传（如 XRD 图谱/判读结果），
     (b) 真源确实无容器路径的操作（如向 96位石英孔板 定量装粉）。写入 offline_handoffs。
     **mg 级固体称量/分装永远不属于离线条件**（见下方固体称量链路规则）；
     用户提供的刚性基底/载体（如镍泡沫）若没有任何工作站 operation 接受其样品状态，
     其清洗、干燥或装管属于真实输入能力缺口：必须把“已在外部完成前处理并按明确状态交入”
     写成流程起点的 offline_handoff，从首个兼容 operation 开始自动化。不得先取得空瓶，再用
     notes 或后续 handoff 把空瓶伪装成已经含有该载体。这个输入边界不等于工作站之间的人工搬运。
     同样地，若自动反应结束后产物仍负载在刚性载体上，而完整真源没有任何 operation 接受
     该载体完成取出、洗涤、干燥或刮取，则这是化学处理能力缺口，不是运输链缺口：把从
     反应输出到“已处理并装入首个兼容容器”的整段写入 offline_handoffs，设备流程从首个
     真实兼容 operation 恢复。绝不能把刚性载体改称悬浊液/沉淀后送入离心、纯化或倾倒站。
     Research macro step 自身要求的还原、氧化、煅烧、退火、水热/溶剂热或其他核心化学
     反应不得用 offline_handoff 代替。若完整真源不存在单一工作站同时满足其必要温度、
     气氛与安全条件，按硬路线缺口返回 feasibility_error；不得用“离线处理后重新装载”
     绕过路线可行性门。纯 XRD/XPS/显微等 observation 数据回传不受此限制。
   - uncontrollable_mandatory（不可控强制条件）：真源既不能下发、不能派生、也没有固定能力
     覆盖，且科学上必须满足——只有这一类才可能构成阻塞；若不确定是否满足，标记
     requires_review=true 交人工审核，而不是直接判不可行。
   仅仅“真源参数表中没有同名字段”绝不是 feasibility_error 的理由。
   设备 Skill 的正确解释规则（三类参数）：Skill 中列出的设备和操作**默认存在且可正常运行**；
   (1) 论文/Research 参考参数——说明实验依据，不一定需要下发；
   (2) 设备固定参数——设备运行时自动采用（如 XRD 的辐射源、扫描起止角），你不填写，
       也绝不能因为它们没有出现在可下发参数表中而判定设备不可行；
   (3) 设备开放参数——Skill 参数表列出的字段，只有这一类需要写入计划并严格满足取值范围。
6. 不要因为 macro action 缺少容器编号、工作站名、原液瓶位、开盖/关盖、偶数进样瓶配平、重复洗涤子步骤而拒绝；这些都由你在计划级补全。
7. 如果 macro action 明确把 XRD/PXRD/SEM/TEM/Raman/XAS 等写成离线 observation/handoff/数据回传，保留为 handoff note，不作为当前设备不可执行原因。
   对 XRD_V1 还要检查“语义 Skill 参数”与 `0410数据转换.txt` 的实际 wire schema 是否能同时
   表达样品容器和 XRD 基底片。若实际 wire schema 不能无损承载 Skill 要求的独立
   `样品载体类型/数量/编号`，禁止生成一个随后会丢字段的 XRD operation；应在合法的谱学
   搅拌/中转节点结束自动 workflow，把滴涂、晾干、XRD 采集和原始图谱回传写成离线
   observation handoff。该 handoff 仍以 XRD 结果回传为 stage completion，不能改用代理观察。
7a. **45 个工作站、默认容器运输、物料转移和样品处理链在物理上是联通的**：同一个已有
    容器可从工作站 1 移动到工作站 2；也允许先在容器 A 中加试剂，再通过平台转移链把样品
    转入容器 B。跨站运输本身不要求每个工作站 Skill 重复声明“运输”操作，也绝不能仅因
    缺少这类声明而判 feasibility_error。目标工作站对容器类型、盖态、相态、容量和输入状态
    的 operation 约束仍须严格满足；必要时插入真源中最合适的转移/分装操作来建立输入状态。
    禁止生成任何 human handoff / manual handoff / 人工拿取 / 人工搬运 / 人工转移 /
    人工称取 / 人工装载 / 人工重新装载步骤（"人工审核/复核/确认"属于审查语义，不受此限）。
    但上一条定义的“设备流程开始前、真源不接受的用户提供刚性载体前处理”必须作为
    offline_handoff 如实声明；不得为了追求全自动而虚构样品谱系。
7aa. **最短样品链规则**：默认沿用同一容器和同一样品身份。只有目标 operation 的输入容器/
     容量/相态明确不兼容，或实验明确要求分样、合并、换载体、产物输出格式时，才新建容器并
     插入一次必要转移。禁止为了匹配工作站名字而反复换瓶、倒回原瓶、移动溶剂位置，禁止
     A→B→A 的无意义往返。多个等价路线中，选择容器转换次数和溶剂位置变化次数最少的路线。
7ab. **设备 operation 接受的专用容器视为可供给输入**：例如高温高压微反应平台明确接受
     10ml耐压反应管，就不得因没有另设“耐压反应管物料站”而判容器不可获得。可在
     container_plan 中登记这些反应管，并把 operation 所要求的初始盖态（通常为无盖）写成
     平台提供时的确定初始状态；专用空反应管本身不需要再生成物料站取得/开盖步骤。
     预配溶液/用户提供载体装入状态可写成流程起点的 offline_handoff；反应后到进样瓶的
     必要取样/转移遵循默认联通与最小换瓶规则。
7ac. **反应管冷却不得虚构不兼容工作站**：若反应平台提供“开盖温度”等结束条件，就把
     冷却到该温度作为反应 operation 的固定结束状态/notes；不要把 10ml耐压反应管送入
     不接受它的容器置放平台。若没有兼容冷却 operation，则在真实后处理 handoff 中声明
     冷却完成状态，不能生成 schema 不接受该容器的“静置”步骤。
7ad. **反应管容器身份与托盘位置分离**：高温高压微反应平台的 `反应管编号` 是每次运行的
     托盘位置，只允许 1–4；一次最多安排 4 管。多于 4 个样品时分批重复执行，每批使用
     1–4 位置，并在 container_plan / sample_lineage 中明确 `sample_id -> batch_index -> slot`。
     不得把 R1、R2 等全局样品标签填入 `反应管编号`。前一批已完成并离开平台后，下一批
     复用 1–4 托盘位置不等于复用或覆盖样品容器身份。
7b. **固体定量称量链路（设备内规范路径，禁止交给人工）**：
    - 干粉定量加入 进样瓶/西林瓶/50ml耐热瓶：先用 Solid_Sample_Transfer_Workstation_V1 /
      固体样品转移 把源容器（进样瓶/西林瓶/50ml耐热瓶/96位塑料孔板，无盖固体）中的粉末转入
      料斗（该操作参数只有 容器类型/容器数量/容器编号）；再用
      Multi_Channel_Solid_Weighing_Workstation_V1 / 固体进样-文件传参-机器人 定量加入目标容器，
      配方经 上传文件 CSV（每行：瓶号, 加样量(单位 g，5 mg = 0.005 g，范围 [0,50]), 料罐号(1-30)）。
      **料斗与料罐是同一进料接口的两种叫法**。
    - 或用 Single_Channel_Solid_Weighing_Workstation_V1 / 固体进样 直接向 进样瓶/50ml耐热瓶
      定量进样（参数 进样质量，单位 g，范围 (0,200)）。
    - 10ml耐压反应管 定量装粉走 Multi_Channel_Solid_Weighing_Workstation_V2 / 固体称量
      （--料斗编号 1-10，--加料样 单位 g，范围 (0,40]）。
    - 只有 **96位石英孔板** 的定量装粉是当前真源的真实容器路径缺口（可保留 handoff 或按
      hard 判定）；除此之外的 mg 级称量/分装一律必须映射为上述设备步骤。
    - 只要使用文件传参固体称量，device_plan.key_values 必须给出每一目标瓶的确定配方行：
      `瓶号 + 加样量(g) + 料罐号`。严禁写“按实测干粉量”“适量”“按比例称取”或只写比例。
      若物理混合对照只给出 Ni:Fe 等比例，必须选择明确且可解释的固定批量，并按名义金属量/
      摩尔质量换算为具体克数；不能把称量决定推迟到运行时，也不能改成外部预称取。
8. 不要改变研究目标、目标材料、当前 stage 或目标 observation point。
9. 计划中的每个步骤都必须能从给定工作站 USAGE/AUDIT-RULES 找到依据；不要臆造不存在的工作站。
10. 维护容器身份台账：同一个容器编号在整个计划中必须保持同一种容器类型。换瓶时保留
    source_container、destination_container 和 sample_lineage，并显式规划一次必要的转移/
    分装步骤；跨站移动同一容器不是换瓶，不要生成额外倒液步骤。
11. 维护体积台账：每次加液、洗涤、去上清、保留清洗液、干燥前后都要避免让任一容器的液体体积超过当前工作站/容器真源限制。若 macro 给定的名义批次体积过大，优先把同一名义批次拆分到多个等价样品容器，并在 device_layer_adaptations 中说明；除非 Research 明确授权，不能用等比例缩小改变名义总量，也不能静默超限。拆分出的子反应共享一个 parent_batch_id；若后续化学步骤不要求把全部物料重新放进一个容器，可继续并行处理并以该父批次谱系进入观察点，不得仅因“尚未重新合并”判 feasibility_error。
12. 离心/纯化需要配平时，进入同一次离心的容器数量、容器类型和液体体积必须可配平。若使用配平瓶，配平瓶体积应与主样瓶接近；配平、容量、瓶数或料位问题必须在 Device 内通过分批/分瓶/配平瓶修复，仍无法确定时转人工，绝不能返回 Research。
13. 保留化学上有意义的加料方式：若 macro action 明确要求滴加、缓慢加入、分批加入或给出加入速率，必须在计划中写成设备支持的分批/多次加液节拍；不要擅自改成一次性加入，除非 macro action 明确允许。
    “边滴入边搅拌/边搅拌边滴加/同步搅拌加液”默认是可适配的时间语义：若液体进样站和磁力搅拌站支持同一容器，
    应规划“预搅拌 -> 小份加液 -> 固定时间搅拌 -> 重复 -> 最终搅拌”的交替节拍，并在 temporal_adaptations 中标明
    `execution_fidelity=approximated`、原始要求和分批计划；不得仅因没有单站原子化并行动作就返回 feasibility_error。
    只有明确要求不可中断的连续流/恒定流速/微流控进料，才可把时间语义作为 feasibility_error。
14. 不得仅因为相邻工作站没有在各自 Skill 中显式声明“跨站运输/转移路径”而判不可行；平台
    默认运输与物料转移链已经联通。若后续 operation 不接受当前容器，优先在完整真源中选择
    接受当前容器的等价工作站；仍不满足时，插入一次最小必要的 A→B 转移并记录样品谱系。
    只有完整真源确实缺少必要的化学 operation、强制安全条件无法满足，或必需站点离线且
    完整真源中没有任何替代时，才返回 device_feasibility_error。容量/剂量/分批/料位问题不是
    Research 路线缺口，必须留在 Device 层。
15. 一个物理 Device 步骤若同时服务多个 Research macro step，不得复制该物理步骤：
    `source_macro_step` 必须是按 Research 顺序排列的首个 primary scalar，另用
    `source_macro_steps` 数组列出全部来源。单一来源也输出只含一个元素的数组。
    `source_reagent_identity` 回显来源试剂/对象；悬浊液、湿固体、上清液、沉淀和粉末只表示
    同一样品的状态变化，不得另造样品名。任何配平样品/额外批次仍须在冻结样品矩阵中获授权。
15a. 每个真实转移/换瓶步在 `sample_lineage` 中写明唯一 sample_id、单一
     source_container、单一 destination_container、transfer_reason 和
     trace_complete=true。若 transfer_reason 声称 capacity_split/split_batch/aliquot/
     pooling/merge，必须同时给 `justification_evidence_refs`，逐项引用顶层
     `quantity_adjustments[].adjustment_id` 的匹配结构化证据；仅写理由标签无效。
     容器范围/集合不得伪装成单样品谱系。默认联通的普通容器
     转移（包括耐压反应管 -> 进样瓶）必须是 Device 内的最短转移，不得写成
     no_supported_container_path/offline_handoff。容器不兼容、容量拆批、分样/合并、
     固体料斗、XRD 载体或产物输出格式可作为必要换容器原因；原因不确定时留在 Device
     补证据/人工复核，永不因样品链返回 Research。
16. 输出必须是一个 JSON object，不要 Markdown，不要代码块。
""".strip()

TRANSLATION_SYSTEM_PROMPT = """
你是化学自动化平台的 workflow translation agent（设备两段流程的第二段）。
你只做一件事：把第一段产出的 device_plan **逐步、忠实地**翻译成严格符合工作站 Skill
参数表的 workflow_txt 和 workflow_json。

铁律：
1. **不做化学决策**：不增删步骤、不改变工作站选择、不改变任何化学数值（体积/质量/温度/
   时间/转速）；device_plan 说什么你翻什么。
2. 每个 workflow_json 步骤的参数字段名、嵌套层级、类型、枚举、单位、取值范围必须严格来自
   下方给出的工作站参数表；**禁止发明参数名或聚合字段**。
3. device_plan 的每个 plan 步骤可以展开为多个 workflow 步骤（如开盖→加液→关盖），但每个
   workflow 步骤必须用 scalar `source_plan_step` 精确指向唯一来源 plan_step，并继承该步骤的
   source_macro_step 和 source_macro_steps，
   并携带 macro_action_id / observation_point_id（如 device_plan 提供）。
4. 必填参数（参数表 是否必填=是）必须全部填写；容器数量必须等于容器编号数组长度。
5. workflow_txt 严格仿照 TXT 格式参考（第N步 工作站：…），且与 workflow_json 步骤
   一一对应（块数相同、每块出现该步工作站名）。
6. temporal_adaptations 与 offline_handoffs 原样搬运 device_plan 中的内容，不新增不删除。
6a. 对「固体进样-文件传参-机器人」，参数表中的「上传文件」只承载文件路径；必须把
    device_plan.key_values 中已经确定的每瓶加样质量和料罐/料斗号原样保留在该步骤 notes，
    采用可解析格式「每瓶加样量=0.005 g；料罐号=1」，供程序生成真实 XLSX。不得省略或猜测。
7. 输出必须是一个 JSON object：{"workflow_txt": "...", "workflow_json": {...}}，
   不要 Markdown，不要代码块，不要输出其它键。
""".strip()


FEASIBILITY_PLAN_TASK_PROMPT = """
请判断 research handoff 能否由当前设备完成，并产出 device_plan（设备级计划，不含 workflow_json）。

## research handoff
{research_handoff_json}

## 已冻结的 LLM 语义合同
这份合同由独立语义审查调用基于完整上下文生成。你必须按其中的 capability、core/observation、
quantity kind 和 material identity_id 规划；不得自行改写或省略。
{semantic_analysis_json}

## 当前设备/器材真源
下面是每个工作站的 USAGE.md 和 AUDIT-RULES.md 摘要/全文。只能使用这里出现的工作站、容器和操作。
{workstation_descriptions}

## 数量与消费者谱系硬约束
- 先读取 Research macro step 的 `quantity_requirements` 和工作站真源：只有冻结科学要求或
  Skill 必填输入确实要求数值时才建立数值库存审计。`target_dose` 是工作站的目标取用设定，
  不等于源样品的整批实际质量；Skill 未声明数值反馈时不得把设定值冒充读数。
- 数值权限以 Research 的 `owner/required_by/device_policy/scientifically_fixed` 为准：
  `research_scientific` 必须保持；`runtime_observation` 只能引用 Skill 明确 Report 或冻结 observation；
  `device_execution + bind_skill_setpoint` 必须绑定当前真源某个 operation 的必填数量参数；
  `device_execution + device_semantic_decision` 必须由你结合冻结科学目标、下游 operation 的真实必填参数、
  上游可观测数值和是否存在 whole_batch 路径判断：保留科学目标、在设备范围内调整、绑定 Skill、删除、
  改 whole_batch 或等待真实运行时测量。不能仅因 Research 写了数值就保留，也不能仅因 Skill 没有同名
  参数就删除。每个待判断目标都必须在 `quantity_requirement_dispositions` 中给出明确决定、科学/设备理由
  和引用的真源证据；确定性审计只核验你引用的 Skill/测量事实，不替你做科学必要性判断。
- 若一个中间体会原容器/整批 1→1 进入下一处理，且没有 split/merge、多 consumer、固定取样量
  或容量计算要求，batch 使用 `quantity_mode=whole_batch`，transition 使用
  `quantity_basis=whole_batch`。此模式不填写 total_quantity、allocation 或数值 edge，ledger 只保留
  sample/material/batch/consumer/processing 谱系。Research root 只有在对应 macro step 的
  `quantity_requirements` 已显式声明 `kind=whole_batch`，或对应可调执行目标已由
  `quantity_requirement_dispositions.decision=replace_with_whole_batch` 确定处置时才能使用此模式；
  不得用它删除冻结的 target_dose/scientific_input_setpoint。whole_batch 不能用于复制、拆分、合并或多个下游。
- `batch_plan` 的一个 batch 只描述一个确切 `material_id`/物料状态。纯化、
  洗涤、干燥等将该物料转化为下游状态的处理节点，不能和对下游新物料的
  XRD/XPS 取样混写成同一上游 batch 的并列消费。必要时用新
  `batch_id + material_id` 表示处理后物料，并保留 parent/source_refs 谱系。
- `allocation` 只接受以每个 consumer_id 为键的定量映射，或每项都显式
  含 `consumer_id` 的 record list，例如 `{"XPS":{"value":0.06,"unit":"mmol"},
  "XRD":{"value":0.12,"unit":"mmol"}}`；禁止用一个标量 allocation 同时指向
  多个 consumer，也禁止将无 consumer_id 的 list 按声明顺序或排序顺序自动
  配对，即使 list 长度恰好等于 consumer 数量。声明的 `consumer_ids` 集合必须
  与 allocation 显式绑定的 consumer 集合完全相等，不得缺少或额外引入。
- `material_ledger` 必须为每个真实取用 consumer 提供独立、可对账的
  consumed allocation（可用多条 entry 或一条 entry 的 `consumers[]`）；其 consumer
  集合和数量必须与 batch_plan 逐项一致。无法从 Research 或已知台账唯一
  确定分配时，保留 Device-local 错误/人工复核，不得猜分配。
- 任何由纯化、洗涤、干燥、反应或其他设备处理产生的 derived/device_operational
  batch 不得冒充 `is_root_batch=true`。它必须声明 `parent_batch_id`、
  `transition_kind`、`source_plan_steps` 和 `source_macro_steps`，且由顶层
  `material_transitions` 的 parent_batch_ids→child_batch_ids 记录逐 ID 覆盖。父批次、
  子批次和 plan step 都必须真实存在；对应 ledger entry 必须用
  `processing_step_refs` 回指同一处理步。每个改变物料状态的 device plan step
  都必须显式输出这套 transition 谱系；不得从名称猜测或删除中间处理。
- `material_transitions` 必须是 DAG；parent 不得等于 child，每个 child 的
  parent_batch_ids 必须与 child batch 声明集合完全相等（不得多/少），root 不得
  作为 transition child。处理 plan step 必须声明非 `none` 的
  `material_event_kind` 和 `material_transition_ids`，transition.source_plan_steps 必须反向
  包含该 step；ledger.processing_step_refs 必须与子 transition 步集完全相等。
  每条 transition 还必须有 consumer-bound `input_allocations` 和
  `output_allocations`；parent ledger 必须以 `consumer_id=material_transition:<id>` 实际扣减
  input，child ledger produced 必须以 `source_refs=material_transition:<id>` 实际入账。
  `conserved_inventory` 聚合输入/输出必须严格守恒；state_change 的自由文本
  measured 不是证据，只能使用与冻结 observation digest+sample+material+数量字段
  交叉一致的 `measurement_artifact`，否则 unknown_yield/人工复核。
  `research handoff.observation_evidence_catalog` 已由程序机械生成 digest 和唯一数量
  span；模型只能逐字选择其中的 `measurement_artifact`，不得自行计算/改写 hash、
  sample、material、batch、field 或 context。
- 只有 `source_kind=research` 且有可验证真源的 batch 才能是 root。每个 root
  必须显式给出 `material_id`、冻结语义合同中的 `material_identity_id` 以及
  research_source_refs；不得再靠物料名称子串判断身份。只有
  `quantity_mode=numeric_inventory` 才必须给
  带单位 `total_quantity` 和 quantity；`quantity_mode=whole_batch` 只验证身份/来源绑定，不猜总量。
  数值 root 的
  `research_source_refs=[{{source_path,source_macro_step,source_field,source_context,
  material_identity,quantity}}]`。`source_path` 必须同时出现在 batch.source_refs；
  `source_context` 必须是冻结 Research 字段中唯一出现的逐字片段，用于在
  同一字段存在多个数量时精确锁定。直接单位换算可通过；改变 Research
  数量必须有逐项 before/after 匹配且 `requires_scientific_review=true` 的
  quantity_adjustment，否则不得信任 source_kind/source_refs/calculation 自我声明。
  若数量是每批，必须 `quantity_scope=per_batch` 且用冻结的独立批次 quote
  验证 `multiplicity_ref/count/per_batch_quantity/multiplicity`；其中
  multiplicity_ref 必须由模型按完整上下文声明
  `semantic_scope=all_samples|material_specific`、`material_identity_id`（仅
  material_specific）、`semantic_reason` 与 `evidence_refs`，确定性层不从“每种样品”等
  关键词猜适用范围。若是同一 root 中的重复
  洗涤/加液等消耗，使用 `quantity_scope=per_operation` + 同一字段局部
  `operation_repeat_ref` + `operation_repeat_count`，该 count 不得创建额外 batch/sample。

## 输出 JSON 格式
必须返回一个 JSON object，三选一：

### 情况 A：可以由设备完成 → 输出 device_plan
{{
  "status": "device_plan",
  "feasibility": {{
    "is_feasible": true,
    "blocking_constraints": [],
    "device_layer_adaptations": ["你在计划级补全了哪些设备映射决策"]
  }},
  "macro_plan_summary": "一句话说明从 macro action 到设备计划的映射策略",
  "sample_control_matrix": "逐字、完整回显 Research 中的样品组/对照组/变量梯度；不得增删合并",
  "device_self_check": {{
    "container_continuity": "pass/fail + 简短说明",
    "transfer_minimality": "pass/fail + 换瓶次数及每次必要性；同容器跨站不算换瓶",
    "sample_container_lineage": "pass/fail + 每个样品的完整容器链；转移步写明源/目标容器与必要性",
    "macro_semantic_coverage": "pass/fail + 每个 macro step 的显式操作、先后顺序、时长和全量转移是否覆盖",
    "material_conservation": "pass/fail + 分装、合并和全量转移前后的体积/质量/样品谱系核对",
    "volume_and_capacity": "pass/fail + 简短说明",
    "centrifuge_balancing": "pass/fail/not_applicable + 简短说明",
    "addition_mode_preserved": "pass/fail/not_applicable + 简短说明",
    "workstation_constraints": "pass/fail + 简短说明"
  }},
  "reagent_slot_plan": [
    {{
      "工作站": "使用该原液瓶的工作站名称",
      "原液编号": 1,
      "名称": "原液/试剂名称",
      "浓度或说明": "浓度、预配说明或固体说明",
      "来源": "来自哪个 macro step"
    }}
  ],
  "container_plan": [
    {{
      "容器编号": 1,
      "容器类型": "进样瓶/西林瓶/50ml耐热瓶/留样瓶等",
      "用途": "用途",
      "sample_id": "sample_A",
      "lifecycle": "初始状态 -> 中间状态 -> 最终去向"
    }}
  ],
  "quantity_adjustments": [
    {{
      "adjustment_id": "qa_001",
      "kind": "unit_conversion | split_transfer | split_batch | replicate_batch | slot_reallocation | concentration_change | molar_ratio_change | amount_change",
      "before": {{"value": 0.18, "unit": "mmol"}},
      "after": {{"value": 0.54, "unit": "mmol"}},
      "source_kind": "research | derived | device_operational",
      "source_refs": ["macro_step:1"],
      "calculation": "0.18 mmol × 3 independent consumers = 0.54 mmol",
      "preserved_invariants": ["route", "reagent_identity", "sample_matrix"],
      "route_changed": false,
      "requires_scientific_review": true
    }}
  ],
  "quantity_requirement_dispositions": [
    {{
      "source_macro_step": 1,
      "requirement_index": 0,
      "decision": "retain_as_scientific_target | adapt_within_device_bounds | bind_skill_setpoint | omit_as_nonessential | replace_with_whole_batch | request_runtime_measurement",
      "plan_step": 3,
      "workstation": "仅 bind 时填写真源工作站",
      "operation": "仅 bind 时填写真源 operation",
      "parameter": "仅 bind 时填写必填 material quantity 参数",
      "reason": "模型对科学必要性和设备可执行性的判断理由（必填）",
      "evidence_refs": ["Research 数量来源或 workstation/operation/Report 真源"],
      "requires_scientific_review": false
    }}
  ],
  "batch_plan": [
    {{
      "batch_id": "batch_001",
      "quantity_mode": "numeric_inventory | whole_batch",
      "material_id": "该批次分配所对应的物料/中间体身份",
      "material_identity_id": "每个批次引用冻结语义合同中的 identity_id",
      "research_material_identity_id": "root 批次必须引用冻结语义合同中的 identity_id",
      "parent_batch_id": "batch_000（该父批次也必须在 batch_plan 显式列出）",
      "is_root_batch": false,
      "transition_kind": "state_change | split_same_material | replicate_same_material | process_same_material",
      "research_material_identity": "仅 root：逐字引用 Research 物料身份或其明确子集",
      "research_source_refs": [{{"source_path":"macro_action_steps[0].参数","source_macro_step":1,"source_field":"参数","source_context":"含唯一目标数量的逐字片段；whole_batch 可只绑定身份而不填数量","material_identity_id":"冻结语义合同 identity_id","quantity":{{"value":0.18,"unit":"mmol（仅 numeric_inventory）"}},"quantity_scope":"total | per_batch | per_operation","multiplicity_ref":{{"source_path":"macro_action_steps[0].参数","source_context":"独立批次数量的逐字片段","count":3,"semantic_scope":"all_samples | material_specific","material_identity_id":"material_specific 时必填","semantic_reason":"结合完整上下文的适用范围判断","evidence_refs":["冻结 Research 路径"]}},"operation_repeat_ref":{{"source_path":"macro_action_steps[0].参数","source_context":"加入 3 mL 水并重复洗涤 3 次","count":3}}}}],
      "total_quantity": {{"value":0.18,"unit":"mmol"}},
      "per_batch_quantity": {{"value":0.18,"unit":"mmol（仅 quantity_scope=per_batch）"}},
      "multiplicity": 1,
      "operation_repeat_count": 1,
      "source_plan_steps": [2],
      "source_macro_steps": [1],
      "sample_id": "sample_A",
      "consumer_ids": ["consumer_1"],
      "allocation": {{"consumer_1": {{"value": 0.18, "unit": "mmol"}}}},
      "source_kind": "device_operational",
      "source_refs": ["macro_step:1"],
      "calculation": "确定性分配式",
      "pooling_policy": "not_pooled | explicitly_authorized"
    }}
  ],
  "material_transitions": [
    {{
      "transition_id": "mt_001",
      "transition_kind": "state_change",
      "parent_batch_ids": ["batch_000"],
      "child_batch_ids": ["batch_001"],
      "source_plan_steps": [2],
      "source_macro_steps": [1],
      "quantity_basis": "whole_batch | conserved_inventory | measured_observation | planning_yield_lower_bound",
      "input_allocations": [{{"batch_id":"batch_000","quantity":{{"value":0.18,"unit":"mmol"}}}}],
      "output_allocations": [{{"batch_id":"batch_001","quantity":{{"value":0.18,"unit":"mmol"}}}}],
      "before_material_state": "反应悬浊液",
      "after_material_state": "纯化干燥粉末",
      "measurement_artifact": {{"observation_id":"仅 measured_observation 使用","artifact_digest":"observation digest","material_identity_id":"模型基于完整 observation 上下文绑定的冻结 identity_id","quantity_source_field":"measured_quantity","quantity_source_context":"冻结 observation 中唯一数量逐字片段"}},
      "calculation_or_basis": "该设备处理步将父批次转换为子批次，不猜收率"
    }}
  ],
  "material_ledger": {{
    "entries": [
      {{
        "entry_id": "material_001",
        "quantity_mode": "numeric_inventory | whole_batch",
        "material_id": "中间体A",
        "batch_id": "batch_001",
        "sample_id": "sample_A",
        "consumer_id": "material_transition:mt_001（parent edge 扣减） | consumer_1",
        "processing_step_refs": [2],
        "produced": {{"value": 0.18, "unit": "mmol"}},
        "consumed": {{"value": 0.18, "unit": "mmol"}},
        "reserved": {{"value": 0, "unit": "mmol"}},
        "balance": {{"value": 0, "unit": "mmol"}},
        "source_kind": "derived",
        "source_refs": ["material_transition:mt_001（child produced 必填）"],
        "calculation": "produced-consumed-reserved"
      }}
    ]
  }},
  "device_plan": [
    {{
      "plan_step": 1,
      "workstation": "工作站名称（真源中的名称）",
      "objective": "这一步要达成什么（化学语义）",
      "operation_intent": "操作意图（如 拿取容器/开盖/分批加液/搅拌/离心/烘干/固体转移/定量称量）",
      "key_values": {{"体积/质量/温度/时间/转速等化学数值": "值+单位"}},
      "containers": {{"容器类型": "进样瓶", "容器编号": [1, 2]}},
      "source_macro_step": 1,
      "source_macro_steps": [1],
      "source_reagent_identity": "逐字回显该 source_macro_step 的 Research 试剂/对象身份",
      "source_material_identity_ids": ["逐项引用冻结语义合同 identity_id，不得自行按名称猜测"],
      "material_event_kind": "none | state_change | split_same_material | replicate_same_material | process_same_material",
      "material_transition_ids": ["mt_001（处理步必填）"],
      "sample_lineage": {{
        "sample_id": "sample_A（仅转移/换瓶步必填）",
        "source_container": {{"container_type": "10ml耐压反应管", "container_id": "RT01"}},
        "destination_container": {{"container_type": "进样瓶", "container_id": "V01"}},
        "transfer_reason": "operation_input_incompatibility | capacity_split | aliquot | pooling | solid_hopper | xrd_carrier | product_output | minimal_required_transfer",
        "justification_evidence_refs": ["quantity_adjustment_id（仅容量/拆批/分样/合并豁免时必填）"],
        "trace_complete": true
      }},
      "notes": "可选：配平/节拍/换容器等计划级说明"
    }}
  ],
  "temporal_adaptations": [
    {{
      "original_requirement": "边滴入边搅拌",
      "execution_fidelity": "approximated",
      "adaptation_schedule": "预搅拌 -> 分批加液 -> 每批固定时间搅拌 -> 最终搅拌",
      "requires_scientific_review": true
    }}
  ],
  "offline_handoffs": [
    {{
      "name": "离线 XRD observation",
      "sample": "样品",
      "required_return_data": ["XRD 图谱", "判读结果"],
      "source_macro_step": 1,
      "source_macro_steps": [1],
      "source_material_identity_ids": ["冻结语义合同 identity_id"],
      "semantic_classification": "observation_data_return | input_boundary | post_process_transfer | core_chemistry_execution | unsupported_external_operation",
      "semantic_reason": "结合完整上下文的分类理由",
      "semantic_evidence_refs": ["冻结语义合同和 Research/Device 路径"],
      "material_operation_kind": "none | solid_dosing | liquid_transfer | carrier_loading | other",
      "requested_quantity": {{"value": 5, "unit": "mg（仅真实定量操作填写）"}},
      "source_container": "结构化源容器",
      "destination_container": "结构化目标容器"
    }}
  ]
}}

### 情况 B：不能映射
{{
  "feedback_type": "device_feasibility_error",
  "status": "feasibility_error",
  "feasibility": {{
    "is_feasible": false,
    "blocking_constraints": ["硬阻塞原因"],
    "unsupported_items": [
      {{
        "macro_step": "步骤",
        "requirement": "必须能力",
        "constraint_category": "hard_capability_gap | unverifiable_condition",
        "reason": "为什么真源不支持（写明缺失的工作站/转移路径/容量上限等硬证据；若只是无法证明满足，写 unverifiable_condition）",
        "missing_device_capability": "缺失能力",
        "suggested_research_revision": "给 research agent 的化学语义改写建议"
      }}
    ],
    "device_layer_adaptations": ["本可由 device agent 自己补全的内容"]
  }},
  "device_capability_summary": {{
    "supported_containers": ["容器"],
    "supported_workstations": ["工作站"],
    "not_supported": ["缺失能力"]
  }},
  "recommendation_to_research_agent": "给 research agent 的简短反馈"
}}

### 情况 C：路线可行但数量信息必须人工判断
{{
  "feedback_type": "human_review_required",
  "status": "manual_required",
  "failure_scope": "device_quantity",
  "feedback_route": "human",
  "feasibility": {{"is_feasible": true, "blocking_constraints": []}},
  "quantity_audit": {{
    "status": "human_review_required",
    "issues": [{{"code": "unknown_yield | unauthorized_pooling", "message": "不能猜测的事实"}}]
  }},
  "device_plan": ["必须输出与情况A相同结构的完整 best-known device_plan，不得为空"],
  "quantity_adjustments": ["完整 best-known sidecar"],
  "batch_plan": ["完整 best-known batch lineage"],
  "material_ledger": {{"entries": ["完整 best-known ledger entries"]}}
}}

情况 C 仍表示路线和设备映射已通过：必须先用完整 device_plan 签发 feasibility certificate，
再停在人工数量判断；空 device_plan 是输出契约错误，不能作为 condition review 返回。

计划要求：
- offline_handoff 不是工作站，严禁把“离线输入handoff”“offline_handoff”等伪站名写入
  device_plan；它们只能出现在顶层 offline_handoffs。device_plan 中每个 workstation 都必须
  是设备真源中的真实工作站。
- device_plan 步骤按执行顺序排列；开盖/关盖/配平/重复洗涤在计划级写成显式步骤或 notes 说明。
- **压缩同参数平行样**：同一工作站、同一操作意图和同一参数的多个平行样必须合并成一个
  plan_step，用 containers.容器编号 数组表达，禁止为每个平行样复制一套相同步骤。分批加液/
  重复洗涤可在 key_values 中给出“重复次数+单次参数”，由 translation agent 展开。完整
  device_plan 原则上不超过 48 个 plan_step；字段保持简洁，不重复抄写 research 叙述。
- 样品矩阵中的每个配方、变量水平和独立批次都必须有确定 sample_id。若前驱体由多股外部
  预配溶液组成，container_plan/reagent_slot_plan 必须给出 `source container -> sample_id ->
  target container/反应批次` 的绑定表；不能用通用“A液/B液”代替不同 NH4F 水平、Ni-only、
  Fe-only 等实际来源。相同操作参数可以数组压缩，但样品谱系不能压缩丢失。
- 物料站取得容器后必须遵守该站 Skill 的直接后继约束；例如耐热瓶物料站后应立即进入移液
  或固体称量，不得直接进入加热/搅拌。对于后续才需要的容器，优先在起始阶段按最大活跃库存
  一次取得并紧接合法装料；若确需分阶段取得，先在 offline_handoff/container_plan 中明确
  哪些旧容器已消耗、释放或退出活跃库存，避免无解释地增加全局活跃容器数。
- 涉及洗涤时，展开成固定次数；涉及干燥时，使用固定温度和固定时间；不写“至……为止”。
- 容量适配优先不改变 macro plan 的化学总量或浓度。例如 macro 明确每个名义批次为 8.0 mL、
  设备单管上限为 6.0 mL 时，不得静默把配方等比例缩成 6.0 mL；优先把同一名义批次拆成两份
  组成相同且各自不超过 6.0 mL 的子反应（如 4.0+4.0 mL），为两者记录相同 parent_batch_id
  和各自 child_batch_id。只有后续化学步骤明确要求“全量合并后再处理”或单一混合样品时，
  才在首个兼容阶段通过默认联通的最短 A→B 转移链合并；若后续可对子批次并行洗涤、干燥、
  取等量代表性样品或按父批次汇总观察，则无需物理重新合并，不能因真源没有专门写出
  “反应管合并”操作而返回 feasibility_error。确需合并时，也不得只因跨站运输边未显式声明
  而拒绝；应选择接受目标相态/容器的现有转移操作，或在真实后处理 offline_handoff 中记录
  全量汇合和谱系。
- Device 可以为满足消费量、称量下限或设备范围而新增完整批次，或调整单批/总量、浓度、
  摩尔比，但必须在 quantity_adjustments 中逐项记录 before/after、来源、计算式和
  `requires_scientific_review=true`，并保持研究目标、路线、试剂身份/顺序、observation point
  及样品/对照矩阵不变。纯分次、分瓶、容量拆批、容器/料位变化和单位换算标记 false。
  未知收率或未经授权的独立样品合批不得猜测，应返回 human_review_required 供人工判断，
  不得把这类数量问题包装成 device_feasibility_error。
- 原液编号是工作站本地槽位，不是跨工作站全局编号。reagent_slot_plan 每项必须写明
  `工作站`；同一试剂在不同工作站可以使用不同本地编号，不同工作站也可以各自复用 1 号。
  只有同一工作站内才执行一瓶一液和固定编号约束。若无法确定本地编号，按该工作站参数表
  的合法范围自行分配并在 reagent_slot_plan 说明；需要偶数配平时自己选偶数容器并贯穿。
- 对每个容器维护台账（类型/体积/带盖/用途/样品谱系）；同一容器可以直接跨站移动，换容器
  只在输入约束或科学目的要求时插入一次必要转移。禁止无意义的溶剂位置变化和 A→B→A 往返。
- 任何开盖步骤只要后续还存在关盖，必须把 `保留瓶盖`/`是否保留瓶盖` 设为 1；只有该容器
  后续始终保持无盖并在合法测试/交接节点结束时才可设为 0。复审重写不得在保留 0 的同时
  继续生成关盖步骤。
- 时间语义（边滴入边搅拌等）在计划级展开为交替节拍并记入 temporal_adaptations。
- macro plan 中明确写出的前置混合、分段加料、加料时长、中间搅拌、合并后搅拌和再处理
  顺序必须逐项成为 device_plan 中可执行的独立步骤，不得只保留最终反应/熟化步骤。例如
  “600 rpm 预混20 min后水热”必须先有20 min预混；“先混合、10 min内加NaOH、再搅拌
  30 min、60℃熟化2 h”必须保持四段顺序；“合并后搅拌15 min再干燥”必须在目标容器
  上先搅拌再干燥。严禁在 notes、adaptations 或翻译指令中写“若容器不兼容则省略”、
  “由后续反应内搅拌替代”等条件性跳过语句。若最终反应容器不兼容前置搅拌站，必须先在
  兼容容器中完成预混，再用默认联通的最短 A→B 转移链进入专用反应容器；不能删除前置时序。
- 全量、aliquot、分装和合并必须做物料守恒。macro 要求“全量固体/全量悬浊液合并”时，
  不得从3.0 mL均匀悬浊液只转1.5 mL，也不得为生成新对照而无说明削减原对照样品。
  若单次移液上限不足，拆成多次并保持累计体积；若需保留原样且所需产量可由确定性计算支持，
  Device 可复制完整独立批次并标记科学复核；若实际收率未知，则转人工复核而不是回 Research。
- 每个数量写明 `source_kind=research|derived|device_operational`、source_refs 和 calculation；
  material_ledger 校验生产量、消费量、预留量、sample_id/batch_id 谱系与重复计量。
- 不要把“建议/范围/上限”当作可贴边的默认值；必须贴边时在 device_layer_adaptations 解释。
""".strip()


TRANSLATION_TASK_PROMPT = """
请把下面的 device_plan 忠实翻译成严格符合工作站参数表的 workflow_txt 与 workflow_json。

## 完整 device_plan（第一段产出，化学决策已定，禁止更改；仅作上下文，用于理解容器编排）
{device_plan_json}

## 本次只需翻译的范围
{chunk_directive}

## 前序步骤结束时的容器状态（carry-over，用于保持连续性；请勿重复已完成的开/关盖）
{carryover_state}

## 各工作站参数表（唯一合法的参数字段来源）
{station_parameter_tables}

## TXT workflow 格式参考
{txt_format_reference}

## JSON workflow 格式参考
{json_format_reference}

## 输出 JSON 格式
只输出本范围内 plan 步骤对应的 workflow 步骤：
{{
  "workflow_txt": "本范围的工作流文本片段（第N步 工作站：…；step_number 从 1 顺序编号即可，最终由程序统一重排）",
  "workflow_json": {{
    "steps": [
      {{
        "step_number": 1,
        "workstation": "工作站名称",
        "operation": "操作名称（参数表中存在的操作）",
        "parameters": {{}},
        "source_plan_step": 1,
        "source_macro_step": 1,
        "source_macro_steps": [1],
        "notes": "可选说明"
      }}
    ]
  }}
}}

翻译要求：
- **只输出上述范围内 plan 步骤的 workflow 步骤**，不要输出范围外的步骤，也不要输出
  temporal_adaptations / offline_handoffs（这些由程序从 device_plan 统一附加）。
- 每个 plan 步骤可展开为多个 workflow 步骤（如 开盖→加液→关盖），每个 workflow 步骤必须
  写 scalar `source_plan_step` 精确指向该 plan_step，并继承该 plan 步骤的 source_macro_step
  与 source_macro_steps（以及 macro_action_id/
  observation_point_id，若提供）。不得为多来源追踪而复制物理步骤。
- 参数字段名/嵌套/类型/枚举/单位/范围严格来自参数表；必填参数全部填写；
  容器数量 == len(容器编号)。
- 硬性契约（违反即整步作废）：
  1. 操作名必须逐字等于参数表中列出的操作，禁止扩写/合并（如参数表只有「开始搅拌」，
     不得写「常温磁力搅拌全流程」）；
  2. 参数名只能取自该操作的参数表，禁止发明字段（如「配料名称」不在表中就不能出现）；
  3. JSON 类型严格照表：声明 int 的写数字不加引号，声明 string 的写字符串加引号，
     声明 object/array 的保持层级结构，不得用裸数字代替对象；
  4. 「N号原液瓶」等动态字段：N 和瓶号都必须落在参数表声明的范围内（如 [1,6]），
     超出的瓶位必须换瓶或拆步，绝不允许写 7 号及以上；
  5. 液量硬上限：单次移液不得超过该移液平台的单次最大量（如 5ml 平台 ≤5 mL），
     单一原液瓶对同一容器的累计加注不得超过表定总量（如 1ml_V2 平台 ≤3 mL）；
     超限时必须拆分为多步或改用更大量程的平台/多个原液瓶；
  6. 一瓶一液按工作站作用域执行：同一工作站内，同一原液瓶编号在整个 workflow 中只能
     盛放一种溶液且编号保持不变；不同工作站拥有彼此独立的本地原液瓶槽位，可以各自使用
     1 号，也允许同一试剂在不同工作站映射到不同编号。不得拿 A 工作站的槽位绑定去否决
     B 工作站的合法本地绑定。
- 化学数值（体积/质量/温度/时间/转速）从 device_plan.key_values 原样搬运，只做单位换算。
- 文件传参固体称量步骤必须在 notes 保留「每瓶加样量=... g；料罐号=...」，上传文件由程序
  物化为任务专属 XLSX；不得只输出占位文件名而丢失配方行。
- 若一个称量步骤有多个目标瓶或多个料罐，notes 必须逐行列出完整确定配方，例如
  「瓶4: 加样量=0.0278 g, 料罐号=1；瓶4: 加样量=0.0089 g, 料罐号=2」。不得写
  “按实测质量”“按比例”“分别定量”等运行时未知值。
- temporal_adaptations 与 offline_handoffs 从 device_plan 原样搬运。
- workflow_txt 与 workflow_json 一一对应（块数相同、每块出现该步工作站名）。
""".strip()


WORKFLOW_SKILL_REVIEW_SYSTEM_PROMPT = """
你是化学自动化平台的最终 workflow 可执行性审核与修复 agent。你必须把输入中的工作站
Skill、根规则和 AUDIT-RULES 视为唯一设备真源，逐步模拟 workflow_json 在工作站上的执行。

审核范围必须覆盖：
1. 工作站、operation、id、参数名、嵌套层级、JSON 类型、必填项、枚举、单位和范围；
2. 每个容器是否已由前序步骤创建或取得，容器编号与类型是否稳定，数量是否自洽；
3. 每个 operation 的输入状态是否由前序输出证明，包括盖态、样品相态、液固状态、载体、
   容器来源，以及工作站之间的交接关系；unknown 不能当作满足明确的输入要求；
4. 开盖/关盖、固液转移、换瓶、分装、配平、离心、洗涤、干燥和表征的完整状态变化；
5. reagent_slot_plan 的工作站作用域、一瓶一液约束、加液来源与目标、单次和累计体积、容器容量；
6. macro plan 规定的样品数量、对照组、加料顺序、分批/滴加/搅拌时序和所有化学数值；
7. notes、summary、temporal_adaptations 只提供语义，不能代替可执行 operation 参数；
8. workflow 从物料配置到实验结束是否形成完整、连续、可下发的路径。

判定边界（优先于“字段缺失”类结论）：
1. 参数分为三类：研究计划给出的化学参数、Skill 对 operation 明确开放的下发参数、设备固定/
   默认能力。只有第二类字段必须出现在 workflow_json。辐射源、XRD 扫描起止范围等若未在
   operation 参数表中开放，属于设备固定/默认能力：不得要求 workflow 填写，不得据此报错，
   也不得在重写时发明相应字段；
2. macro/device plan 已将表征定义为 offline_handoff、离线观测接口或后续离线任务时，本
   workflow 的可执行终点就是完成该交接。不得强制增加当前 workflow 不负责的 XRD 等操作；
3. 同时加料与搅拌、慢速加入等连续时序，只要能按 Skill 中现有操作展开为小批次交替执行，
   就属于可修复的 temporal adaptation。应拆步重写，不得直接判定为物理不可执行；
4. workflow 遗漏了步骤，但 device_plan 已含完整且可映射的工作站序列时，属于翻译缺陷。
   应以 device_plan 为依据完整重写，而不是因当前 workflow 缺步骤而判 not_executable；
5. 只有真源明确证明存在不可消除的硬能力缺口，例如没有所需工作站/operation、容量或范围
   越界且无可拆分替代、容器/样品状态转换无任何兼容路径，才能判 not_executable。不能仅凭
   Skill 没有描述某个非开放字段、当前 workflow 没写某步或可修复的状态链断裂作此结论。
6. 判 not_executable 前必须搜索完整真源中的跨工作站等价路径，不能只检查 device_plan 当前
   指定的单个工作站。若一个聚合工作站没有开放所需参数，应尝试把流程拆成独立操作链。例如
   “离心 -> 去上清 -> 定量加洗液 -> 关盖 -> 磁力搅拌指定时间 -> 再离心”可分别由离心机、
   开盖/液体倾倒、移液平台和常温磁力搅拌工作站实现；此类情况必须重写，不能判硬能力缺口。
7. 根规则中的“容器数量不能变多”约束全局已创建且仍活跃的容器库存，不是要求相邻 operation
   的参与容器列表单调递减。每步参数里的“容器数量”只等于该步 len(容器编号)。初始物料站已
   一次取得全部容器后，后续在这些既有编号之间切换 8 瓶、10 瓶或20瓶批次不会创建新容器，
   也不违反数量规则。只有未经前序取得而出现新编号，或明确创建了额外活跃容器，才算增加。
8. 体积上限若 Skill 没有明确声明为整批全局上限，应按每个目标容器、每种溶液解释，不能把
   多个独立容器的总需求相加后与单瓶上限比较。不同工作站可共同完成同一目标体积：例如批量
   加液站先向干固体加入1 mL溶剂，再由1ml平台从固定原液槽向每瓶加入3 mL；后一步明确输出
   纯液态或悬浊液，即可建立4 mL后续搅拌/表征输入状态。批量站按配料名称取液不等于重绑定
   编号原液瓶。若审核理由已经找到这样一条合法链，就必须重写，不能再以当前 workflow 未展开
   该链、总实验用量较大或前一子步骤状态尚未转换为由判 not_executable。
8a. 一个名义批次因单容器容量拆成多个组成相同的子反应时，只要 workflow 为子反应保留共同
    parent_batch_id/样品谱系，并且后续允许并行处理或按父批次汇总观察，就不要求先物理合并，
    不得以缺少“反应管合并”专用 operation 判 not_executable。只有 macro 明确要求全量合并后
    进行下一化学步骤时才必须合并；此时应使用默认联通的最短 A→B 转移链，或由真实后处理
    offline_handoff 明确返回已全量汇合的兼容输入状态。
9. 平台工作站之间的物理运输、默认容器移动和物料转移链视为联通。同一已有容器跨站不需要
   虚构搬运 operation；容器 A 到 B 的必要换瓶应由最少的受支持转移/分装步骤建立样品谱系。
   不得只因两个工作站的 Skill 未重复声明彼此的运输边，就判 not_executable；但目标 operation
   的容器、盖态、相态、容量和参数约束仍必须满足。重写时优先保留同一容器，删除无意义的
   溶剂位置变化、重复换瓶和 A→B→A 往返。
10. 若 macro plan 的输入是用户提供的刚性载体/基底，而所有工作站对相关清洗、超声、离心
   operation 的输入样品状态仅允许液体、悬浊液或沉淀，则必须保留 device_plan 中的外部输入
   handoff，并从首个兼容设备 operation 开始审核。不得取得空容器后声称其中已有该载体；
   offline_handoff 也必须写明载体来源、前处理完成状态、进入设备时的容器和盖态。
11. 对刚性载体做完自动反应后，不得把“载体浸在液体中”重命名为悬浊液或离心沉淀。
   如果真源没有接受该刚性载体的后处理 operation，合法做法是保留反应 workflow，并以
   offline_handoff 覆盖冷却、取出、洗涤、干燥/刮取及进入下一个真实兼容 operation 前的
   状态建立；这是真实化学处理缺口，不是禁止的跨站人工搬运。重写后的 workflow 必须跳过
   所有假设刚性载体可离心/纯化/倾倒的步骤。
12. 对 10ml耐压反应管的冷却，优先使用反应 operation 的“开盖温度”等结束条件。不得把
   该反应管送入容器枚举不接受它的容器置放平台。若后续 offline_handoff 已明确返回完成
   冷却和处理的样品，则 workflow 不再另造冷却工作站步骤。
13. XRD 输入分支必须一致：若 offline_handoff 已返回不少于 4 mL 的均匀悬浊液，直接从
   谱学中转/搅拌/XRD 开始，不要重复加溶剂；若返回干粉，则严格走 Skill 的干粉分支并加入
   4.0 mL 无水乙醇。不得先声明“干粉”再加入乙醇/水混合液。
   如果 deterministic_validation 显示 XRD 语义 Skill 要求的独立样品载体字段不被实际
   dispatch wire schema 接受，严禁在“删除载体字段”和“补回后再被 formatter 丢弃”之间
   循环。应删除在线 XRD operation，保留合法的谱学搅拌/中转前处理，并以 offline_handoff
   明确滴涂、晾干、XRD 采集和原始图谱回传；测试/观察终点允许在该合法交接点结束。
14. device_plan 中的工作站选择、容器编号、开关盖和“保留瓶盖”等都是设备层机械决策，
   不是不可变的化学数值。若重写为满足 Skill 而修正了这些字段，最终复审必须按修正后的
   workflow 判断，不能仅因它与旧 device_plan 的机械字段不同而报参数不一致。例如后续没有
   关盖操作时，把“保留瓶盖”从 1 修正为 0 是合法修复，必须保留并继续审核。
15. 原液瓶编号属于具体工作站的本地命名空间，不是跨平台全局命名空间。只有
   reagent_slot_plan 中 `工作站` 与 workflow 当前工作站相同的绑定才能用于一致性检查；
   不同工作站可以各自复用 1 号槽位。对于缺少 `工作站` 字段的旧式全局计划，不得用另一
   工作站的槽位绑定否决当前工作站，只能依据当前工作站内的前后绑定一致性和参数表范围审核。
16. 设备 operation 明确接受的专用容器属于平台可供给的初始库存。例如移液平台_5ml_V3、
   高温高压微反应平台明确接受 10ml耐压反应管时，可由 container_plan 在流程起点登记，
   并把该 operation 要求的初始无盖状态一并登记；不要求另有物料站取得/开盖该空反应管，
   也不得据此报 NO_INITIAL_CONTAINER_ACQUISITION 或 UNKNOWN_REACTION_TUBE_LID_STATE。
   这一例外只解决专用空容器的来源；容器中的样品、盖态和后续状态仍须由 workflow、
   reagent_slot_plan 或明确的 offline_handoff 建立。
16a. 高温高压微反应平台的 `反应管编号` 是托盘位置 1–4，不是全局样品/容器标签。超过 4 管
    必须分批执行并复用 1–4 位置；用 sample_lineage 记录每个 sample_id 对应的批次与位置。
    不得把 R1–R18 等标签直接下发为托盘编号，也不得因合法的分批位置复用判样品被覆盖。
17. macro/device plan 中明确要求的前置混合、分段加料、中间搅拌或合并后搅拌不可省略。
   若专用反应容器不被常温搅拌站接受，应在兼容容器中执行该前置步骤，再依平台默认联通规则
   做一次最短必要转移，或把已完成预混的确定配液作为有谱系的输入；不得用后续高温反应内
   搅拌替代反应前预混。当前 workflow 漏掉此类步骤时必须重写补齐，不能保留条件性省略。

你不能修改 macro plan 的研究目标、样品集合、化学路线或化学数值。你可以在重写时：
- 插入、删除、重排或拆分设备级步骤；
- 选择真源中等价且兼容的工作站操作；
- 修复容器编排、开关盖、转移、配平、参数结构和时序展开；
- 补全 Skill 明确要求且能从 macro/device plan 唯一确定的字段。

如果当前 workflow 已完全可执行，返回 verdict=executable。若不可执行且允许重写，必须返回
verdict=rewritten 和完整 replacement workflow_json；workflow_txt 将由调用方从 JSON 确定性生成，
不要返回 workflow_txt。若不改变 macro plan 就无法修复，
或本轮禁止再次重写，返回 verdict=not_executable。只输出一个 JSON object，不要 Markdown。
""".strip()


WORKFLOW_SKILL_REVIEW_TASK_PROMPT = """
请依据完整工作站真源审核当前 workflow。

## 本轮策略
{review_policy}

## Research handoff 与 macro/device plan
{macro_plan_json}

## 当前 workflow_txt
{workflow_txt}

## 当前 workflow_json
{workflow_json}

## 完整工作站 Skill、根规则与 AUDIT-RULES
{all_workstation_skills}

## 输出格式
返回且只返回：
{{
  "verdict": "executable | rewritten | not_executable",
  "summary": "结论和关键原因",
  "issues": [
    {{
      "code": "稳定的英文错误码",
      "severity": "error",
      "step_numbers": [1, 2],
      "reason": "为什么不满足输入输出或运行要求",
      "skill_evidence": "对应工作站规则的简短依据"
    }}
  ],
  "workflow_json": {{"steps": []}}
}}

判定要求：
- 不要因为 JSON 看起来规范就判 executable；必须按容器逐步推演输入状态和输出状态。
- 明确输入约束没有前序证据时属于错误，不得把 unknown 当作满足。
- verdict=rewritten 时必须返回完整 workflow_json，而不是 patch、建议或局部步骤；不要返回
  workflow_txt，调用方会从同一 JSON 生成，避免两份执行表示错位。
- 重写后的每一步必须保留 scalar source_plan_step 以及正确的 source_macro_step 与
  source_macro_steps；不得为多来源追踪
  复制物理步骤；macro_action_id 和 observation_point_id 在原计划存在时也必须保留。
- 不得用 notes 代替工作站真正需要的参数或转移目标。
- 只校验 operation 参数表明确开放的字段；不得要求或虚构设备固定/默认参数。
- 若工作站说明文字与 deterministic_validation 中的平台下发 schema 冲突，以同一
  lab-design-all 目录内的实际下发 schema 为准；必须改用可映射的工作站组合，不得忽略
  formatter_unmapped_step、unknown_operation 或容器枚举错误。
- offline_handoffs 和 temporal_adaptations 是计划边界；前者按交接闭环审核，后者优先拆步修复。
- verdict=not_executable 必须指出真源中不可替代的硬能力缺口；若 device_plan 已提供可映射路径，
  必须优先返回 verdict=rewritten 和完整 workflow。
- 判 not_executable 前必须检查所有工作站 Skill，并尝试用多个合法 operation 组合替代单站缺失
  参数；存在跨站等价链时必须返回 verdict=rewritten。
- 容器数量规则按全局活跃库存审核；不同既有编号子集之间的分批切换不是新增容器，不得据此
  判容量冲突或 not_executable。
- 体积限制默认按目标容器计算，不得把多个容器的总用量误当成单瓶超限；多个工作站的合法
  分段加液可以共同建立目标体积和最终样品状态。
- issues/reason 已描述出一条满足全部参数和状态约束的替代链时，verdict 不得为
  not_executable，必须返回 rewritten 并实际展开该链。
""".strip()


@dataclass
class SingleDeviceAgentState:
    research_handoff: Dict[str, Any]
    exp_id: str
    iteration_id: int = 0
    workflow_id: int = 0
    workstation_descriptions: str = ""
    device_truth_sha256: str = ""
    loaded_workstation_skills: List[Dict[str, Any]] = field(default_factory=list)
    skill_load_events: List[Dict[str, Any]] = field(default_factory=list)
    txt_format_reference: str = ""
    json_format_reference: str = ""
    status: str = "running"
    workflow_txt: str = ""
    workflow_json: Dict[str, Any] = field(default_factory=dict)
    terminal_package: Dict[str, Any] = field(default_factory=dict)
    raw_llm_output: Dict[str, Any] = field(default_factory=dict)
    feasibility_accepted: bool = False
    feasibility_certificate: Dict[str, Any] = field(default_factory=dict)
    semantic_analysis: Dict[str, Any] = field(default_factory=dict)
    workflow_repair_history: List[Dict[str, Any]] = field(default_factory=list)
    workflow_repair_cycles: List[Dict[str, Any]] = field(default_factory=list)
    device_plan_rewrite_count: int = 0
    errors: List[str] = field(default_factory=list)
    llm_diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    llm_request_attempts: List[Dict[str, Any]] = field(default_factory=list)
    feasibility_progress: List[Dict[str, Any]] = field(default_factory=list)
    checkpoints: Dict[str, Any] = field(default_factory=dict)
    logs: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def add_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.logs.append(f"[{timestamp}] {message}")

    def add_error(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.errors.append(f"[{timestamp}] {message}")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SingleDeviceAgent:
    """One LLM agent for device-layer workflow mapping."""

    # Several focused tests and lightweight integrations construct the agent
    # with ``__new__`` and inject only the collaborators they exercise.  Keep
    # that legacy path on V1 unless the caller explicitly opts into V2.
    _contract_version = "v1"

    def __init__(
        self,
        model: Any,
        *,
        use_new_format: bool = True,
        workstation_loader: Optional[WorkstationLoader] = None,
        workflow_validator: Optional[WorkflowValidator] = None,
        contract_version: str = "v1",
        workflow_repair_limit: Optional[int] = None,
    ) -> None:
        if contract_version not in {"v1", "v2"}:
            raise ValueError("contract_version must be 'v1' or 'v2'")
        if workflow_repair_limit is not None and workflow_repair_limit < 0:
            raise ValueError("workflow_repair_limit cannot be negative")
        self._model = model
        self._contract_version = contract_version
        self._configured_workflow_repair_limit = workflow_repair_limit
        # Natural-language chemistry semantics are supplied by a separate LLM
        # analysis and kept in runtime state.  Model-generated plan JSON cannot
        # overwrite this contract.
        self._active_semantic_analysis: Dict[str, Any] = {}
        self._checkpoint_store: Optional[DeviceCheckpointStore] = None
        self._workstation_loader = workstation_loader or WorkstationLoader(
            use_new_format=use_new_format
        )
        self._workflow_validator = workflow_validator or WorkflowValidator(
            self._workstation_loader
        )
        self._dispatch_catalog = DispatchCatalog.load(self._workstation_loader)
        self._skill_session: Optional[WorkstationSkillSession] = None
        self._txt_format_reference = self._read_text(format_reference_path("txt"))
        self._json_format_reference = self._read_text(format_reference_path("json"))
        try:
            self._contract_engine: Optional[SkillContractEngine] = SkillContractEngine(
                workstation_dir(use_new_format=True)
            )
        except Exception as exc:  # pragma: no cover - catalog parse failure
            logging.getLogger(__name__).warning(
                "SkillContractEngine unavailable, falling back to legacy checks: %s", exc
            )
            self._contract_engine = None
        # Runtime-only approval channel.  It is populated only after a typed,
        # one-shot resume capability is consumed; no JSON plan field is ever
        # trusted as human approval.
        self._active_trusted_human_quantity_approvals: List[
            Dict[str, Any]
        ] = []

    def _device_snapshot_id(self) -> str:
        loader = self._workstation_loader
        if hasattr(loader, "snapshot_id"):
            try:
                return loader.snapshot_id()
            except Exception:
                return ""
        return ""

    def _workstation_skill_session(self) -> WorkstationSkillSession:
        if self._skill_session is None:
            self._skill_session = WorkstationSkillSession(
                self._workstation_loader, self._dispatch_catalog, self._workflow_validator
            )
        return self._skill_session

    def _full_device_truth_digest(self) -> str:
        session = self._workstation_skill_session()
        session.assert_current()
        return session.truth_digest()

    def _refresh_workstation_snapshot(self) -> None:
        """Begin each run with matching loader, wire and validator snapshots."""
        self._skill_session = None
        refresh_validator = getattr(self._workflow_validator, "refresh", None)
        if not callable(refresh_validator):
            raise DeviceConfigurationError(
                "Configured workflow_validator must provide refresh(workstation_loader) "
                "to rebuild its own facts without discarding caller source paths"
            )
        auxiliary_before = WorkstationSkillSession.auxiliary_source_manifest(
            self._dispatch_catalog, self._workflow_validator
        )
        self._workstation_loader.refresh_truth_snapshot()
        self._dispatch_catalog = DispatchCatalog.load(self._workstation_loader)
        refresh_validator(self._workstation_loader)
        self._contract_engine = SkillContractEngine(self._workstation_loader.truth_source_root())
        self._txt_format_reference = self._read_text(format_reference_path("txt"))
        self._json_format_reference = self._read_text(format_reference_path("json"))
        self._workstation_loader.assert_snapshot_current()
        if auxiliary_before != WorkstationSkillSession.auxiliary_source_manifest(
            self._dispatch_catalog, self._workflow_validator
        ):
            raise WorkstationTruthChangedError("Wire or validator references changed while rebuilding the Device snapshot")
        self._skill_session = WorkstationSkillSession(
            self._workstation_loader, self._dispatch_catalog, self._workflow_validator
        )

    def _assert_workstation_snapshot_current(self, state: SingleDeviceAgentState) -> None:
        if not state.device_truth_sha256:
            return  # Standalone pure-text helper tests have no active Device run.
        current = self._full_device_truth_digest()
        if current != state.device_truth_sha256:
            raise WorkstationTruthChangedError("Device run's workstation snapshot binding changed")

    def _sync_skill_load_state(self, state: SingleDeviceAgentState) -> None:
        session = self._workstation_skill_session()
        state.loaded_workstation_skills = session.manifest()
        state.skill_load_events = copy.deepcopy(session.events)

    @staticmethod
    def _plan_signature(research_handoff: Dict[str, Any]) -> str:
        """Stable signature of the macro steps this device attempt worked on
        (issue #4: research must be able to refuse regenerating a failed
        route). Mirrors the research-side idea: ops + reagents + numbers."""
        import hashlib

        parts: List[str] = []
        for step in research_handoff.get("macro_action_steps", []) or []:
            if not isinstance(step, dict):
                continue
            numbers = re.findall(
                r"\d+(?:\.\d+)?", str(step.get("参数", ""))
            )
            parts.append(
                f"{step.get('操作', '')}|{step.get('试剂/对象', '')}|{','.join(numbers)}"
            )
        digest = hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()[:12]
        return f"plan_{digest}"

    @staticmethod
    def _stable_digest(value: Any, *, prefix: str = "sha256") -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return f"{prefix}_{hashlib.sha256(encoded).hexdigest()}"

    @staticmethod
    def _safe_model_attribute(model: Any, name: str) -> Any:
        try:
            value = getattr(model, name, None)
        except Exception:
            return None
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, (list, tuple)) and all(
            isinstance(item, (str, int, float, bool)) for item in value
        ):
            return list(value)
        return None

    @classmethod
    def _planning_model_checkpoint_binding(cls, model: Any) -> Dict[str, Any]:
        """Describe output-affecting model config without persisting secrets."""

        def sanitized_endpoint(endpoint_text: str) -> Tuple[str, str]:
            """Keep routing identity while removing URL credentials."""
            credential_query_names = {
                "api_key", "apikey", "key", "token", "access_token",
                "auth", "authorization", "credential", "credentials",
                "password", "passwd", "secret", "signature", "sig",
                "subscription_key",
            }

            def is_credential_query(name: str) -> bool:
                normalized = name.strip().lower().replace("-", "_").replace(".", "_")
                return normalized in credential_query_names or normalized.endswith(
                    ("_api_key", "_token", "_secret", "_password", "_signature")
                )

            try:
                parsed = urlsplit(endpoint_text)
                hostname = (parsed.hostname or "").lower()
                if not hostname:
                    raise ValueError("endpoint has no hostname")
                host_for_url = f"[{hostname}]" if ":" in hostname else hostname
                netloc = host_for_url
                if parsed.port is not None:
                    netloc += f":{parsed.port}"
                safe_query = urlencode(
                    sorted(
                        (name, value)
                        for name, value in parse_qsl(
                            parsed.query, keep_blank_values=True
                        )
                        if not is_credential_query(name)
                    ),
                    doseq=True,
                )
                sanitized = urlunsplit(
                    (
                        parsed.scheme.lower(),
                        netloc,
                        parsed.path,
                        safe_query,
                        "",
                    )
                )
                return hostname, sanitized
            except (TypeError, ValueError):
                # Never hash a malformed raw endpoint: it may itself be a
                # credential-bearing string. Keep only a non-secret marker.
                return "", "invalid_or_relative_endpoint"

        def one(candidate: Any) -> Dict[str, Any]:
            candidate_type = type(candidate)
            identity: Dict[str, Any] = {
                "adapter": (
                    f"{candidate_type.__module__}."
                    f"{candidate_type.__qualname__}"
                )
            }
            for attribute in (
                "model_name",
                "model",
                "reasoning_effort",
                "_reasoning_effort",
                "max_output_tokens",
                "max_tokens",
                "temperature",
                "disable_thinking",
                "do_sample",
            ):
                value = cls._safe_model_attribute(candidate, attribute)
                if value not in (None, ""):
                    identity[attribute] = value
            endpoint = None
            for attribute in ("base_url", "endpoint_url", "_base_url"):
                endpoint = cls._safe_model_attribute(candidate, attribute)
                if endpoint not in (None, ""):
                    break
            if endpoint not in (None, ""):
                endpoint_text = str(endpoint)
                endpoint_host, endpoint_route = sanitized_endpoint(endpoint_text)
                if endpoint_host:
                    identity["endpoint_host"] = endpoint_host
                # Userinfo, credential query values and fragment are removed
                # before hashing. Non-secret query parameters such as an API
                # version remain part of the logical route binding.
                identity["endpoint_config_sha256"] = checkpoint_digest(
                    endpoint_route
                )
            return identity

        # Inspect pool members only after a guarded getattr; never stringify
        # backend objects because clients may retain credentials internally.
        try:
            raw_backends = getattr(model, "backends", None)
        except Exception:
            raw_backends = None
        candidates = (
            list(raw_backends)
            if isinstance(raw_backends, (list, tuple)) and raw_backends
            else [model]
        )
        binding: Dict[str, Any] = {
            "adapter": one(model)["adapter"],
            "backends": [one(candidate) for candidate in candidates],
            "wire_api": os.getenv("REFINER_LLM_WIRE_API", "chat")
            .strip()
            .lower(),
            "provider": os.getenv("REFINER_LLM_MODEL_PROVIDER", "")
            .strip()
            .lower(),
            "feasibility_reasoning_effort": os.getenv(
                "CHEM_DEVICE_FEASIBILITY_REASONING_EFFORT", ""
            ).strip(),
        }
        for attribute in ("max_rounds", "round_backoff_seconds"):
            value = cls._safe_model_attribute(model, attribute)
            if value not in (None, ""):
                binding[attribute] = value
        return binding

    @classmethod
    def _strip_untrusted_approval_fields(
        cls, value: Any
    ) -> Tuple[Any, Set[str]]:
        """Remove every JSON-spellable human-approval trust marker.

        Valid approvals travel only through a typed runtime bundle.  This
        recursive scrub is applied to Research handoffs, LLM candidates,
        rewrites and overrides so a model cannot hide a reserved field in a
        nested sidecar.
        """

        reserved = {
            "_trusted_human_quantity_approvals",
            "human_quantity_approval_validation",
            "validated_human_quantity_approvals",
            "human_quantity_approvals",
            "approval_capability_token",
        }
        found: Set[str] = set()

        def scrub(item: Any) -> Any:
            if isinstance(item, dict):
                cleaned: Dict[str, Any] = {}
                for key, nested in item.items():
                    key_text = str(key)
                    if key_text in reserved:
                        found.add(key_text)
                        continue
                    cleaned[key] = scrub(nested)
                return cleaned
            if isinstance(item, list):
                return [scrub(nested) for nested in item]
            return copy.deepcopy(item)

        return scrub(value), found

    @staticmethod
    def _plan_is_accepted(plan_result: Dict[str, Any]) -> bool:
        return str(plan_result.get("status", "")).strip().lower() in {
            "device_plan",
            "success",
        }

    @staticmethod
    def _promote_feasible_quantity_human_plan(
        plan_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Let a feasible Case-C plan reach certificate issuance first."""
        if str(plan_result.get("status", "")).strip().lower() != "manual_required":
            return plan_result
        feasibility = plan_result.get("feasibility")
        quantity_audit = plan_result.get("quantity_audit")
        is_quantity_human = bool(
            isinstance(feasibility, dict)
            and feasibility.get("is_feasible") is True
            and isinstance(quantity_audit, dict)
            and quantity_audit.get("status") == "human_review_required"
        )
        if not is_quantity_human:
            return plan_result
        promoted = copy.deepcopy(plan_result)
        if not isinstance(promoted.get("device_plan"), list) or not promoted.get(
            "device_plan"
        ):
            return {
                "status": "failed",
                "feedback_type": "device_internal_error",
                "feedback_route": "device",
                "failure_scope": "device_internal",
                "feasibility_accepted": False,
                "feasibility_certificate": {},
                "workflow_txt": "",
                "workflow_json": {},
                "dispatch_validation": {
                    "status": "failed",
                    "errors": [
                        "feasible quantity-human Stage1 output omitted the complete device_plan"
                    ],
                    "warnings": [],
                    "checked_steps": 0,
                    "assessment_source": "stage1_output_contract_internal",
                    "_device_internal_error": True,
                },
                "error_package": {
                    "type": "device_internal_error",
                    "message": (
                        "路线可行但数量需人工判断时，Stage1 必须返回完整 best-known "
                        "device_plan 以便先签发 feasibility certificate。"
                    ),
                },
            }
        promoted["status"] = "device_plan"
        promoted["pending_quantity_human_review"] = copy.deepcopy(quantity_audit)
        return promoted

    @staticmethod
    def _constraint_is_device_local(text: Any) -> bool:
        value = str(text or "")
        if re.search(
            r"剂量|物质的量|质量不足|用量|料位|累计取液|重复消费|重复计量|"
            r"容器|容量|体积|分批|拆批|分瓶|配平|瓶盖|开盖|关盖",
            value,
            re.I,
        ):
            return True
        if re.search(
            r"schema|参数|字段|枚举|translation|workflow|skill|"
            r"unknown_operation|invalid_operation|operation.{0,16}(?:参数|名称|映射|无效)",
            value,
            re.I,
        ):
            return True
        return False

    @staticmethod
    def _constraint_is_true_route_gap(text: Any) -> bool:
        value = str(text or "")
        if re.search(r"离线|offline|停机|不可用", value, re.I):
            return bool(
                re.search(r"无.{0,8}替代|没有.{0,8}替代|no\s+alternative", value, re.I)
                and re.search(r"必需|必须|必要|唯一|required|mandatory", value, re.I)
            )
        mandatory_safety = bool(
            re.search(r"安全|防爆|惰性|无氧|高压|耐压|mandatory\s+safety", value, re.I)
            and re.search(r"无法满足|不能满足|不支持|缺少|不存在|unavailable", value, re.I)
        )
        if mandatory_safety:
            return True
        if SingleDeviceAgent._constraint_is_device_local(value):
            return False
        missing_operation = bool(
            re.search(r"缺少|不存在|无任何|没有|不支持|无法实现|cannot|missing", value, re.I)
            and re.search(
                r"必要.{0,8}(?:化学|操作|反应)|化学操作|反应釜|反应器|"
                r"工作站|设备能力|operation|连续流|微流控|载体.{0,12}(?:处理|取出|洗涤|干燥)",
                value,
                re.I,
            )
        )
        return missing_operation

    @staticmethod
    def _explicit_temperature_ceiling(text: Any) -> Optional[float]:
        """Read only an explicitly declared station temperature ceiling.

        A random temperature example is not a capability limit.  The patterns
        below deliberately accept only ``highest/max/upper-bound`` wording or
        a parameter range whose name is temperature.  This keeps the route
        gate proof-only: an ambiguous Skill can force human review, but can
        never manufacture a Research replan.
        """
        value = str(text or "")
        ceilings: List[float] = []
        patterns = (
            r"(?:最高(?:支持)?温度|最高支持|最高|最大温度|温度上限)"
            r"[^\d]{0,16}(\d+(?:\.\d+)?)\s*(?:℃|°\s*C|摄氏度)",
            r"温度[^\n|]{0,80}?\[\s*-?\d+(?:\.\d+)?\s*,\s*"
            r"(-?\d+(?:\.\d+)?)\s*\]",
            r"温度[^\n。；;]{0,40}?(?:不得超过|不超过|最大(?:为)?|上限(?:为)?)"
            r"\s*(\d+(?:\.\d+)?)\s*(?:℃|°\s*C|摄氏度)",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, value, re.I):
                try:
                    ceilings.append(float(match.group(1)))
                except (TypeError, ValueError):
                    continue
        return max(ceilings) if ceilings else None

    def _complete_lab_design_truth(self) -> Tuple[List[Dict[str, Any]], str]:
        """Return the full lab-design-all roster, or a reason it is unproved.

        The capability index is used only as the roster manifest.  Capability
        facts still come from every loaded per-workstation SKILL.md.  A custom
        loader, a missing/partial Skill, or a roster mismatch therefore cannot
        be used to prove absence of a combined capability.
        """
        try:
            entries = [
                entry
                for entry in self._workstation_loader.get_all()
                if isinstance(entry, dict)
            ]
        except Exception as exc:
            return [], f"cannot enumerate workstation truth: {exc}"
        index_path = chem_resources_root() / "workstation_capability_index.json"
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return [], f"capability-index manifest unavailable: {exc}"
        if not isinstance(index, dict) or index.get("source_kind") != "lab-design-all":
            return [], "capability-index manifest is not lab-design-all"
        manifest_records = index.get("workstations")
        if not isinstance(manifest_records, list):
            return [], "capability-index manifest omits workstation roster"
        manifest_codes = {
            str(item.get("station_code", "")).strip()
            for item in manifest_records
            if isinstance(item, dict) and str(item.get("station_code", "")).strip()
        }
        loaded_codes = {
            str(item.get("station_name", "")).strip()
            for item in entries
            if str(item.get("station_name", "")).strip()
        }
        expected_count = index.get("workstation_count")
        if (
            not manifest_codes
            or not isinstance(expected_count, int)
            or expected_count != len(manifest_codes)
            or loaded_codes != manifest_codes
        ):
            return [], (
                "loaded workstation roster does not exactly match the complete "
                "lab-design-all manifest"
            )
        for entry in entries:
            content = str(
                entry.get("skill_content", "")
                or entry.get("usage_content", "")
                or ""
            ).strip()
            station_path = str(entry.get("station_path", ""))
            if not content:
                return [], (
                    f"workstation {entry.get('station_name', '?')} has no Skill truth"
                )
            if "lab-design-all" not in station_path:
                return [], (
                    f"workstation {entry.get('station_name', '?')} is not loaded "
                    "from lab-design-all"
                )
        return entries, ""

    @staticmethod
    def _text_mentions_truth_alias(text: str, alias: str) -> bool:
        """Match one station alias without letting short Latin tokens leak.

        ``LC`` must not match the middle of an English word, while Chinese
        display names and full workstation codes are safe substring matches.
        """
        alias = str(alias or "").strip()
        if not alias:
            return False
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]*", alias):
            return bool(
                re.search(
                    rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])",
                    text,
                    re.I,
                )
            )
        return alias.casefold() in text.casefold()

    def _constraint_contradicted_by_workstation_truth(self, text: Any) -> bool:
        """Return true only for a *bare existence* claim disproved by truth.

        This is deliberately narrower than capability matching.  Complete
        ``lab-design-all`` truth can refute ``XRD 工作站不存在`` because the
        named station is present.  Mere station presence cannot refute a
        qualified joint requirement (temperature + atmosphere, in-situ
        operation, a particular container/parameter range), nor a live
        offline/maintenance report.  Incomplete/custom loaders prove nothing.
        """
        value = str(text or "").strip()
        if not value or not re.search(
            r"缺少|缺失|不存在|未配置|没有(?:任何)?|无任何|"
            r"\bmissing\b|does\s+not\s+exist|not\s+(?:present|configured)",
            value,
            re.I,
        ):
            return False
        # Availability and compound-capability claims require a different,
        # live or conjunctive proof; roster membership alone is insufficient.
        if re.search(
            r"离线|停机|故障|维护|不可用|\boffline\b|\bunavailable\b|"
            r"同时|联合|兼容|组合|原位|in[- ]?situ|"
            r"温度|℃|°\s*C|摄氏度|压力|MPa|\bbar\b|气氛|"
            r"\bH\s*2\b|H₂|氢气|精度|波长|扫描范围|参数范围|"
            r"容器|载体|样品状态",
            value,
            re.I,
        ):
            return False

        entries, incomplete_reason = self._complete_lab_design_truth()
        if incomplete_reason:
            return False

        # Short scientific aliases do not always appear in a Chinese display
        # name.  Bind them to an exact loaded station code rather than merely
        # searching arbitrary Skill prose.
        known_aliases: Dict[str, Tuple[str, ...]] = {
            "High_Temperature_High_Pressure_Microreaction_Platform_V1": (
                "反应釜",
                "反应器",
                "高压釜",
                "高温高压微反应平台",
            ),
            "XRD_V1": ("XRD", "PXRD", "X射线衍射", "X射线衍射仪"),
            "Infrared_Spectrometer_V1": ("FTIR", "红外光谱", "红外光谱仪"),
            "UV_Vis_Spectrometer_V1": (
                "UV-Vis",
                "UV Vis",
                "紫外可见光谱",
                "紫外可见光谱仪",
            ),
            "Fluorescence_Spectrometer_V1": ("荧光光谱", "荧光光谱仪"),
            "Gas_Chromatograph_V1": ("GC", "气相色谱", "气相色谱仪"),
            "Liquid_Chromatograph_V1": ("HPLC", "液相色谱", "液相色谱仪"),
            "Microplate_Reader_V1": ("酶标仪", "microplate reader"),
        }
        for entry in entries:
            code = str(entry.get("station_name", "") or "").strip()
            display = str(entry.get("display_name", "") or "").strip()
            aliases: Set[str] = {code, display}
            aliases.update(known_aliases.get(code, ()))
            code_without_version = re.sub(r"_V\d+$", "", code, flags=re.I)
            if re.fullmatch(r"[A-Z0-9]{2,12}", code_without_version):
                aliases.add(code_without_version)
            display_without_version = re.sub(
                r"[_（(]?V\d+[）)]?$", "", display, flags=re.I
            ).strip()
            if display_without_version:
                aliases.add(display_without_version)
            if any(
                self._text_mentions_truth_alias(value, alias)
                for alias in aliases
            ):
                return True
        return False

    @classmethod
    def _constraint_grounded_in_frozen_research(
        cls,
        text: Any,
        research_handoff: Dict[str, Any],
    ) -> bool:
        """Require every LLM-only hard claim to name a frozen requirement.

        This is a conservative grounding gate, not a capability proof.  It
        prevents an unrelated blocker (for example 450 °C H2 reduction on an
        XRD-only plan) from changing the Research route.  Unknown or vague hard
        claims stay human-reviewable until a deterministic verifier exists.
        """
        blocker = str(text or "")
        macros = [
            item
            for item in research_handoff.get("macro_action_steps", []) or []
            if isinstance(item, dict)
        ]
        all_research = "\n".join(_json_text(item) for item in macros)
        operational_research = "\n".join(
            _json_text(item)
            for item in macros
            if not cls._is_observation_only_macro(item)
        )

        if cls._requires_controlled_hydrogen_reduction(blocker):
            return cls._requires_controlled_hydrogen_reduction(
                operational_research
            )

        instrument_patterns = (
            r"XRD|PXRD|XPS|SEM|TEM|Raman|XAS|FTIR|UV[- ]?Vis|"
            r"气相色谱|液相色谱|GC|HPLC|电化学",
        )
        if re.search(instrument_patterns[0], blocker, re.I):
            return bool(re.search(instrument_patterns[0], all_research, re.I))

        if re.search(r"反应釜|反应器|高压釜|reactor", blocker, re.I):
            return bool(
                re.search(
                    r"反应釜|反应器|高压釜|水热|溶剂热|高压.{0,12}反应|reactor",
                    operational_research,
                    re.I,
                )
            )

        core_patterns = (
            r"水热|溶剂热|煅烧|焙烧|退火|氧化|还原|聚合|沉淀|络合|"
            r"hydrothermal|solvothermal|calcination|anneal|oxid|reduc"
        )
        core_match = re.search(core_patterns, blocker, re.I)
        if core_match:
            concept = core_match.group(0)
            return bool(re.search(re.escape(concept), operational_research, re.I))

        safety_patterns = r"防爆|惰性|无氧|耐压|高压|气氛|压力|MPa|bar"
        mentioned_safety = re.findall(safety_patterns, blocker, re.I)
        if mentioned_safety:
            return all(
                re.search(re.escape(item), operational_research, re.I)
                for item in mentioned_safety
            )

        # A generic "missing necessary workstation/operation" sentence does
        # not identify which frozen requirement is impossible, so it cannot
        # consume a Research iteration or participate in deadlock.
        return False

    @staticmethod
    def _requires_controlled_hydrogen_reduction(text: Any) -> bool:
        value = str(text or "")
        has_hydrogen = bool(
            re.search(r"(?:\bH\s*2\b|H₂|氢气)", value, re.I)
        )
        has_reduction = bool(re.search(r"还原|reduc(?:e|ed|tion)", value, re.I))
        return has_hydrogen and has_reduction

    @staticmethod
    def _station_supports_hydrogen_reduction(text: Any) -> bool:
        value = str(text or "")
        return bool(
            re.search(
                r"氢气还原|催化加氢|(?:\bH\s*2\b|H₂).{0,40}(?:还原|反应|预处理)"
                r"|(?:还原|反应|预处理).{0,40}(?:\bH\s*2\b|H₂)",
                value,
                re.I,
            )
        )

    def _prove_high_temperature_hydrogen_gap(
        self,
        requirement_text: str,
    ) -> Dict[str, Any]:
        """Prove that no *single* station meets temperature + H2 reduction.

        This intentionally implements only the currently required, fully
        deterministic B01 class.  Other core offline chemistry stays a Device
        human-review finding until an equally strong truth-source proof exists.
        """
        if not self._requires_controlled_hydrogen_reduction(requirement_text):
            return {
                "status": "unverified",
                "reason": "requirement is not a controlled hydrogen-reduction condition",
            }
        temperatures = []
        for match in re.finditer(
            r"(-?\d+(?:\.\d+)?)\s*(?:℃|°\s*C|摄氏度)",
            requirement_text,
            re.I,
        ):
            try:
                temperatures.append(float(match.group(1)))
            except ValueError:
                continue
        if not temperatures:
            return {
                "status": "unverified",
                "reason": "required hydrogen-reduction temperature is not explicit",
            }
        required_temperature = max(temperatures)
        entries, incomplete_reason = self._complete_lab_design_truth()
        if incomplete_reason:
            return {
                "status": "unverified",
                "reason": incomplete_reason,
                "required_temperature_c": required_temperature,
            }

        hydrogen_capable: List[Dict[str, Any]] = []
        high_temperature_only: List[Dict[str, Any]] = []
        indeterminate_hydrogen: List[str] = []
        compatible: List[str] = []
        for entry in entries:
            code = str(entry.get("station_name", ""))
            content = str(
                entry.get("skill_content", "")
                or entry.get("usage_content", "")
                or ""
            )
            ceiling = self._explicit_temperature_ceiling(content)
            supports_hydrogen = self._station_supports_hydrogen_reduction(content)
            if supports_hydrogen:
                if ceiling is None:
                    indeterminate_hydrogen.append(code)
                    continue
                hydrogen_capable.append(
                    {"workstation": code, "temperature_ceiling_c": ceiling}
                )
                if ceiling >= required_temperature:
                    compatible.append(code)
            elif ceiling is not None and ceiling >= required_temperature:
                high_temperature_only.append(
                    {"workstation": code, "temperature_ceiling_c": ceiling}
                )
        if indeterminate_hydrogen:
            return {
                "status": "unverified",
                "reason": (
                    "hydrogen-reduction station has no explicit temperature ceiling"
                ),
                "required_temperature_c": required_temperature,
                "indeterminate_workstations": indeterminate_hydrogen,
            }
        if compatible:
            return {
                "status": "satisfied",
                "required_temperature_c": required_temperature,
                "compatible_workstations": compatible,
            }
        return {
            "status": "proved_gap",
            "proof_kind": "single_station_joint_capability_absence",
            "truth_source": "complete lab-design-all per-workstation SKILL roster",
            "truth_workstation_count": len(entries),
            "required_temperature_c": required_temperature,
            "required_atmosphere": "controlled hydrogen reduction (H2/Ar)",
            "hydrogen_reduction_workstations": hydrogen_capable,
            "high_temperature_without_hydrogen_reduction": high_temperature_only,
            "compatible_workstations": [],
        }

    def _has_verified_complete_joint_capability_proof(
        self,
        state: SingleDeviceAgentState,
        result: Dict[str, Any],
    ) -> bool:
        """Accept only the canonical proof derived from frozen Research state.

        Recomputing from an arbitrary blocker string is insufficient: a model
        could attach the deterministic marker and an unrelated 450 °C H2 text
        to an XRD-only Research plan.  The supplied proof must exactly match
        the proof generated from this state's frozen macro steps and the
        current complete workstation truth.
        """
        if str(result.get("assessment_source", "")).strip() != (
            "deterministic_complete_workstation_joint_capability_audit"
        ):
            return False
        feasibility = result.get("feasibility")
        if not isinstance(feasibility, dict):
            return False
        supplied = feasibility.get("deterministic_route_gap_proof")
        if not isinstance(supplied, list):
            return False
        canonical = self._verified_stage1_core_route_gap_result(state, {})
        if not isinstance(canonical, dict):
            return False
        canonical_feasibility = canonical.get("feasibility")
        if not isinstance(canonical_feasibility, dict):
            return False
        expected = canonical_feasibility.get("deterministic_route_gap_proof")
        if not isinstance(expected, list) or not expected:
            return False
        return self._stable_digest(
            supplied, prefix="deterministic_route_gap_proof"
        ) == self._stable_digest(
            expected, prefix="deterministic_route_gap_proof"
        )

    def _workflow_repair_limit(self) -> int:
        if self._configured_workflow_repair_limit is not None:
            return min(10, self._configured_workflow_repair_limit)
        raw = os.getenv("CHEM_DEVICE_WORKFLOW_REPAIR_LIMIT", "").strip()
        if raw.isdigit() and int(raw) >= 0:
            return min(10, int(raw)) if self._contract_version == "v2" else int(raw)
        return 10 if self._contract_version == "v2" else DEFAULT_WORKFLOW_REPAIR_LIMIT

    @staticmethod
    def _unique_json_records(records: List[Any]) -> List[Any]:
        unique: Dict[str, Any] = {}
        for record in records:
            key = json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            unique.setdefault(key, record)
        return [unique[key] for key in sorted(unique)]

    @classmethod
    def _extract_sample_control_matrix(cls, payload: Any) -> List[Any]:
        """Extract explicit sample/control/variable records without guessing.

        The certificate stores only fields already present in Research or the
        accepted device plan.  Container ids alone are intentionally excluded:
        changing a bottle is legal, changing a sample/control identity is not.
        """
        records: List[Any] = []
        matrix_keys = {
            "sample_matrix",
            "sample_control_matrix",
            "samples",
            "sample_groups",
            "control_groups",
            "experiment_groups",
            "variable_matrix",
            "样品矩阵",
            "样品组",
            "对照组",
            "变量矩阵",
        }
        identity_keys = {
            "sample_id",
            "control_id",
            "group_id",
            "sample_group",
            "control_group",
            "variable_level",
            "样品编号",
            "对照编号",
            "样品组",
            "对照组",
            "变量水平",
        }

        def _walk(value: Any, parent_key: str = "") -> None:
            if isinstance(value, dict):
                has_identity = any(
                    str(key).strip().lower() in identity_keys
                    for key in value
                )
                explicit = {}
                if has_identity:
                    scientific_field = re.compile(
                        r"sample|control|group|variable|level|condition|composition|"
                        r"样品|对照|组|变量|水平|条件|配方",
                        re.I,
                    )
                    explicit = {
                        str(key): copy.deepcopy(item)
                        for key, item in value.items()
                        if scientific_field.search(str(key))
                    }
                if explicit:
                    records.append(explicit)
                for key, item in value.items():
                    normalized_key = str(key).strip().lower()
                    if normalized_key in matrix_keys:
                        records.append({str(key): copy.deepcopy(item)})
                    _walk(item, normalized_key)
            elif isinstance(value, list):
                for item in value:
                    _walk(item, parent_key)

        _walk(payload)
        return cls._unique_json_records(records)

    @classmethod
    def _sample_matrix_contract_value(cls, payload: Any) -> Any:
        matrix_keys = {
            "sample_matrix",
            "sample_control_matrix",
            "sample_groups",
            "control_groups",
            "experiment_groups",
            "variable_matrix",
            "样品矩阵",
            "样品组",
            "对照组",
            "变量矩阵",
        }
        found: List[Any] = []

        def _walk(value: Any) -> None:
            if found:
                return
            if isinstance(value, dict):
                for key, item in value.items():
                    if str(key).strip().lower() in matrix_keys:
                        found.append(copy.deepcopy(item))
                        return
                for item in value.values():
                    _walk(item)
                    if found:
                        return
            elif isinstance(value, list):
                for item in value:
                    _walk(item)
                    if found:
                        return

        _walk(payload)
        return found[0] if found else cls._extract_sample_control_matrix(payload)

    @staticmethod
    def _extract_sample_ids(payload: Any) -> List[str]:
        ids: Set[str] = set()
        keys = {
            "sample_id",
            "control_id",
            "group_id",
            "样品编号",
            "对照编号",
        }

        def _walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if str(key).strip().lower() in keys:
                        if isinstance(item, list):
                            ids.update(str(entry).strip() for entry in item if str(entry).strip())
                        elif str(item).strip():
                            ids.add(str(item).strip())
                    _walk(item)
            elif isinstance(value, list):
                for item in value:
                    _walk(item)

        _walk(payload)
        return sorted(ids)

    def _sample_matrix_drift_errors(
        self,
        research_handoff: Dict[str, Any],
        device_plan: Dict[str, Any],
    ) -> List[str]:
        errors: List[str] = []
        research_matrix = self._sample_matrix_contract_value(research_handoff)
        device_matrix = self._sample_matrix_contract_value(device_plan)
        research_ids = self._extract_sample_ids(research_handoff)
        device_ids = self._extract_sample_ids(device_plan)
        if research_ids and research_ids != device_ids:
            errors.append(
                "device_plan 样品/对照 id 与 Research 不一致："
                f"research={research_ids}, device={device_ids}。"
            )
        if research_matrix:
            if not device_matrix:
                errors.append(
                    "Research 提供了显式样品/对照/变量矩阵，但 device_plan 未完整回显。"
                )
            elif research_matrix != device_matrix:
                errors.append(
                    "device_plan 的完整样品/对照/变量矩阵与 Research 真值不一致。"
                )
        return errors

    @staticmethod
    def _route_view(research_handoff: Dict[str, Any]) -> List[Dict[str, Any]]:
        route: List[Dict[str, Any]] = []
        for index, step in enumerate(
            research_handoff.get("macro_action_steps", []) or [], start=1
        ):
            if not isinstance(step, dict):
                continue
            route.append(
                {
                    "step": step.get("步骤序号", step.get("step", index)),
                    "operation": step.get("操作", step.get("operation", "")),
                    "reagent_or_object": step.get(
                        "试剂/对象", step.get("reagent_or_object", "")
                    ),
                }
            )
        return route

    @staticmethod
    def _identity_text(value: Any) -> str:
        return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "")).lower()

    @staticmethod
    def _source_macro_scalar(value: Any) -> Any:
        """Return the stable scalar representation used in emitted plans."""
        text = str(value).strip()
        if re.fullmatch(r"[-+]?\d+", text):
            try:
                return int(text)
            except ValueError:  # pragma: no cover - guarded by the regexp
                pass
        return text

    @classmethod
    def _source_macro_step_ids(cls, payload: Dict[str, Any]) -> List[str]:
        """Read the scalar/list trace contract without duplicating a step.

        Models occasionally put a JSON array directly in ``source_macro_step``
        for one physical operation shared by several Research steps.  Treat
        that as trace metadata, not as a request to clone the operation.
        """
        raw_values: List[Any] = []

        def append(value: Any) -> None:
            if isinstance(value, (list, tuple, set)):
                for nested in value:
                    append(nested)
                return
            if isinstance(value, str):
                stripped = value.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    try:
                        parsed = json.loads(stripped)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        parsed = None
                    if isinstance(parsed, list):
                        append(parsed)
                        return
                if re.fullmatch(r"[-+]?\d+(?:\s*[,，]\s*[-+]?\d+)+", stripped):
                    append(re.split(r"\s*[,，]\s*", stripped))
                    return
            if value not in (None, ""):
                raw_values.append(value)

        # Preserve the model's declared primary source when it is scalar;
        # otherwise the first list member becomes the deterministic primary.
        append(payload.get("source_macro_step"))
        append(payload.get("source_macro_steps"))
        result: List[str] = []
        for value in raw_values:
            source = str(value).strip()
            if source and source not in result:
                result.append(source)
        return result

    @classmethod
    def _normalize_source_macro_fields(cls, payload: Dict[str, Any]) -> bool:
        """Canonicalize trace metadata to primary scalar + complete list."""
        sources = cls._source_macro_step_ids(payload)
        if not sources:
            return False
        typed = [cls._source_macro_scalar(source) for source in sources]
        changed = (
            payload.get("source_macro_step") != typed[0]
            or payload.get("source_macro_steps") != typed
        )
        payload["source_macro_step"] = typed[0]
        payload["source_macro_steps"] = typed
        return changed

    @classmethod
    def _inherit_missing_repaired_reagent_identities(
        cls,
        research_handoff: Dict[str, Any],
        previous_plan: Dict[str, Any],
        repaired_plan: Dict[str, Any],
    ) -> int:
        """Mechanically restore omitted frozen reagent trace metadata.

        A plan-level rewrite may split or merge Device operations, but it may
        not make a new chemistry decision.  ``source_reagent_identity`` is
        trace metadata, so a missing/blank value can be restored from an
        already-certified step with the same valid source set.  A newly split
        or merged source set is *not* auto-filled from Research: that would let
        an LLM authorize a source-binding change merely by leaving identity
        blank. Such structural rewrites must carry explicit identities and a
        structured ``plan_changes`` record.

        Non-empty values are deliberately never touched: an explicit A→B
        rewrite must still reach the frozen-identity validator and be rejected.
        """

        research_identities: Dict[str, str] = {}
        for index, step in enumerate(
            research_handoff.get("macro_action_steps", []) or [], start=1
        ):
            if not isinstance(step, dict):
                continue
            source = str(step.get("步骤序号", step.get("step", index))).strip()
            raw_identity = step.get("试剂/对象", step.get("reagent_or_object", ""))
            identity = str(raw_identity).strip() if raw_identity is not None else ""
            research_identities[source] = identity
        valid_sources = set(research_identities)

        certified_by_source_set: Dict[Tuple[str, ...], List[str]] = {}
        for step in previous_plan.get("device_plan", []) or []:
            if not isinstance(step, dict):
                continue
            sources = tuple(cls._source_macro_step_ids(step))
            if not sources or any(source not in valid_sources for source in sources):
                continue
            raw_identity = step.get("source_reagent_identity")
            identity = str(raw_identity).strip() if raw_identity is not None else ""
            if not identity:
                continue
            values = certified_by_source_set.setdefault(sources, [])
            if identity not in values:
                values.append(identity)

        def is_blank(value: Any) -> bool:
            if value is None:
                return True
            if isinstance(value, str):
                return not value.strip()
            if isinstance(value, (list, tuple, set, dict)):
                return not value
            return False

        inherited_count = 0
        for step in repaired_plan.get("device_plan", []) or []:
            if not isinstance(step, dict) or not is_blank(
                step.get("source_reagent_identity")
            ):
                continue
            sources = tuple(cls._source_macro_step_ids(step))
            if not sources or any(source not in valid_sources for source in sources):
                # Invalid/missing source bindings are validator errors, not a
                # license to infer an identity from unrelated plan text.
                continue
            identities = list(certified_by_source_set.get(sources, []))
            identities = list(dict.fromkeys(identity for identity in identities if identity))
            if not identities:
                continue
            step["source_reagent_identity"] = "；".join(identities)
            inherited_count += 1
        return inherited_count

    @staticmethod
    def _structured_operation_recomposition_records(
        repaired_plan: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Return complete, step-bound split/merge evidence records only."""
        accepted_types = {
            "operation_split",
            "operation_merge",
            "operation_decomposition",
            "plan_step_split",
            "plan_step_merge",
        }
        records: List[Dict[str, Any]] = []
        for change in repaired_plan.get("plan_changes", []) or []:
            if not isinstance(change, dict):
                continue
            change_type = str(
                change.get("change_type") or change.get("type") or ""
            ).strip().lower()
            if change_type not in accepted_types:
                continue
            before_step_ids = change.get("before_step_ids")
            after_step_ids = change.get("after_step_ids")
            if (
                not isinstance(before_step_ids, list)
                or not before_step_ids
                or not isinstance(after_step_ids, list)
                or not after_step_ids
            ):
                continue
            if "before" not in change or "after" not in change:
                continue
            if not str(change.get("reason") or "").strip():
                continue
            preserved = change.get("preserved_invariants")
            if not isinstance(preserved, list):
                continue
            normalized = {
                str(value).strip().lower()
                for value in preserved
                if str(value).strip()
            }
            if not {
                "route",
                "reagent_identity",
                "macro_source_coverage",
            }.issubset(normalized):
                continue
            normalized_change = copy.deepcopy(change)
            normalized_change["before_step_ids"] = [
                str(value).strip()
                for value in before_step_ids
                if str(value).strip()
            ]
            normalized_change["after_step_ids"] = [
                str(value).strip()
                for value in after_step_ids
                if str(value).strip()
            ]
            if (
                normalized_change["before_step_ids"]
                and normalized_change["after_step_ids"]
            ):
                records.append(normalized_change)
        return records

    @classmethod
    def _repaired_plan_source_binding_errors(
        cls,
        previous_plan: Dict[str, Any],
        repaired_plan: Dict[str, Any],
    ) -> List[str]:
        """Freeze recognizable step-to-Research-source bindings across rewrite."""

        def step_key(step: Dict[str, Any]) -> str:
            return str(step.get("plan_step", "")).strip()

        def material_identity_ids(step: Dict[str, Any]) -> Set[str]:
            raw = step.get("source_material_identity_ids")
            if not isinstance(raw, list):
                return set()
            return {
                str(value).strip()
                for value in raw
                if str(value).strip()
            }

        previous_by_key: Dict[str, Dict[str, Any]] = {}
        for step in previous_plan.get("device_plan", []) or []:
            if not isinstance(step, dict):
                continue
            key = step_key(step)
            if key and key not in previous_by_key:
                previous_by_key[key] = step

        errors: List[str] = []
        repaired_by_key: Dict[str, Dict[str, Any]] = {}
        repaired_keys: Set[str] = set()
        for step in repaired_plan.get("device_plan", []) or []:
            if not isinstance(step, dict):
                continue
            key = step_key(step)
            if key:
                if key in repaired_keys:
                    errors.append(
                        f"device_plan rewrite 含重复 plan_step={key}，"
                        "无法审计原步骤绑定。"
                    )
                repaired_keys.add(key)
                repaired_by_key.setdefault(key, step)
            previous = previous_by_key.get(key)
            if previous is None:
                continue
            before_sources = cls._source_macro_step_ids(previous)
            after_sources = cls._source_macro_step_ids(step)
            # A persistent stable key is the strongest available identity.
            # Its source set is frozen regardless of punctuation, objective,
            # workstation, or operation edits; true recomposition must use
            # explicit deleted/new keys and step-bound evidence below.
            if before_sources != after_sources:
                errors.append(
                    "计划级 Device LLM 改变了既存 plan_step 的冻结 "
                    f"source_macro_step 绑定：plan_step={key}, "
                    f"before={before_sources}, after={after_sources}。"
                )
            before_material_ids = material_identity_ids(previous)
            after_material_ids = material_identity_ids(step)
            if before_material_ids or after_material_ids:
                if before_material_ids != after_material_ids:
                    errors.append(
                        "计划级 Device LLM 改变了既存 plan_step 的冻结 LLM "
                        f"material identity IDs：plan_step={key}, "
                        f"before={sorted(before_material_ids)}, "
                        f"after={sorted(after_material_ids)}。"
                    )

        deleted_keys = set(previous_by_key) - repaired_keys
        added_keys = repaired_keys - set(previous_by_key)
        if deleted_keys or added_keys:
            evidence = cls._structured_operation_recomposition_records(
                repaired_plan
            )
            covered_before: Set[str] = set()
            covered_after: Set[str] = set()
            for record in evidence:
                before_ids = set(record["before_step_ids"])
                after_ids = set(record["after_step_ids"])
                change_type = str(
                    record.get("change_type") or record.get("type") or ""
                ).strip().lower()
                unknown_before = before_ids - set(previous_by_key)
                unknown_after = after_ids - repaired_keys
                if unknown_before or unknown_after:
                    errors.append(
                        "operation recomposition plan_changes 引用了不存在的步骤："
                        f"unknown_before={sorted(unknown_before)}, "
                        f"unknown_after={sorted(unknown_after)}。"
                    )
                    continue
                if len(before_ids) > 1 and len(after_ids) > 1:
                    errors.append(
                        "operation recomposition 禁止 many-to-many source "
                        "remapping；必须拆成可独立审计的一对多 split 或多对一 merge："
                        f"before_step_ids={sorted(before_ids)}, "
                        f"after_step_ids={sorted(after_ids)}。"
                    )
                    continue
                if change_type in {"operation_split", "plan_step_split"} and len(
                    before_ids
                ) != 1:
                    errors.append(
                        "operation_split 必须恰好绑定一个 before_step_id。"
                    )
                    continue
                if change_type in {"operation_merge", "plan_step_merge"} and len(
                    after_ids
                ) != 1:
                    errors.append(
                        "operation_merge 必须恰好绑定一个 after_step_id。"
                    )
                    continue
                before_union = {
                    source
                    for key in before_ids
                    for source in cls._source_macro_step_ids(previous_by_key[key])
                }
                after_union = {
                    source
                    for key in after_ids
                    for source in cls._source_macro_step_ids(repaired_by_key[key])
                }
                if len(before_ids) == 1:
                    before_sources = {
                        source
                        for source in cls._source_macro_step_ids(
                            previous_by_key[next(iter(before_ids))]
                        )
                    }
                    mismatched_after = {
                        key: sorted(
                            {
                                source
                                for source in cls._source_macro_step_ids(
                                    repaired_by_key[key]
                                )
                            }
                        )
                        for key in after_ids
                        if {
                            source
                            for source in cls._source_macro_step_ids(
                                repaired_by_key[key]
                            )
                        }
                        != before_sources
                    }
                    if mismatched_after:
                        errors.append(
                            "operation split/decomposition 的每个 after step "
                            "必须逐项保留唯一 before step 的完整 source-set："
                            f"before_sources={sorted(before_sources)}, "
                            f"mismatched_after={mismatched_after}。"
                        )
                        continue
                    before_material_ids = material_identity_ids(
                        previous_by_key[next(iter(before_ids))]
                    )
                    mismatched_material_after = {
                        key: sorted(material_identity_ids(repaired_by_key[key]))
                        for key in after_ids
                        if material_identity_ids(repaired_by_key[key])
                        != before_material_ids
                    }
                    if mismatched_material_after:
                        errors.append(
                            "operation split 的每个 after step 必须逐项保留唯一 "
                            "before step 的完整 LLM material identity ID 集合："
                            f"before_ids={sorted(before_material_ids)}, "
                            f"mismatched_after={mismatched_material_after}。"
                        )
                        continue
                elif before_union != after_union:
                    errors.append(
                        "operation recomposition 改变了冻结 macro source-set union："
                        f"before_step_ids={sorted(before_ids)}, "
                        f"after_step_ids={sorted(after_ids)}, "
                        f"before_sources={sorted(before_union)}, "
                        f"after_sources={sorted(after_union)}。"
                    )
                    continue
                elif {
                    identity_id
                    for key in before_ids
                    for identity_id in material_identity_ids(previous_by_key[key])
                } != {
                    identity_id
                    for key in after_ids
                    for identity_id in material_identity_ids(repaired_by_key[key])
                }:
                    errors.append(
                        "operation merge 改变了冻结 LLM material identity ID union："
                        f"before_step_ids={sorted(before_ids)}, "
                        f"after_step_ids={sorted(after_ids)}。"
                    )
                    continue
                covered_before.update(before_ids)
                covered_after.update(after_ids)
            uncovered_deleted = deleted_keys - covered_before
            uncovered_added = added_keys - covered_after
            if uncovered_deleted or uncovered_added:
                errors.append(
                    "计划级 Device LLM 拆分/合并或新增了 plan step，但缺少逐组绑定的 "
                    "operation_split/operation_merge/operation_decomposition "
                    "plan_changes 证据："
                    f"uncovered_before_step_ids={sorted(uncovered_deleted)}, "
                    f"uncovered_after_step_ids={sorted(uncovered_added)}；"
                    "每条证据必须含 before_step_ids/after_step_ids/reason/"
                    "preserved_invariants。"
                )
        return list(dict.fromkeys(errors))

    @classmethod
    def _normalized_identity_token(cls, raw_token: Any) -> str:
        """Collapse sample-state wording while preserving real identities.

        ``NM-raw suspension`` and ``NM-raw wet solid`` are the same sample.
        By contrast ``BAL-Ni-01 dried solid`` remains ``balni01`` and is still
        rejected when Research never authorized that sample.
        """
        token = cls._identity_text(raw_token)
        if not token:
            return ""
        prefix_pattern = (
            r"^(?:最终|初始|上层|下层|\d*(?:反应后)(?:的)?|"
            r"洗涤离心后(?:的)?|离心后(?:的)?|洗涤后(?:的)?|酸化|"
            r"干燥后(?:的)?|冷却后(?:的)?|反应后(?:的)?|加入洗液后(?:的)?|"
            r"加入后(?:的)?|所得|得到的|对应的)+"
        )
        suffix_pattern = (
            r"(?:对照)?(?:样品|产物|反应液|混合物|上清液|上清|固体沉淀|沉淀|"
            r"湿固体|干燥固体|固体粉末|粉末|悬浊液|悬浊体系|分散液|反应体系|"
            r"酸化体系|共沉淀体系|原液|母液|储备液|溶液|水相|有机相|体系|"
            r"碱性|酸化)+$"
        )
        previous = None
        while token and token != previous:
            previous = token
            token = re.sub(prefix_pattern, "", token)
            token = re.sub(suffix_pattern, "", token)
        if token in {
            "样品",
            "产物",
            "反应液",
            "混合物",
            "上清液",
            "上清",
            "沉淀",
            "湿固体",
            "干燥固体",
            "固体",
            "粉末",
            "悬浊液",
            "悬浊体系",
            "反应体系",
            "共沉淀",
            "碱性",
            "酸化",
            "洗液",
            "sample",
            "product",
        }:
            return ""
        return token

    @classmethod
    def _reagent_identity_tokens(cls, value: Any) -> List[str]:
        scalars: List[str] = []

        def walk(item: Any) -> None:
            if isinstance(item, dict):
                for nested in item.values():
                    walk(nested)
            elif isinstance(item, list):
                for nested in item:
                    walk(nested)
            elif item not in (None, ""):
                scalars.append(str(item))

        walk(value)
        tokens: List[str] = []
        for scalar in scalars:
            # Remove complete concentration/quantity expressions before
            # punctuation is collapsed.  Matching ``mol`` before ``mol/L``
            # used to leave a stray ``/L`` and fabricate identities such as
            # ``...lnino32`` from ``0.20 mol/L Ni(NO3)2``.
            scalar = re.sub(
                r"(?<![A-Za-z0-9])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\s*"
                r"(?:mol\s*/\s*[lL]|mmol\s*/\s*[lL]|umol\s*/\s*[lL]|"
                r"[mun]?mol|kg|mg|[uµμ]g|g|mL|ml|[uµμ]L|[uµμ]l|L|l|"
                r"[mun]?M)\b",
                "",
                scalar,
                flags=re.I,
            )
            for raw_token in re.split(r"[,，、;；+]|\s*(?:和|与)\s*", scalar):
                token = cls._normalized_identity_token(raw_token)
                if token:
                    tokens.append(token)
        return sorted(set(tokens))

    @classmethod
    def _device_plan_research_alignment_errors(
        cls,
        research_handoff: Dict[str, Any],
        plan_result: Dict[str, Any],
        *,
        reference_plan: Optional[Dict[str, Any]] = None,
        semantic_analysis: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """Deterministically bind every Device step to frozen Research chemistry."""
        errors: List[str] = []
        research_steps = [
            step
            for step in research_handoff.get("macro_action_steps", []) or []
            if isinstance(step, dict)
        ]
        plan_steps = [
            step
            for step in plan_result.get("device_plan", []) or []
            if isinstance(step, dict)
        ]
        expected_order: List[str] = []
        expected_reagents: Dict[str, List[str]] = {}
        expected_material_ids: Dict[str, Set[str]] = {}
        semantic_records = {
            str(item.get("source_macro_step") or "").strip(): item
            for item in (semantic_analysis or {}).get("macro_step_assessments", []) or []
            if isinstance(item, dict)
        }
        for index, step in enumerate(research_steps, start=1):
            source = str(step.get("步骤序号", step.get("step", index)))
            expected_order.append(source)
            expected_reagents[source] = cls._reagent_identity_tokens(
                step.get("试剂/对象", step.get("reagent_or_object", ""))
            )
            expected_material_ids[source] = {
                str(item.get("identity_id") or "").strip()
                for item in semantic_records.get(source, {}).get(
                    "material_identities", []
                ) or []
                if isinstance(item, dict)
                and str(item.get("identity_id") or "").strip()
            }
        expected_set = set(expected_order)
        mapped: Dict[str, List[Dict[str, Any]]] = {}
        actual_source_sets: List[List[str]] = []
        for step in plan_steps:
            sources = cls._source_macro_step_ids(step)
            if not sources:
                errors.append(
                    f"device_plan[{step.get('plan_step', '?')}] 缺少 source_macro_step。"
                )
                continue
            valid_sources: List[str] = []
            for source in sources:
                if source not in expected_set:
                    errors.append(
                        "device_plan 引用了 Research 不存在的 "
                        f"source_macro_step={source}。"
                    )
                    continue
                mapped.setdefault(source, []).append(step)
                valid_sources.append(source)
            if valid_sources:
                actual_source_sets.append(valid_sources)
                explicit_identity = step.get("source_reagent_identity")
                if semantic_records:
                    raw_material_ids = step.get("source_material_identity_ids")
                    actual_material_ids = {
                        str(value).strip()
                        for value in raw_material_ids or []
                        if str(value).strip()
                    } if isinstance(raw_material_ids, list) else set()
                    authorized_material_ids = {
                        identity_id
                        for source in valid_sources
                        for identity_id in expected_material_ids.get(source, set())
                    }
                    if not actual_material_ids or not actual_material_ids.issubset(
                        authorized_material_ids
                    ):
                        errors.append(
                            "device_plan 的 source_material_identity_ids 未精确引用"
                            "冻结 LLM 语义合同："
                            f"plan_step={step.get('plan_step', '?')}, "
                            f"actual={sorted(actual_material_ids)}, "
                            f"authorized={sorted(authorized_material_ids)}。"
                        )
                elif explicit_identity not in (None, ""):
                    actual_identity_tokens = set(
                        cls._reagent_identity_tokens(explicit_identity)
                    )
                    authorized_identity_tokens = {
                        token
                        for source in valid_sources
                        for token in expected_reagents.get(source, [])
                    }
                    if (
                        not actual_identity_tokens
                        or not actual_identity_tokens.issubset(
                            authorized_identity_tokens
                        )
                    ):
                        errors.append(
                            "device_plan 明示的 source_reagent_identity 不是冻结 "
                            "Research 物种级身份子集："
                            f"plan_step={step.get('plan_step', '?')}, "
                            f"actual={sorted(actual_identity_tokens)}, "
                            f"authorized={sorted(authorized_identity_tokens)}。"
                        )
        handoffs = [
            handoff
            for handoff in plan_result.get("offline_handoffs", []) or []
            if isinstance(handoff, dict)
        ]
        for handoff in handoffs:
            sources = [
                source
                for source in cls._source_macro_step_ids(handoff)
                if source in expected_set
            ]
            if sources:
                for source in sources:
                    mapped.setdefault(source, []).append(handoff)
                continue
            if semantic_records:
                continue
            handoff_text = cls._identity_text(_json_text(handoff))
            scores: Dict[str, int] = {}
            for index, research_step in enumerate(research_steps, start=1):
                candidate_source = str(
                    research_step.get("步骤序号", research_step.get("step", index))
                )
                operation = cls._identity_text(
                    research_step.get("操作", research_step.get("operation", ""))
                )
                score = 0
                if operation and operation in handoff_text:
                    score += 8
                for acronym in re.findall(
                    r"[A-Za-z][A-Za-z0-9_-]{1,}",
                    str(
                        research_step.get(
                            "操作", research_step.get("operation", "")
                        )
                    ),
                ):
                    if cls._identity_text(acronym) in handoff_text:
                        score += 4
                score += sum(
                    2
                    for token in expected_reagents.get(candidate_source, [])
                    if token and token in handoff_text
                )
                if score:
                    scores[candidate_source] = score
            if scores:
                best = max(scores.values())
                winners = [source for source, score in scores.items() if score == best]
                if len(winners) == 1:
                    mapped.setdefault(winners[0], []).append(handoff)
        for source in expected_order:
            if source not in mapped:
                errors.append(f"device_plan 缺少 Research macro step {source} 的覆盖。")
                continue
            if semantic_records:
                mapped_ids = {
                    str(value).strip()
                    for record in mapped[source]
                    if isinstance(record, dict)
                    for value in record.get("source_material_identity_ids", []) or []
                    if str(value).strip()
                }
                missing_ids = expected_material_ids.get(source, set()) - mapped_ids
                if missing_ids:
                    errors.append(
                        f"source_macro_step={source} 未保留冻结 LLM 物料身份 ID："
                        f"missing={sorted(missing_ids)}。"
                    )
                continue
            mapped_text = cls._identity_text(_json_text(mapped[source]))
            mapped_identity_tokens: Set[str] = set()
            for record in mapped[source]:
                if not isinstance(record, dict):
                    continue
                identity_values = [
                    value
                    for key, value in record.items()
                    if re.search(
                        r"source_reagent_identity|reagent|chemical|precursor|"
                        r"试剂|物料身份|material_(?:id|name)",
                        str(key),
                        re.I,
                    )
                ]
                mapped_identity_tokens.update(
                    cls._reagent_identity_tokens(identity_values)
                )
            missing = [
                token
                for token in expected_reagents.get(source, [])
                if token not in mapped_text
                and token not in mapped_identity_tokens
            ]
            if missing:
                errors.append(
                    f"source_macro_step={source} 未逐字保留 Research 试剂/对象身份："
                    f"missing={missing}。"
                )
        # A physical Device operation may serve parallel Research branches.
        # Treat sources co-declared on one step as an equivalence cohort, then
        # enforce ordering only between independent cohorts.  This preserves
        # the Research partial order without cloning the physical operation.
        order_index = {source: index for index, source in enumerate(expected_order)}
        parent = {source: source for source in expected_order}

        def find(source: str) -> str:
            while parent[source] != source:
                parent[source] = parent[parent[source]]
                source = parent[source]
            return source

        def union(left: str, right: str) -> None:
            left_root, right_root = find(left), find(right)
            if left_root == right_root:
                return
            if order_index[left_root] <= order_index[right_root]:
                parent[right_root] = left_root
            else:
                parent[left_root] = right_root

        for sources in actual_source_sets:
            ordered = sorted(sources, key=order_index.__getitem__)
            if sources != ordered:
                errors.append(
                    "device_plan 的 source_macro_steps 未按 Research macro step 顺序排列。"
                )
            for source in ordered[1:]:
                union(ordered[0], source)
        cohort_index = {
            source: min(
                order_index[candidate]
                for candidate in expected_order
                if find(candidate) == find(source)
            )
            for source in expected_order
        }
        # Preparation for a later macro action may legitimately occur early
        # (for example, acquire empty bottles and pre-load a stable Fe stock
        # for macro 5 while explicitly withholding KOH).  Order is therefore
        # defined by when each independent macro/cohort is *completed*, not by
        # its first appearance.  A true wholesale 2 -> 1 reversal still has
        # last(2) < last(1) and is rejected.
        cohort_last_position: Dict[str, int] = {}
        for position, sources in enumerate(actual_source_sets):
            for source in sources:
                if source not in cohort_index:
                    continue
                root = find(source)
                cohort_last_position[root] = max(
                    position, cohort_last_position.get(root, -1)
                )
        ordered_roots: List[str] = []
        for source in expected_order:
            root = find(source)
            if root not in ordered_roots:
                ordered_roots.append(root)
        completion_positions = [
            cohort_last_position[root]
            for root in ordered_roots
            if root in cohort_last_position
        ]
        if completion_positions != sorted(completion_positions):
            errors.append("device_plan 改变了 Research macro step/reagent 执行顺序。")

        if semantic_records:
            # Production identity authorization is the frozen LLM
            # identity-ID graph checked above.  Running the legacy token/name
            # matcher as a second authority would reintroduce substring bugs
            # (for example Ni vs Ni salt, or a material state suffix) and
            # could contradict the full-context judgement.
            return list(dict.fromkeys(errors))

        state_word_pattern = re.compile(
            r"悬浊|湿固体|干燥固体|上清|沉淀|粉末|反应体系|共沉淀体系|"
            r"酸化体系|分散液|洗涤后|离心后|干燥后|冷却后|reaction\s*mixture|"
            r"suspension|wet\s*solid|supernatant|precipitate|powder",
            re.I,
        )

        def explicit_marker_records(
            payload: Dict[str, Any],
        ) -> List[Tuple[List[str], Dict[str, bool]]]:
            records: List[Tuple[List[str], Dict[str, bool]]] = []
            for step in payload.get("device_plan", []) or []:
                if not isinstance(step, dict):
                    continue
                sources = cls._source_macro_step_ids(step)
                text = _json_text(step)
                found: Dict[str, bool] = {}
                for match in re.findall(r"试剂\s*[A-Za-z0-9_-]+", text, re.I):
                    marker = re.sub(
                        r"^(?:试剂|reagent)", "", cls._identity_text(match)
                    )
                    if marker:
                        found[marker] = False
                for key, value in step.items():
                    if re.search(
                        r"reagent|chemical|precursor|试剂|物料身份|material_(?:id|name)",
                        str(key),
                        re.I,
                    ):
                        stateful = bool(state_word_pattern.search(_json_text(value)))
                        for marker in cls._reagent_identity_tokens(value):
                            # A direct reagent declaration wins over a stateful
                            # occurrence of the same marker.
                            found[marker] = found.get(marker, True) and stateful
                if sources and found:
                    records.append((sources, found))
            return records

        def marker_matches(marker: str, expected: str) -> bool:
            return bool(
                marker == expected or marker in expected or expected in marker
            )

        research_identity_text = cls._identity_text(_json_text(research_handoff))
        plan_marker_records = explicit_marker_records(plan_result)
        for sources, actual_markers in plan_marker_records:
            allowed = {
                marker
                for source in sources
                for marker in expected_reagents.get(source, [])
            }
            if not allowed:
                continue
            extras = {
                marker
                for marker, stateful in actual_markers.items()
                if marker
                and not any(
                    marker_matches(marker, expected)
                    for expected in allowed
                )
                and not (
                    stateful
                    and len(marker) >= 2
                    and marker in research_identity_text
                )
            }
            if extras:
                errors.append(
                    f"source_macro_steps={sources} 引入了 Research 未授权的显式试剂身份："
                    f"extra={sorted(extras)}, allowed={sorted(allowed)}。"
                )

        if isinstance(reference_plan, dict):
            def canonical_markers(payload: Dict[str, Any]) -> Dict[str, Set[str]]:
                markers: Dict[str, Set[str]] = {}
                for sources, found in explicit_marker_records(payload):
                    for source in sources:
                        allowed = expected_reagents.get(source, [])
                        matched = {
                            marker
                            for marker in found
                            if any(
                                marker_matches(marker, expected)
                                for expected in allowed
                            )
                        }
                        if matched:
                            markers.setdefault(source, set()).update(matched)
                return markers

            before_markers = canonical_markers(reference_plan)
            after_markers = canonical_markers(plan_result)
            for source, expected in before_markers.items():
                actual = after_markers.get(source, set())
                if actual != expected:
                    errors.append(
                        f"source_macro_step={source} 的显式试剂身份发生漂移："
                        f"before={sorted(expected)}, after={sorted(actual)}。"
                    )
        return list(dict.fromkeys(errors))

    @staticmethod
    def _observation_view(research_handoff: Dict[str, Any]) -> List[Any]:
        observations: List[Any] = []

        def _walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    lowered = str(key).lower()
                    if "observation" in lowered or "观察" in str(key) or "观测" in str(key):
                        observations.append({str(key): copy.deepcopy(item)})
                    _walk(item)
            elif isinstance(value, list):
                for item in value:
                    _walk(item)

        _walk(research_handoff)
        return SingleDeviceAgent._unique_json_records(observations)

    @classmethod
    def _observation_evidence_catalog(
        cls, research_handoff: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Build model-selectable, immutable quantity evidence records.

        The LLM must never be asked to calculate a SHA digest.  Only unique
        observation IDs and unique top-level quantity spans are catalogued;
        duplicate IDs or ambiguous repeated spans are omitted and therefore
        cannot fund a state-change output.
        """

        observations = research_handoff.get("observations", [])
        if not isinstance(observations, list):
            return []
        id_counts: Dict[str, int] = {}
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            observation_id = str(
                observation.get("observation_id")
                or observation.get("id")
                or ""
            ).strip()
            if observation_id:
                id_counts[observation_id] = id_counts.get(observation_id, 0) + 1
        catalog: List[Dict[str, Any]] = []
        metadata_fields = {
            "observation_id",
            "id",
            "sample_id",
            "material_id",
            "batch_id",
        }
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            observation_id = str(
                observation.get("observation_id")
                or observation.get("id")
                or ""
            ).strip()
            if not observation_id or id_counts.get(observation_id) != 1:
                continue
            digest = cls._stable_digest(observation, prefix="observation")
            for field_name, raw_field_value in observation.items():
                if field_name in metadata_fields or raw_field_value in (None, ""):
                    continue
                field_text = (
                    json.dumps(
                        raw_field_value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if isinstance(raw_field_value, (dict, list))
                    else str(raw_field_value)
                )
                candidates = cls._extract_explicit_quantities(field_text)
                direct_value, direct_dimension, _ = cls._canonical_quantity(
                    raw_field_value
                )
                if (
                    not candidates
                    and direct_value is not None
                    and cls._quantity_dimension_is_inventory(direct_dimension)
                ):
                    candidates = [
                        {
                            "raw": field_text,
                            "value": direct_value,
                            "dimension": direct_dimension,
                            "start": 0,
                            "end": len(field_text),
                        }
                    ]
                for candidate in candidates:
                    source_context = str(candidate.get("raw") or "").strip()
                    if (
                        not source_context
                        or field_text.count(source_context) != 1
                        or not cls._quantity_dimension_is_inventory(
                            str(candidate.get("dimension") or "")
                        )
                    ):
                        continue
                    catalog.append(
                        {
                            "observation_id": observation_id,
                            "artifact_digest": digest,
                            "sample_id": observation.get("sample_id"),
                            "material_id": observation.get("material_id"),
                            "batch_id": observation.get("batch_id"),
                            "quantity_source_field": str(field_name),
                            "quantity_source_context": source_context,
                            "canonical_quantity": {
                                "value": candidate.get("value"),
                                "dimension": candidate.get("dimension"),
                            },
                            "measurement_artifact": {
                                "observation_id": observation_id,
                                "artifact_digest": digest,
                                "quantity_source_field": str(field_name),
                                "quantity_source_context": source_context,
                            },
                        }
                    )
        return catalog

    @staticmethod
    def _external_return_contracts(
        research_handoff: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Read mandatory external returns only from frozen Research input."""
        contracts: List[Dict[str, Any]] = []
        for index, step in enumerate(
            research_handoff.get("macro_action_steps", []) or [], start=1
        ):
            if not isinstance(step, dict):
                continue
            source = str(step.get("步骤序号", step.get("step", index)))
            returns = step.get("intermediate_returns", [])
            if not isinstance(returns, list):
                continue
            for feedback in returns:
                if (
                    isinstance(feedback, dict)
                    and feedback.get("availability") == "undeclared"
                    and feedback.get("required_for_next_step") is True
                ):
                    contracts.append({
                        **copy.deepcopy(feedback),
                        "source_macro_step": source,
                    })
        return contracts

    @classmethod
    def _external_return_wait_findings(
        cls,
        research_handoff: Dict[str, Any],
        candidate: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Do not compile later macro steps across an external feedback barrier.

        The order is Research's list order, not numeric step labels or the
        candidate's proposed order. A terminal observation needs no machine
        wait command. The existing observation boundary can collect it.
        """
        contracts = cls._external_return_contracts(research_handoff)
        if not contracts:
            return []
        source_order = [
            str(step.get("步骤序号", step.get("step", index)))
            for index, step in enumerate(
                research_handoff.get("macro_action_steps", []) or [], start=1
            )
            if isinstance(step, dict)
        ]
        plan_steps = [
            item for item in candidate.get("device_plan", []) or []
            if isinstance(item, dict)
        ] if isinstance(candidate.get("device_plan", []), list) else []
        plan_by_id = {
            str(item.get("plan_step")): item for item in plan_steps
        }
        workflow = candidate.get("workflow_json")
        workflow_steps = workflow.get("steps", []) if isinstance(workflow, dict) else []
        machine_steps: List[Dict[str, Any]] = []
        for kind, steps in (("device_plan", plan_steps), ("workflow", workflow_steps)):
            for item in steps if isinstance(steps, list) else []:
                if not isinstance(item, dict) or not str(item.get("workstation") or "").strip():
                    continue
                sources = set(cls._source_macro_step_ids(item))
                if kind == "workflow":
                    source_plan = plan_by_id.get(str(item.get("source_plan_step")))
                    if source_plan is not None:
                        # Either trace can expose a crossed barrier; lying in
                        # one trace cannot erase the other frozen plan source.
                        sources.update(cls._source_macro_step_ids(source_plan))
                machine_steps.append({
                    "kind": kind,
                    "step": item.get("plan_step") if kind == "device_plan" else item.get("step_number"),
                    "sources": sources,
                })
        findings: List[Dict[str, Any]] = []
        for contract in contracts:
            source = contract["source_macro_step"]
            later = set(source_order[source_order.index(source) + 1:])
            blocked = [item for item in machine_steps if item["sources"] & later]
            if not blocked:
                continue
            downstream = sorted({ref for item in blocked for ref in item["sources"] & later})
            findings.append({
                "type": "device_external_return_wait_required",
                "source_macro_step": source,
                "wait_for": copy.deepcopy(contract.get("wait_for", "")),
                "return_contract": copy.deepcopy(contract),
                "downstream_source_macro_steps": downstream,
                "blocked_machine_steps": [
                    {"kind": item["kind"], "step": item["step"]} for item in blocked
                ],
                "message": (
                    f"Research macro step {source} 的必需外部返回 "
                    f"{contract.get('name', '')!r} 尚需等待：{contract.get('wait_for', '')}；"
                    f"不得把后续 macro steps {downstream} 编入同一自动执行包。"
                ),
            })
        return findings

    def _external_return_wait_result(
        self,
        state: SingleDeviceAgentState,
        candidate: Dict[str, Any],
        findings: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Keep every scientific step for review, but expose no executable payload."""
        pending = [copy.deepcopy(item["return_contract"]) for item in findings]
        package = copy.deepcopy(candidate)
        prior_context = package.get("manual_repair_context")
        context = copy.deepcopy(prior_context) if isinstance(prior_context, dict) else {}
        if candidate.get("workflow_json"):
            context["last_workflow"] = copy.deepcopy(candidate["workflow_json"])
            context["last_workflow_txt"] = candidate.get("workflow_txt", "")
        context["last_device_plan"] = copy.deepcopy(candidate.get("device_plan", []))
        context["research_handoff"] = copy.deepcopy(state.research_handoff)
        context["pending_returns"] = pending
        context["allowed_changes"] = []
        context["resume_requires"] = (
            "保留后续科学步骤，由 Research 在该 observation/manual_handoff 边界拆分执行；"
            "获得真实返回并形成新的 Research 交接后再规划后续步骤。"
            "Device override 或候选自述已满足等待不能解除此门。"
        )
        # An old/pre-gate certificate must never authorize the crossing, even
        # when this check runs after workflow repair or manual restoration.
        state.feasibility_accepted = False
        state.feasibility_certificate = {}
        package.update({
            "status": "manual_required",
            "feedback_type": "human_review_required",
            "feedback_route": "human",
            "failure_scope": "device_plan",
            "failure_stage": "external_return_wait",
            "exp_id": state.exp_id,
            "iteration_id": state.iteration_id,
            "workflow_id": state.workflow_id,
            "feasibility_accepted": False,
            "feasibility_certificate": {},
            "workflow_txt": "",
            "workflow_json": {},
            "dispatch_payload": {},
            "dispatch_formatting": {},
            "macro_plan": copy.deepcopy(state.research_handoff),
            "pending_returns": pending,
            "return_wait_audit": {"status": "waiting", "findings": copy.deepcopy(findings)},
            "manual_repair_context": context,
            "requires_scientific_review": True,
            "agent_mode": "single_device_agent",
            "error_package": {
                "type": "device_external_return_wait_required",
                "assessment_source": "deterministic_frozen_research_return_contract",
                "feedback_route": "human",
                "failure_scope": "device_plan",
                "blocking_constraints": [item["message"] for item in findings],
                "structured_errors": copy.deepcopy(findings),
                "pending_returns": pending,
                "message": context["resume_requires"],
            },
        })
        # Legacy repair wrappers inspect nested acceptance flags. Retain
        # scientific/history details but revoke every reserved authorization
        # record, not only the top-level certificate.
        stack: List[Any] = [package]
        seen: Set[int] = set()
        while stack:
            node = stack.pop()
            if not isinstance(node, (dict, list)) or id(node) in seen:
                continue
            seen.add(id(node))
            if isinstance(node, list):
                stack.extend(node)
                continue
            if "feasibility_accepted" in node:
                node["feasibility_accepted"] = False
            certificate = node.get("feasibility_certificate")
            if isinstance(certificate, dict) and certificate:
                certificate["accepted"] = False
                certificate["revoked_reason"] = "device_external_return_wait_required"
            for dispatch_key in ("dispatch_payload", "dispatch_formatting"):
                if dispatch_key in node:
                    node[dispatch_key] = {}
            stack.extend(node.values())
        state.add_log("mandatory external return barrier blocked automatic continuation")
        return package

    def _build_feasibility_certificate(
        self,
        state: SingleDeviceAgentState,
        plan_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        if self._external_return_wait_findings(state.research_handoff, plan_result):
            raise ValueError("mandatory external return wait prevents feasibility certification")
        route = self._route_view(state.research_handoff)
        research_matrix = self._sample_matrix_contract_value(
            state.research_handoff
        )
        device_matrix = self._sample_matrix_contract_value(plan_result)
        device_sample_ids = self._extract_sample_ids(plan_result)
        protected = {
            "research_plan_signature": self._plan_signature(state.research_handoff),
            "target_materials": self._extract_named_values(
                state.research_handoff,
                {"target_material", "target_materials", "目标材料", "目标产物"},
            ),
            "reaction_route": route,
            "reagent_identity_and_order": [
                {
                    "step": item.get("step"),
                    "reagent_or_object": item.get("reagent_or_object", ""),
                }
                for item in route
            ],
            "observation_points": self._observation_view(state.research_handoff),
            "sample_control_matrix": research_matrix or device_matrix,
            "accepted_device_sample_control_matrix": device_matrix,
            "device_sample_ids": device_sample_ids,
            "semantic_analysis": copy.deepcopy(self._active_semantic_analysis),
        }
        external_returns = self._external_return_contracts(state.research_handoff)
        if external_returns:
            protected["external_return_contracts"] = external_returns
        protected_digest = self._stable_digest(protected)
        certificate = {
            "certificate_version": FEASIBILITY_CERTIFICATE_VERSION,
            "accepted": True,
            "accepted_at": datetime.now().isoformat(),
            **protected,
            "device_snapshot_id": self._device_snapshot_id(),
            "device_truth_sha256": self._full_device_truth_digest(),
            "accepted_device_plan_signature": self._stable_digest(
                plan_result.get("device_plan", []), prefix="device_plan"
            ),
            "protected_digest": protected_digest,
        }
        certificate["route_signature"] = protected[
            "research_plan_signature"
        ]
        certificate["sample_matrix_signature"] = self._stable_digest(
            protected["sample_control_matrix"], prefix="sample_matrix"
        )
        certificate["device_snapshot_signature"] = certificate[
            "device_truth_sha256"
        ]
        certificate["certificate_id"] = self._stable_digest(
            {
                "protected_digest": protected_digest,
                "device_snapshot_id": certificate["device_snapshot_id"],
                "device_truth_sha256": certificate["device_truth_sha256"],
            },
            prefix="feasibility",
        )
        return certificate

    @staticmethod
    def _extract_named_values(payload: Any, names: Set[str]) -> List[Any]:
        values: List[Any] = []

        def _walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if str(key).strip().lower() in names:
                        values.append(copy.deepcopy(item))
                    _walk(item)
            elif isinstance(value, list):
                for item in value:
                    _walk(item)

        _walk(payload)
        return SingleDeviceAgent._unique_json_records(values)

    def _validate_feasibility_certificate(
        self,
        state: SingleDeviceAgentState,
        certificate: Dict[str, Any],
        *,
        require_snapshot_match: bool,
    ) -> List[str]:
        errors: List[str] = []
        if not isinstance(certificate, dict) or not certificate.get("accepted"):
            return ["缺少已接受的 feasibility_certificate。"]
        expected_signature = self._plan_signature(state.research_handoff)
        if certificate.get("research_plan_signature") != expected_signature:
            errors.append(
                "Research plan signature 与人工修订请求不一致；Device override 不得修改实验路线。"
            )
        protected = {
            key: copy.deepcopy(certificate.get(key, [] if key != "research_plan_signature" else ""))
            for key in (
                "research_plan_signature",
                "target_materials",
                "reaction_route",
                "reagent_identity_and_order",
                "observation_points",
                "sample_control_matrix",
                "accepted_device_sample_control_matrix",
                "device_sample_ids",
                "semantic_analysis",
            )
        }
        if "external_return_contracts" in certificate:
            protected["external_return_contracts"] = copy.deepcopy(
                certificate["external_return_contracts"]
            )
        if certificate.get("external_return_contracts", []) != self._external_return_contracts(
            state.research_handoff
        ):
            errors.append("feasibility_certificate 的冻结外部返回等待合同发生变化或缺失。")
        if certificate.get("protected_digest") != self._stable_digest(protected):
            errors.append("feasibility_certificate protected_digest 校验失败。")
        if self._active_semantic_analysis and certificate.get(
            "semantic_analysis"
        ) != self._active_semantic_analysis:
            errors.append("feasibility_certificate 的冻结 LLM 语义合同发生漂移。")
        snapshot = str(certificate.get("device_snapshot_id", ""))
        current_snapshot = self._device_snapshot_id()
        if require_snapshot_match and snapshot != current_snapshot:
            errors.append(
                "设备快照已变化；必须重新执行 Device 路线可行性门，不能直接续跑旧 override。"
            )
        expected_truth = str(certificate.get("device_truth_sha256", ""))
        try:
            current_truth = self._full_device_truth_digest()
        except WorkstationTruthChangedError:
            errors.append("完整 Workstation Skill/能力/容器真源内容已变化；旧 Device override 失效。")
            return errors
        if require_snapshot_match and (
            not expected_truth or expected_truth != current_truth
        ):
            errors.append(
                "完整 Workstation Skill/能力/容器真源内容已变化；旧 Device override 失效。"
            )
        return errors

    def _accept_feasibility_plan(
        self,
        state: SingleDeviceAgentState,
        plan_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        proved_route_gap = self._verified_stage1_core_route_gap_result(
            state, plan_result
        )
        if proved_route_gap is not None:
            return proved_route_gap
        plan_result = self._normalize_plan_handoff_steps(state, plan_result)
        wait_findings = self._external_return_wait_findings(state.research_handoff, plan_result)
        if wait_findings:
            return self._external_return_wait_result(state, plan_result, wait_findings)
        accepted = self._normalize_quantity_contract(
            copy.deepcopy(plan_result),
            research_handoff=state.research_handoff,
        )
        matrix_errors = self._sample_matrix_drift_errors(
            state.research_handoff, accepted
        )
        invariant_errors = self._device_plan_research_alignment_errors(
            state.research_handoff,
            accepted,
            semantic_analysis=self._active_semantic_analysis,
        )
        invariant_errors.extend(self._declared_route_change_errors(plan_result))
        invariant_errors.extend(self._forbidden_plan_change_claims(accepted))
        invariant_errors.extend(
            str(finding.get("message", finding))
            for finding in self._audit_research_core_joint_capability_mapping(
                state, accepted
            )
        )
        acceptance_errors = list(dict.fromkeys(matrix_errors + invariant_errors))
        if acceptance_errors:
            accepted.update(
                {
                    "status": "manual_required",
                    "feedback_type": "human_review_required",
                    "feedback_route": "human",
                    "failure_scope": "device_plan",
                    "feasibility_accepted": False,
                    "feasibility_certificate": {},
                    "workflow_txt": "",
                    "workflow_json": {},
                    "error_package": {
                        "type": "stage1_frozen_invariant_violation",
                        "blocking_constraints": acceptance_errors,
                        "message": (
                            "Stage-1 device_plan 未保持 Research 路线、试剂顺序或"
                            "样品/对照/变量矩阵，"
                            "不能签发可行性证书。"
                        ),
                    },
                }
            )
            return accepted
        state.feasibility_accepted = True
        state.feasibility_certificate = self._build_feasibility_certificate(
            state, accepted
        )
        accepted["feasibility_accepted"] = True
        accepted["feasibility_certificate"] = copy.deepcopy(
            state.feasibility_certificate
        )
        state.add_log(
            "route feasibility accepted; immutable certificate created "
            f"({state.feasibility_certificate.get('certificate_id', '')})"
        )
        return accepted

    def _prepare_device_plan_override(
        self,
        state: SingleDeviceAgentState,
        override: Dict[str, Any],
        repair_request: Dict[str, Any],
        *,
        approval_bundle_payload: Optional[
            Tuple[Dict[str, Any], List[Dict[str, Any]]]
        ] = None,
        approval_bundle_invalid: bool = False,
    ) -> Tuple[Dict[str, Any], List[str]]:
        """Validate a human plan override and skip Research/Stage-1 LLM."""
        source_certificate = repair_request.get("feasibility_certificate")
        if not isinstance(source_certificate, dict):
            source_certificate = override.get("feasibility_certificate")
        certificate = copy.deepcopy(source_certificate) if isinstance(source_certificate, dict) else {}
        if certificate:
            # Keep the original certificate available in a structured
            # rejection package even when the override itself is invalid.
            state.feasibility_certificate = copy.deepcopy(certificate)
            state.feasibility_accepted = bool(certificate.get("accepted"))
        errors = self._validate_feasibility_certificate(
            state, certificate, require_snapshot_match=True
        )

        base = repair_request.get("last_device_plan")
        if not isinstance(base, dict):
            base = repair_request.get("device_plan_package")
        base = copy.deepcopy(base) if isinstance(base, dict) else {}
        if isinstance(repair_request.get("last_device_plan"), list):
            base["device_plan"] = copy.deepcopy(repair_request["last_device_plan"])
        reference_plan = copy.deepcopy(base)

        untrusted_override_fields: Set[str] = set()
        if isinstance(override, dict):
            override, untrusted_override_fields = (
                self._strip_untrusted_approval_fields(override)
            )
        if untrusted_override_fields:
            errors.append(
                "device_plan_override 含 JSON 可伪造的 human approval 保留字段："
                + ", ".join(sorted(untrusted_override_fields))
                + "；仅接受独立 typed runtime bundle。"
            )
        if approval_bundle_invalid:
            errors.append(
                "human quantity approval runtime bundle 无效、已重放、已过期或被篡改。"
            )
        if not isinstance(override, dict):
            errors.append("device_plan_override 必须是 JSON object。")
            plan_result = base
        else:
            plan_result = copy.deepcopy(base)
            supplied_plan = override.get("device_plan")
            if isinstance(supplied_plan, dict):
                # The public resume wrapper stores the complete editable plan
                # under ``device_plan``.  Accept both that form and the legacy
                # form where the value is directly the list of plan steps.
                plan_result.update(copy.deepcopy(supplied_plan))
            elif isinstance(supplied_plan, list):
                plan_result["device_plan"] = copy.deepcopy(supplied_plan)
            for sidecar_key in (
                "quantity_adjustments",
                "batch_plan",
                "material_transitions",
                "material_ledger",
                "reagent_slot_plan",
                "container_plan",
                "temporal_adaptations",
                "offline_handoffs",
                "sample_control_matrix",
            ):
                if sidecar_key in override:
                    plan_result[sidecar_key] = copy.deepcopy(
                        override[sidecar_key]
                    )
            if isinstance(override.get("changes"), list):
                plan_result["plan_changes"] = copy.deepcopy(
                    override.get("changes", [])
                )
        # Reserved approval records never become trusted merely because they
        # appear in an editable plan payload.  Only the resume wrapper's
        # validation envelope, cross-checked below against the frozen repair
        # request/certificate and exact transition child, can populate this
        # private gate input.
        plan_result.pop("_trusted_human_quantity_approvals", None)
        plan_result.pop("human_quantity_approval_validation", None)
        plan_result.pop("validated_human_quantity_approvals", None)
        # ``quantity_audit`` is deterministic derived state from the prior
        # failed candidate.  Carrying its old ``unknown_yield`` issue into the
        # resumed plan would re-add the human finding even after a valid typed
        # approval and create an unbreakable manual-review loop.  Always audit
        # the submitted override from source sidecars below.
        plan_result.pop("quantity_audit", None)
        if approval_bundle_payload is not None:
            trusted_envelope, trusted_records = approval_bundle_payload
            trusted_approvals, approval_errors = (
                self._trusted_human_quantity_approvals_from_runtime_bundle(
                    trusted_envelope,
                    trusted_records,
                    repair_request,
                    plan_result,
                    certificate,
                )
            )
        else:
            trusted_approvals, approval_errors = [], []
        errors.extend(approval_errors)
        if trusted_approvals and not approval_errors:
            self._active_trusted_human_quantity_approvals = copy.deepcopy(
                trusted_approvals
            )
        plan_steps = plan_result.get("device_plan")
        if not isinstance(plan_steps, list) or not plan_steps:
            errors.append("device_plan_override 缺少完整非空 device_plan。")
        plan_result["status"] = "device_plan"
        plan_result.setdefault(
            "feasibility", {"is_feasible": True, "blocking_constraints": []}
        )
        plan_result = self._normalize_plan_handoff_steps(state, plan_result)
        frozen_matrix = repair_request.get("frozen_sample_matrix")
        if (
            "sample_control_matrix" not in plan_result
            and isinstance(frozen_matrix, (list, dict))
        ):
            # This is a deterministic carry-forward of signed frozen data, not
            # an override-supplied scientific change.
            plan_result["sample_control_matrix"] = copy.deepcopy(frozen_matrix)

        if certificate:
            echoed = override.get("feasibility_certificate")
            if isinstance(echoed, dict) and echoed.get("protected_digest") != certificate.get(
                "protected_digest"
            ):
                errors.append("override 中的 feasibility_certificate 与修订请求不一致。")
            signature_checks = (
                (
                    "route_signature",
                    repair_request.get("frozen_route_signature"),
                    certificate.get("route_signature")
                    or certificate.get("research_plan_signature"),
                ),
                (
                    "sample_matrix_signature",
                    repair_request.get("frozen_sample_matrix_signature"),
                    certificate.get("sample_matrix_signature"),
                ),
                (
                    "device_snapshot_signature",
                    repair_request.get("device_snapshot_signature"),
                    certificate.get("device_snapshot_signature")
                    or certificate.get("device_snapshot_id"),
                ),
            )
            for override_key, request_value, certificate_value in signature_checks:
                override_value = override.get(override_key)
                if request_value and override_value != request_value:
                    errors.append(
                        f"override {override_key} 与人工修订请求不一致。"
                    )
                if (
                    request_value
                    and certificate_value
                    and request_value != certificate_value
                ):
                    errors.append(
                        f"repair request {override_key} 与 feasibility_certificate 不一致。"
                    )
            declarations = override.get("declarations")
            if not isinstance(declarations, dict):
                errors.append(
                    "人工 override 缺少 declarations；四个冻结字段必须显式声明 false。"
                )
            else:
                forbidden_declarations = [
                    key
                    for key in (
                        "route_changed",
                        "sample_matrix_changed",
                        "reagent_identity_or_order_changed",
                        "observation_points_changed",
                    )
                    if declarations.get(key) is not False
                ]
                if forbidden_declarations:
                    errors.append(
                        "人工 override 必须显式声明所有冻结字段未改变："
                        + ", ".join(forbidden_declarations)
                    )
            expected_ids = sorted(str(item) for item in certificate.get("device_sample_ids", []) or [])
            actual_ids = self._extract_sample_ids(plan_result)
            if expected_ids and actual_ids != expected_ids:
                errors.append(
                    "人工 device_plan 改变或遗漏了冻结的样品/对照矩阵："
                    f"expected={expected_ids}, actual={actual_ids}。"
                )
            expected_matrix = certificate.get("sample_control_matrix", [])
            actual_matrix = plan_result.get("sample_control_matrix", [])
            if expected_matrix and actual_matrix != expected_matrix:
                errors.append(
                    "人工 device_plan 的完整样品/对照/变量矩阵与可行性证书不一致。"
                )

        forbidden = self._forbidden_plan_change_claims(plan_result)
        errors.extend(forbidden)
        errors.extend(self._declared_route_change_errors(plan_result))
        errors.extend(
            self._device_plan_research_alignment_errors(
                state.research_handoff,
                plan_result,
                reference_plan=reference_plan,
                semantic_analysis=self._active_semantic_analysis,
            )
        )
        errors.extend(
            "人工 Device override 未通过计划级确定性校验 "
            f"[{finding.get('type', 'plan_level_finding')}]："
            f"{finding.get('message', finding)}"
            for finding in self._plan_level_findings(state, plan_result)
        )
        if not errors:
            state.feasibility_accepted = True
            state.feasibility_certificate = certificate
            plan_result = self._normalize_quantity_contract(
                plan_result, research_handoff=state.research_handoff
            )
            plan_result["feasibility_accepted"] = True
            plan_result["feasibility_certificate"] = copy.deepcopy(certificate)
            state.add_log(
                "manual device plan accepted; skipped Research bootstrap and "
                "Stage-1 LLM feasibility planning"
            )
        return plan_result, errors

    def _trusted_human_quantity_approvals_from_runtime_bundle(
        self,
        envelope: Dict[str, Any],
        approvals: List[Dict[str, Any]],
        repair_request: Dict[str, Any],
        plan_result: Dict[str, Any],
        certificate: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Recheck wrapper attestations before exposing them to quantity audit.

        Ordinary model output and raw ``human_quantity_approvals`` are never
        read here.  The reserved validation products must appear identically
        in the override and handoff and remain bound to the current repair
        request, accepted certificate, and one exact transition child.
        """

        errors: List[str] = []
        if not isinstance(envelope, dict) or not isinstance(approvals, list):
            return [], [
                "validated human quantity approval 缺少结构化 validation envelope/records。"
            ]
        if (
            envelope.get("validated") is not True
            or envelope.get("validation_version")
            != "chem-human-quantity-approval/v1"
            or self._quantity_value(envelope.get("approval_count"))
            != len(approvals)
        ):
            errors.append("human quantity approval validation envelope 无效。")
        expected_contract_digest = str(
            envelope.get("approved_quantity_contract_digest") or ""
        ).strip()
        actual_contract_digest = approved_quantity_contract_digest(plan_result)
        if (
            not re.fullmatch(r"[0-9a-f]{64}", expected_contract_digest)
            or expected_contract_digest != actual_contract_digest
        ):
            errors.append(
                "human quantity approval 绑定的 Device quantity contract 已漂移。"
            )

        request_id = str(repair_request.get("request_id") or "").strip()
        certificate_bindings = {
            "repair_request_id": request_id,
            "feasibility_certificate_id": str(
                certificate.get("certificate_id") or ""
            ).strip(),
            "feasibility_certificate_digest": str(
                certificate.get("protected_digest") or ""
            ).strip(),
            "research_plan_signature": str(
                repair_request.get("frozen_route_signature")
                or certificate.get("research_plan_signature")
                or ""
            ).strip(),
            "sample_matrix_signature": str(
                repair_request.get("frozen_sample_matrix_signature")
                or certificate.get("sample_matrix_signature")
                or ""
            ).strip(),
            "device_snapshot_signature": str(
                repair_request.get("device_snapshot_signature")
                or certificate.get("device_snapshot_signature")
                or ""
            ).strip(),
        }
        for field_name, expected in certificate_bindings.items():
            if not expected or str(envelope.get(field_name) or "").strip() != expected:
                errors.append(
                    f"human quantity approval envelope 的 {field_name} 与冻结请求不一致。"
                )
        request_digest = str(envelope.get("repair_request_digest") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", request_digest):
            errors.append("human quantity approval repair_request_digest 无效。")

        transition_by_id = {
            str(item.get("transition_id") or "").strip(): item
            for item in plan_result.get("material_transitions", []) or []
            if isinstance(item, dict) and str(item.get("transition_id") or "").strip()
        }
        batch_by_id = {
            str(item.get("batch_id") or "").strip(): item
            for item in plan_result.get("batch_plan", []) or []
            if isinstance(item, dict) and str(item.get("batch_id") or "").strip()
        }

        unknown_yield_transition_ids: Set[str] = set()

        def collect_unknown_yield(value: Any) -> None:
            if isinstance(value, dict):
                code = str(
                    value.get("code")
                    or value.get("error_code")
                    or value.get("type")
                    or ""
                ).strip()
                if code == "unknown_yield":
                    transition_id = str(value.get("transition_id") or "").strip()
                    if transition_id:
                        unknown_yield_transition_ids.add(transition_id)
                for nested in value.values():
                    collect_unknown_yield(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect_unknown_yield(nested)

        collect_unknown_yield(repair_request.get("structured_errors", []))
        seen_targets: Set[Tuple[str, str]] = set()
        trusted: List[Dict[str, Any]] = []
        for index, approval in enumerate(approvals, start=1):
            if not isinstance(approval, dict):
                errors.append(f"validated approval[{index}] 必须是 object。")
                continue
            if any(
                str(approval.get(field_name) or "").strip() != expected
                for field_name, expected in certificate_bindings.items()
            ) or str(approval.get("repair_request_digest") or "").strip() != request_digest:
                errors.append(f"validated approval[{index}] 的请求/证书绑定漂移。")
                continue
            transition_id = str(approval.get("transition_id") or "").strip()
            batch_id = str(approval.get("batch_id") or "").strip()
            target = (transition_id, batch_id)
            transition = transition_by_id.get(transition_id)
            batch = batch_by_id.get(batch_id)
            if (
                target in seen_targets
                or transition is None
                or batch is None
                or transition_id not in unknown_yield_transition_ids
            ):
                errors.append(f"validated approval[{index}] 不是当前唯一 unknown_yield child。")
                continue
            seen_targets.add(target)
            child_ids = {
                str(value).strip()
                for value in transition.get("child_batch_ids", []) or []
                if str(value).strip()
            } if isinstance(transition.get("child_batch_ids"), list) else set()
            approved_value, approved_dimension, _ = self._canonical_quantity(
                approval.get("approved_quantity")
            )
            batch_value, batch_dimension, _ = self._canonical_quantity(
                batch.get("total_quantity")
            )
            output_matches = [
                edge
                for edge in transition.get("output_allocations", []) or []
                if isinstance(edge, dict)
                and str(edge.get("batch_id") or "").strip() == batch_id
            ]
            output_value, output_dimension, _ = self._canonical_quantity(
                output_matches[0].get("quantity")
                if len(output_matches) == 1
                else None
            )
            basis = str(approval.get("approval_basis") or "").strip()
            attestation_kind = str(
                approval.get("attestation_kind") or ""
            ).strip()
            transition_kind = str(
                transition.get("transition_kind") or ""
            ).strip()
            transition_quantity_basis = str(
                transition.get("quantity_basis") or ""
            ).strip()
            basis_valid = (
                basis == "observed_quantity"
                and attestation_kind == "human_attested_measurement"
                and transition_kind == "state_change"
                and transition_quantity_basis == "measured_observation"
            ) or (
                basis == "planning_yield_lower_bound"
                and attestation_kind == "human_approved_planning_bound"
                and transition_kind == "state_change"
                and transition_quantity_basis
                == "planning_yield_lower_bound"
                and self._quantity_value(approval.get("yield_lower_bound"))
                == self._quantity_value(transition.get("yield_lower_bound"))
            )
            if (
                batch_id not in child_ids
                or str(approval.get("sample_id") or "").strip()
                != str(batch.get("sample_id") or "").strip()
                or str(approval.get("material_id") or "").strip()
                != str(batch.get("material_id") or "").strip()
                or approval.get("scientific_review") is not True
                or approval.get("acknowledges_scientific_review") is not True
                or approved_value is None
                or approved_value <= 0
                or not self._canonical_quantities_equal(
                    approved_value,
                    approved_dimension,
                    batch_value if batch_value is not None else float("nan"),
                    batch_dimension,
                )
                or not self._canonical_quantities_equal(
                    approved_value,
                    approved_dimension,
                    output_value if output_value is not None else float("nan"),
                    output_dimension,
                )
                or not basis_valid
            ):
                errors.append(f"validated approval[{index}] 的目标/数量/attestation 不一致。")
                continue
            trusted.append(copy.deepcopy(approval))
        return trusted, list(dict.fromkeys(errors))

    @staticmethod
    def _forbidden_plan_change_claims(plan_result: Dict[str, Any]) -> List[str]:
        changes = plan_result.get("plan_changes")
        if not isinstance(changes, list):
            changes = plan_result.get("before_after")
        if not isinstance(changes, list):
            return []
        forbidden = re.compile(
            r"目标材料|研究目标|化学路线|反应路线|试剂(?:身份|种类|顺序)|"
            r"observation(?:_point)?|观察点|观测点|样品矩阵|对照组|样品组",
            re.I,
        )
        errors = []
        for change in changes:
            text = _json_text(change)
            if forbidden.search(text):
                errors.append(
                    "device plan change ledger 触及冻结字段；当前 Device repair/override "
                    "必须拒绝（如确需改路线，由外部显式启动新的 Research 规划）："
                    + text[:240]
                )
        return errors

    @staticmethod
    def _declared_route_change_errors(plan_result: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        if plan_result.get("route_changed") not in (None, False):
            errors.append("device_plan 声明 route_changed=true。")
        for adjustment in plan_result.get("quantity_adjustments", []) or []:
            if isinstance(adjustment, dict) and adjustment.get("route_changed") not in (
                None,
                False,
            ):
                errors.append(
                    "quantity adjustment "
                    f"{adjustment.get('adjustment_id', '?')} 声明 route_changed=true。"
                )
        return errors

    def _run_accepted_device_plan(
        self,
        state: SingleDeviceAgentState,
        plan_result: Dict[str, Any],
        *,
        allow_plan_rewrite: bool,
        resumed_from_manual: bool,
    ) -> Dict[str, Any]:
        """Run one or two Device-only workflow repair cycles.

        Each cycle contains an initial workflow candidate plus at most eight
        new LLM candidates.  Only a non-resumed first cycle may trigger the
        single plan-level Device rewrite.
        """
        wait_findings = self._external_return_wait_findings(state.research_handoff, plan_result)
        if wait_findings:
            return self._external_return_wait_result(state, plan_result, wait_findings)
        plan_result = self._normalize_quantity_contract(
            copy.deepcopy(plan_result),
            research_handoff=state.research_handoff,
        )
        quantity_audit = plan_result.get("quantity_audit", {})
        (
            initial_human_quantity_issues,
            deterministic_quantity_issues,
        ) = self._partition_quantity_audit_issues(quantity_audit)
        quantity_status = (
            str(quantity_audit.get("status", "")).strip().lower()
            if isinstance(quantity_audit, dict)
            else ""
        )
        if (
            quantity_status == "human_review_required"
            and not deterministic_quantity_issues
        ):
            return self._build_manual_result(
                state,
                plan_result,
                reason="quantity judgement requires human input",
                error_type="device_quantity_human_review_required",
            )
        quantity_failed_before_workflow = bool(
            quantity_status == "failed" or deterministic_quantity_issues
        )
        if quantity_failed_before_workflow:
            quantity_errors = [
                str(item.get("message", item))
                for item in (
                    deterministic_quantity_issues
                    or (
                        quantity_audit.get("issues", [])
                        if isinstance(quantity_audit, dict)
                        else []
                    )
                )
            ]
            first_result = copy.deepcopy(plan_result)
            first_result.update(
                {
                    "status": "failed",
                    "feedback_type": "device_local_quantity_error",
                    "feedback_route": "device",
                    "failure_scope": "device_quantity",
                    "workflow_txt": "",
                    "workflow_json": {},
                    "dispatch_validation": {
                        "status": "failed",
                        "errors": quantity_errors,
                        "warnings": [],
                        "checked_steps": 0,
                        "assessment_source": "deterministic_quantity_auditor",
                    },
                    "workflow_repair_cycle": {
                        "initial_candidate_count": 0,
                        "modification_count": 0,
                        "max_modifications": self._workflow_repair_limit(),
                        "status": "failed",
                        "rounds": [
                            {
                                "round": 0,
                                "candidate_kind": "pre_workflow_quantity_audit",
                                "status": "failed",
                                "errors": quantity_errors,
                                "issues": copy.deepcopy(
                                    quantity_audit.get("issues", [])
                                ),
                            }
                        ],
                        "mechanical_completions_counted": False,
                    },
                }
            )
        else:
            first_result = self._translate_and_verify(state, plan_result)
        self._record_workflow_cycle(state, 1, plan_result, first_result)
        self._attach_device_repair_context(state, first_result, resumed_from_manual)
        if self._workflow_result_passed(first_result):
            return first_result
        if self._result_is_device_internal(first_result):
            return first_result
        if not allow_plan_rewrite:
            return self._build_manual_result(
                state,
                first_result,
                reason=(
                    "manual override still fails deterministic quantity audit"
                    if quantity_failed_before_workflow
                    else "manual override workflow repair cycle exhausted"
                ),
                error_type=(
                    "device_quantity_human_review_required"
                    if quantity_failed_before_workflow
                    else "device_workflow_repair_exhausted"
                ),
            )

        state.device_plan_rewrite_count += 1
        repaired_plan = self._invoke_device_plan_repair(
            state, plan_result, first_result
        )
        if str(repaired_plan.get("status", "")).strip().lower() == "human_review_required":
            # Keep the last certified plan executable for human resume even if
            # the repair LLM returns only an unknown-yield/pooling judgement.
            # Its unvalidated plan edits are never adopted on this branch.
            human_plan = copy.deepcopy(plan_result)
            for key in (
                "quantity_adjustments",
                "batch_plan",
                "material_ledger",
                "quantity_audit",
                "pending_quantity_human_review",
            ):
                if key in repaired_plan:
                    human_plan[key] = copy.deepcopy(repaired_plan[key])
            repaired_human_issues, _ = self._partition_quantity_audit_issues(
                repaired_plan.get("pending_quantity_human_review")
                if isinstance(
                    repaired_plan.get("pending_quantity_human_review"), dict
                )
                else repaired_plan.get("quantity_audit")
            )
            human_plan["pending_quantity_human_review"] = (
                self._pending_quantity_human_review_audit(
                    initial_human_quantity_issues + repaired_human_issues
                )
            )
            human_plan = self._normalize_quantity_contract(
                human_plan, research_handoff=state.research_handoff
            )
            return self._build_manual_result(
                state,
                human_plan,
                reason=(
                    "plan-level Device repair found unknown yield, unauthorized "
                    "pooling, or another non-inferable quantity decision"
                ),
                error_type="device_quantity_human_review_required",
            )
        repaired_plan, repair_errors = self._validate_repaired_device_plan(
            state, plan_result, repaired_plan
        )
        if repair_errors:
            first_result.setdefault("plan_level_repair", {})
            first_result["plan_level_repair"].update(
                {
                    "status": "rejected",
                    "errors": repair_errors,
                    "attempt_count": state.device_plan_rewrite_count,
                }
            )
            return self._build_manual_result(
                state,
                first_result,
                reason="plan-level Device rewrite violated or omitted frozen invariants",
                error_type="device_plan_rewrite_rejected",
                extra_errors=repair_errors,
            )

        if initial_human_quantity_issues:
            repaired_human_issues, _ = self._partition_quantity_audit_issues(
                repaired_plan.get("pending_quantity_human_review")
                if isinstance(
                    repaired_plan.get("pending_quantity_human_review"), dict
                )
                else repaired_plan.get("quantity_audit")
            )
            repaired_plan["pending_quantity_human_review"] = (
                self._pending_quantity_human_review_audit(
                    initial_human_quantity_issues + repaired_human_issues
                )
            )
        repaired_plan = self._normalize_quantity_contract(
            repaired_plan, research_handoff=state.research_handoff
        )
        repaired_quantity_status = (repaired_plan.get("quantity_audit") or {}).get(
            "status"
        )
        if repaired_quantity_status in {"human_review_required", "failed"}:
            return self._build_manual_result(
                state,
                repaired_plan,
                reason=(
                    "replanned quantities still require unknown-yield or pooling judgement"
                    if repaired_quantity_status == "human_review_required"
                    else "plan-level Device rewrite did not resolve deterministic quantity errors"
                ),
                error_type="device_quantity_human_review_required",
            )

        second_result = self._translate_and_verify(state, repaired_plan)
        second_result["plan_level_repair"] = {
            "status": "accepted",
            "attempt_count": state.device_plan_rewrite_count,
            "plan_changes": repaired_plan.get("plan_changes", []),
            "change_rationale": repaired_plan.get("change_rationale", ""),
            "expected_resolved_errors": repaired_plan.get(
                "expected_resolved_errors", []
            ),
        }
        self._record_workflow_cycle(state, 2, repaired_plan, second_result)
        self._attach_device_repair_context(state, second_result, resumed_from_manual)
        if self._workflow_result_passed(second_result) or self._result_is_device_internal(second_result):
            return second_result
        return self._build_manual_result(
            state,
            second_result,
            reason="two Device workflow repair cycles exhausted",
            error_type="device_workflow_repair_exhausted",
        )

    @staticmethod
    def _partition_quantity_audit_issues(
        quantity_audit: Any,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Split human judgements from deterministic Device quantity errors."""
        human: List[Dict[str, Any]] = []
        deterministic: List[Dict[str, Any]] = []
        if not isinstance(quantity_audit, dict):
            return human, deterministic
        status = str(quantity_audit.get("status", "")).strip().lower()
        for issue in quantity_audit.get("issues", []) or []:
            record = (
                copy.deepcopy(issue)
                if isinstance(issue, dict)
                else {
                    "code": "declared_quantity_human_review",
                    "scope": (
                        "human_review_required"
                        if status == "human_review_required"
                        else "device_local_quantity"
                    ),
                    "message": str(issue),
                }
            )
            if str(record.get("scope", "")).strip() == "human_review_required":
                human.append(record)
            else:
                deterministic.append(record)
        return human, deterministic

    @staticmethod
    def _pending_quantity_human_review_audit(
        issues: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Carry human-only issues across a Device rewrite without duplication."""
        deduplicated: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            record = copy.deepcopy(issue)
            record["scope"] = "human_review_required"
            identity = json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            if identity in seen:
                continue
            seen.add(identity)
            deduplicated.append(record)
        return {
            "status": "human_review_required",
            "failure_scope": "human_review_required",
            "issues": deduplicated,
            "assessment_source": "device_rewrite_human_issue_carry_forward",
        }

    @staticmethod
    def _workflow_result_passed(result: Dict[str, Any]) -> bool:
        report = result.get("dispatch_validation")
        workflow = result.get("workflow_json")
        quantity_audit = result.get("quantity_audit")
        quantity_blocked = bool(
            isinstance(quantity_audit, dict)
            and quantity_audit.get("status") in {"failed", "human_review_required"}
        ) or bool(
            result.get("quantity_contract_required")
            and (
                not isinstance(quantity_audit, dict)
                or quantity_audit.get("status") != "passed"
            )
        )
        return bool(
            not quantity_blocked
            and isinstance(report, dict)
            and report.get("status") != "failed"
            and isinstance(workflow, dict)
            and isinstance(workflow.get("steps"), list)
            and workflow.get("steps")
        )

    @staticmethod
    def _result_is_device_internal(result: Dict[str, Any]) -> bool:
        report = result.get("dispatch_validation")
        source = report.get("assessment_source") if isinstance(report, dict) else ""
        return bool(
            result.get("feedback_type") == "device_internal_error"
            or source
            in {
                "deterministic_recipe_materializer",
                "llm_workstation_skill_reviewer_internal",
                "deterministic_device_validation_internal",
                "final_dispatch_formatter_internal",
            }
        )

    def _record_workflow_cycle(
        self,
        state: SingleDeviceAgentState,
        cycle: int,
        plan_result: Dict[str, Any],
        result: Dict[str, Any],
    ) -> None:
        local = result.get("workflow_repair_cycle")
        if not isinstance(local, dict):
            skill = result.get("workflow_skill_review")
            skill = skill if isinstance(skill, dict) else {}
            local = {
                "initial_candidate_count": 1,
                "modification_count": int(skill.get("rewrite_count", 0) or 0),
                "max_modifications": self._workflow_repair_limit(),
                "status": "passed" if self._workflow_result_passed(result) else "failed",
                "rounds": skill.get("rounds", []),
            }
        record = copy.deepcopy(local)
        record.update(
            {
                "cycle": cycle,
                "device_plan_signature": self._stable_digest(
                    plan_result.get("device_plan", []), prefix="device_plan"
                ),
            }
        )
        state.workflow_repair_cycles.append(record)
        for item in self._cycle_error_records(cycle, result):
            key = self._stable_digest(
                {
                    "errors": item.get("errors", []),
                    "issues": item.get("issues", []),
                },
                prefix="repair_error",
            )
            if any(existing.get("dedupe_key") == key for existing in state.workflow_repair_history):
                continue
            item["dedupe_key"] = key
            state.workflow_repair_history.append(item)

    def _cycle_error_records(
        self, cycle: int, result: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        repair_cycle = result.get("workflow_repair_cycle")
        rounds = repair_cycle.get("rounds", []) if isinstance(repair_cycle, dict) else []
        if isinstance(rounds, list):
            for round_record in rounds:
                if not isinstance(round_record, dict):
                    continue
                errors = round_record.get("errors", [])
                issues = round_record.get("issues", [])
                records.append(
                    {
                        "cycle": cycle,
                        "round": round_record.get("round"),
                        "candidate_kind": round_record.get("candidate_kind", "workflow"),
                        "errors": [str(item) for item in errors] if isinstance(errors, list) else [],
                        "issues": copy.deepcopy(issues) if isinstance(issues, list) else [],
                    }
                )
        report = result.get("dispatch_validation")
        if not records and isinstance(report, dict) and report.get("status") == "failed":
            records.append(
                {
                    "cycle": cycle,
                    "round": 1,
                    "candidate_kind": "workflow",
                    "errors": [str(item) for item in report.get("errors", [])],
                    "issues": [],
                }
            )
        return records

    def _attach_device_repair_context(
        self,
        state: SingleDeviceAgentState,
        result: Dict[str, Any],
        resumed_from_manual: bool,
    ) -> None:
        result["feasibility_accepted"] = True
        result["feasibility_certificate"] = copy.deepcopy(
            state.feasibility_certificate
        )
        result["workflow_repair"] = {
            "max_modifications_per_cycle": self._workflow_repair_limit(),
            "cycles": copy.deepcopy(state.workflow_repair_cycles),
            "plan_level_rewrite_count": state.device_plan_rewrite_count,
            "resumed_from_manual": resumed_from_manual,
            "deduplicated_error_history": copy.deepcopy(
                state.workflow_repair_history
            ),
        }

    def _build_manual_result(
        self,
        state: SingleDeviceAgentState,
        result: Dict[str, Any],
        *,
        reason: str,
        error_type: str,
        extra_errors: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        manual = copy.deepcopy(result)
        errors = list(extra_errors or [])
        report = manual.get("dispatch_validation")
        if isinstance(report, dict):
            errors.extend(str(item) for item in report.get("errors", []) if str(item).strip())
        quantity_audit = manual.get("quantity_audit")
        if isinstance(quantity_audit, dict):
            errors.extend(
                str(item.get("message", item))
                for item in quantity_audit.get("issues", [])
            )
        manual.update(
            {
                "status": "manual_required",
                "feedback_type": "human_review_required",
                "feedback_route": "human",
                "failure_scope": (
                    "device_quantity"
                    if "quantity" in error_type
                    else (
                        "device_plan"
                        if "plan" in error_type
                        else "device_workflow"
                    )
                ),
                "failure_stage": (
                    "device_quantity_repair_exhausted"
                    if "quantity" in error_type
                    else (
                        "device_plan_repair_exhausted"
                        if "plan" in error_type
                        else "device_repair_exhausted"
                    )
                ),
                "feasibility_accepted": state.feasibility_accepted,
                "feasibility_certificate": copy.deepcopy(
                    state.feasibility_certificate
                ),
                "requires_scientific_review": bool(
                    manual.get("requires_scientific_review")
                    or (manual.get("quantity_audit") or {}).get(
                        "requires_scientific_review"
                    )
                    or (manual.get("quantity_audit") or {}).get("status")
                    == "human_review_required"
                ),
            }
        )
        prior_repair = manual.get("workflow_repair")
        resumed_from_manual = bool(
            isinstance(prior_repair, dict)
            and prior_repair.get("resumed_from_manual")
        )
        self._attach_device_repair_context(
            state, manual, resumed_from_manual
        )
        structured_errors = self._manual_structured_errors(
            quantity_audit=quantity_audit,
            errors=errors,
            workflow_json=(
                manual.get("workflow_json")
                if isinstance(manual.get("workflow_json"), dict)
                else {}
            ),
            default_scope=str(manual.get("failure_scope", "")),
        )
        manual["error_package"] = {
            "type": error_type,
            "assessment_source": "single_device_agent_local_repair_state_machine",
            "blocking_constraints": list(dict.fromkeys(errors))[:32],
            "structured_errors": structured_errors,
            "failed_plan_signature": self._plan_signature(state.research_handoff),
            "device_snapshot_id": self._device_snapshot_id(),
            "message": reason,
        }
        manual["manual_repair_context"] = {
            "reason": reason,
            "last_device_plan": copy.deepcopy(manual.get("device_plan", [])),
            "last_workflow": copy.deepcopy(manual.get("workflow_json", {})),
            "repair_history": copy.deepcopy(state.workflow_repair_history),
            "workflow_repair_cycles": copy.deepcopy(state.workflow_repair_cycles),
            "allowed_changes": [
                "workstation",
                "container",
                "operation_decomposition",
                "total_amount",
                "concentration",
                "molar_ratio",
            ],
            "frozen_fields": [
                "research_goal",
                "target_material",
                "reaction_route",
                "reagent_identity_and_order",
                "observation_point",
                "sample_control_matrix",
            ],
        }
        manual["repair_history"] = copy.deepcopy(
            state.workflow_repair_history
        )
        manual["repair_cycles"] = copy.deepcopy(
            state.workflow_repair_cycles
        )
        manual["workflow_repair_history"] = copy.deepcopy(
            state.workflow_repair_history
        )
        manual["last_device_plan"] = copy.deepcopy(
            manual.get("device_plan", [])
        )
        manual["last_workflow"] = copy.deepcopy(
            manual.get("workflow_json", {})
        )
        manual["research_plan_signature"] = state.feasibility_certificate.get(
            "research_plan_signature", ""
        )
        manual["sample_matrix_signature"] = state.feasibility_certificate.get(
            "sample_matrix_signature", ""
        )
        manual["device_snapshot_signature"] = state.feasibility_certificate.get(
            "device_snapshot_signature", self._device_snapshot_id()
        )
        return manual

    @staticmethod
    def _manual_structured_errors(
        *,
        quantity_audit: Any,
        errors: List[str],
        workflow_json: Dict[str, Any],
        default_scope: str,
    ) -> List[Dict[str, Any]]:
        """Preserve quantity-audit issue codes in manual repair packages.

        Quantity issues are already structured by the deterministic auditor;
        converting their messages back through the workflow-error string
        parser loses the original ``code`` and turns every record into
        ``unparsed``.  Normalize those records directly, then use the legacy
        parser only for additional string errors (for example workflow or
        override validation messages).
        """

        structured: List[Dict[str, Any]] = []
        seen_records: Set[str] = set()
        quantity_messages: Set[str] = set()
        legacy_errors: List[str] = []

        def append_record(record: Dict[str, Any]) -> None:
            message = str(record.get("message", "")).strip()
            if not message:
                return
            normalized = copy.deepcopy(record)
            normalized["message"] = message
            identity = json.dumps(
                normalized,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            if identity in seen_records:
                return
            seen_records.add(identity)
            structured.append(normalized)

        issues = (
            quantity_audit.get("issues", [])
            if isinstance(quantity_audit, dict)
            else []
        )
        for issue in issues if isinstance(issues, list) else []:
            if not isinstance(issue, dict):
                text = str(issue).strip()
                if text:
                    legacy_errors.append(text)
                continue
            message = str(issue.get("message", "")).strip()
            if not message:
                message = _json_text(issue).strip()
            if not message:
                continue
            code = str(
                issue.get("error_code") or issue.get("code") or "unparsed"
            ).strip() or "unparsed"
            scope = str(
                issue.get("scope")
                or issue.get("failure_scope")
                or default_scope
            ).strip()
            record: Dict[str, Any] = {
                "error_code": code,
                "code": code,
                "message": message,
            }
            if scope:
                record["scope"] = scope
            for target_field in (
                "transition_id",
                "batch_id",
                "sample_id",
                "material_id",
                "entry_id",
            ):
                if issue.get(target_field) not in (None, ""):
                    record[target_field] = copy.deepcopy(
                        issue[target_field]
                    )
            quantity_messages.add(message)
            append_record(record)

        for raw in errors:
            text = str(raw).strip()
            if text and text not in quantity_messages:
                legacy_errors.append(text)

        for record in structure_validation_errors(
            list(dict.fromkeys(legacy_errors)), workflow_json
        ):
            if default_scope and not record.get("scope"):
                record["scope"] = default_scope
            append_record(record)
        return structured

    def _manual_override_rejected_result(
        self,
        state: SingleDeviceAgentState,
        plan_result: Dict[str, Any],
        errors: List[str],
    ) -> Dict[str, Any]:
        state.feasibility_accepted = bool(state.feasibility_certificate)
        rejected = copy.deepcopy(plan_result)
        rejected["dispatch_validation"] = {
            "status": "failed",
            "errors": list(errors),
            "warnings": [],
            "checked_steps": 0,
            "assessment_source": "deterministic_device_override_validator",
        }
        return self._build_manual_result(
            state,
            rejected,
            reason="人工 Device override 被拒绝；冻结路线、样品矩阵或设备快照不匹配。",
            error_type="device_plan_override_invariant_violation",
            extra_errors=errors,
        )

    def _stage1_device_local_manual_result(
        self,
        state: SingleDeviceAgentState,
        plan_result: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Certify the frozen route when only Device-local planning is stuck."""
        wait_findings = self._external_return_wait_findings(state.research_handoff, plan_result)
        if wait_findings:
            return self._external_return_wait_result(state, plan_result, wait_findings)
        if str(plan_result.get("status", "")).strip().lower() not in {
            "feasibility_error",
            "unsupported",
            "not_feasible",
        } and plan_result.get("feedback_type") != "device_feasibility_error":
            return None
        blocking = self._canonical_stage1_blockers(plan_result)
        if (
            not blocking
            or any(self._constraint_is_true_route_gap(item) for item in blocking)
            or not all(self._constraint_is_device_local(item) for item in blocking)
        ):
            return None
        local_text = _json_text(blocking)
        if not re.search(
            r"剂量|物质的量|质量不足|用量|料位|累计取液|重复消费|重复计量|"
            r"容量|体积|分批|拆批|分瓶|配平|容器|瓶盖|开盖|关盖",
            local_text,
            re.I,
        ):
            # A pre-certificate schema/fixed-capability complaint is not proof
            # that route feasibility was accepted.  Keep it human/unaccepted;
            # only concrete quantity/container planning dead-ends receive the
            # route-only certificate used for Device override repair.
            return None
        provisional = copy.deepcopy(plan_result)
        provisional["status"] = "device_plan"
        if not isinstance(provisional.get("device_plan"), list):
            provisional["device_plan"] = []
        original_feasibility = copy.deepcopy(
            provisional.get("feasibility")
            if isinstance(provisional.get("feasibility"), dict)
            else {}
        )
        original_unsupported = original_feasibility.get("unsupported_items", [])
        if isinstance(original_unsupported, list):
            for item in original_unsupported:
                if not isinstance(item, dict):
                    continue
                suggestion = str(
                    item.pop("suggested_research_revision", "") or ""
                ).strip()
                if suggestion:
                    item["device_review_note"] = (
                        "仅供 Device 层人工科学复核，不得自动返回 Research。"
                        f"原建议：{suggestion}"
                    )
        route_feasibility = copy.deepcopy(original_feasibility)
        route_feasibility.update(
            {
                "is_feasible": True,
                "route_feasibility_accepted": True,
                "acceptance_scope": "route_only_pending_device_plan_repair",
                "blocking_constraints": [],
                "unsupported_items": [],
                "device_local_pending_constraints": copy.deepcopy(blocking),
            }
        )
        provisional["stage1_original_feasibility"] = original_feasibility
        provisional["feasibility"] = route_feasibility
        provisional["blocking_constraints"] = []
        provisional["pending_device_local_constraints"] = copy.deepcopy(blocking)
        provisional.pop("recommendation_to_research_agent", None)
        provisional.pop("constraint_classification", None)
        state.feasibility_accepted = True
        state.feasibility_certificate = self._build_feasibility_certificate(
            state, provisional
        )
        state.feasibility_certificate["acceptance_scope"] = (
            "route_only_pending_device_plan_repair"
        )
        provisional["feasibility_accepted"] = True
        provisional["feasibility_certificate"] = copy.deepcopy(
            state.feasibility_certificate
        )
        is_quantity = bool(
            re.search(
                r"剂量|物质的量|质量不足|用量|料位|累计取液|重复消费|"
                r"重复计量|容量|体积|分批|拆批|分瓶|配平",
                local_text,
                re.I,
            )
        )
        if is_quantity:
            quantity_issues = [
                {
                    "code": "stage1_quantity_conflict",
                    "scope": "device_quantity",
                    "message": item,
                }
                for item in blocking
                if re.search(
                    r"剂量|物质的量|质量不足|用量|料位|累计取液|"
                    r"重复消费|重复计量|容量|体积|分批|拆批|分瓶|配平",
                    item,
                    re.I,
                )
            ]
            provisional["quantity_audit"] = {
                "status": "human_review_required",
                "failure_scope": "device_quantity",
                "issues": quantity_issues,
                "requires_scientific_review": True,
                "assessment_source": (
                    "stage1_device_local_constraint_classifier"
                ),
            }
            provisional["pending_quantity_human_review"] = copy.deepcopy(
                provisional["quantity_audit"]
            )
            provisional["requires_scientific_review"] = True
        return self._build_manual_result(
            state,
            provisional,
            reason=(
                "Stage-1 已确认 Research 路线不变，但 Device-local 数量/容器/"
                "参数计划在同层重试后仍未产出完整计划；等待人工 Device plan。"
            ),
            error_type=(
                "device_quantity_stage1_repair_required"
                if is_quantity
                else "device_plan_stage1_repair_required"
            ),
            extra_errors=blocking,
        )

    @staticmethod
    def _quantity_value(value: Any) -> Optional[float]:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            number = float(value)
            return number if math.isfinite(number) else None
        if isinstance(value, dict):
            for key in ("value", "amount", "quantity", "数值", "数量"):
                if key in value:
                    return SingleDeviceAgent._quantity_value(value[key])
            return None
        if isinstance(value, str):
            match = re.search(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", value)
            if match:
                try:
                    number = float(match.group(0))
                    return number if math.isfinite(number) else None
                except ValueError:
                    return None
        return None

    @staticmethod
    def _canonical_quantity(value: Any) -> Tuple[Optional[float], str, str]:
        """Return value in a canonical unit for conservative ledger sums."""
        number = SingleDeviceAgent._quantity_value(value)
        unit = ""
        if isinstance(value, dict):
            unit = str(value.get("unit", value.get("单位", ""))).strip()
        elif isinstance(value, str):
            match = re.search(
                r"(?:[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*"
                r"([A-Za-zµμ]+(?:/[A-Za-z]+)?)",
                value,
            )
            if match:
                unit = match.group(1)
        normalized_unit = unit.replace("μ", "u").replace("µ", "u").lower()
        conversions = {
            "mol": (1.0, "amount", "mol"),
            "mmol": (1e-3, "amount", "mol"),
            "umol": (1e-6, "amount", "mol"),
            "nmol": (1e-9, "amount", "mol"),
            "g": (1.0, "mass", "g"),
            "mg": (1e-3, "mass", "g"),
            "ug": (1e-6, "mass", "g"),
            "kg": (1e3, "mass", "g"),
            "l": (1000.0, "volume", "mL"),
            "ml": (1.0, "volume", "mL"),
            "ul": (1e-3, "volume", "mL"),
            "nl": (1e-6, "volume", "mL"),
            "m": (1.0, "concentration", "mol/L"),
            "mm": (1e-3, "concentration", "mol/L"),
            "um": (1e-6, "concentration", "mol/L"),
            "nm": (1e-9, "concentration", "mol/L"),
            "mol/l": (1.0, "concentration", "mol/L"),
            "mmol/l": (1e-3, "concentration", "mol/L"),
            "umol/l": (1e-6, "concentration", "mol/L"),
        }
        if number is None:
            return None, "unknown", unit
        if not normalized_unit:
            return number, "unitless", ""
        if normalized_unit not in conversions:
            return number, f"unknown:{normalized_unit}", unit
        factor, dimension, canonical_unit = conversions[normalized_unit]
        canonical_number = number * factor
        if not math.isfinite(canonical_number):
            return None, "unknown", unit
        return canonical_number, dimension, canonical_unit

    @classmethod
    def _extract_explicit_quantities(
        cls, value: Any
    ) -> List[Dict[str, Any]]:
        """Extract auditable quantities from one frozen Research field.

        This helper deliberately does not infer which of several quantities is
        a batch total.  Callers must reject an ambiguous source field instead
        of choosing by position or magnitude.
        """
        if value in (None, ""):
            return []
        text = str(value)
        pattern = re.compile(
            r"(?<![A-Za-z0-9])([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*"
            r"((?:mol|mmol|umol)\s*(?:/|·)?\s*[lL]\s*(?:[-−]\s*1|⁻¹)?|"
            r"[mun]?mol(?!\s*(?:/|·)?\s*[lL])|kg|mg|[uµμ]g|g|mL|ml|[uµμ]L|"
            r"[uµμ]l|nL|L|[mun]?M)\b",
            re.I,
        )
        extracted: List[Dict[str, Any]] = []
        for match in pattern.finditer(text):
            raw = match.group(0)
            canonical_input = re.sub(
                r"(mol|mmol|umol)\s*(?:/|·)?\s*[lL]\s*(?:[-−]\s*1|⁻¹)?",
                lambda unit_match: f"{unit_match.group(1)}/L",
                raw,
                flags=re.I,
            )
            canonical_value, dimension, canonical_unit = cls._canonical_quantity(
                canonical_input
            )
            if canonical_value is None or not cls._quantity_dimension_is_auditable(
                dimension
            ):
                continue
            extracted.append(
                {
                    "raw": raw,
                    "start": match.start(),
                    "end": match.end(),
                    "value": canonical_value,
                    "dimension": dimension,
                    "canonical_unit": canonical_unit,
                }
            )
        return extracted

    @classmethod
    def _source_context_binds_material_identity(
        cls,
        source_context: Any,
        material_identity: Any,
        quantity_record: Dict[str, Any],
        frozen_research_identity: Any,
        frozen_source_field: Any,
    ) -> bool:
        """Bind a cited quantity to the reagent named next to it.

        A macro step may list Ni, Fe, base and water in one field.  Merely
        proving that both an identity and a number occur somewhere in that
        macro step would allow the Ni batch to steal Fe's volume.  Require a
        deterministic identity anchor in the local phrase around the selected
        quantity instead.
        """
        context = str(source_context or "")
        frozen_field = str(frozen_source_field or "")
        context_offset = frozen_field.find(context)
        if context_offset < 0:
            return False
        start = context_offset + int(quantity_record.get("start", 0))
        end = context_offset + int(quantity_record.get("end", 0))
        identity_text = str(material_identity or "").strip()
        if not identity_text or not frozen_field:
            return False
        frozen_parts = [
            part.strip()
            for part in re.split(
                r"[,，、;；]|\s+(?:和|与)\s+",
                str(frozen_research_identity or ""),
            )
            if part.strip()
        ]
        element_counts: Dict[str, int] = {}
        for part in frozen_parts:
            formula_match = re.match(r"\s*([A-Za-z0-9()·.]+)", part)
            element_match = (
                re.match(r"([A-Z][a-z]?)", formula_match.group(1))
                if formula_match
                else None
            )
            if element_match:
                element = element_match.group(1).casefold()
                element_counts[element] = element_counts.get(element, 0) + 1

        def anchors_for(identity: str) -> List[str]:
            anchors: List[str] = []
            formula_match = re.match(r"\s*([A-Za-z0-9()·.]+)", identity)
            if formula_match:
                formula = formula_match.group(1).strip(".")
                if formula:
                    anchors.append(formula)
                element_match = re.match(r"([A-Z][a-z]?)", formula)
                if (
                    element_match
                    and element_counts.get(element_match.group(1).casefold(), 0) == 1
                ):
                    anchors.append(element_match.group(1))
            else:
                trimmed = re.sub(
                    r"(?:原液|溶液|悬浊液|沉淀物|对照|组分)$",
                    "",
                    identity.strip(),
                ).strip()
                if trimmed:
                    anchors.append(trimmed)
            return sorted(set(anchors), key=len, reverse=True)

        def anchor_distances(identity: str) -> List[int]:
            distances: List[int] = []
            for anchor in anchors_for(identity):
                if re.fullmatch(r"[A-Za-z0-9()·.]+", anchor):
                    pattern = re.compile(
                        rf"(?<![A-Za-z0-9]){re.escape(anchor)}(?![A-Za-z0-9])",
                        re.I,
                    )
                    matches = pattern.finditer(frozen_field)
                else:
                    pattern = re.compile(re.escape(anchor))
                    matches = pattern.finditer(frozen_field)
                for match in matches:
                    if match.end() <= start:
                        distance = start - match.end()
                    elif match.start() >= end:
                        distance = match.start() - end
                    else:
                        distance = 0
                    distances.append(distance)
            return distances

        declared_distances = anchor_distances(identity_text)
        if not declared_distances:
            return False
        declared_distance = min(declared_distances)
        if declared_distance > 18:
            return False
        competitor_distances: List[int] = []
        for frozen_part in frozen_parts:
            if cls._identity_token_sets_match(identity_text, frozen_part):
                continue
            competitor_distances.extend(anchor_distances(frozen_part))
        return not competitor_distances or declared_distance < min(
            competitor_distances
        )

    @classmethod
    def _identity_token_sets_match(cls, actual: Any, expected: Any) -> bool:
        """Require two material identities to cover each other, without extras."""
        actual_tokens = cls._reagent_identity_tokens(actual)
        expected_tokens = cls._reagent_identity_tokens(expected)
        if not actual_tokens or not expected_tokens:
            return False
        return set(actual_tokens) == set(expected_tokens)

    @classmethod
    def _identity_tokens_are_authorized_subset(
        cls, actual: Any, frozen_research_identity: Any
    ) -> bool:
        """Allow an exact reagent subset from a combined Research macro step."""
        actual_tokens = cls._reagent_identity_tokens(actual)
        expected_tokens = cls._reagent_identity_tokens(frozen_research_identity)
        if not actual_tokens or not expected_tokens:
            return False
        return set(actual_tokens).issubset(set(expected_tokens))

    @staticmethod
    def _parse_research_source_path(value: Any) -> Optional[Tuple[int, str]]:
        match = re.fullmatch(
            r"macro_action_steps\[(\d+)\]\.([^\s.]+)", str(value or "").strip()
        )
        if not match:
            return None
        return int(match.group(1)), match.group(2)

    @staticmethod
    def _canonical_quantities_equal(
        left_value: float,
        left_dimension: str,
        right_value: float,
        right_dimension: str,
    ) -> bool:
        if left_dimension != right_dimension:
            return False
        return abs(left_value - right_value) <= max(
            1e-12, abs(left_value) * 1e-6, abs(right_value) * 1e-6
        )

    @staticmethod
    def _quantity_dimension_is_auditable(dimension: str) -> bool:
        return dimension in {"amount", "mass", "volume", "concentration"}

    @staticmethod
    def _quantity_dimension_is_inventory(dimension: str) -> bool:
        """Only extensive quantities can fund a batch or ledger consumer."""
        return dimension in {"amount", "mass", "volume"}

    @staticmethod
    def _theoretical_quantity_pattern() -> "re.Pattern[str]":
        return re.compile(
            r"theoretical[_\s-]*(?:quantity|yield|amount|mass|availability)|"
            r"nominal[_\s-]*(?:equivalent|yield|amount|mass|quantity)|"
            r"(?:理论|名义).{0,24}(?:收率|产量|质量|数量|当量)|"
            r"(?:收率|产量|质量|数量|当量).{0,24}(?:理论|名义)|"
            r"名义.{0,48}当量",
            re.I,
        )

    @classmethod
    def _theoretical_availability_fields(cls, entry: Dict[str, Any]) -> List[str]:
        """Find positive availability claims backed only by theory.

        A stoichiometric/nominal oxide-equivalent calculation is useful
        planning evidence, but it cannot fund a downstream draw until actual
        yield is measured.  Inspect raw payloads before unit parsing so dict
        and string forms behave identically.
        """
        # Use values rather than a fixed-key object: merely serializing the
        # key ``theoretical_quantity`` would otherwise make every ledger row
        # look theoretical, even when its value is absent.
        context_values = [
            entry.get("quantity_provenance"),
            entry.get("provenance"),
            entry.get("availability_basis"),
            entry.get("quantity_basis"),
            entry.get("source_kind"),
            entry.get("source_refs"),
            entry.get("calculation", entry.get("formula", "")),
            entry.get("material_id", entry.get("material", "")),
            entry.get("theoretical_quantity"),
        ]
        context = _json_text(
            [value for value in context_values if value not in (None, "", [], {})]
        )
        pattern = cls._theoretical_quantity_pattern()
        context_is_theoretical = bool(pattern.search(context))
        flagged: List[str] = []
        aliases = {
            "produced": ("produced", "produced_quantity"),
            "reserved": ("reserved", "reserved_quantity"),
            "balance": ("balance",),
        }
        for canonical, names in aliases.items():
            raw = next((entry.get(name) for name in names if name in entry), None)
            number = cls._quantity_value(raw)
            if number is None or number <= 0:
                continue
            field_context = _json_text(
                {
                    "value": raw,
                    "provenance": entry.get(f"{canonical}_provenance"),
                    "basis": entry.get(f"{canonical}_basis"),
                }
            )
            if context_is_theoretical or pattern.search(field_context):
                flagged.append(canonical)
        return flagged

    @classmethod
    def _normalize_theoretical_quantity_value(cls, value: Any) -> Dict[str, Any]:
        """Keep theoretical evidence with a real unit, never as inventory."""
        number = cls._quantity_value(value)
        unit = ""
        if isinstance(value, dict):
            unit = str(value.get("unit", value.get("单位", ""))).strip()
        elif isinstance(value, str):
            match = re.search(
                r"(?:[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*"
                r"(kg|mg|ug|µg|μg|g|mol|mmol|umol|µmol|μmol|nmol|"
                r"L|mL|uL|µL|μL|nL)\b",
                value,
                re.I,
            )
            if match:
                unit = match.group(1)
        base_match = re.match(
            r"\s*(kg|mg|ug|µg|μg|g|mol|mmol|umol|µmol|μmol|nmol|"
            r"L|mL|uL|µL|μL|nL)\b",
            unit,
            re.I,
        )
        base_unit = base_match.group(1) if base_match else unit
        return {
            "value": number,
            "unit": base_unit,
            "provenance": "theoretical_quantity",
            "original_unit": unit,
        }

    @classmethod
    def _batch_consumer_allocation_contract(
        cls,
        batch: Dict[str, Any],
    ) -> Tuple[Set[str], Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
        """Normalize only explicitly consumer-bound allocation shapes.

        Consumer order is not a quantity contract.  In particular, an
        unkeyed list must never be zipped to ``consumer_ids`` merely because
        the lengths happen to match.  The declared consumer set and the
        explicitly allocated consumer set are kept separate and must match
        exactly before cross-ledger audit.
        """
        batch_id = str(batch.get("batch_id") or "")
        declared_consumer_ids = {
            str(value).strip()
            for value in batch.get("consumer_ids", []) or []
            if str(value).strip()
        } if isinstance(batch.get("consumer_ids"), list) else set()
        allocated_consumer_ids: Set[str] = set()
        amounts: Dict[str, Dict[str, Any]] = {}
        problems: List[Dict[str, Any]] = []

        def add_amount(consumer_id: str, raw_quantity: Any) -> None:
            cid = str(consumer_id or "").strip()
            if not cid:
                problems.append(
                    {
                        "code": "missing_batch_allocation_consumer",
                        "scope": "device_local_quantity",
                        "message": f"batch {batch_id} 的 allocation 缺少 consumer_id。",
                    }
                )
                return
            allocated_consumer_ids.add(cid)
            value, dimension, unit = cls._canonical_quantity(raw_quantity)
            if value is None or not cls._quantity_dimension_is_auditable(dimension):
                problems.append(
                    {
                        "code": "batch_allocation_unit_missing_or_unknown",
                        "scope": "device_local_quantity",
                        "message": (
                            f"batch {batch_id} 对 consumer {cid} 的 allocation 缺少"
                            "可审计单位。"
                        ),
                    }
                )
                return
            if not cls._quantity_dimension_is_inventory(dimension):
                problems.append(
                    {
                        "code": "batch_allocation_requires_extensive_quantity",
                        "scope": "device_local_quantity",
                        "message": (
                            f"batch {batch_id} 对 consumer {cid} 的 allocation "
                            "必须是 amount/mass/volume；concentration 不能单独"
                            "作为可消费库存。"
                        ),
                    }
                )
                return
            if value <= 0:
                problems.append(
                    {
                        "code": "nonpositive_batch_consumer_allocation",
                        "scope": "device_local_quantity",
                        "message": (
                            f"batch {batch_id} 对 consumer {cid} 的 allocation "
                            "必须为有限正数；零或负数不能资助消费。"
                        ),
                    }
                )
                return
            existing = amounts.get(cid)
            if existing and existing["dimension"] != dimension:
                problems.append(
                    {
                        "code": "batch_allocation_unit_mismatch",
                        "scope": "device_local_quantity",
                        "message": (
                            f"batch {batch_id} 对 consumer {cid} 的 allocation "
                            "使用了不可比较单位。"
                        ),
                    }
                )
                return
            if existing:
                existing["value"] += value
            else:
                amounts[cid] = {
                    "value": value,
                    "dimension": dimension,
                    "unit": unit,
                }

        allocation = batch.get("allocation")
        quantity_keys = {"value", "unit", "amount", "quantity", "数值", "数量", "单位"}
        single_record_keys = quantity_keys | {"consumer_id", "allocation"}
        if isinstance(allocation, dict):
            allocation_keys = set(allocation)
            explicit_cid = str(allocation.get("consumer_id") or "").strip()
            if explicit_cid:
                unexpected_keys = allocation_keys - single_record_keys
                if unexpected_keys:
                    problems.append(
                        {
                            "code": "mixed_batch_allocation_schema",
                            "scope": "device_local_quantity",
                            "message": (
                                f"batch {batch_id} 的 allocation 同时混用 single-record "
                                f"与 consumer-keyed 字段：{sorted(unexpected_keys)}。"
                            ),
                        }
                    )
                else:
                    add_amount(
                        explicit_cid,
                        allocation.get(
                            "quantity", allocation.get("amount", allocation)
                        ),
                    )
            elif allocation_keys and allocation_keys.issubset(quantity_keys):
                problems.append(
                    {
                        "code": "ambiguous_batch_allocation",
                        "scope": "device_local_quantity",
                        "message": (
                            f"batch {batch_id} 的 allocation quantity 没有显式 "
                            "consumer_id；即使只声明一个 consumer 也不能"
                            "按位置或集合猜测映射。"
                        ),
                    }
                )
            elif allocation_keys & quantity_keys:
                problems.append(
                    {
                        "code": "mixed_batch_allocation_schema",
                        "scope": "device_local_quantity",
                        "message": (
                            f"batch {batch_id} 的 allocation 同时混用数量 record "
                            "字段和 consumer-keyed map；必须只选一种 schema。"
                        ),
                    }
                )
            else:
                for cid, quantity in allocation.items():
                    nested_cid = (
                        str(quantity.get("consumer_id") or "").strip()
                        if isinstance(quantity, dict)
                        else ""
                    )
                    if nested_cid and nested_cid != str(cid).strip():
                        problems.append(
                            {
                                "code": "batch_allocation_consumer_id_conflict",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"batch {batch_id} allocation key={cid} 与内嵌 "
                                    f"consumer_id={nested_cid} 冲突。"
                                ),
                            }
                        )
                        continue
                    add_amount(str(cid), quantity)
        elif isinstance(allocation, list):
            for index, record in enumerate(allocation):
                if isinstance(record, dict):
                    cid = str(record.get("consumer_id") or "").strip()
                    if cid:
                        add_amount(
                            cid,
                            record.get(
                                "quantity",
                                record.get("amount", record.get("allocation")),
                            ),
                        )
                        continue
                problems.append(
                    {
                        "code": "ambiguous_batch_allocation",
                        "scope": "device_local_quantity",
                        "message": (
                            f"batch {batch_id} 的 allocation[{index}] 没有显式 "
                            "consumer_id；禁止按 list 位置、consumer 声明顺序"
                            "或排序后顺序自动配对。"
                        ),
                    }
                )
        elif allocation not in (None, ""):
            problems.append(
                {
                    "code": "ambiguous_batch_allocation",
                    "scope": "device_local_quantity",
                    "message": (
                        f"batch {batch_id} 的 allocation 未显式绑定 consumer_id；"
                        "禁止根据 consumer 数量猜测。"
                    ),
                }
            )

        missing_allocations = declared_consumer_ids - allocated_consumer_ids
        if missing_allocations:
            problems.append(
                {
                    "code": "missing_batch_consumer_allocation",
                    "scope": "device_local_quantity",
                    "message": (
                        f"batch {batch_id} 声明的 consumers 缺少显式 allocation："
                        f"missing={sorted(missing_allocations)}。"
                    ),
                }
            )
        undeclared_allocations = allocated_consumer_ids - declared_consumer_ids
        if undeclared_allocations:
            problems.append(
                {
                    "code": "undeclared_batch_allocation_consumer",
                    "scope": "device_local_quantity",
                    "message": (
                        f"batch {batch_id} 的 allocation 引用了未声明 consumer："
                        f"extra={sorted(undeclared_allocations)}。"
                    ),
                }
            )
        return declared_consumer_ids, amounts, problems

    @staticmethod
    def _infer_adjustment_kind(adjustment: Dict[str, Any]) -> str:
        declared_kind = str(
            adjustment.get("kind")
            or adjustment.get("adjustment_type")
            or adjustment.get("type")
            or ""
        ).strip().lower()
        aliases = {
            "split": "split_transfer",
            "aliquot": "split_transfer",
            "batch_split": "split_batch",
            "replicate": "replicate_batch",
            "extra_batch": "replicate_batch",
            "scale": "amount_change",
            "concentration": "concentration_change",
            "ratio": "molar_ratio_change",
            "slot": "slot_reallocation",
            "container": "container_change",
            "unit": "unit_conversion",
        }
        kind = aliases.get(declared_kind, declared_kind)
        # A free-form/unknown label is not trusted.  Infer ownership from the
        # structured change itself so an LLM cannot hide a scale change behind
        # ``kind=device_operational`` (or an invented label).
        semantic_view = {
            key: value
            for key, value in adjustment.items()
            if key
            not in {
                "kind",
                "adjustment_type",
                "type",
                "preserved_invariants",
                "total_before",
                "total_after",
                "calculation",
                "formula",
                "source_refs",
            }
        }
        text = _json_text(semantic_view).lower()
        if re.search(r"浓度|concentration", text):
            return "concentration_change"
        if re.search(r"摩尔比|molar\s*ratio|stoichiometr", text):
            return "molar_ratio_change"
        before_value, before_dim, before_unit = SingleDeviceAgent._canonical_quantity(
            adjustment.get("before")
        )
        after_value, after_dim, after_unit = SingleDeviceAgent._canonical_quantity(
            adjustment.get("after")
        )
        if (
            before_value is not None
            and after_value is not None
            and before_dim == after_dim
            and before_dim not in {"unknown", "unitless"}
        ):
            if abs(before_value - after_value) > max(
                1e-12, abs(before_value) * 1e-9
            ):
                invariant_text = _json_text(
                    {
                        "preserved_invariants": adjustment.get(
                            "preserved_invariants", []
                        ),
                        "total_before": adjustment.get("total_before"),
                        "total_after": adjustment.get("total_after"),
                    }
                )
                if kind in _MECHANICAL_ADJUSTMENT_KINDS and re.search(
                    r"总量.{0,16}(?:不变|保持)|(?:total\s*amount).{0,16}"
                    r"(?:preserv|unchanged)",
                    invariant_text,
                    re.I,
                ):
                    return kind
                if before_dim == "concentration":
                    return "concentration_change"
                return "amount_change"
            if before_unit != after_unit:
                return "unit_conversion"
        if re.search(r"新增.{0,8}批|复制.{0,8}批|replicat|extra\s*batch", text):
            return "replicate_batch"
        if re.search(r"拆批|split.{0,8}batch|容量拆", text):
            return "split_batch"
        if re.search(r"分次|分装|aliquot|split.{0,8}transfer", text):
            return "split_transfer"
        if re.search(r"料位|slot", text):
            return "slot_reallocation"
        if re.search(r"容器|瓶", text):
            return "container_change"
        if re.search(r"(?:字段|field).{0,12}(?:单位|unit)|(?:单位|unit).{0,12}(?:换算|convert)", text):
            return "unit_conversion"
        if re.search(r"总量|单批|total[_\s-]*amount|amount|quantity|scale", text):
            return "amount_change"
        return kind if kind in _ALLOWED_ADJUSTMENT_KINDS else "device_operational"

    @classmethod
    def _adjustment_changes_scientific_quantity(
        cls,
        adjustment: Dict[str, Any],
        inferred_kind: str,
    ) -> bool:
        """Fail closed when before/after changes scientific quantities.

        Mechanical splitting stays review-free only when its sidecar explicitly
        proves that total amount, concentration and molar ratio are preserved.
        """
        semantic_view = {
            key: value
            for key, value in adjustment.items()
            if key
            not in {
                "kind",
                "adjustment_type",
                "type",
                "preserved_invariants",
                "total_before",
                "total_after",
                "calculation",
                "formula",
                "source_refs",
            }
        }
        text = _json_text(semantic_view)
        if re.search(
            r"(?:浓度|concentration|摩尔比|molar\s*ratio|stoichiometr).{0,20}"
            r"(?:改变|增加|减少|change|increase|decrease|before|after)|"
            r"(?:改变|增加|减少|change|increase|decrease).{0,20}"
            r"(?:浓度|concentration|摩尔比|molar\s*ratio|stoichiometr)|"
            r"新增.{0,8}(?:完整)?批|复制.{0,8}批|replicat|extra\s*batch|"
            r"(?:总量|单批|total[_\s-]*amount|single[_\s-]*batch).{0,20}"
            r"(?:改变|增加|减少|change|increase|decrease|before|after)",
            text,
            re.I,
        ):
            return True

        before = adjustment.get("before")
        after = adjustment.get("after")
        before_value, before_dim, _ = cls._canonical_quantity(before)
        after_value, after_dim, _ = cls._canonical_quantity(after)
        changed_quantity = bool(
            before_value is not None
            and after_value is not None
            and before_dim == after_dim
            and before_dim not in {"unknown", "unitless"}
            and abs(before_value - after_value)
            > max(1e-12, abs(before_value) * 1e-9)
        )
        if not changed_quantity:
            return False
        if inferred_kind not in _MECHANICAL_ADJUSTMENT_KINDS:
            return True

        invariant_text = _json_text(
            {
                "preserved_invariants": adjustment.get("preserved_invariants", []),
                "total_before": adjustment.get("total_before"),
                "total_after": adjustment.get("total_after"),
                "calculation": adjustment.get("calculation", adjustment.get("formula", "")),
            }
        )
        explicit_invariants = bool(
            re.search(r"总量.{0,12}(?:不变|保持|preserv|unchanged)", invariant_text, re.I)
            and re.search(r"浓度.{0,12}(?:不变|保持|preserv|unchanged)", invariant_text, re.I)
            and re.search(r"摩尔比.{0,12}(?:不变|保持|preserv|unchanged)", invariant_text, re.I)
        )
        total_before, total_before_dim, _ = cls._canonical_quantity(
            adjustment.get("total_before")
        )
        total_after, total_after_dim, _ = cls._canonical_quantity(
            adjustment.get("total_after")
        )
        explicit_equal_total = bool(
            total_before is not None
            and total_after is not None
            and total_before_dim == total_after_dim
            and abs(total_before - total_after)
            <= max(1e-12, abs(total_before) * 1e-9)
        )
        return not (explicit_invariants or explicit_equal_total)

    def _normalize_quantity_contract(
        self,
        plan_result: Dict[str, Any],
        *,
        research_handoff: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Normalize quantity provenance and run a conservative local audit.

        The audit never invents yield or pooling permission.  Arithmetically
        checkable shortages remain Device-local plan errors; unknown yield and
        ambiguous pooling become human review requests.
        """
        normalized, _ = self._strip_untrusted_approval_fields(plan_result)
        declared_quantity_audit = normalized.get("pending_quantity_human_review")
        if not isinstance(declared_quantity_audit, dict):
            declared_quantity_audit = normalized.get("quantity_audit")
        declared_quantity_audit = (
            copy.deepcopy(declared_quantity_audit)
            if isinstance(declared_quantity_audit, dict)
            else {}
        )
        had_adjustments = "quantity_adjustments" in normalized
        had_batch_plan = "batch_plan" in normalized
        had_material_ledger = "material_ledger" in normalized
        original_adjustments = normalized.get("quantity_adjustments")
        original_batches = normalized.get("batch_plan")
        original_ledger = normalized.get("material_ledger")
        sidecar_records_present = bool(
            (isinstance(original_adjustments, list) and original_adjustments)
            or (isinstance(original_batches, list) and original_batches)
            or (
                isinstance(original_ledger, dict)
                and isinstance(original_ledger.get("entries"), list)
                and original_ledger.get("entries")
            )
            or (had_adjustments and not isinstance(original_adjustments, list))
            or (had_batch_plan and not isinstance(original_batches, list))
            or (had_material_ledger and not isinstance(original_ledger, dict))
        )
        quantity_context = research_handoff or normalized
        quantity_text = _json_text(quantity_context)
        has_explicit_quantity = bool(
            re.search(
                r"\d+(?:\.\d+)?\s*(?:mol|mmol|umol|µmol|μmol|g|mg|µg|μg|"
                r"mL|ml|uL|µL|μL|L|M|mM|uM|µM|μM|mol/L|mmol/L)\b",
                quantity_text,
                re.I,
            )
        )
        has_multiple_consumers = bool(
            re.search(
                r"多个.{0,12}(?:用途|下游|consumer)|分别.{0,24}(?:用于|取样|表征)|"
                r"重复消费|多路消费|consumer_ids|XPS.{0,24}(?:XRD|电化学)|"
                r"(?:XRD|电化学).{0,24}XPS",
                quantity_text,
                re.I,
            )
        )
        quantity_contract_required = bool(
            normalized.get("quantity_contract_required")
            or has_multiple_consumers
            or has_explicit_quantity
            or sidecar_records_present
        )
        normalized["quantity_contract_required"] = quantity_contract_required
        raw_adjustments = normalized.get("quantity_adjustments", [])
        if not isinstance(raw_adjustments, list):
            raw_adjustments = []
        adjustments: List[Dict[str, Any]] = []
        issues: List[Dict[str, Any]] = []
        if quantity_contract_required and not had_adjustments:
            issues.append(
                {
                    "code": "missing_quantity_adjustments_contract",
                    "scope": "device_local_quantity",
                    "message": (
                        "该计划含多步骤显式数量/多下游消费，但缺少 "
                        "quantity_adjustments；无调整时也必须显式输出空数组。"
                    ),
                }
            )

        for index, raw in enumerate(raw_adjustments, start=1):
            if not isinstance(raw, dict):
                issues.append(
                    {
                        "code": "invalid_quantity_adjustment",
                        "scope": "device_local_quantity",
                        "message": f"quantity_adjustments[{index}] 必须是 object。",
                    }
                )
                continue
            item = copy.deepcopy(raw)
            declared_kind = str(
                item.get("kind")
                or item.get("adjustment_type")
                or item.get("type")
                or ""
            ).strip().lower()
            kind = self._infer_adjustment_kind(item)
            item["adjustment_id"] = str(
                item.get("adjustment_id") or f"qa_{index:03d}"
            )
            item["kind"] = kind
            for quantity_field in ("before", "after"):
                raw_adjustment_quantity = item.get(quantity_field)
                if raw_adjustment_quantity in (None, ""):
                    continue
                adjustment_value, _, _ = self._canonical_quantity(
                    raw_adjustment_quantity
                )
                if adjustment_value is None:
                    issues.append(
                        {
                            "code": "invalid_quantity_adjustment_value",
                            "scope": "device_local_quantity",
                            "message": (
                                f"{item['adjustment_id']}.{quantity_field} 必须是"
                                "有限且带可识别单位的数量。"
                            ),
                        }
                    )
                elif adjustment_value < 0:
                    issues.append(
                        {
                            "code": "negative_quantity_adjustment",
                            "scope": "device_local_quantity",
                            "message": (
                                f"{item['adjustment_id']}.{quantity_field} 不得为负；"
                                "scientific review 也不能授权负库存。"
                            ),
                        }
                    )
            if declared_kind:
                aliases = {
                    "split": "split_transfer",
                    "aliquot": "split_transfer",
                    "batch_split": "split_batch",
                    "replicate": "replicate_batch",
                    "extra_batch": "replicate_batch",
                    "scale": "amount_change",
                    "concentration": "concentration_change",
                    "ratio": "molar_ratio_change",
                    "slot": "slot_reallocation",
                    "container": "container_change",
                    "unit": "unit_conversion",
                }
                if aliases.get(declared_kind, declared_kind) not in _ALLOWED_ADJUSTMENT_KINDS:
                    issues.append(
                        {
                            "code": "invalid_quantity_adjustment_kind",
                            "scope": "device_local_quantity",
                            "message": (
                                f"{item['adjustment_id']} 使用未知 kind={declared_kind}；"
                                f"已按 before/after 语义重新分类为 {kind}。"
                            ),
                        }
                    )
            raw_adjustment_source_kind = str(
                item.get("source_kind")
                or (item.get("provenance") or {}).get("source_kind")
                or ""
            ).strip()
            item["source_kind"] = raw_adjustment_source_kind or "device_operational"
            if raw_adjustment_source_kind not in {
                "research",
                "derived",
                "device_operational",
            }:
                issues.append(
                    {
                        "code": "invalid_quantity_source",
                        "scope": "device_local_quantity",
                        "message": (
                            f"{item['adjustment_id']} 缺少合法 source_kind；必须为 "
                            "research/derived/device_operational。"
                        ),
                    }
                )
            raw_adjustment_source_refs = item.get("source_refs")
            raw_adjustment_calculation = str(
                item.get("calculation", item.get("formula", "")) or ""
            ).strip()
            item.setdefault("source_refs", [])
            item.setdefault("calculation", item.get("formula", ""))
            item.setdefault("preserved_invariants", [])
            if item.get("route_changed") not in (None, False):
                issues.append(
                    {
                        "code": "quantity_adjustment_changes_frozen_route",
                        "scope": "device_local_quantity",
                        "message": (
                            f"{item['adjustment_id']} 声明 route_changed=true；"
                            "Device quantity adjustment 不得改变冻结路线。"
                        ),
                    }
                )
            item["route_changed"] = False
            scientific = bool(
                kind in _SCIENTIFIC_REVIEW_ADJUSTMENT_KINDS
                or self._adjustment_changes_scientific_quantity(item, kind)
            )
            item["requires_scientific_review"] = scientific
            if not isinstance(raw_adjustment_source_refs, list) or not raw_adjustment_source_refs:
                issues.append(
                    {
                        "code": "missing_quantity_adjustment_source_refs",
                        "scope": "device_local_quantity",
                        "message": f"{item['adjustment_id']} 缺少 source_refs。",
                    }
                )
            if not raw_adjustment_calculation:
                issues.append(
                    {
                        "code": "missing_quantity_calculation",
                        "scope": "device_local_quantity",
                        "message": (
                            f"{item['adjustment_id']} 缺少 calculation/derivation。"
                        ),
                    }
                )
            adjustments.append(item)

        # If a plan-level change ledger declares a scientific parameter change,
        # require it to appear in the auditable quantity sidecar.
        changes = normalized.get("plan_changes", [])
        if isinstance(changes, list):
            existing_plan_change_digests = {
                str(item.get("source_plan_change_digest"))
                for item in adjustments
                if isinstance(item, dict) and item.get("source_plan_change_digest")
            }
            for change in changes:
                if not isinstance(change, dict):
                    continue
                inferred = self._infer_adjustment_kind(change)
                if inferred not in _SCIENTIFIC_REVIEW_ADJUSTMENT_KINDS:
                    continue
                change_digest = self._stable_digest(
                    change, prefix="plan_change"
                )
                if change_digest in existing_plan_change_digests:
                    continue
                item = copy.deepcopy(change)
                item.update(
                    {
                        "adjustment_id": f"plan_change_{len(adjustments) + 1:03d}",
                        "kind": inferred,
                        "source_kind": "device_operational",
                        "source_refs": item.get("source_refs", []),
                        "calculation": item.get("calculation", item.get("formula", "")),
                        "preserved_invariants": item.get("preserved_invariants", []),
                        "route_changed": False,
                        "requires_scientific_review": True,
                        "source_plan_change_digest": change_digest,
                    }
                )
                adjustments.append(item)
                existing_plan_change_digests.add(change_digest)
                if not isinstance(item.get("source_refs"), list) or not item.get(
                    "source_refs"
                ):
                    issues.append(
                        {
                            "code": "missing_quantity_adjustment_source_refs",
                            "scope": "device_local_quantity",
                            "message": f"{item['adjustment_id']} 缺少 source_refs。",
                        }
                    )
                if not str(item.get("calculation", "")).strip():
                    issues.append(
                        {
                            "code": "missing_quantity_calculation",
                            "scope": "device_local_quantity",
                            "message": (
                                f"{item['adjustment_id']} 缺少 calculation/derivation。"
                            ),
                        }
                    )

        batch_plan = normalized.get("batch_plan", [])
        if not isinstance(batch_plan, list):
            batch_plan = []
            issues.append(
                {
                    "code": "invalid_batch_plan",
                    "scope": "device_local_quantity",
                    "message": "batch_plan 必须是 array。",
                }
            )
        normalized_batches: List[Dict[str, Any]] = []
        seen_batches: Set[str] = set()
        whole_batch_batch_ids: Set[str] = set()
        device_plan_steps = [
            step
            for step in normalized.get("device_plan", []) or []
            if isinstance(step, dict)
        ]
        plan_step_by_id = {
            str(step.get("plan_step", "")).strip(): step
            for step in device_plan_steps
            if str(step.get("plan_step", "")).strip()
        }
        plan_step_ids = set(plan_step_by_id)
        research_steps = [
            step
            for step in (research_handoff or {}).get("macro_action_steps", []) or []
            if isinstance(step, dict)
        ]
        adaptable_whole_batch_dispositions: Set[Tuple[str, int]] = set()
        for disposition in normalized.get("quantity_requirement_dispositions", []) or []:
            if not isinstance(disposition, dict):
                continue
            if str(disposition.get("decision") or "").strip() != "replace_with_whole_batch":
                continue
            source_macro = str(
                disposition.get("source_macro_step") or ""
            ).strip()
            requirement_index = disposition.get("requirement_index")
            if (
                source_macro
                and isinstance(requirement_index, int)
                and not isinstance(requirement_index, bool)
            ):
                adaptable_whole_batch_dispositions.add(
                    (source_macro, requirement_index)
                )
        research_macro_by_id = {
            str(step.get("步骤序号", step.get("step", index))).strip(): step
            for index, step in enumerate(research_steps, start=1)
        }
        research_macro_ids = set(research_macro_by_id)
        nonroot_batch_requirements: Dict[str, Dict[str, Any]] = {}
        research_root_source_usage: Dict[Tuple[Any, ...], Set[str]] = {}
        allowed_transition_kinds = set(_ALLOWED_MATERIAL_EVENT_KINDS)

        def source_ref_mentions_macro(source_ref: Any, macro_id: str) -> bool:
            if isinstance(source_ref, dict):
                return str(source_ref.get("source_macro_step") or "").strip() == macro_id
            text = str(source_ref or "").strip()
            if text == f"macro_step:{macro_id}":
                return True
            parsed = self._parse_research_source_path(text)
            if parsed is None or parsed[0] >= len(research_steps):
                return False
            indexed_step = research_steps[parsed[0]]
            indexed_id = str(
                indexed_step.get("步骤序号", indexed_step.get("step", parsed[0] + 1))
            ).strip()
            return indexed_id == macro_id

        def quantity_change_is_authorized(
            source_value: float,
            source_dimension: str,
            target_value: float,
            target_dimension: str,
            macro_id: str,
        ) -> bool:
            for adjustment in adjustments:
                if not isinstance(adjustment, dict):
                    continue
                before_value, before_dimension, _ = self._canonical_quantity(
                    adjustment.get("before")
                )
                after_value, after_dimension, _ = self._canonical_quantity(
                    adjustment.get("after")
                )
                if before_value is None or after_value is None:
                    continue
                if not self._canonical_quantities_equal(
                    before_value,
                    before_dimension,
                    source_value,
                    source_dimension,
                ) or not self._canonical_quantities_equal(
                    after_value,
                    after_dimension,
                    target_value,
                    target_dimension,
                ):
                    continue
                if adjustment.get("requires_scientific_review") is not True:
                    continue
                refs = adjustment.get("source_refs")
                if not isinstance(refs, list) or not any(
                    source_ref_mentions_macro(ref, macro_id) for ref in refs
                ):
                    continue
                return True
            return False
        for index, raw in enumerate(batch_plan, start=1):
            if not isinstance(raw, dict):
                issues.append(
                    {
                        "code": "invalid_batch_record",
                        "scope": "device_local_quantity",
                        "message": f"batch_plan[{index}] 必须是 object。",
                    }
                )
                continue
            item = copy.deepcopy(raw)
            batch_id = str(
                item.get("batch_id")
                or item.get("child_batch_id")
                or f"device_batch_{index:03d}"
            )
            item["batch_id"] = batch_id
            quantity_mode = str(
                item.get("quantity_mode") or "numeric_inventory"
            ).strip()
            if quantity_mode not in {"numeric_inventory", "whole_batch"}:
                issues.append(
                    {
                        "code": "invalid_batch_quantity_mode",
                        "scope": "device_local_quantity",
                        "message": (
                            f"batch {batch_id} 的 quantity_mode 必须是 "
                            "numeric_inventory 或 whole_batch。"
                        ),
                    }
                )
                quantity_mode = "numeric_inventory"
            item["quantity_mode"] = quantity_mode
            is_whole_batch = quantity_mode == "whole_batch"
            if is_whole_batch:
                whole_batch_batch_ids.add(batch_id)
            raw_source_kind = str(item.get("source_kind") or "").strip()
            raw_source_refs = item.get("source_refs")
            raw_calculation = str(
                item.get("calculation", item.get("formula", "")) or ""
            ).strip()
            item.setdefault("source_kind", "device_operational")
            item.setdefault("source_refs", [])
            item.setdefault("calculation", item.get("formula", ""))
            if batch_id in seen_batches:
                issues.append(
                    {
                        "code": "duplicate_batch_id",
                        "scope": "device_local_quantity",
                        "message": f"batch_id {batch_id} 重复，谱系无法审计。",
                    }
                )
            seen_batches.add(batch_id)
            sample_id = str(item.get("sample_id") or "").strip()
            if not sample_id:
                issues.append(
                    {
                        "code": "missing_batch_sample_lineage",
                        "scope": "device_local_quantity",
                        "message": f"batch {batch_id} 缺少 sample_id。",
                    }
                )
            raw_parent_ids = item.get("parent_batch_ids")
            parent_ids = {
                str(value).strip()
                for value in raw_parent_ids or []
                if str(value).strip()
            } if isinstance(raw_parent_ids, list) else set()
            scalar_parent_id = str(item.get("parent_batch_id") or "").strip()
            if scalar_parent_id:
                parent_ids.add(scalar_parent_id)
            if not parent_ids and item.get("is_root_batch") is not True:
                issues.append(
                    {
                        "code": "missing_parent_batch_lineage",
                        "scope": "device_local_quantity",
                        "message": (
                            f"batch {batch_id} 缺少 parent_batch_id(s)；根批次必须显式 "
                            "is_root_batch=true。"
                        ),
                    }
                )
            is_root_batch = item.get("is_root_batch") is True
            root_research_quantities: List[Tuple[float, str, str]] = []
            whole_batch_authorized_by_research = False
            if is_root_batch:
                if parent_ids:
                    issues.append(
                        {
                            "code": "root_batch_has_parent_lineage",
                            "scope": "device_local_quantity",
                            "message": (
                                f"root batch {batch_id} 不得同时声明 parent_batch_id(s)="
                                f"{sorted(parent_ids)}。"
                            ),
                        }
                    )
                if raw_source_kind != "research":
                    issues.append(
                        {
                            "code": (
                                "derived_batch_cannot_be_root"
                                if raw_source_kind in {"derived", "device_operational"}
                                else "research_root_requires_research_source"
                            ),
                            "scope": "device_local_quantity",
                            "message": (
                                f"batch {batch_id} 声明 is_root_batch=true，但 "
                                f"source_kind={raw_source_kind or '<missing>'}；只有绑定"
                                " Research 真源的物料才能作为 root。"
                            ),
                        }
                    )
                research_source_refs = item.get("research_source_refs")
                if not isinstance(research_source_refs, list) or not research_source_refs:
                    issues.append(
                        {
                            "code": "missing_research_root_provenance",
                            "scope": "device_local_quantity",
                            "message": (
                                f"research root batch {batch_id} 缺少结构化 "
                                "research_source_refs。"
                            ),
                        }
                    )
                    research_source_refs = []
                declared_source_paths = {
                    str(value).strip()
                    for value in raw_source_refs or []
                    if isinstance(value, str) and str(value).strip()
                } if isinstance(raw_source_refs, list) else set()
                referenced_identities: List[Any] = []
                referenced_identity_ids: List[str] = []
                for ref_index, source_ref in enumerate(research_source_refs, start=1):
                    if not isinstance(source_ref, dict):
                        issues.append(
                            {
                                "code": "invalid_research_root_source_ref",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"batch {batch_id} research_source_refs[{ref_index}] "
                                    "必须是 object。"
                                ),
                            }
                        )
                        continue
                    source_path = str(source_ref.get("source_path") or "").strip()
                    parsed_path = self._parse_research_source_path(source_path)
                    declared_macro_id = str(
                        source_ref.get("source_macro_step") or ""
                    ).strip()
                    declared_field = str(source_ref.get("source_field") or "").strip()
                    if (
                        parsed_path is None
                        or parsed_path[0] >= len(research_steps)
                        or not declared_macro_id
                        or not declared_field
                    ):
                        issues.append(
                            {
                                "code": "invalid_research_root_source_ref",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"batch {batch_id} research source ref 必须显式绑定"
                                    " source_path/source_macro_step/source_field。"
                                ),
                            }
                        )
                        continue
                    source_index, path_field = parsed_path
                    source_step = research_steps[source_index]
                    actual_macro_id = str(
                        source_step.get(
                            "步骤序号", source_step.get("step", source_index + 1)
                        )
                    ).strip()
                    if (
                        declared_macro_id != actual_macro_id
                        or declared_field != path_field
                        or path_field not in source_step
                        or source_path not in declared_source_paths
                    ):
                        issues.append(
                            {
                                "code": "invalid_research_root_source_binding",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"batch {batch_id} 的 {source_path or '<missing path>'} "
                                    "未与真实 Research macro step/字段及 source_refs "
                                    "交叉一致。"
                                ),
                            }
                        )
                        continue
                    expected_identity = source_step.get(
                        "试剂/对象", source_step.get("reagent_or_object", "")
                    )
                    referenced_identity = source_ref.get("material_identity")
                    referenced_identities.append(referenced_identity)
                    referenced_identity_id = str(
                        source_ref.get("material_identity_id") or ""
                    ).strip()
                    referenced_identity_ids.append(referenced_identity_id)
                    if self._active_semantic_analysis:
                        authorized_identity_ids = self._semantic_material_ids(
                            actual_macro_id
                        )
                        invalid_identity = (
                            not referenced_identity_id
                            or referenced_identity_id not in authorized_identity_ids
                        )
                    else:
                        invalid_identity = not self._identity_tokens_are_authorized_subset(
                            referenced_identity, expected_identity
                        )
                    if invalid_identity:
                        issues.append(
                            {
                                "code": "invalid_research_root_material_identity",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"batch {batch_id} 的 research source ref 物料身份"
                                    f"与 macro step {actual_macro_id} 的冻结 LLM identity_id 不一致。"
                                ),
                            }
                        )
                    if is_whole_batch:
                        quantity_requirements = source_step.get(
                            "quantity_requirements"
                        )
                        if isinstance(quantity_requirements, list):
                            whole_batch_authorized_by_research = (
                                whole_batch_authorized_by_research
                                or any(
                                    isinstance(requirement, dict)
                                    and str(requirement.get("kind") or "").strip()
                                    == "whole_batch"
                                    and (
                                        str(requirement.get("material_identity_id") or "").strip()
                                        == referenced_identity_id
                                        if self._active_semantic_analysis
                                        else self._identity_token_sets_match(
                                            requirement.get("material"),
                                            referenced_identity,
                                        )
                                    )
                                    for requirement in quantity_requirements
                                )
                            )
                            whole_batch_authorized_by_research = (
                                whole_batch_authorized_by_research
                                or any(
                                    (str(actual_macro_id), requirement_index)
                                    in adaptable_whole_batch_dispositions
                                    and isinstance(requirement, dict)
                                    and self._device_adaptable_target(requirement)
                                    and (
                                        str(requirement.get("material_identity_id") or "").strip()
                                        == referenced_identity_id
                                        if self._active_semantic_analysis
                                        else self._identity_token_sets_match(
                                            requirement.get("material"),
                                            referenced_identity,
                                        )
                                    )
                                    for requirement_index, requirement in enumerate(
                                        quantity_requirements
                                    )
                                )
                            )
                        # Identity and frozen macro binding are still checked,
                        # but a whole-batch handoff deliberately has no numeric
                        # inventory quantity to extract from Research prose.
                        continue
                    frozen_source_text = str(source_step.get(path_field) or "")
                    source_context = str(
                        source_ref.get("source_context") or ""
                    ).strip()
                    quantity_source: Any = frozen_source_text
                    if not source_context:
                        issues.append(
                            {
                                "code": "missing_research_root_source_context",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"batch {batch_id} 的 research source ref 必须给出"
                                    "非空、逐字且唯一的 source_context。"
                                ),
                            }
                        )
                        quantity_source = ""
                    else:
                        if frozen_source_text.count(source_context) != 1:
                            issues.append(
                                {
                                    "code": "invalid_research_root_source_context",
                                    "scope": "device_local_quantity",
                                    "message": (
                                        f"batch {batch_id} 的 source_context 必须是 "
                                        f"{source_path} 中唯一出现的逐字片段。"
                                    ),
                                }
                            )
                            quantity_source = ""
                        else:
                            quantity_source = source_context
                    extracted_quantities = self._extract_explicit_quantities(
                        quantity_source
                    )
                    ref_value, ref_dimension, _ = self._canonical_quantity(
                        source_ref.get("quantity")
                    )
                    compatible_quantities = [
                        quantity
                        for quantity in extracted_quantities
                        if ref_value is not None
                        and quantity["dimension"] == ref_dimension
                    ]
                    if (
                        ref_value is None
                        or not self._quantity_dimension_is_auditable(ref_dimension)
                    ):
                        issues.append(
                            {
                                "code": "missing_research_root_quantity_ref",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"batch {batch_id} 的 research source ref 缺少可审计 quantity。"
                                ),
                            }
                        )
                    elif not self._quantity_dimension_is_inventory(ref_dimension):
                        issues.append(
                            {
                                "code": "research_root_quantity_requires_extensive_dimension",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"batch {batch_id} research source ref.quantity "
                                    "必须是 amount/mass/volume；浓度不能单独"
                                    "证明可消费库存。"
                                ),
                            }
                        )
                    elif ref_value <= 0:
                        issues.append(
                            {
                                "code": "nonpositive_research_root_quantity_ref",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"batch {batch_id} research source ref.quantity "
                                    "必须为有限正数。"
                                ),
                            }
                        )
                    elif len(compatible_quantities) != 1:
                        issues.append(
                            {
                                "code": "ambiguous_research_root_quantity_provenance",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"batch {batch_id} 在 {source_path} 中无法唯一解析"
                                    f" {ref_dimension} 数量；candidates="
                                    f"{[value['raw'] for value in compatible_quantities]}。"
                                ),
                            }
                        )
                    elif not self._canonical_quantities_equal(
                        float(compatible_quantities[0]["value"]),
                        str(compatible_quantities[0]["dimension"]),
                        ref_value,
                        ref_dimension,
                    ):
                        issues.append(
                            {
                                "code": "research_root_quantity_ref_mismatch",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"batch {batch_id} research source ref.quantity "
                                    f"与 {source_path} 中的真实数量不一致。"
                                ),
                            }
                        )
                    elif (
                        not self._active_semantic_analysis
                        and
                        len(self._reagent_identity_tokens(expected_identity)) > 1
                        and not self._source_context_binds_material_identity(
                            quantity_source,
                            referenced_identity,
                            compatible_quantities[0],
                            expected_identity,
                            frozen_source_text,
                        )
                    ):
                        issues.append(
                            {
                                "code": "research_root_identity_quantity_context_mismatch",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"batch {batch_id} 的 material_identity 未出现在"
                                    "所引用 quantity 的局部逐字上下文，不得"
                                    "从同一 macro 中其他试剂借用数量。"
                                ),
                            }
                        )
                    else:
                        funded_quantity_value = float(
                            compatible_quantities[0]["value"]
                        )
                        funded_quantity_dimension = str(
                            compatible_quantities[0]["dimension"]
                        )
                        quantity_scope = str(
                            source_ref.get("quantity_scope") or "total"
                        ).strip()
                        if quantity_scope not in {
                            "total",
                            "per_batch",
                            "per_operation",
                        }:
                            issues.append(
                                {
                                    "code": "invalid_research_root_quantity_scope",
                                    "scope": "device_local_quantity",
                                    "message": (
                                        f"batch {batch_id} research source ref.quantity_scope "
                                        "必须是 total/per_batch/per_operation。"
                                    ),
                                }
                            )
                        elif quantity_scope == "per_batch":
                            multiplicity_ref = source_ref.get("multiplicity_ref")
                            valid_multiplicity = False
                            declared_multiplicity = self._quantity_value(
                                item.get("multiplicity")
                            )
                            if isinstance(multiplicity_ref, dict):
                                multiplicity_path = str(
                                    multiplicity_ref.get("source_path") or ""
                                ).strip()
                                parsed_multiplicity_path = (
                                    self._parse_research_source_path(
                                        multiplicity_path
                                    )
                                )
                                multiplicity_context = str(
                                    multiplicity_ref.get("source_context") or ""
                                ).strip()
                                declared_count = self._quantity_value(
                                    multiplicity_ref.get("count")
                                )
                                if (
                                    parsed_multiplicity_path is not None
                                    and parsed_multiplicity_path[0]
                                    < len(research_steps)
                                ):
                                    multiplicity_step = research_steps[
                                        parsed_multiplicity_path[0]
                                    ]
                                    multiplicity_field = parsed_multiplicity_path[1]
                                    multiplicity_field_text = str(
                                        multiplicity_step.get(
                                            multiplicity_field, ""
                                        )
                                    )
                                    multiplicity_count_matches = list(
                                        re.finditer(
                                            r"(?<!\d)(\d+)\s*(?:批|个独立批次|"
                                            r"independent\s+replicates?|batches?)|"
                                            r"独立重复\s*(\d+)\s*次",
                                            multiplicity_context,
                                            re.I,
                                        )
                                    )
                                    frozen_counts = [
                                        int(match.group(1) or match.group(2))
                                        for match in multiplicity_count_matches
                                    ]
                                    if self._active_semantic_analysis:
                                        # Whether a count applies to every
                                        # sample or only one material is a
                                        # semantic judgement made by Device's
                                        # planning LLM with full context.  The
                                        # deterministic layer validates the
                                        # declaration and frozen identity ID;
                                        # it does not infer scope from words
                                        # such as “每种样品”.
                                        multiplicity_scope = str(
                                            multiplicity_ref.get(
                                                "semantic_scope"
                                            )
                                            or ""
                                        ).strip()
                                        multiplicity_identity_id = str(
                                            multiplicity_ref.get(
                                                "material_identity_id"
                                            )
                                            or ""
                                        ).strip()
                                        semantic_scope_valid = bool(
                                            multiplicity_scope == "all_samples"
                                            or (
                                                multiplicity_scope
                                                == "material_specific"
                                                and multiplicity_identity_id
                                                == referenced_identity_id
                                            )
                                        )
                                        semantic_scope_valid = bool(
                                            semantic_scope_valid
                                            and str(
                                                multiplicity_ref.get(
                                                    "semantic_reason"
                                                )
                                                or ""
                                            ).strip()
                                            and isinstance(
                                                multiplicity_ref.get(
                                                    "evidence_refs"
                                                ),
                                                list,
                                            )
                                            and multiplicity_ref.get(
                                                "evidence_refs"
                                            )
                                        )
                                        scope_binding_valid = (
                                            semantic_scope_valid
                                        )
                                    else:
                                        universal_sample_scope = bool(
                                            re.search(
                                                r"每种样品.{0,16}独立.{0,12}批|"
                                                r"each\s+sample.{0,24}independent.{0,16}(?:batches|replicates)",
                                                multiplicity_context,
                                                re.I,
                                            )
                                        )
                                        multiplicity_identity = multiplicity_step.get(
                                            "试剂/对象",
                                            multiplicity_step.get(
                                                "reagent_or_object", ""
                                            ),
                                        )
                                        local_material_scope = False
                                        if len(multiplicity_count_matches) == 1:
                                            count_match = multiplicity_count_matches[0]
                                            count_group = (
                                                1 if count_match.group(1) else 2
                                            )
                                            local_material_scope = bool(
                                                len(
                                                    self._reagent_identity_tokens(
                                                        multiplicity_identity
                                                    )
                                                )
                                                <= 1
                                                or self._source_context_binds_material_identity(
                                                    multiplicity_context,
                                                    referenced_identity,
                                                    {
                                                        "start": count_match.start(
                                                            count_group
                                                        ),
                                                        "end": count_match.end(
                                                            count_group
                                                        ),
                                                    },
                                                    multiplicity_identity,
                                                    multiplicity_field_text,
                                                )
                                            )
                                        scope_binding_valid = bool(
                                            (
                                                parsed_multiplicity_path
                                                == parsed_path
                                                and local_material_scope
                                            )
                                            or universal_sample_scope
                                        )
                                    valid_multiplicity = bool(
                                        scope_binding_valid
                                        and
                                        multiplicity_context
                                        and multiplicity_field_text.count(
                                            multiplicity_context
                                        )
                                        == 1
                                        and len(frozen_counts) == 1
                                        and declared_count is not None
                                        and declared_multiplicity is not None
                                        and float(frozen_counts[0])
                                        == declared_count
                                        == declared_multiplicity
                                        and declared_count >= 1
                                        and float(declared_count).is_integer()
                                    )
                            per_batch_value, per_batch_dimension, _ = (
                                self._canonical_quantity(
                                    item.get("per_batch_quantity")
                                )
                            )
                            if (
                                not valid_multiplicity
                                or per_batch_value is None
                                or not self._canonical_quantities_equal(
                                    funded_quantity_value,
                                    funded_quantity_dimension,
                                    per_batch_value,
                                    per_batch_dimension,
                                )
                            ):
                                issues.append(
                                    {
                                        "code": "invalid_research_root_multiplicity_provenance",
                                        "scope": "device_local_quantity",
                                        "message": (
                                            f"batch {batch_id} 声明 per_batch Research "
                                            "quantity，但 multiplicity/count/context 或 "
                                            "per_batch_quantity 未与冻结 Research 逐项一致。"
                                        ),
                                    }
                                )
                            else:
                                funded_quantity_value *= float(
                                    declared_multiplicity
                                )
                        elif quantity_scope == "per_operation":
                            repeat_ref = source_ref.get("operation_repeat_ref")
                            valid_repeat = False
                            declared_repeat_count = self._quantity_value(
                                item.get("operation_repeat_count")
                            )
                            if isinstance(repeat_ref, dict):
                                repeat_path = str(
                                    repeat_ref.get("source_path") or ""
                                ).strip()
                                parsed_repeat_path = self._parse_research_source_path(
                                    repeat_path
                                )
                                repeat_context = str(
                                    repeat_ref.get("source_context") or ""
                                ).strip()
                                declared_repeat_ref_count = self._quantity_value(
                                    repeat_ref.get("count")
                                )
                                if (
                                    parsed_repeat_path == parsed_path
                                    and parsed_repeat_path is not None
                                ):
                                    repeat_step = research_steps[
                                        parsed_repeat_path[0]
                                    ]
                                    repeat_field_text = str(
                                        repeat_step.get(parsed_repeat_path[1], "")
                                    )
                                    repeat_counts = [
                                        int(value)
                                        for match in re.findall(
                                            r"(?:重复|连续).{0,16}?(\d+)\s*次|"
                                            r"(\d+)\s*次(?:洗涤|加液|重分散|离心|取样)",
                                            repeat_context,
                                            re.I,
                                        )
                                        for value in match
                                        if value
                                    ]
                                    valid_repeat = bool(
                                        repeat_context
                                        and repeat_field_text.count(repeat_context)
                                        == 1
                                        and source_context in repeat_context
                                        and len(repeat_context)
                                        <= len(source_context) + 48
                                        and len(repeat_counts) == 1
                                        and declared_repeat_count is not None
                                        and declared_repeat_ref_count is not None
                                        and float(repeat_counts[0])
                                        == declared_repeat_count
                                        == declared_repeat_ref_count
                                        and declared_repeat_count >= 1
                                        and float(declared_repeat_count).is_integer()
                                        and self._quantity_value(
                                            item.get("multiplicity", 1)
                                        )
                                        == 1
                                    )
                            if not valid_repeat:
                                issues.append(
                                    {
                                        "code": "invalid_research_operation_repeat_provenance",
                                        "scope": "device_local_quantity",
                                        "message": (
                                            f"batch {batch_id} 的 per_operation quantity "
                                            "未与同一冻结字段中的局部重复操作"
                                            "和 operation_repeat_count 一致。"
                                        ),
                                    }
                                )
                            else:
                                funded_quantity_value *= float(
                                    declared_repeat_count
                                )
                        absolute_quantity_start = frozen_source_text.find(
                            source_context
                        ) + int(compatible_quantities[0].get("start", 0))
                        root_source_key = (
                            source_path,
                            absolute_quantity_start,
                            int(compatible_quantities[0].get("end", 0))
                            - int(compatible_quantities[0].get("start", 0)),
                            round(float(compatible_quantities[0]["value"]), 15),
                            str(compatible_quantities[0]["dimension"]),
                        )
                        research_root_source_usage.setdefault(
                            root_source_key, set()
                        ).add(batch_id)
                        root_research_quantities.append(
                            (
                                funded_quantity_value,
                                funded_quantity_dimension,
                                actual_macro_id,
                            )
                        )
                research_material_identity = item.get("research_material_identity")
                material_id = item.get("material_id")
                if self._active_semantic_analysis:
                    batch_identity_id = str(
                        item.get("research_material_identity_id") or ""
                    ).strip()
                    if (
                        not referenced_identity_ids
                        or not batch_identity_id
                        or any(
                            identity_id != batch_identity_id
                            for identity_id in referenced_identity_ids
                        )
                    ):
                        issues.append(
                            {
                                "code": "research_root_material_identity_mismatch",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"root batch {batch_id} 的 research_material_identity_id "
                                    "未与所有 Research source ref 的冻结 LLM identity_id 精确一致。"
                                ),
                            }
                        )
                elif not referenced_identities:
                    pass
                elif not all(
                    self._identity_token_sets_match(
                        research_material_identity, referenced_identity
                    )
                    and self._identity_token_sets_match(
                        material_id, referenced_identity
                    )
                    for referenced_identity in referenced_identities
                ):
                    issues.append(
                        {
                            "code": "research_root_material_identity_mismatch",
                            "scope": "device_local_quantity",
                            "message": (
                                f"root batch {batch_id} 的 material_id/"
                                "research_material_identity 未与 Research 物料身份双向匹配。"
                            ),
                        }
                    )
                if is_whole_batch and not whole_batch_authorized_by_research:
                    issues.append(
                        {
                            "code": "whole_batch_not_authorized_by_research",
                            "scope": "device_local_quantity",
                            "message": (
                                f"research root batch {batch_id} 使用 whole_batch，"
                                "但绑定的 Research macro step 未对同一物料显式声明 "
                                "quantity_requirements.kind=whole_batch；不得借此删除"
                                "冻结的目标进样量或科学输入量。"
                            ),
                        }
                    )
            if not is_root_batch:
                transition_kind = str(item.get("transition_kind") or "").strip()
                raw_plan_refs = item.get("source_plan_steps")
                raw_macro_refs = item.get("source_macro_steps")
                plan_refs = {
                    str(value).strip()
                    for value in raw_plan_refs or []
                    if str(value).strip()
                } if isinstance(raw_plan_refs, list) else set()
                macro_refs = {
                    str(value).strip()
                    for value in raw_macro_refs or []
                    if str(value).strip()
                } if isinstance(raw_macro_refs, list) else set()
                if transition_kind not in allowed_transition_kinds:
                    issues.append(
                        {
                            "code": "missing_or_invalid_batch_transition_kind",
                            "scope": "device_local_quantity",
                            "message": (
                                f"non-root batch {batch_id} 缺少合法 transition_kind。"
                            ),
                        }
                    )
                if not plan_refs:
                    issues.append(
                        {
                            "code": "missing_batch_source_plan_steps",
                            "scope": "device_local_quantity",
                            "message": f"non-root batch {batch_id} 缺少 source_plan_steps。",
                        }
                    )
                elif not plan_refs.issubset(plan_step_ids):
                    issues.append(
                        {
                            "code": "invalid_batch_source_plan_step_ref",
                            "scope": "device_local_quantity",
                            "message": (
                                f"non-root batch {batch_id} 引用了不存在 plan step："
                                f"{sorted(plan_refs - plan_step_ids)}。"
                            ),
                        }
                    )
                if not macro_refs:
                    issues.append(
                        {
                            "code": "missing_batch_source_macro_steps",
                            "scope": "device_local_quantity",
                            "message": f"non-root batch {batch_id} 缺少 source_macro_steps。",
                        }
                    )
                elif research_macro_ids and not macro_refs.issubset(
                    research_macro_ids
                ):
                    issues.append(
                        {
                            "code": "invalid_batch_source_macro_step_ref",
                            "scope": "device_local_quantity",
                            "message": (
                                f"non-root batch {batch_id} 引用了不存在 macro step："
                                f"{sorted(macro_refs - research_macro_ids)}。"
                            ),
                        }
                    )
                nonroot_batch_requirements[batch_id] = {
                    "parent_batch_ids": parent_ids,
                    "transition_kind": transition_kind,
                    "source_plan_steps": plan_refs,
                    "source_macro_steps": macro_refs,
                }
            declared_whole_batch_consumers = {
                str(value).strip()
                for value in item.get("consumer_ids", []) or []
                if str(value).strip()
            } if isinstance(item.get("consumer_ids"), list) else set()
            if is_whole_batch:
                if item.get("allocation") not in (None, "", {}, []):
                    issues.append(
                        {
                            "code": "whole_batch_must_not_declare_numeric_allocation",
                            "scope": "device_local_quantity",
                            "message": (
                                f"batch {batch_id} 是 whole_batch；不得把目标取样量或"
                                "猜测总量写成整批 allocation。"
                            ),
                        }
                    )
                if len(declared_whole_batch_consumers) > 1:
                    issues.append(
                        {
                            "code": "whole_batch_multiple_consumers_require_explicit_split",
                            "scope": "device_local_quantity",
                            "message": (
                                f"batch {batch_id} 的 whole_batch 只能直接流向一个 consumer；"
                                "多个下游必须显式 split，不能复制整批。"
                            ),
                        }
                    )
                if any(
                    item.get(key) not in (None, "")
                    for key in ("total_quantity", "parent_quantity", "per_batch_quantity")
                ):
                    issues.append(
                        {
                            "code": "whole_batch_must_not_fake_numeric_inventory",
                            "scope": "device_local_quantity",
                            "message": (
                                f"batch {batch_id} 是 whole_batch；实际总量未知时不得"
                                "同时伪造 total_quantity/parent_quantity。"
                            ),
                        }
                    )
            elif not item.get("consumer_ids") and not item.get("allocation"):
                issues.append(
                    {
                        "code": "missing_batch_consumer_allocation",
                        "scope": "device_local_quantity",
                        "message": f"batch {batch_id} 缺少 consumer_ids/allocation。",
                    }
                )
            total, total_dim, _ = self._canonical_quantity(
                item.get("total_quantity", item.get("parent_quantity"))
            )
            per_batch, per_batch_dim, _ = self._canonical_quantity(
                item.get("per_batch_quantity")
            )
            for quantity_name, raw_quantity, value, dimension in (
                (
                    "total_quantity",
                    item.get("total_quantity", item.get("parent_quantity")),
                    total,
                    total_dim,
                ),
                (
                    "per_batch_quantity",
                    item.get("per_batch_quantity"),
                    per_batch,
                    per_batch_dim,
                ),
            ):
                if not is_whole_batch and raw_quantity not in (None, "") and (
                    value is None
                    or not self._quantity_dimension_is_auditable(dimension)
                ):
                    issues.append(
                        {
                            "code": "batch_quantity_unit_missing_or_unknown",
                            "scope": "device_local_quantity",
                            "message": (
                                f"batch {batch_id} 的 {quantity_name} 缺少可审计单位。"
                            ),
                        }
                    )
                elif not is_whole_batch and raw_quantity not in (None, "") and not self._quantity_dimension_is_inventory(
                    dimension
                ):
                    issues.append(
                        {
                            "code": "batch_quantity_requires_extensive_dimension",
                            "scope": "device_local_quantity",
                            "message": (
                                f"batch {batch_id} 的 {quantity_name} 必须是 "
                                "amount/mass/volume；concentration 不能独立做库存。"
                            ),
                        }
                    )
                elif not is_whole_batch and raw_quantity not in (None, "") and value is not None and value <= 0:
                    issues.append(
                        {
                            "code": "nonpositive_batch_quantity",
                            "scope": "device_local_quantity",
                            "message": (
                                f"batch {batch_id} 的 {quantity_name} 必须为有限正数。"
                            ),
                        }
                    )
            if is_root_batch and not is_whole_batch:
                if total is None or not self._quantity_dimension_is_auditable(total_dim):
                    issues.append(
                        {
                            "code": "missing_research_root_total_quantity",
                            "scope": "device_local_quantity",
                            "message": (
                                f"research root batch {batch_id} 必须显式给出"
                                "带单位 total_quantity。"
                            ),
                        }
                    )
                unique_root_quantities = {
                    (round(value, 15), dimension, macro_id)
                    for value, dimension, macro_id in root_research_quantities
                }
                if len(unique_root_quantities) > 1:
                    issues.append(
                        {
                            "code": "ambiguous_research_root_quantity_provenance",
                            "scope": "device_local_quantity",
                            "message": (
                                f"research root batch {batch_id} 绑定了多个不同"
                                "Research 数量/来源，不得自动选择。"
                            ),
                        }
                    )
                elif total is not None and len(unique_root_quantities) == 1:
                    source_value, source_dimension, source_macro_id = next(
                        iter(unique_root_quantities)
                    )
                    if not self._canonical_quantities_equal(
                        source_value,
                        source_dimension,
                        total,
                        total_dim,
                    ) and not quantity_change_is_authorized(
                        source_value,
                        source_dimension,
                        total,
                        total_dim,
                        source_macro_id,
                    ):
                        issues.append(
                            {
                                "code": "unauthorized_research_quantity_change",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"research root batch {batch_id} 的 total_quantity "
                                    "与冻结 Research 数量不一致，且缺少绑定"
                                    " before/after 的 scientific-review quantity_adjustment。"
                                ),
                            }
                        )
            multiplicity = self._quantity_value(item.get("multiplicity"))
            if "multiplicity" in item and (
                multiplicity is None
                or multiplicity < 1
                or not float(multiplicity).is_integer()
            ):
                issues.append(
                    {
                        "code": "invalid_batch_multiplicity",
                        "scope": "device_local_quantity",
                        "message": (
                            f"batch {batch_id} 的 multiplicity 必须是有限正整数。"
                        ),
                    }
                )
            operation_repeat_count = self._quantity_value(
                item.get("operation_repeat_count")
            )
            if "operation_repeat_count" in item and (
                operation_repeat_count is None
                or operation_repeat_count < 1
                or not float(operation_repeat_count).is_integer()
            ):
                issues.append(
                    {
                        "code": "invalid_operation_repeat_count",
                        "scope": "device_local_quantity",
                        "message": (
                            f"batch {batch_id} 的 operation_repeat_count 必须是"
                            "有限正整数。"
                        ),
                    }
                )
            if total is not None and per_batch is not None and multiplicity is not None:
                if total_dim != per_batch_dim:
                    issues.append(
                        {
                            "code": "batch_quantity_unit_mismatch",
                            "scope": "device_local_quantity",
                            "message": (
                                f"batch {batch_id}: total_quantity 与 "
                                "per_batch_quantity 单位维度不一致。"
                            ),
                        }
                    )
                elif abs(total - per_batch * multiplicity) > max(1e-9, abs(total) * 1e-6):
                    issues.append(
                        {
                            "code": "batch_quantity_mismatch",
                            "scope": "device_local_quantity",
                            "message": (
                                f"batch {batch_id}: total_quantity != "
                                "per_batch_quantity × multiplicity。"
                            ),
                        }
                    )
            has_batch_quantity = not is_whole_batch and any(
                key in item
                for key in (
                    "total_quantity",
                    "parent_quantity",
                    "per_batch_quantity",
                    "allocation",
                    "multiplicity",
                )
            )
            if has_batch_quantity:
                if raw_source_kind not in {
                    "research",
                    "derived",
                    "device_operational",
                }:
                    issues.append(
                        {
                            "code": "missing_batch_quantity_source",
                            "scope": "device_local_quantity",
                            "message": f"batch {batch_id} 的数量缺少合法 source_kind。",
                        }
                    )
                if not isinstance(raw_source_refs, list) or not raw_source_refs:
                    issues.append(
                        {
                            "code": "missing_batch_quantity_source_refs",
                            "scope": "device_local_quantity",
                            "message": f"batch {batch_id} 的数量缺少 source_refs。",
                        }
                    )
                if not raw_calculation:
                    issues.append(
                        {
                            "code": "missing_batch_quantity_calculation",
                            "scope": "device_local_quantity",
                            "message": f"batch {batch_id} 的数量缺少 calculation/derivation。",
                        }
                    )
            normalized_batches.append(item)

        for source_key, funded_batch_ids in research_root_source_usage.items():
            if len(funded_batch_ids) <= 1:
                continue
            issues.append(
                {
                    "code": "research_root_source_reused",
                    "scope": "device_local_quantity",
                    "message": (
                        "同一冻结 Research quantity/material source 默认只能"
                        "资助一个聚合 root batch；检测到重复 roots="
                        f"{sorted(funded_batch_ids)}。多批必须在单一 root 中用 "
                        "per_batch_quantity×multiplicity 表示，然后显式 split。"
                    ),
                    "source_key_digest": self._stable_digest(
                        source_key, prefix="research_root_source"
                    ),
                }
            )

        batch_by_id = {
            str(item.get("batch_id") or ""): item
            for item in normalized_batches
            if str(item.get("batch_id") or "")
        }
        for batch_id, requirement in nonroot_batch_requirements.items():
            missing_parent_ids = requirement["parent_batch_ids"] - set(batch_by_id)
            if missing_parent_ids:
                issues.append(
                    {
                        "code": "transition_parent_batch_missing",
                        "scope": "device_local_quantity",
                        "message": (
                            f"non-root batch {batch_id} 的 parent_batch_id(s)="
                            f"{sorted(missing_parent_ids)} 未在 batch_plan 中显式定义。"
                        ),
                    }
                )

        raw_transitions = normalized.get("material_transitions", [])
        if not isinstance(raw_transitions, list):
            raw_transitions = []
            issues.append(
                {
                    "code": "invalid_material_transitions",
                    "scope": "device_local_quantity",
                    "message": "material_transitions 必须是 array。",
                }
            )
        normalized_transitions: List[Dict[str, Any]] = []
        seen_transition_ids: Set[str] = set()
        child_transition_counts: Dict[str, int] = {}
        transition_plan_refs_by_id: Dict[str, Set[str]] = {}
        transition_macro_refs_by_id: Dict[str, Set[str]] = {}
        transition_kind_by_id: Dict[str, str] = {}
        child_transition_ids: Dict[str, Set[str]] = {}
        transition_edge_flows: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = {}
        transition_human_issues: List[Dict[str, Any]] = []
        trusted_human_quantity_approvals = copy.deepcopy(
            self._active_trusted_human_quantity_approvals
        )
        transition_graph: Dict[str, Set[str]] = {
            batch_id: set() for batch_id in batch_by_id
        }
        for index, raw in enumerate(raw_transitions, start=1):
            if not isinstance(raw, dict):
                issues.append(
                    {
                        "code": "invalid_material_transition_record",
                        "scope": "device_local_quantity",
                        "message": f"material_transitions[{index}] 必须是 object。",
                    }
                )
                continue
            transition = copy.deepcopy(raw)
            transition_id = str(
                transition.get("transition_id") or f"mt_{index:03d}"
            ).strip()
            transition["transition_id"] = transition_id
            if transition_id in seen_transition_ids:
                issues.append(
                    {
                        "code": "duplicate_material_transition_id",
                        "scope": "device_local_quantity",
                        "message": f"material transition {transition_id} 重复。",
                    }
                )
            seen_transition_ids.add(transition_id)
            transition_kind = str(
                transition.get("transition_kind") or ""
            ).strip()
            parent_ids = {
                str(value).strip()
                for value in transition.get("parent_batch_ids", []) or []
                if str(value).strip()
            } if isinstance(transition.get("parent_batch_ids"), list) else set()
            child_ids = {
                str(value).strip()
                for value in transition.get("child_batch_ids", []) or []
                if str(value).strip()
            } if isinstance(transition.get("child_batch_ids"), list) else set()
            transition_plan_refs = {
                str(value).strip()
                for value in transition.get("source_plan_steps", []) or []
                if str(value).strip()
            } if isinstance(transition.get("source_plan_steps"), list) else set()
            transition_macro_refs = {
                str(value).strip()
                for value in transition.get("source_macro_steps", []) or []
                if str(value).strip()
            } if isinstance(transition.get("source_macro_steps"), list) else set()
            transition_plan_refs_by_id.setdefault(
                transition_id, set(transition_plan_refs)
            )
            transition_macro_refs_by_id.setdefault(
                transition_id, set(transition_macro_refs)
            )
            transition_kind_by_id.setdefault(transition_id, transition_kind)
            if transition_kind not in allowed_transition_kinds:
                issues.append(
                    {
                        "code": "invalid_material_transition_kind",
                        "scope": "device_local_quantity",
                        "message": (
                            f"material transition {transition_id} 缺少合法 transition_kind。"
                        ),
                    }
                )
            if not parent_ids or not child_ids:
                issues.append(
                    {
                        "code": "missing_material_transition_batch_refs",
                        "scope": "device_local_quantity",
                        "message": (
                            f"material transition {transition_id} 必须显式给出 "
                            "parent_batch_ids 和 child_batch_ids。"
                        ),
                    }
                )
            unknown_batches = (parent_ids | child_ids) - set(batch_by_id)
            if unknown_batches:
                issues.append(
                    {
                        "code": "invalid_material_transition_batch_ref",
                        "scope": "device_local_quantity",
                        "message": (
                            f"material transition {transition_id} 引用了不存在 batch："
                            f"{sorted(unknown_batches)}。"
                        ),
                    }
                )
            self_loop_batches = parent_ids & child_ids
            if self_loop_batches:
                issues.append(
                    {
                        "code": "material_transition_self_loop",
                        "scope": "device_local_quantity",
                        "message": (
                            f"material transition {transition_id} 的 parent/child "
                            f"重叠：{sorted(self_loop_batches)}。"
                        ),
                    }
                )
            for parent_id in parent_ids & set(batch_by_id):
                transition_graph.setdefault(parent_id, set()).update(
                    child_ids & set(batch_by_id)
                )
            if not transition_plan_refs:
                issues.append(
                    {
                        "code": "missing_material_transition_plan_refs",
                        "scope": "device_local_quantity",
                        "message": (
                            f"material transition {transition_id} 缺少 source_plan_steps。"
                        ),
                    }
                )
            elif not transition_plan_refs.issubset(plan_step_ids):
                issues.append(
                    {
                        "code": "invalid_material_transition_plan_ref",
                        "scope": "device_local_quantity",
                        "message": (
                            f"material transition {transition_id} 引用了不存在 plan step："
                            f"{sorted(transition_plan_refs - plan_step_ids)}。"
                        ),
                    }
                )
            if not transition_macro_refs:
                issues.append(
                    {
                        "code": "missing_material_transition_macro_refs",
                        "scope": "device_local_quantity",
                        "message": (
                            f"material transition {transition_id} 缺少 source_macro_steps。"
                        ),
                    }
                )
            elif research_macro_ids and not transition_macro_refs.issubset(
                research_macro_ids
            ):
                issues.append(
                    {
                        "code": "invalid_material_transition_macro_ref",
                        "scope": "device_local_quantity",
                        "message": (
                            f"material transition {transition_id} 引用了不存在 macro step："
                            f"{sorted(transition_macro_refs - research_macro_ids)}。"
                        ),
                    }
                )
            if transition_kind == "state_change":
                before_state = str(
                    transition.get("before_material_state") or ""
                ).strip()
                after_state = str(
                    transition.get("after_material_state") or ""
                ).strip()
                if not before_state or not after_state or before_state == after_state:
                    issues.append(
                        {
                            "code": "invalid_material_state_transition",
                            "scope": "device_local_quantity",
                            "message": (
                                f"material transition {transition_id} 的 state_change "
                                "必须给出不同的 before_material_state/after_material_state。"
                            ),
                        }
                    )
            quantity_basis = str(
                transition.get("quantity_basis") or ""
            ).strip()
            whole_batch_transition = quantity_basis == "whole_batch"
            if whole_batch_transition:
                if transition_kind not in {"process_same_material", "state_change"}:
                    issues.append(
                        {
                            "code": "invalid_whole_batch_transition_kind",
                            "scope": "device_local_quantity",
                            "message": (
                                f"whole_batch transition {transition_id} 只能表示直接整批处理"
                                "或整批状态变化；split/replicate/merge 必须使用数值谱系。"
                            ),
                        }
                    )
                if len(parent_ids) != 1 or len(child_ids) != 1:
                    issues.append(
                        {
                            "code": "whole_batch_transition_requires_one_to_one_lineage",
                            "scope": "device_local_quantity",
                            "message": (
                                f"whole_batch transition {transition_id} 必须是 1→1；"
                                "拆分/合并不能用 whole_batch 绕过数量审计。"
                            ),
                        }
                    )
                non_whole_batches = (parent_ids | child_ids) - whole_batch_batch_ids
                if non_whole_batches:
                    issues.append(
                        {
                            "code": "whole_batch_transition_batch_mode_mismatch",
                            "scope": "device_local_quantity",
                            "message": (
                                f"whole_batch transition {transition_id} 的 parent/child "
                                f"必须都声明 quantity_mode=whole_batch；mismatch="
                                f"{sorted(non_whole_batches)}。"
                            ),
                        }
                    )
            parent_quantities: List[Tuple[str, float, str]] = []
            child_quantities: List[Tuple[str, float, str]] = []
            normalized_edge_flows: Dict[str, Dict[str, Dict[str, Any]]] = {
                "input": {},
                "output": {},
            }
            for role, field_name, batch_ids, destination in (
                ("input", "input_allocations", parent_ids, parent_quantities),
                ("output", "output_allocations", child_ids, child_quantities),
            ):
                raw_edge_allocations = transition.get(field_name)
                if whole_batch_transition and raw_edge_allocations not in (
                    None, "", [],
                ):
                    issues.append(
                        {
                            "code": "whole_batch_transition_must_not_declare_edge_quantity",
                            "scope": "device_local_quantity",
                            "message": (
                                f"whole_batch transition {transition_id} 不得声明"
                                f" {field_name} 数值；它只证明整批 1→1 谱系。"
                            ),
                        }
                    )
                    raw_edge_allocations = []
                if not isinstance(raw_edge_allocations, list):
                    raw_edge_allocations = []
                    if not whole_batch_transition:
                        issues.append(
                            {
                                "code": "missing_material_transition_edge_allocations",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"material transition {transition_id} 缺少结构化 "
                                    f"{field_name}。"
                                ),
                            }
                        )
                seen_edge_batch_ids: Set[str] = set()
                for edge_index, edge_allocation in enumerate(
                    raw_edge_allocations, start=1
                ):
                    if not isinstance(edge_allocation, dict):
                        issues.append(
                            {
                                "code": "invalid_material_transition_edge_allocation",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"transition {transition_id} {field_name}[{edge_index}] "
                                    "必须是 object。"
                                ),
                            }
                        )
                        continue
                    transition_batch_id = str(
                        edge_allocation.get("batch_id") or ""
                    ).strip()
                    edge_quantity = edge_allocation.get("quantity")
                    edge_value, edge_dimension, edge_unit = (
                        self._canonical_quantity(edge_quantity)
                    )
                    if not transition_batch_id or transition_batch_id in seen_edge_batch_ids:
                        issues.append(
                            {
                                "code": "duplicate_or_missing_transition_edge_batch",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"transition {transition_id} {field_name} 的 batch_id "
                                    "缺失或重复。"
                                ),
                            }
                        )
                        continue
                    seen_edge_batch_ids.add(transition_batch_id)
                    if (
                        edge_value is None
                        or not self._quantity_dimension_is_inventory(
                            edge_dimension
                        )
                    ):
                        issues.append(
                            {
                                "code": "invalid_transition_edge_quantity",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"transition {transition_id} {field_name} batch "
                                    f"{transition_batch_id} 必须有 amount/mass/volume quantity。"
                                ),
                            }
                        )
                        continue
                    if edge_value <= 0:
                        issues.append(
                            {
                                "code": "nonpositive_transition_edge_quantity",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"transition {transition_id} {field_name} batch "
                                    f"{transition_batch_id} 的 quantity 必须为有限正数。"
                                ),
                            }
                        )
                        continue
                    normalized_edge_flows[role][transition_batch_id] = {
                        "value": edge_value,
                        "dimension": edge_dimension,
                        "unit": edge_unit,
                        "raw": copy.deepcopy(edge_quantity),
                    }
                    destination.append(
                        (transition_batch_id, edge_value, edge_dimension)
                    )
                if not whole_batch_transition and seen_edge_batch_ids != batch_ids:
                    issues.append(
                        {
                            "code": "material_transition_edge_batch_set_mismatch",
                            "scope": "device_local_quantity",
                            "message": (
                                f"transition {transition_id} {field_name} batch 集必须"
                                f"精确等于拓扑声明；missing="
                                f"{sorted(batch_ids - seen_edge_batch_ids)}, extra="
                                f"{sorted(seen_edge_batch_ids - batch_ids)}。"
                            ),
                        }
                    )
                for transition_batch_id in batch_ids & set(batch_by_id):
                    if whole_batch_transition:
                        continue
                    transition_batch = batch_by_id[transition_batch_id]
                    batch_total, batch_dimension, _ = self._canonical_quantity(
                        transition_batch.get(
                            "total_quantity",
                            transition_batch.get("parent_quantity"),
                        )
                    )
                    if (
                        batch_total is None
                        or not self._quantity_dimension_is_inventory(
                            batch_dimension
                        )
                    ):
                        issues.append(
                            {
                                "code": "missing_transition_batch_total_quantity",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"material transition {transition_id} 的 {role} "
                                    f"batch {transition_batch_id} 必须显式给出"
                                    " amount/mass/volume total_quantity。"
                                ),
                            }
                        )
                        continue
                    edge_record = normalized_edge_flows[role].get(
                        transition_batch_id
                    )
                    if edge_record is None:
                        continue
                    if edge_record["dimension"] != batch_dimension:
                        issues.append(
                            {
                                "code": "transition_edge_batch_dimension_mismatch",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"transition {transition_id} {role} batch "
                                    f"{transition_batch_id} edge/batch 维度不一致。"
                                ),
                            }
                        )
                    elif (
                        role == "input"
                        and edge_record["value"]
                        > batch_total + max(1e-12, abs(batch_total) * 1e-6)
                    ):
                        issues.append(
                            {
                                "code": "transition_input_exceeds_parent_batch",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"transition {transition_id} input 超过 parent batch "
                                    f"{transition_batch_id} total_quantity。"
                                ),
                            }
                        )
                    elif role == "output" and not self._canonical_quantities_equal(
                        edge_record["value"],
                        edge_record["dimension"],
                        batch_total,
                        batch_dimension,
                    ):
                        issues.append(
                            {
                                "code": "transition_output_child_total_mismatch",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"transition {transition_id} output 必须精确等于"
                                    f" child batch {transition_batch_id} total_quantity。"
                                ),
                            }
                        )
            transition_edge_flows.setdefault(
                transition_id, normalized_edge_flows
            )
            quantity_dimensions = {
                dimension
                for _, _, dimension in parent_quantities + child_quantities
            }
            complete_transition_quantities = bool(
                len(parent_quantities) == len(parent_ids)
                and len(child_quantities) == len(child_ids)
            )
            if not whole_batch_transition and transition_kind in {
                "split_same_material",
                "replicate_same_material",
                "process_same_material",
            }:
                if quantity_basis != "conserved_inventory":
                    issues.append(
                        {
                            "code": "invalid_conserved_transition_quantity_basis",
                            "scope": "device_local_quantity",
                            "message": (
                                f"identity-preserving transition {transition_id} 必须"
                                "声明 quantity_basis=conserved_inventory。"
                            ),
                        }
                    )
                if complete_transition_quantities:
                    if len(quantity_dimensions) != 1:
                        issues.append(
                            {
                                "code": "transition_quantity_dimension_mismatch",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"conserved transition {transition_id} 的 parent/child "
                                    "库存维度不一致。"
                                ),
                            }
                        )
                    else:
                        parent_total = sum(
                            value for _, value, _ in parent_quantities
                        )
                        child_total = sum(
                            value for _, value, _ in child_quantities
                        )
                        if abs(parent_total - child_total) > max(
                            1e-12,
                            abs(parent_total) * 1e-6,
                            abs(child_total) * 1e-6,
                        ):
                            issues.append(
                                {
                                    "code": "conserved_transition_quantity_mismatch",
                                    "scope": "device_local_quantity",
                                    "message": (
                                        f"conserved transition {transition_id} 必须聚合"
                                        f"守恒；parent_total={parent_total:g}, "
                                        f"child_total={child_total:g}。"
                                    ),
                                }
                            )
            elif not whole_batch_transition and transition_kind == "state_change":
                measurement_valid = False
                planning_yield_valid = False
                human_observation_approval_valid = False

                def trusted_approval_covers_all_children(
                    approval_basis: str,
                ) -> bool:
                    approved_child_ids: Set[str] = set()
                    for approval in trusted_human_quantity_approvals:
                        if not isinstance(approval, dict):
                            continue
                        if (
                            str(approval.get("transition_id") or "").strip()
                            != transition_id
                            or str(
                                approval.get("approval_basis") or ""
                            ).strip()
                            != approval_basis
                        ):
                            continue
                        approved_batch_id = str(
                            approval.get("batch_id") or ""
                        ).strip()
                        child_batch = batch_by_id.get(approved_batch_id)
                        expected_edge = normalized_edge_flows["output"].get(
                            approved_batch_id
                        )
                        approved_value, approved_dimension, _ = (
                            self._canonical_quantity(
                                approval.get("approved_quantity")
                            )
                        )
                        if (
                            approved_batch_id not in child_ids
                            or approved_batch_id in approved_child_ids
                            or not isinstance(child_batch, dict)
                            or expected_edge is None
                            or str(approval.get("sample_id") or "").strip()
                            != str(child_batch.get("sample_id") or "").strip()
                            or str(approval.get("material_id") or "").strip()
                            != str(child_batch.get("material_id") or "").strip()
                            or approved_value is None
                            or not self._canonical_quantities_equal(
                                approved_value,
                                approved_dimension,
                                float(expected_edge["value"]),
                                str(expected_edge["dimension"]),
                            )
                        ):
                            continue
                        approved_child_ids.add(approved_batch_id)
                    return bool(child_ids) and approved_child_ids == child_ids

                human_observation_approval_valid = bool(
                    quantity_basis == "measured_observation"
                    and trusted_approval_covers_all_children(
                        "observed_quantity"
                    )
                )
                if quantity_basis == "measured_observation":
                    measurement = transition.get("measurement_artifact")
                    observations = (
                        (research_handoff or {}).get("observations", []) or []
                    )
                    observation_id_counts: Dict[str, int] = {}
                    for observation in observations:
                        if not isinstance(observation, dict):
                            continue
                        observation_id = str(
                            observation.get("observation_id")
                            or observation.get("id")
                            or ""
                        ).strip()
                        if observation_id:
                            observation_id_counts[observation_id] = (
                                observation_id_counts.get(observation_id, 0) + 1
                            )
                    observation_by_id = {
                        str(
                            observation.get("observation_id")
                            or observation.get("id")
                            or ""
                        ): observation
                        for observation in observations
                        if isinstance(observation, dict)
                        and observation_id_counts.get(
                            str(
                                observation.get("observation_id")
                                or observation.get("id")
                                or ""
                            ).strip(),
                            0,
                        )
                        == 1
                    }
                    if isinstance(measurement, dict):
                        observation_id = str(
                            measurement.get("observation_id") or ""
                        ).strip()
                        frozen_observation = observation_by_id.get(observation_id)
                        child_total = sum(
                            value for _, value, _ in child_quantities
                        )
                        child_dimensions = {
                            dimension for _, _, dimension in child_quantities
                        }
                        quantity_source_field = str(
                            measurement.get("quantity_source_field") or ""
                        ).strip()
                        quantity_source_context = str(
                            measurement.get("quantity_source_context") or ""
                        ).strip()
                        frozen_quantity_value = (
                            frozen_observation.get(quantity_source_field)
                            if isinstance(frozen_observation, dict)
                            and quantity_source_field in frozen_observation
                            else None
                        )
                        frozen_quantity_field = (
                            json.dumps(
                                frozen_quantity_value,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            if isinstance(frozen_quantity_value, (dict, list))
                            else str(frozen_quantity_value or "")
                        )
                        observed_quantities = (
                            self._extract_explicit_quantities(
                                quantity_source_context
                            )
                            if quantity_source_context
                            and frozen_quantity_field.count(
                                quantity_source_context
                            )
                            == 1
                            else []
                        )
                        direct_observed_value, direct_observed_dimension, _ = (
                            self._canonical_quantity(frozen_quantity_value)
                        )
                        if (
                            not observed_quantities
                            and quantity_source_context == frozen_quantity_field
                            and direct_observed_value is not None
                            and self._quantity_dimension_is_inventory(
                                direct_observed_dimension
                            )
                        ):
                            observed_quantities = [
                                {
                                    "raw": frozen_quantity_field,
                                    "value": direct_observed_value,
                                    "dimension": direct_observed_dimension,
                                }
                            ]
                        matching_observed_quantities = [
                            quantity
                            for quantity in observed_quantities
                            if len(child_dimensions) == 1
                            and quantity["dimension"]
                            == next(iter(child_dimensions))
                        ]
                        measured_child_id = (
                            next(iter(child_ids)) if len(child_ids) == 1 else ""
                        )
                        measured_child = batch_by_id.get(measured_child_id, {})
                        observation_sample_id = (
                            str(frozen_observation.get("sample_id") or "").strip()
                            if isinstance(frozen_observation, dict)
                            else ""
                        )
                        observation_material_id = (
                            frozen_observation.get("material_id")
                            if isinstance(frozen_observation, dict)
                            else None
                        )
                        observation_batch_id = (
                            str(frozen_observation.get("batch_id") or "").strip()
                            if isinstance(frozen_observation, dict)
                            else ""
                        )
                        child_sample_id = str(
                            measured_child.get("sample_id") or ""
                        ).strip()
                        child_material_id = measured_child.get("material_id")
                        child_semantic_material_id = str(
                            measured_child.get("material_identity_id") or ""
                        ).strip()
                        measurement_semantic_material_id = str(
                            measurement.get("material_identity_id") or ""
                        ).strip()
                        if self._active_semantic_analysis:
                            material_binding_valid = bool(
                                child_semantic_material_id
                                and measurement_semantic_material_id
                                == child_semantic_material_id
                                and child_semantic_material_id
                                in {
                                    identity_id
                                    for assessment in self._active_semantic_analysis.get(
                                        "macro_step_assessments", []
                                    )
                                    if isinstance(assessment, dict)
                                    for identity_id in self._semantic_material_ids(
                                        assessment.get("source_macro_step")
                                    )
                                }
                            )
                        else:
                            material_binding_valid = self._identity_token_sets_match(
                                observation_material_id, child_material_id
                            )
                        measurement_valid = bool(
                            frozen_observation is not None
                            and measured_child_id
                            and observation_sample_id
                            and observation_sample_id == child_sample_id
                            and material_binding_valid
                            and (
                                not observation_batch_id
                                or observation_batch_id == measured_child_id
                            )
                            and str(measurement.get("artifact_digest") or "")
                            == self._stable_digest(
                                frozen_observation, prefix="observation"
                            )
                            and len(child_dimensions) == 1
                            and len(matching_observed_quantities) == 1
                            and self._canonical_quantities_equal(
                                float(
                                    matching_observed_quantities[0]["value"]
                                ),
                                str(
                                    matching_observed_quantities[0]["dimension"]
                                ),
                                child_total,
                                next(iter(child_dimensions)),
                            )
                        )
                if quantity_basis == "planning_yield_lower_bound":
                    lower_bound = self._quantity_value(
                        transition.get("yield_lower_bound")
                    )
                    if lower_bound is None or not 0 <= lower_bound <= 1:
                        issues.append(
                            {
                                "code": "invalid_planning_yield_lower_bound",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"state_change transition {transition_id} 的 "
                                    "yield_lower_bound 必须在 [0,1]。"
                                ),
                            }
                        )
                    elif complete_transition_quantities:
                        parent_dimensions = {
                            dimension
                            for _, _, dimension in parent_quantities
                        }
                        child_dimensions = {
                            dimension
                            for _, _, dimension in child_quantities
                        }
                        parent_total = sum(
                            value for _, value, _ in parent_quantities
                        )
                        child_total = sum(
                            value for _, value, _ in child_quantities
                        )
                        if (
                            len(parent_dimensions) != 1
                            or parent_dimensions != child_dimensions
                            or child_total
                            > parent_total * lower_bound
                            + max(1e-12, abs(parent_total) * 1e-6)
                        ):
                            issues.append(
                                {
                                    "code": "planning_yield_allocation_exceeded",
                                    "scope": "device_local_quantity",
                                    "message": (
                                        f"state_change transition {transition_id} 的 child "
                                        "计划数量超过 parent×yield_lower_bound，或单位"
                                        "维度不可比。"
                                    ),
                                }
                            )
                        elif trusted_approval_covers_all_children(
                            "planning_yield_lower_bound"
                        ):
                            planning_yield_valid = True
                if not (
                    measurement_valid
                    or human_observation_approval_valid
                    or planning_yield_valid
                ):
                    transition_human_issues.append(
                        {
                            "code": "unknown_yield",
                            "scope": "human_review_required",
                            "transition_id": transition_id,
                            "message": (
                                f"state_change transition {transition_id} 没有与冻结"
                                " observation 摘要匹配的 measurement_artifact；"
                                "Device 不得用自由文本 measured/calculation 猜产率。"
                            ),
                        }
                    )
            for child_id in child_ids & set(batch_by_id):
                child_transition_ids.setdefault(child_id, set()).add(transition_id)
                child_transition_counts[child_id] = (
                    child_transition_counts.get(child_id, 0) + 1
                )
                requirement = nonroot_batch_requirements.get(child_id)
                if requirement is None:
                    issues.append(
                        {
                            "code": "root_batch_referenced_as_transition_child",
                            "scope": "device_local_quantity",
                            "message": (
                                f"material transition {transition_id} 将 root batch "
                                f"{child_id} 作为 child。"
                            ),
                        }
                    )
                    continue
                if requirement["parent_batch_ids"] != parent_ids:
                    missing_parents = requirement["parent_batch_ids"] - parent_ids
                    extra_parents = parent_ids - requirement["parent_batch_ids"]
                    issues.append(
                        {
                            "code": "material_transition_parent_mismatch",
                            "scope": "device_local_quantity",
                            "message": (
                                f"transition {transition_id} 的 parents 必须与 child "
                                f"{child_id} 声明集合完全一致；missing="
                                f"{sorted(missing_parents)}, extra={sorted(extra_parents)}。"
                            ),
                        }
                    )
                if requirement["transition_kind"] != transition_kind:
                    issues.append(
                        {
                            "code": "material_transition_kind_mismatch",
                            "scope": "device_local_quantity",
                            "message": (
                                f"transition {transition_id} 与 child {child_id} 的 "
                                "transition_kind 不一致。"
                            ),
                        }
                    )
                if requirement["source_plan_steps"] != transition_plan_refs:
                    issues.append(
                        {
                            "code": "material_transition_plan_refs_mismatch",
                            "scope": "device_local_quantity",
                            "message": (
                                f"transition {transition_id} 与 child {child_id} 的 "
                                "source_plan_steps 不一致。"
                            ),
                        }
                    )
                if requirement["source_macro_steps"] != transition_macro_refs:
                    issues.append(
                        {
                            "code": "material_transition_macro_refs_mismatch",
                            "scope": "device_local_quantity",
                            "message": (
                                f"transition {transition_id} 与 child {child_id} 的 "
                                "source_macro_steps 不一致。"
                            ),
                        }
                    )
            normalized_transitions.append(transition)
        for batch_id in nonroot_batch_requirements:
            count = child_transition_counts.get(batch_id, 0)
            if count != 1:
                issues.append(
                    {
                        "code": "missing_or_ambiguous_batch_material_transition",
                        "scope": "device_local_quantity",
                        "message": (
                            f"non-root batch {batch_id} 必须被恰好一条 "
                            f"material transition 作为 child 覆盖；actual={count}。"
                        ),
                    }
                )
        cycle_nodes: Set[str] = set()
        visit_state: Dict[str, int] = {}

        def visit_transition_node(batch_id: str, stack: List[str]) -> None:
            state = visit_state.get(batch_id, 0)
            if state == 1:
                if batch_id in stack:
                    cycle_nodes.update(stack[stack.index(batch_id) :])
                else:
                    cycle_nodes.add(batch_id)
                return
            if state == 2:
                return
            visit_state[batch_id] = 1
            stack.append(batch_id)
            for child_id in transition_graph.get(batch_id, set()):
                visit_transition_node(child_id, stack)
            stack.pop()
            visit_state[batch_id] = 2

        for graph_batch_id in transition_graph:
            if visit_state.get(graph_batch_id, 0) == 0:
                visit_transition_node(graph_batch_id, [])
        if cycle_nodes:
            issues.append(
                {
                    "code": "material_transition_cycle",
                    "scope": "device_local_quantity",
                    "message": (
                        "material transition 必须是 DAG，检测到环："
                        f"{sorted(cycle_nodes)}。"
                    ),
                }
            )

        plan_transition_refs: Dict[str, Set[str]] = {}
        plan_material_event_kind: Dict[str, str] = {}
        for plan_step_id, plan_step in plan_step_by_id.items():
            workstation = str(plan_step.get("workstation") or "").strip()
            event_kind = str(plan_step.get("material_event_kind") or "").strip()
            raw_step_transition_refs = plan_step.get("material_transition_ids")
            step_transition_refs = {
                str(value).strip()
                for value in raw_step_transition_refs or []
                if str(value).strip()
            } if isinstance(raw_step_transition_refs, list) else set()
            plan_transition_refs[plan_step_id] = step_transition_refs
            plan_material_event_kind[plan_step_id] = event_kind
            exact_processing_workstation = (
                workstation in _CONTROLLED_MATERIAL_PROCESSING_WORKSTATIONS
            )
            mapped_macro_operations = [
                str(
                    research_macro_by_id[source_id].get(
                        "操作", research_macro_by_id[source_id].get("operation", "")
                    )
                )
                for source_id in self._source_macro_step_ids(plan_step)
                if source_id in research_macro_by_id
            ]
            plan_step_intent_text = " ".join(
                str(plan_step.get(field) or "")
                for field in ("objective", "operation_intent")
            )
            synthesis_processing_step = bool(
                not self._active_semantic_analysis
                and
                workstation in _CONDITIONAL_REACTION_PROCESSING_WORKSTATIONS
                and any(
                    _FROZEN_SYNTHESIS_OPERATION_PATTERN.search(operation)
                    for operation in mapped_macro_operations
                )
                and _DEVICE_STATE_CHANGE_INTENT_PATTERN.search(
                    plan_step_intent_text
                )
            )
            controlled_processing_step = bool(
                exact_processing_workstation or synthesis_processing_step
            )
            declares_material_event = event_kind not in {"", "none"}
            if controlled_processing_step and not declares_material_event:
                issues.append(
                    {
                        "code": "processing_step_missing_material_event",
                        "scope": "device_local_quantity",
                        "message": (
                            f"controlled processing workstation step {plan_step_id}/"
                            f"{workstation} 必须显式声明非 none "
                            "material_event_kind。"
                        ),
                    }
                )
            if declares_material_event and event_kind not in allowed_transition_kinds:
                issues.append(
                    {
                        "code": "invalid_plan_material_event_kind",
                        "scope": "device_local_quantity",
                        "message": (
                            f"device plan step {plan_step_id} 的 material_event_kind="
                            f"{event_kind} 不在受控枚举中。"
                        ),
                    }
                )
            if (controlled_processing_step or declares_material_event) and not step_transition_refs:
                issues.append(
                    {
                        "code": "processing_step_missing_transition_refs",
                        "scope": "device_local_quantity",
                        "message": (
                            f"material-processing device plan step {plan_step_id} "
                            "缺少 material_transition_ids。"
                        ),
                    }
                )
            unknown_transition_refs = step_transition_refs - set(
                transition_plan_refs_by_id
            )
            if unknown_transition_refs:
                issues.append(
                    {
                        "code": "invalid_plan_material_transition_ref",
                        "scope": "device_local_quantity",
                        "message": (
                            f"device plan step {plan_step_id} 引用了不存在 transition："
                            f"{sorted(unknown_transition_refs)}。"
                        ),
                    }
                )
            for transition_id in step_transition_refs & set(
                transition_plan_refs_by_id
            ):
                if plan_step_id not in transition_plan_refs_by_id[transition_id]:
                    issues.append(
                        {
                            "code": "plan_step_transition_missing_reverse_ref",
                            "scope": "device_local_quantity",
                            "message": (
                                f"device plan step {plan_step_id} 引用 transition "
                                f"{transition_id}，但 transition.source_plan_steps "
                                "未反向包含该 step。"
                            ),
                        }
                    )
                if (
                    declares_material_event
                    and transition_kind_by_id.get(transition_id) != event_kind
                ):
                    issues.append(
                        {
                            "code": "plan_step_transition_kind_mismatch",
                            "scope": "device_local_quantity",
                            "message": (
                                f"device plan step {plan_step_id} 的 material_event_kind "
                                f"与 transition {transition_id} 不一致。"
                            ),
                        }
                    )
        for transition_id, source_plan_refs in transition_plan_refs_by_id.items():
            for source_plan_step in source_plan_refs & plan_step_ids:
                if (
                    transition_id not in plan_transition_refs.get(source_plan_step, set())
                    or plan_material_event_kind.get(source_plan_step, "")
                    in {"", "none"}
                ):
                    issues.append(
                        {
                            "code": "material_transition_missing_step_backref",
                            "scope": "device_local_quantity",
                            "message": (
                                f"material transition {transition_id} 声明 source plan step "
                                f"{source_plan_step}，但该 step 未双向声明"
                                " material_event_kind/material_transition_ids。"
                            ),
                        }
                    )
        # Do not let an LLM erase a reaction event merely by rewriting the
        # mutable Device objective/intent to the generic word "stir".  When a
        # frozen synthesis macro is mapped to Device steps, the mapped group
        # must contain at least
        # one explicit, bidirectionally linked material transition.  This is a
        # group invariant: preparatory homogenization steps may remain
        # identity-preserving/none when another mapped step records the actual
        # reaction state change.
        for macro_id, research_macro in research_macro_by_id.items():
            frozen_operation = str(
                research_macro.get(
                    "操作", research_macro.get("operation", "")
                )
            )
            if self._active_semantic_analysis:
                assessment = self._semantic_assessment(macro_id)
                category_requirements = {
                    category: self._stations_for_semantic_category(category)
                    for category in {
                        str(item.get("category") or "").strip()
                        for item in assessment.get(
                            "required_capabilities", []
                        )
                        or []
                        if isinstance(item, dict)
                    }
                    if category
                    in {"reaction", "drying", "calcination", "purification"}
                }
                frozen_processing_text = "LLM semantic analysis"
            else:
                frozen_processing_text = _frozen_required_processing_text(
                    research_macro
                )
                category_requirements = (
                    _required_state_change_workstation_categories(
                        frozen_processing_text
                    )
                )
            if not category_requirements:
                continue
            frozen_macro_text = _json_text(research_macro)
            external_distribution_only = bool(
                not self._active_semantic_analysis
                and
                _FROZEN_EXTERNAL_MATERIAL_PATTERN.search(frozen_macro_text)
                and _FROZEN_EXTERNAL_DISTRIBUTION_OPERATION_PATTERN.search(
                    frozen_operation
                )
                and not _FROZEN_HARD_STATE_CHANGE_OPERATION_PATTERN.search(
                    frozen_processing_text
                )
            )
            if external_distribution_only:
                continue
            mapped_material_steps = [
                plan_step_id
                for plan_step_id, plan_step in plan_step_by_id.items()
                if macro_id in self._source_macro_step_ids(plan_step)
            ]
            if not mapped_material_steps:
                continue
            missing_categories = [
                category
                for category, allowed_stations in category_requirements.items()
                if not any(
                    plan_material_event_kind.get(plan_step_id, "")
                    == "state_change"
                    and self._truth_workstation_code(
                        plan_step_by_id[plan_step_id].get("workstation")
                    ) in allowed_stations
                    and any(
                        transition_kind_by_id.get(transition_id)
                        == "state_change"
                        and plan_step_id
                        in transition_plan_refs_by_id.get(
                            transition_id, set()
                        )
                        and macro_id
                        in transition_macro_refs_by_id.get(
                            transition_id, set()
                        )
                        for transition_id in plan_transition_refs.get(
                            plan_step_id, set()
                        )
                    )
                    for plan_step_id in mapped_material_steps
                )
            ]
            if not missing_categories:
                continue
            issues.append(
                {
                    "code": "frozen_synthesis_macro_missing_material_transition",
                    "scope": "device_local_quantity",
                    "missing_processing_categories": missing_categories,
                    "message": (
                        f"冻结物料状态变化 macro step {macro_id}（{frozen_operation}）的"
                        "整个 Device 映射组没有逐类完成真源工作站的双向"
                        " state_change transition；不得遗漏复合处理类别。缺失="
                        f"{missing_categories}。"
                    ),
                }
            )
        child_transition_plan_refs: Dict[str, Set[str]] = {
            child_id: set().union(
                *(
                    transition_plan_refs_by_id.get(transition_id, set())
                    for transition_id in transition_ids
                )
            )
            for child_id, transition_ids in child_transition_ids.items()
            if transition_ids
        }
        normalized["material_transitions"] = normalized_transitions

        ledger = normalized.get("material_ledger", {})
        if not isinstance(ledger, dict):
            ledger = {}
            issues.append(
                {
                    "code": "invalid_material_ledger",
                    "scope": "device_local_quantity",
                    "message": "material_ledger 必须是 object。",
                }
            )
        ledger = copy.deepcopy(ledger)
        entries = ledger.get("entries", [])
        if not isinstance(entries, list):
            entries = []
            issues.append(
                {
                    "code": "invalid_material_ledger_entries",
                    "scope": "device_local_quantity",
                    "message": "material_ledger.entries 必须是 array。",
                }
            )
        normalized_entries: List[Dict[str, Any]] = []
        seen_entry_ids: Set[str] = set()
        theoretical_human_issues: List[Dict[str, Any]] = []
        for index, raw in enumerate(entries, start=1):
            if not isinstance(raw, dict):
                issues.append(
                    {
                        "code": "invalid_material_ledger_entry",
                        "scope": "device_local_quantity",
                        "message": f"material_ledger.entries[{index}] 必须是 object。",
                    }
                )
                continue
            item = copy.deepcopy(raw)
            entry_id = str(item.get("entry_id") or item.get("transaction_id") or f"ml_{index:03d}")
            item["entry_id"] = entry_id
            entry_batch_id = str(item.get("batch_id") or "").strip()
            expected_quantity_mode = (
                "whole_batch"
                if entry_batch_id in whole_batch_batch_ids
                else "numeric_inventory"
            )
            entry_quantity_mode = str(
                item.get("quantity_mode") or expected_quantity_mode
            ).strip()
            item["quantity_mode"] = entry_quantity_mode
            if entry_quantity_mode != expected_quantity_mode:
                issues.append(
                    {
                        "code": "ledger_batch_quantity_mode_mismatch",
                        "scope": "device_local_quantity",
                        "message": (
                            f"ledger entry {entry_id} quantity_mode={entry_quantity_mode} "
                            f"与 batch {entry_batch_id} 的 {expected_quantity_mode} 不一致。"
                        ),
                    }
                )
            is_whole_batch_entry = entry_quantity_mode == "whole_batch"
            raw_source_kind = str(item.get("source_kind") or "").strip()
            raw_source_refs = item.get("source_refs")
            raw_calculation = str(
                item.get("calculation", item.get("formula", "")) or ""
            ).strip()
            item.setdefault("source_kind", "device_operational")
            item.setdefault("source_refs", [])
            item.setdefault("calculation", item.get("formula", ""))
            transition_requirement = nonroot_batch_requirements.get(
                entry_batch_id
            )
            if transition_requirement is not None:
                raw_processing_refs = item.get("processing_step_refs")
                processing_refs = {
                    str(value).strip()
                    for value in raw_processing_refs or []
                    if str(value).strip()
                } if isinstance(raw_processing_refs, list) else set()
                if not processing_refs:
                    issues.append(
                        {
                            "code": "missing_ledger_processing_step_refs",
                            "scope": "device_local_quantity",
                            "message": (
                                f"ledger entry {entry_id} 对应 non-root batch "
                                f"{entry_batch_id}，但缺少 processing_step_refs。"
                            ),
                        }
                    )
                elif processing_refs != child_transition_plan_refs.get(
                    entry_batch_id, set()
                ):
                    issues.append(
                        {
                            "code": "ledger_processing_step_refs_mismatch",
                            "scope": "device_local_quantity",
                            "message": (
                                f"ledger entry {entry_id} 的 processing_step_refs "
                                f"与 batch {entry_batch_id} 子 transition 处理谱系"
                                "不完全一致。"
                            ),
                        }
                    )
            if entry_id in seen_entry_ids:
                issues.append(
                    {
                        "code": "duplicate_material_transaction",
                        "scope": "device_local_quantity",
                        "message": f"物料台账 transaction/entry {entry_id} 被重复计量。",
                    }
                )
            seen_entry_ids.add(entry_id)
            theoretical_fields = self._theoretical_availability_fields(item)
            if theoretical_fields:
                declared_theoretical = item.get("theoretical_quantity")
                theoretical_record: Dict[str, Any] = (
                    copy.deepcopy(declared_theoretical)
                    if isinstance(declared_theoretical, dict)
                    else {}
                )
                if declared_theoretical not in (None, "") and not isinstance(
                    declared_theoretical, dict
                ):
                    theoretical_record["declared"] = copy.deepcopy(
                        declared_theoretical
                    )
                field_aliases = {
                    "produced": ("produced", "produced_quantity"),
                    "reserved": ("reserved", "reserved_quantity"),
                    "balance": ("balance",),
                }
                for field_name in theoretical_fields:
                    aliases = field_aliases[field_name]
                    raw_value = next(
                        (item.get(alias) for alias in aliases if alias in item),
                        None,
                    )
                    theoretical_record[field_name] = (
                        self._normalize_theoretical_quantity_value(raw_value)
                    )
                    for alias in aliases:
                        item.pop(alias, None)
                theoretical_record["provenance"] = "theoretical_quantity"
                theoretical_record["source_kind"] = raw_source_kind or item.get(
                    "source_kind", "derived"
                )
                theoretical_record["source_refs"] = copy.deepcopy(
                    item.get("source_refs", [])
                )
                theoretical_record["calculation"] = item.get("calculation", "")
                item["theoretical_quantity"] = theoretical_record
                item["quantity_provenance"] = "theoretical_quantity"
                theoretical_human_issues.append(
                    {
                        "code": "unknown_yield",
                        "scope": "human_review_required",
                        "entry_id": entry_id,
                        "material_id": str(
                            item.get("material_id", item.get("material", ""))
                        ),
                        "batch_id": str(item.get("batch_id", "")),
                        "theoretical_fields": list(theoretical_fields),
                        "message": (
                            f"ledger entry {entry_id} 的 {theoretical_fields} 仅有"
                            "理论/名义当量依据，不能作为实际可用产量；必须测得实际收率"
                            "或由人工确认后才能资助下游消费。"
                        ),
                    }
                )
            produced, produced_dim, produced_unit = self._canonical_quantity(
                item.get("produced", item.get("produced_quantity"))
            )
            consumed, consumed_dim, _ = self._canonical_quantity(
                item.get("consumed", item.get("consumed_quantity"))
            )
            reserved, reserved_dim, _ = self._canonical_quantity(
                item.get("reserved", item.get("reserved_quantity"))
            )
            declared_balance, balance_dim, _ = self._canonical_quantity(
                item.get("balance")
            )
            raw_ledger_quantities = {
                "produced": item.get("produced", item.get("produced_quantity")),
                "consumed": item.get("consumed", item.get("consumed_quantity")),
                "reserved": item.get("reserved", item.get("reserved_quantity")),
                "balance": item.get("balance"),
            }
            if is_whole_batch_entry and any(
                value not in (None, "") for value in raw_ledger_quantities.values()
            ):
                issues.append(
                    {
                        "code": "whole_batch_ledger_must_not_fake_numeric_inventory",
                        "scope": "device_local_quantity",
                        "message": (
                            f"ledger entry {entry_id} 是 whole_batch；不得声明"
                            " produced/consumed/reserved/balance 数值。"
                        ),
                    }
                )
            parsed_ledger_quantities = {
                "produced": produced,
                "consumed": consumed,
                "reserved": reserved,
                "balance": declared_balance,
            }
            invalid_finite_fields = [
                field_name
                for field_name, raw_quantity in raw_ledger_quantities.items()
                if raw_quantity not in (None, "")
                and parsed_ledger_quantities[field_name] is None
            ]
            if invalid_finite_fields:
                issues.append(
                    {
                        "code": "ledger_quantity_not_finite_or_invalid",
                        "scope": "device_local_quantity",
                        "message": (
                            f"ledger entry {entry_id} 的 {invalid_finite_fields} "
                            "必须是有限且带可识别单位的数量。"
                        ),
                    }
                )
            negative_fields = [
                field_name
                for field_name, parsed_quantity in parsed_ledger_quantities.items()
                if parsed_quantity is not None and parsed_quantity < 0
            ]
            if negative_fields:
                issues.append(
                    {
                        "code": "negative_ledger_quantity",
                        "scope": "device_local_quantity",
                        "message": (
                            f"ledger entry {entry_id} 的 {negative_fields} 不得为负；"
                            "balance 可为零，库存流量不能靠负数抵消。"
                        ),
                    }
                )
            zero_flow_fields = [
                field_name
                for field_name in ("produced", "consumed")
                if raw_ledger_quantities[field_name] not in (None, "")
                and parsed_ledger_quantities[field_name] == 0
            ]
            if zero_flow_fields:
                issues.append(
                    {
                        "code": "nonpositive_ledger_flow_quantity",
                        "scope": "device_local_quantity",
                        "message": (
                            f"ledger entry {entry_id} 显式声明的 {zero_flow_fields} "
                            "必须为正；无流量时应省略对应字段。"
                        ),
                    }
                )
            has_ledger_quantity = any(
                value is not None
                for value in (produced, consumed, reserved, declared_balance)
            ) or isinstance(item.get("consumers"), list)
            if has_ledger_quantity:
                sample_lineage = item.get("sample_id") or item.get("sample_ids")
                if sample_lineage in (None, "", []):
                    issues.append(
                        {
                            "code": "missing_ledger_sample_lineage",
                            "scope": "device_local_quantity",
                            "message": (
                                f"ledger entry {entry_id} 缺少 sample_id/sample_ids。"
                            ),
                        }
                    )
                if raw_source_kind not in {
                    "research",
                    "derived",
                    "device_operational",
                }:
                    issues.append(
                        {
                            "code": "missing_ledger_quantity_source",
                            "scope": "device_local_quantity",
                            "message": f"ledger entry {entry_id} 缺少合法 source_kind。",
                        }
                    )
                if not isinstance(raw_source_refs, list) or not raw_source_refs:
                    issues.append(
                        {
                            "code": "missing_ledger_quantity_source_refs",
                            "scope": "device_local_quantity",
                            "message": f"ledger entry {entry_id} 缺少 source_refs。",
                        }
                    )
                if not raw_calculation:
                    issues.append(
                        {
                            "code": "missing_ledger_quantity_calculation",
                            "scope": "device_local_quantity",
                            "message": f"ledger entry {entry_id} 缺少 calculation/derivation。",
                        }
                    )
            active_dimensions = {
                dim
                for value, dim in (
                    (produced, produced_dim),
                    (consumed, consumed_dim),
                    (reserved, reserved_dim),
                    (declared_balance, balance_dim),
                )
                if value is not None
            }
            invalid_dimension_fields = [
                name
                for name, value, dimension in (
                    ("produced", produced, produced_dim),
                    ("consumed", consumed, consumed_dim),
                    ("reserved", reserved, reserved_dim),
                    ("balance", declared_balance, balance_dim),
                )
                if value is not None
                and not self._quantity_dimension_is_auditable(dimension)
            ]
            if invalid_dimension_fields:
                issues.append(
                    {
                        "code": "ledger_quantity_unit_missing_or_unknown",
                        "scope": "device_local_quantity",
                        "message": (
                            f"ledger entry {entry_id} 的 {invalid_dimension_fields} "
                            "缺少可审计单位。"
                        ),
                    }
                )
            non_inventory_fields = [
                name
                for name, value, dimension in (
                    ("produced", produced, produced_dim),
                    ("consumed", consumed, consumed_dim),
                    ("reserved", reserved, reserved_dim),
                    ("balance", declared_balance, balance_dim),
                )
                if value is not None
                and self._quantity_dimension_is_auditable(dimension)
                and not self._quantity_dimension_is_inventory(dimension)
            ]
            if non_inventory_fields:
                issues.append(
                    {
                        "code": "ledger_quantity_requires_extensive_dimension",
                        "scope": "device_local_quantity",
                        "message": (
                            f"ledger entry {entry_id} 的 {non_inventory_fields} 必须是"
                            " amount/mass/volume；concentration 不能做库存加减。"
                        ),
                    }
                )
            if len(active_dimensions) > 1:
                issues.append(
                    {
                        "code": "incompatible_quantity_units",
                        "scope": "device_local_quantity",
                        "message": (
                            f"物料 {entry_id} 的 produced/consumed/reserved/balance "
                            f"单位维度不一致：{sorted(active_dimensions)}。"
                        ),
                    }
                )
            elif produced is not None and consumed is not None:
                expected_balance = produced - consumed - (reserved or 0.0)
                if expected_balance < -max(1e-9, abs(produced) * 1e-6):
                    issues.append(
                        {
                            "code": "material_quantity_insufficient",
                            "scope": "device_local_quantity",
                            "message": (
                                f"物料 {entry_id} 生产量 {produced:g} {produced_unit} "
                                f"小于消费量+预留量 {consumed + (reserved or 0.0):g} "
                                f"{produced_unit}。"
                            ),
                        }
                    )
                if declared_balance is not None and abs(declared_balance - expected_balance) > max(
                    1e-9, abs(produced) * 1e-6
                ):
                    issues.append(
                        {
                            "code": "material_balance_mismatch",
                            "scope": "device_local_quantity",
                            "message": (
                                f"物料 {entry_id} balance={declared_balance} 与计算值 "
                                f"{expected_balance} 不一致。"
                            ),
                        }
                    )
            normalized_entries.append(item)

        # Aggregate independently of entry_id.  A common failure mode is to
        # repeat the same 0.180 mmol production record for three consumers;
        # summing each row's `produced` would silently fabricate 0.540 mmol.
        # Production is therefore unique per (material,batch,production source),
        # while every consumer draw is accumulated.
        aggregate: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for item in normalized_entries:
            material_id = str(
                item.get("material_id")
                or item.get("material")
                or item.get("物料编号")
                or item.get("物料")
                or ""
            ).strip()
            batch_id = str(
                item.get("batch_id")
                or item.get("source_batch_id")
                or item.get("批次编号")
                or ""
            ).strip()
            produced, produced_dim, produced_unit = self._canonical_quantity(
                item.get("produced", item.get("produced_quantity"))
            )
            consumed, consumed_dim, consumed_unit = self._canonical_quantity(
                item.get("consumed", item.get("consumed_quantity"))
            )
            reserved, reserved_dim, reserved_unit = self._canonical_quantity(
                item.get("reserved", item.get("reserved_quantity"))
            )
            has_quantity = any(
                value is not None for value in (produced, consumed, reserved)
            )
            if has_quantity and (not material_id or not batch_id):
                issues.append(
                    {
                        "code": "missing_material_lineage",
                        "scope": "device_local_quantity",
                        "message": (
                            f"ledger entry {item.get('entry_id')} 缺少 material_id 或 "
                            "batch_id，无法聚合生产/消费。"
                        ),
                    }
                )
            key = (material_id or "<missing-material>", batch_id or "<missing-batch>")
            group = aggregate.setdefault(
                key,
                {
                    "material_id": material_id,
                    "batch_id": batch_id,
                    "dimension": "",
                    "unit": "",
                    "productions": {},
                    "consumed": 0.0,
                    "reserved": 0.0,
                    "consumers": set(),
                    "consumer_draws": {},
                    "allocation_ids": set(),
                    "samples": set(),
                    "entry_ids": [],
                },
            )
            group["entry_ids"].append(str(item.get("entry_id", "")))
            if str(item.get("quantity_mode") or "") == "whole_batch":
                group["consumers"].update(
                    str(value).strip()
                    for value in item.get("consumer_ids", []) or []
                    if str(value).strip()
                )
            sample_id = str(item.get("sample_id") or item.get("样品编号") or "").strip()
            if sample_id:
                group["samples"].add(sample_id)
            sample_ids = item.get("sample_ids")
            if isinstance(sample_ids, list):
                group["samples"].update(
                    str(value).strip()
                    for value in sample_ids
                    if str(value).strip()
                )
            consumer_id = str(
                item.get("consumer_id")
                or item.get("consumer")
                or item.get("消费方")
                or ""
            ).strip()
            nested_consumers = item.get("consumers")
            nested_ids: List[str] = []
            nested_draws: List[Tuple[str, float, str, str, str]] = []
            nested_total = 0.0
            nested_dim = ""
            nested_quantity_count = 0
            if isinstance(nested_consumers, list):
                for consumer_index, consumer in enumerate(nested_consumers):
                    if not isinstance(consumer, dict):
                        continue
                    cid = str(
                        consumer.get("consumer_id")
                        or consumer.get("sample_id")
                        or consumer.get("id")
                        or ""
                    ).strip()
                    amount, dimension, allocation_unit = self._canonical_quantity(
                        consumer.get("quantity", consumer.get("allocation"))
                    )
                    if cid:
                        nested_ids.append(cid)
                    else:
                        issues.append(
                            {
                                "code": "missing_nested_consumer_id",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"ledger entry {item.get('entry_id')} 的 consumer "
                                    "allocation 缺少 consumer_id/sample_id。"
                                ),
                            }
                        )
                    if amount is not None:
                        if not self._quantity_dimension_is_auditable(dimension):
                            issues.append(
                                {
                                    "code": "nested_consumer_unit_missing_or_unknown",
                                    "scope": "device_local_quantity",
                                    "message": (
                                        f"ledger entry {item.get('entry_id')} 对 consumer "
                                        f"{cid or '?'} 的 allocation 缺少可审计单位。"
                                    ),
                                }
                            )
                        elif not self._quantity_dimension_is_inventory(dimension):
                            issues.append(
                                {
                                    "code": "nested_consumer_requires_extensive_quantity",
                                    "scope": "device_local_quantity",
                                    "message": (
                                        f"ledger entry {item.get('entry_id')} 对 consumer "
                                        f"{cid or '?'} 的 allocation 必须是 "
                                        "amount/mass/volume。"
                                    ),
                                }
                            )
                        if amount <= 0:
                            issues.append(
                                {
                                    "code": "nonpositive_ledger_consumer_allocation",
                                    "scope": "device_local_quantity",
                                    "message": (
                                        f"ledger entry {item.get('entry_id')} 对 consumer "
                                        f"{cid or '?'} 的 allocation 必须为有限正数。"
                                    ),
                                }
                            )
                        if nested_dim and dimension != nested_dim:
                            issues.append(
                                {
                                    "code": "nested_consumer_unit_mismatch",
                                    "scope": "device_local_quantity",
                                    "message": (
                                        f"ledger entry {item.get('entry_id')} 的 consumer "
                                        "allocations 使用了不可比较的单位维度。"
                                    ),
                                }
                            )
                        nested_total += amount
                        nested_dim = nested_dim or dimension
                        nested_quantity_count += 1
                        if cid:
                            explicit_allocation_id = str(
                                consumer.get("allocation_id")
                                or consumer.get("transaction_id")
                                or ""
                            ).strip()
                            nested_draws.append(
                                (
                                    cid,
                                    amount,
                                    dimension,
                                    allocation_unit,
                                    explicit_allocation_id,
                                )
                            )
                    else:
                        issues.append(
                            {
                                "code": "missing_nested_consumer_quantity",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"ledger entry {item.get('entry_id')} 的 consumer "
                                    "allocation 缺少 quantity/allocation。"
                                ),
                            }
                        )
                if nested_quantity_count:
                    if consumed is not None:
                        if consumed_dim != nested_dim:
                            issues.append(
                                {
                                    "code": "consumer_allocation_unit_mismatch",
                                    "scope": "device_local_quantity",
                                    "message": (
                                        f"ledger entry {item.get('entry_id')} 的 declared "
                                        "consumed 与 consumer allocations 单位维度不一致。"
                                    ),
                                }
                            )
                        elif abs(consumed - nested_total) > max(
                            1e-9, abs(nested_total) * 1e-6
                        ):
                            issues.append(
                                {
                                    "code": "consumer_allocation_sum_mismatch",
                                    "scope": "device_local_quantity",
                                    "message": (
                                        f"ledger entry {item.get('entry_id')} declared consumed "
                                        f"{consumed:g} != consumer allocations sum "
                                        f"{nested_total:g}；聚合按实际 allocations 计。"
                                    ),
                                }
                            )
                    consumed, consumed_dim = nested_total, nested_dim
            if nested_quantity_count:
                consumer_draws = nested_draws
            elif consumer_id and consumed is not None:
                if consumed <= 0:
                    issues.append(
                        {
                            "code": "nonpositive_ledger_consumer_allocation",
                            "scope": "device_local_quantity",
                            "message": (
                                f"ledger entry {item.get('entry_id')} 对 consumer "
                                f"{consumer_id} 的 consumed 必须为有限正数。"
                            ),
                        }
                    )
                consumer_draws = [
                    (
                        consumer_id,
                        consumed,
                        consumed_dim,
                        consumed_unit,
                        str(item.get("allocation_id") or "").strip(),
                    )
                ]
            else:
                consumer_draws = []
            all_consumer_ids = [draw[0] for draw in consumer_draws]
            if consumed is not None and not all_consumer_ids:
                issues.append(
                    {
                        "code": "missing_consumer_lineage",
                        "scope": "device_local_quantity",
                        "message": (
                            f"ledger entry {item.get('entry_id')} 有消费量但缺少 consumer_id。"
                        ),
                    }
                )
            for cid, draw, draw_dim, draw_unit, allocation_id in consumer_draws:
                if allocation_id and allocation_id in group["allocation_ids"]:
                    issues.append(
                        {
                            "code": "duplicate_consumer_allocation",
                            "scope": "device_local_quantity",
                            "message": (
                                f"物料 {key[0]}/{key[1]} 的 allocation_id "
                                f"{allocation_id} 被重复计量。"
                            ),
                        }
                    )
                if allocation_id:
                    group["allocation_ids"].add(allocation_id)
                group["consumers"].add(cid)
                existing_draw = group["consumer_draws"].get(cid)
                if existing_draw and existing_draw["dimension"] != draw_dim:
                    issues.append(
                        {
                            "code": "consumer_draw_unit_mismatch",
                            "scope": "device_local_quantity",
                            "message": (
                                f"物料 {key[0]}/{key[1]} 对 consumer {cid} 的"
                                "分次取用单位维度不一致。"
                            ),
                        }
                    )
                elif existing_draw:
                    existing_draw["value"] += draw
                else:
                    group["consumer_draws"][cid] = {
                        "value": draw,
                        "dimension": draw_dim,
                        "unit": draw_unit,
                    }

            dimensions = {
                dim
                for value, dim in (
                    (produced, produced_dim),
                    (consumed, consumed_dim),
                    (reserved, reserved_dim),
                )
                if value is not None
            }
            if len(dimensions) > 1 or (
                group["dimension"] and dimensions and group["dimension"] not in dimensions
            ):
                issues.append(
                    {
                        "code": "aggregate_unit_dimension_mismatch",
                        "scope": "device_local_quantity",
                        "message": (
                            f"物料 {key[0]}/{key[1]} 聚合时出现不可比较单位维度 "
                            f"{sorted(dimensions | ({group['dimension']} if group['dimension'] else set()))}。"
                        ),
                    }
                )
            if dimensions and not group["dimension"]:
                group["dimension"] = sorted(dimensions)[0]
                group["unit"] = produced_unit or consumed_unit or reserved_unit

            if produced is not None:
                # One material/batch owns one production total.  Separate
                # physical productions must have separate batch ids; otherwise
                # repeated rows (even with different transaction/source ids)
                # would fabricate inventory, exactly the A01 0.180 mmol bug.
                production_source = "unique_material_batch_production"
                if production_source in group["productions"]:
                    issues.append(
                        {
                            "code": "duplicate_production_record",
                            "scope": "device_local_quantity",
                            "message": (
                                f"物料 {key[0]}/{key[1]} 的同一生产来源被重复写入；"
                                "生产量只计一次。"
                            ),
                        }
                    )
                else:
                    group["productions"][production_source] = produced
            if consumed is not None:
                group["consumed"] += consumed
            if reserved is not None:
                group["reserved"] += reserved

            transfer_value = item.get(
                "transfer_quantity",
                item.get("single_transfer", item.get("draw_quantity")),
            )
            transfer, transfer_dim, _ = self._canonical_quantity(transfer_value)
            transfer_limit, limit_dim, _ = self._canonical_quantity(
                item.get("max_single_transfer")
            )
            if transfer_value not in (None, "") and (
                transfer is None or transfer <= 0
            ):
                issues.append(
                    {
                        "code": "invalid_transfer_quantity_domain",
                        "scope": "device_local_quantity",
                        "message": (
                            f"ledger entry {item.get('entry_id')} 的 transfer quantity "
                            "必须为有限正数。"
                        ),
                    }
                )
            if item.get("max_single_transfer") not in (None, "") and (
                transfer_limit is None or transfer_limit <= 0
            ):
                issues.append(
                    {
                        "code": "invalid_transfer_limit_domain",
                        "scope": "device_local_quantity",
                        "message": (
                            f"ledger entry {item.get('entry_id')} 的 max_single_transfer "
                            "必须为有限正数。"
                        ),
                    }
                )
            if (
                transfer is not None
                and transfer_limit is not None
                and transfer_dim == limit_dim
                and transfer > transfer_limit + max(1e-9, abs(transfer_limit) * 1e-6)
            ):
                issues.append(
                    {
                        "code": "single_transfer_limit_exceeded",
                        "scope": "device_local_quantity",
                        "message": f"ledger entry {item.get('entry_id')} 单次转移超过设备上限。",
                    }
                )
            volume_after, volume_dim, _ = self._canonical_quantity(
                item.get("container_volume_after", item.get("volume_after"))
            )
            capacity, capacity_dim, _ = self._canonical_quantity(
                item.get("container_capacity")
            )
            raw_volume_after = item.get(
                "container_volume_after", item.get("volume_after")
            )
            if raw_volume_after not in (None, "") and (
                volume_after is None or volume_after < 0
            ):
                issues.append(
                    {
                        "code": "invalid_container_volume_domain",
                        "scope": "device_local_quantity",
                        "message": (
                            f"ledger entry {item.get('entry_id')} 的容器内体积必须"
                            "为有限非负数。"
                        ),
                    }
                )
            if item.get("container_capacity") not in (None, "") and (
                capacity is None or capacity <= 0
            ):
                issues.append(
                    {
                        "code": "invalid_container_capacity_domain",
                        "scope": "device_local_quantity",
                        "message": (
                            f"ledger entry {item.get('entry_id')} 的容器容量必须"
                            "为有限正数。"
                        ),
                    }
                )
            if (
                volume_after is not None
                and capacity is not None
                and volume_dim == capacity_dim
                and volume_after > capacity + max(1e-9, abs(capacity) * 1e-6)
            ):
                issues.append(
                    {
                        "code": "container_capacity_exceeded",
                        "scope": "device_local_quantity",
                        "message": f"ledger entry {item.get('entry_id')} 容器累计体积超过容量。",
                    }
                )

        aggregate_records: List[Dict[str, Any]] = []
        for key, group in aggregate.items():
            produced_total = sum(group["productions"].values())
            consumed_total = float(group["consumed"])
            reserved_total = float(group["reserved"])
            balance = produced_total - consumed_total - reserved_total
            if balance < -max(1e-9, abs(produced_total) * 1e-6):
                issues.append(
                    {
                        "code": "aggregate_material_quantity_insufficient",
                        "scope": "device_local_quantity",
                        "message": (
                            f"物料 {key[0]}/{key[1]} 聚合生产量 {produced_total:g} "
                            f"{group['unit']} 小于消费+预留 {consumed_total + reserved_total:g} "
                            f"{group['unit']}。"
                        ),
                    }
                )
            aggregate_records.append(
                {
                    "material_id": group["material_id"],
                    "batch_id": group["batch_id"],
                    "sample_ids": sorted(group["samples"]),
                    "consumer_ids": sorted(group["consumers"]),
                    "consumer_allocations": {
                        consumer_id: {
                            "value": allocation["value"],
                            "unit": allocation["unit"],
                            "dimension": allocation["dimension"],
                        }
                        for consumer_id, allocation in sorted(
                            group["consumer_draws"].items()
                        )
                    },
                    "produced": {"value": produced_total, "unit": group["unit"]},
                    "consumed": {"value": consumed_total, "unit": group["unit"]},
                    "reserved": {"value": reserved_total, "unit": group["unit"]},
                    "balance": {"value": balance, "unit": group["unit"]},
                    "entry_ids": group["entry_ids"],
                    "quantity_mode": (
                        "whole_batch"
                        if key[1] in whole_batch_batch_ids
                        else "numeric_inventory"
                    ),
                }
            )

        # The batch plan and material ledger are one lineage contract.  Two
        # independently valid sidecars that disagree on batch/sample/consumer
        # identity must not pass the quantity gate.
        aggregate_by_batch: Dict[str, List[Dict[str, Any]]] = {}
        for record in aggregate_records:
            record_batch_id = str(record.get("batch_id") or "")
            if record_batch_id:
                aggregate_by_batch.setdefault(record_batch_id, []).append(record)
        for transition_id, edge_flows in transition_edge_flows.items():
            transition_consumer_id = f"material_transition:{transition_id}"
            for parent_batch_id, expected_input in edge_flows.get(
                "input", {}
            ).items():
                parent_batch = batch_by_id.get(parent_batch_id, {})
                parent_material_id = str(
                    parent_batch.get("material_id") or ""
                ).strip()
                parent_records = aggregate_by_batch.get(parent_batch_id, [])
                if parent_material_id:
                    parent_records = [
                        record
                        for record in parent_records
                        if str(record.get("material_id") or "").strip()
                        == parent_material_id
                    ]
                transition_draws = [
                    record.get("consumer_allocations", {}).get(
                        transition_consumer_id
                    )
                    for record in parent_records
                    if isinstance(record.get("consumer_allocations"), dict)
                    and isinstance(
                        record.get("consumer_allocations", {}).get(
                            transition_consumer_id
                        ),
                        dict,
                    )
                ]
                if len(transition_draws) != 1:
                    issues.append(
                        {
                            "code": "transition_input_ledger_draw_missing_or_ambiguous",
                            "scope": "device_local_quantity",
                            "message": (
                                f"parent batch {parent_batch_id} 必须在 ledger 中"
                                f"有且仅有一条 consumer_id={transition_consumer_id} "
                                "的实际扣减。"
                            ),
                        }
                    )
                else:
                    draw = transition_draws[0]
                    if not self._canonical_quantities_equal(
                        float(draw.get("value", 0.0)),
                        str(draw.get("dimension") or ""),
                        float(expected_input["value"]),
                        str(expected_input["dimension"]),
                    ):
                        issues.append(
                            {
                                "code": "transition_input_ledger_draw_mismatch",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"parent batch {parent_batch_id} 对 transition "
                                    f"{transition_id} 的 ledger draw 与 input_allocations "
                                    "不一致。"
                                ),
                            }
                        )
            for child_batch_id, expected_output in edge_flows.get(
                "output", {}
            ).items():
                expected_source_ref = f"material_transition:{transition_id}"
                production_entries = []
                for entry in normalized_entries:
                    if str(entry.get("batch_id") or "").strip() != child_batch_id:
                        continue
                    produced_value, produced_dimension, _ = self._canonical_quantity(
                        entry.get("produced", entry.get("produced_quantity"))
                    )
                    source_refs = entry.get("source_refs")
                    if (
                        produced_value is not None
                        and isinstance(source_refs, list)
                        and expected_source_ref in source_refs
                    ):
                        production_entries.append(
                            (produced_value, produced_dimension)
                        )
                if len(production_entries) != 1:
                    issues.append(
                        {
                            "code": "transition_output_ledger_source_missing_or_ambiguous",
                            "scope": "device_local_quantity",
                            "message": (
                                f"child batch {child_batch_id} 必须有且仅有一条"
                                f" produced ledger entry，source_refs 含 {expected_source_ref}。"
                            ),
                        }
                    )
                else:
                    produced_value, produced_dimension = production_entries[0]
                    if not self._canonical_quantities_equal(
                        produced_value,
                        produced_dimension,
                        float(expected_output["value"]),
                        str(expected_output["dimension"]),
                    ):
                        issues.append(
                            {
                                "code": "transition_output_ledger_quantity_mismatch",
                                "scope": "device_local_quantity",
                                "message": (
                                    f"child batch {child_batch_id} 的 transition produced "
                                    f"与 {transition_id}.output_allocations 不一致。"
                                ),
                            }
                        )
        planned_batch_ids = {
            str(batch.get("batch_id") or "")
            for batch in normalized_batches
            if str(batch.get("batch_id") or "")
        }
        for ledger_batch_id in sorted(set(aggregate_by_batch) - planned_batch_ids):
            issues.append(
                {
                    "code": "ledger_batch_missing_from_batch_plan",
                    "scope": "device_local_quantity",
                    "message": (
                        f"material_ledger 的 batch {ledger_batch_id} 未出现在 "
                        "batch_plan；额外批次不得绕过冻结谱系。"
                    ),
                }
            )
        for batch in normalized_batches:
            batch_id = str(batch.get("batch_id") or "")
            ledger_candidates = aggregate_by_batch.get(batch_id, [])
            batch_material_id = str(batch.get("material_id") or "").strip()
            if batch_material_id:
                ledger_candidates = [
                    record
                    for record in ledger_candidates
                    if str(record.get("material_id") or "").strip()
                    == batch_material_id
                ]
            if not ledger_candidates:
                issues.append(
                    {
                        "code": "batch_missing_from_material_ledger",
                        "scope": "device_local_quantity",
                        "message": (
                            f"batch_plan 的 batch {batch_id} 未出现在 material_ledger；"
                            "两份谱系侧车不一致。"
                        ),
                    }
                )
                continue
            if len(ledger_candidates) > 1:
                issues.append(
                    {
                        "code": "ambiguous_batch_material_link",
                        "scope": "device_local_quantity",
                        "message": (
                            f"batch {batch_id} 对应多个 ledger material；batch_plan "
                            "必须提供 material_id 以选择被分配的物料。"
                        ),
                    }
                )
                continue
            ledger_record = ledger_candidates[0]
            planned_total, planned_total_dimension, _ = self._canonical_quantity(
                batch.get("total_quantity", batch.get("parent_quantity"))
            )
            ledger_produced, ledger_produced_dimension, _ = self._canonical_quantity(
                ledger_record.get("produced")
            )
            if (
                planned_total is not None
                and ledger_produced is not None
                and not self._canonical_quantities_equal(
                    planned_total,
                    planned_total_dimension,
                    ledger_produced,
                    ledger_produced_dimension,
                )
            ):
                issues.append(
                    {
                        "code": "batch_ledger_produced_quantity_mismatch",
                        "scope": "device_local_quantity",
                        "message": (
                            f"batch {batch_id} 的 ledger produced 与 "
                            "batch_plan.total_quantity 不一致。"
                        ),
                    }
                )
            batch_sample = str(batch.get("sample_id") or "").strip()
            ledger_samples = {
                str(value).strip()
                for value in ledger_record.get("sample_ids", []) or []
                if str(value).strip()
            }
            if batch_sample and batch_sample not in ledger_samples:
                issues.append(
                    {
                        "code": "batch_sample_lineage_mismatch",
                        "scope": "device_local_quantity",
                        "message": (
                            f"batch {batch_id} 在 batch_plan 中属于 sample {batch_sample}，"
                            f"但 ledger sample_ids={sorted(ledger_samples)}。"
                        ),
                    }
                )

            batch_is_whole = str(batch.get("quantity_mode") or "") == "whole_batch"
            if batch_is_whole:
                planned_consumers = {
                    str(value).strip()
                    for value in batch.get("consumer_ids", []) or []
                    if str(value).strip()
                } if isinstance(batch.get("consumer_ids"), list) else set()
                planned_allocations = {}
                allocation_issues = []
            else:
                planned_consumers, planned_allocations, allocation_issues = (
                    self._batch_consumer_allocation_contract(batch)
                )
            issues.extend(allocation_issues)
            ledger_consumers = {
                str(value).strip()
                for value in ledger_record.get("consumer_ids", []) or []
                if str(value).strip()
            }
            if planned_consumers and planned_consumers != ledger_consumers:
                issues.append(
                    {
                        "code": "batch_consumer_lineage_mismatch",
                        "scope": "device_local_quantity",
                        "message": (
                            f"batch {batch_id} 的 batch_plan consumers="
                            f"{sorted(planned_consumers)} 与 ledger consumers="
                            f"{sorted(ledger_consumers)} 不一致。"
                        ),
                    }
                )
            ledger_allocations = ledger_record.get("consumer_allocations", {})
            ledger_allocations = (
                ledger_allocations if isinstance(ledger_allocations, dict) else {}
            )
            for consumer_id, planned_allocation in planned_allocations.items():
                actual_allocation = ledger_allocations.get(consumer_id)
                if not isinstance(actual_allocation, dict):
                    issues.append(
                        {
                            "code": "batch_consumer_allocation_missing_from_ledger",
                            "scope": "device_local_quantity",
                            "message": (
                                f"batch {batch_id} 对 consumer {consumer_id} 的计划分配"
                                "未出现在 ledger。"
                            ),
                        }
                    )
                    continue
                if planned_allocation["dimension"] != actual_allocation.get("dimension"):
                    issues.append(
                        {
                            "code": "batch_consumer_allocation_unit_mismatch",
                            "scope": "device_local_quantity",
                            "message": (
                                f"batch {batch_id} 对 consumer {consumer_id} 的计划分配"
                                "与实际消费单位维度不一致。"
                            ),
                        }
                    )
                    continue
                expected_value = float(planned_allocation["value"])
                actual_value = float(actual_allocation.get("value", 0.0))
                if abs(expected_value - actual_value) > max(
                    1e-9, abs(expected_value) * 1e-6
                ):
                    issues.append(
                        {
                            "code": "batch_consumer_allocation_quantity_mismatch",
                            "scope": "device_local_quantity",
                            "message": (
                                f"batch {batch_id} 对 consumer {consumer_id} 的 "
                                f"batch_plan allocation={expected_value:g} 与 ledger "
                                f"draw sum={actual_value:g} 不一致。"
                            ),
                        }
                    )
        if quantity_contract_required and (
            not had_batch_plan or not normalized_batches
        ):
            issues.append(
                {
                    "code": "missing_batch_plan_contract",
                    "scope": "device_local_quantity",
                    "message": (
                        "该计划需要数量审计，但 batch_plan 缺失或为空；必须给出 "
                        "batch_id/sample_id/consumer allocation 谱系。"
                    ),
                }
            )
        if quantity_contract_required and (
            not had_material_ledger or not normalized_entries
        ):
            issues.append(
                {
                    "code": "missing_material_ledger_contract",
                    "scope": "device_local_quantity",
                    "message": (
                        "该计划需要数量审计，但 material_ledger.entries 缺失或为空。"
                    ),
                }
            )
        ledger["entries"] = normalized_entries
        ledger["aggregates"] = aggregate_records

        evidence = _json_text(
            {
                "quantity_adjustments": adjustments,
                "batch_plan": normalized_batches,
                "material_ledger": ledger,
            }
        )
        human_issues: List[Dict[str, Any]] = copy.deepcopy(
            theoretical_human_issues + transition_human_issues
        )
        if declared_quantity_audit.get("status") == "human_review_required":
            for declared_issue in declared_quantity_audit.get("issues", []) or []:
                if isinstance(declared_issue, dict):
                    declared_code = str(declared_issue.get("code", "")).strip()
                    declared_scope = str(
                        declared_issue.get("scope", "")
                    ).strip()
                    if (
                        declared_scope != "human_review_required"
                        and declared_code
                        not in {"unknown_yield", "unauthorized_pooling"}
                    ):
                        # A prior deterministic auditor result may have an
                        # overall human status because it also contained a
                        # genuine unknown-yield issue.  Never feed its Device
                        # issues back as if they were human judgements.
                        continue
                    human_issue = copy.deepcopy(declared_issue)
                    human_issue["scope"] = "human_review_required"
                    human_issues.append(human_issue)
                else:
                    human_issues.append(
                        {
                            "code": "declared_quantity_human_review",
                            "scope": "human_review_required",
                            "message": str(declared_issue),
                        }
                    )
        if re.search(
            r"unknown\s*yield|yield\s*unknown|未知收率|收率未知|待测收率|无法确定收率",
            evidence,
            re.I,
        ) and not any(item.get("code") == "unknown_yield" for item in human_issues):
            human_issues.append(
                {
                    "code": "unknown_yield",
                    "scope": "human_review_required",
                    "message": "实际收率未知，Device 不得猜测可供后续消费的产物量。",
                }
            )
        if re.search(
            r"(?:合批|pool(?:ing)?).{0,48}(?:未经授权|未授权|不确定|待确认|unknown|ambiguous)|"
            r"(?:未经授权|未授权|不确定|待确认|unknown|ambiguous).{0,48}(?:合批|pool(?:ing)?)",
            evidence,
            re.I,
        ):
            human_issues.append(
                {
                    "code": "unauthorized_pooling",
                    "scope": "human_review_required",
                    "message": "独立样品是否允许合批不明确，必须人工确认。",
                }
            )
        deduplicated_human_issues: List[Dict[str, Any]] = []
        seen_human_issues: Set[Tuple[str, str, str]] = set()
        for item in human_issues:
            key = (
                str(item.get("code", "human_review_required")),
                str(item.get("entry_id", "")),
                str(item.get("message", "")),
            )
            if key in seen_human_issues:
                continue
            seen_human_issues.add(key)
            deduplicated_human_issues.append(item)
        human_issues = deduplicated_human_issues
        issues.extend(human_issues)
        requires_scientific_review = any(
            bool(item.get("requires_scientific_review")) for item in adjustments
        )
        if human_issues:
            audit_status = "human_review_required"
        elif issues:
            audit_status = "failed"
        else:
            audit_status = "passed"
        normalized["quantity_adjustments"] = adjustments
        normalized["batch_plan"] = normalized_batches
        normalized["material_ledger"] = ledger
        normalized["quantity_audit"] = {
            "status": audit_status,
            "assessment_source": "deterministic_quantity_auditor",
            "failure_scope": (
                "human_review_required"
                if human_issues
                else ("device_local_quantity" if issues else "none")
            ),
            "issues": issues,
            "requires_scientific_review": requires_scientific_review,
            "checks": {
                "duplicate_transactions": not any(
                    item.get("code") == "duplicate_material_transaction" for item in issues
                ),
                "all_consumers_funded": not any(
                    item.get("code")
                    in {
                        "material_quantity_insufficient",
                        "aggregate_material_quantity_insufficient",
                        "batch_consumer_allocation_quantity_mismatch",
                        "batch_consumer_allocation_missing_from_ledger",
                        "missing_batch_consumer_allocation",
                        "undeclared_batch_allocation_consumer",
                        "ledger_quantity_unit_missing_or_unknown",
                        "unknown_yield",
                    }
                    for item in issues
                ),
                "aggregate_material_balance": not any(
                    item.get("code")
                    in {
                        "duplicate_production_record",
                        "duplicate_consumer_allocation",
                        "aggregate_unit_dimension_mismatch",
                        "incompatible_quantity_units",
                    }
                    for item in issues
                ),
                "batch_arithmetic": not any(
                    item.get("code")
                    in {
                        "batch_quantity_mismatch",
                        "batch_quantity_unit_mismatch",
                        "batch_quantity_unit_missing_or_unknown",
                        "batch_allocation_unit_missing_or_unknown",
                        "batch_allocation_unit_mismatch",
                    }
                    for item in issues
                ),
                "lineage_ids_unique": not any(
                    item.get("code")
                    in {
                        "duplicate_batch_id",
                        "batch_missing_from_material_ledger",
                        "ledger_batch_missing_from_batch_plan",
                        "batch_sample_lineage_mismatch",
                        "batch_consumer_lineage_mismatch",
                        "missing_batch_consumer_allocation",
                        "undeclared_batch_allocation_consumer",
                    }
                    for item in issues
                ),
                "material_transition_lineage": not any(
                    (
                        "transition" in str(item.get("code", ""))
                        or "processing_step_refs" in str(item.get("code", ""))
                        or "processing_step" in str(item.get("code", ""))
                        or "material_event" in str(item.get("code", ""))
                        or "research_root" in str(item.get("code", ""))
                        or "root_batch" in str(item.get("code", ""))
                        or item.get("code") == "derived_batch_cannot_be_root"
                    )
                    for item in issues
                ),
            },
            "quantity_contract_coverage": (
                "complete"
                if quantity_contract_required and not any(
                    item.get("code", "").startswith("missing_")
                    for item in issues
                )
                else (
                    "missing"
                    if quantity_contract_required
                    else "not_applicable"
                )
            ),
        }
        return normalized

    def _invoke_device_plan_repair(
        self,
        state: SingleDeviceAgentState,
        current_plan: Dict[str, Any],
        failed_workflow: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Exactly one plan-level Device LLM rewrite after cycle one."""
        error_history = copy.deepcopy(state.workflow_repair_history[-8:])
        system_prompt = (
            "你是 Device 层计划修复 agent。路线可行性已经被接受，因此绝不能把错误返回 "
            "Research。你只能最小改写完整 device_plan 来修复 workflow 的设备执行错误。"
            "可以调整工作站、容器、操作拆分、总量、浓度和摩尔比；不得改变研究目标、目标材料、"
            "反应路线、试剂身份或顺序、observation point、样品组/对照组/变量梯度。"
            "输出必须把 feasibility_certificate.sample_control_matrix 原样回显到顶层 "
            "sample_control_matrix；容器和料位可变，但 sample/control/variable identity 不得漂移。"
            "新增完整批次、改变浓度/摩尔比、改变单批或总量时必须在 quantity_adjustments 标记 "
            "requires_scientific_review=true；纯分次、分瓶、容量拆批、容器/料位变化标记 false。"
            "完整设备语义合同已冻结在 feasibility_certificate.semantic_analysis；每个 Device "
            "step、root source、batch 和 observation measurement 必须继续使用其中的 "
            "material_identity_id，不得重新按名称或关键词猜身份。"
            "数量谱系必须以确切 material_id/物料状态为边界：纯化、洗涤、干燥等转化"
            "节点不得与对下游新物料的 XRD/XPS 取样并写为同一 batch 的消费者。"
            "allocation 只允许 consumer_id 键映射，或每项都含显式 consumer_id 的 record list；"
            "声明的 consumer_ids 与显式 allocation consumer 集合必须完全相等。material_ledger "
            "必须逐 consumer 对账；不得把一个标量同时分配给多个 consumer，也不得将"
            "无 consumer_id 的 list 按位置/排序自动配对或猜测未知分配。"
            "derived/device_operational batch 不得 is_root_batch=true；必须以真实存在的"
            "parent_batch_id(s)、transition_kind、source_plan_steps、source_macro_steps 以及顶层 "
            "material_transitions 建立 parent→child DAG 谱系；parent 集必须与 child 声明"
            "完全相等且不得 self-loop。对应 material_ledger entry 必须有与子 transition "
            "完全一致的 processing_step_refs。改变物料状态的处理步必须双向声明"
            "material_event_kind/material_transition_ids 与 transition.source_plan_steps；纯化/洗涤/"
            "干燥/反应步不得从 sidecar 删除或把其产物伪装成 root batch。"
            "research root 必须 source_kind=research，以 research_source_refs 绑定真实"
            "macro_action_steps[index].field、逐字 source_context 和 Research 试剂/对象身份子集；"
            "material_id/research_material_identity 必须与该身份相同。numeric_inventory root"
            "还必须绑定可审计 quantity 并给正数 total_quantity；whole_batch root 只绑定身份/"
            "真源且不得猜总量，并且必须由对应 Research quantity_requirements.kind=whole_batch，"
            "或可调执行 target_dose 的 quantity_requirement_dispositions=replace_with_whole_batch "
            "对同一物料显式授权。按批次授权时使用 "
            "quantity_scope=per_batch、per_batch_quantity、正整数 multiplicity 和冻结 quote "
            "multiplicity_ref；同一 root 内重复洗涤/加液时使用 quantity_scope=per_operation、"
            "operation_repeat_ref 和正整数 operation_repeat_count。multiplicity_ref 与 "
            "operation_repeat_ref 按 quantity_scope 二选一，不得混用。"
            "root total 改变必须有 before/after 匹配且 requires_scientific_review=true 的"
            "quantity_adjustment，直接单位换算除外。"
            "每条 material_transition 必须给出 quantity_basis；numeric_inventory 还要给"
            " consumer-bound input_allocations[{batch_id,quantity}] / "
            "output_allocations[{batch_id,quantity}]；whole_batch 只允许 1→1 且不填数值 edge。"
            "parent ledger 必须用 consumer_id=material_transition:<id> 实际扣减 input，child "
            "ledger produced 必须用 source_refs=[material_transition:<id>] 入账并逐 batch 对账。"
            "conserved_inventory 的聚合输入输出严格守恒；state_change 只能使用经冻结 observation "
            "的 id/digest/sample/material/quantity field 验证的 measurement_artifact，或可验证的 "
            "planning_yield_lower_bound。所有库存/分配/edge 必须是有限正数。"
            "observation digest 不由模型计算；只能从输入 observation_evidence_catalog "
            "逐字选择完整 measurement_artifact。"
            "未知收率、未经授权合批或无法唯一确定 consumer 分配时必须输出 "
            "human_review_required，不得猜测。已有 plan_step 的 objective/operation/workstation 未变时，"
            "绝不得改其 source_macro_step(s) 绑定。合法操作拆分/合并必须在 plan_changes 中"
            "逐组给出 change_type=operation_split|operation_merge|operation_decomposition、"
            "before_step_ids、after_step_ids、before、after、reason 和 "
            "preserved_invariants=[route,reagent_identity,macro_source_coverage]；每组 before/after "
            "步骤的 source_macro_step(s) union 必须完全相等，且不能用一条全局声明"
            "授权多个未绑定变更。新绑定步骤的 "
            "source_reagent_identity 必须显式填写，不得留空等程序补全。只输出 JSON object。"
        )
        system_prompt += (
            "只有 Research quantity_requirements 或 Skill 必填输入要求数值时才做数值库存审计。"
            "目标进样量是工作站 setpoint，不是整批实际质量。无数值下游要求的 1→1 整批处理必须"
            "使用 batch.quantity_mode=whole_batch 和 transition.quantity_basis=whole_batch，"
            "不得填写猜测的 total/allocation/edge；Research root 还必须由对应 macro step 的 "
            "quantity_requirements.kind=whole_batch 显式授权；whole_batch 禁止 split/merge/多 consumer。"
            "对 owner=device_execution 且 device_policy=device_semantic_decision 的 target_dose，"
            "必须由 Device 模型结合科学目标、下游 Skill 必填参数、上游 Report 和 whole_batch 路径，"
            "在 quantity_requirement_dispositions 逐项判断 retain/adapt/bind/omit/whole_batch/"
            "runtime measurement，并给非空 reason 与 evidence_refs。确定性代码只核验引用的"
            " Skill/测量事实；不得省略该模型判断，也不得伪造设备会返回未声明的实际库存。"
        )
        task = {
            "task": "rewrite_complete_device_plan_once",
            "research_handoff": state.research_handoff,
            "observation_evidence_catalog": self._observation_evidence_catalog(
                state.research_handoff
            ),
            "workstation_truth_source": "完整能力目录、全局规则和所选合同见本轮原生工具上下文。",
            "feasibility_certificate": state.feasibility_certificate,
            "current_device_plan": current_plan,
            "deduplicated_workflow_error_history": error_history,
            "last_workflow": failed_workflow.get("workflow_json", {}),
            "required_output": {
                "status": "device_plan | human_review_required",
                "device_plan": "完整新 device_plan",
                "reagent_slot_plan": "完整槽位计划",
                "container_plan": "完整容器计划",
                "sample_control_matrix": state.feasibility_certificate.get(
                    "sample_control_matrix", []
                ),
                "quantity_adjustments": "结构化数量变化及计算式/来源/复核标志",
                "quantity_requirement_dispositions": (
                    "逐项处置 owner=device_execution + device_semantic_decision 的 target_dose；"
                    "字段为 source_macro_step/requirement_index/decision/plan_step/workstation/"
                    "operation/parameter/reason/evidence_refs/requires_scientific_review；decision 允许"
                    " retain_as_scientific_target、adapt_within_device_bounds、bind_skill_setpoint、"
                    "omit_as_nonessential、replace_with_whole_batch、request_runtime_measurement"
                ),
                "batch_plan": (
                    "按确切 material_id 分阶段的 batch_id/sample_id 谱系；"
                    "每批必须给 material_identity_id；root 还必须给 "
                    "research_material_identity_id，二者均引用冻结语义合同。"
                    "consumer_ids 必须与显式绑定 allocation consumer 集合完全相等；"
                    "只允许 consumer-keyed map 或每项含 consumer_id 的 record list；"
                    "每批必须声明 quantity_mode=numeric_inventory|whole_batch；research root 必须有 "
                    "material_id/research_material_identity 和绑定真实 macro field 及逐字 source_context "
                    "的 research_source_refs；只有 numeric_inventory root 给正数 total_quantity，"
                    "whole_batch 不得猜总量且必须由同物料 Research quantity_requirements.kind=whole_batch，"
                    "或可调执行 target_dose 的 replace_with_whole_batch disposition 显式授权。每批授权用 "
                    "quantity_scope=per_batch + per_batch_quantity + multiplicity + "
                    "multiplicity_ref{source_path,source_context,count,semantic_scope="
                    "all_samples|material_specific,material_identity_id,semantic_reason,evidence_refs}；"
                    "这个适用范围由模型按完整上下文判断，程序不做关键词猜测。同一 root 的重复操作用 "
                    "quantity_scope=per_operation + operation_repeat_ref{source_path,"
                    "source_context,count} + operation_repeat_count；两类 repeat 证据不得混用"
                ),
                "material_transitions": (
                    "每个非根/物料状态变化 batch 的 transition_id、transition_kind、"
                    "parent_batch_ids、child_batch_ids、source_plan_steps、source_macro_steps、"
                    "before_material_state、after_material_state；必须 DAG、无 self-loop，"
                    "parent 集与 child 声明完全一致，并与 device_plan 每个处理步的"
                    "material_event_kind/material_transition_ids 双向绑定；每条必须给出 "
                    "quantity_basis；numeric_inventory 还必须给 input_allocations[{batch_id,quantity}]、"
                    "output_allocations[{batch_id,quantity}]；whole_batch 仅允许 1→1 且不填数值 edge。"
                    "state_change 若用实际产量，"
                    "measurement_artifact 必须含 observation_id/artifact_digest/"
                    "material_identity_id/quantity_source_field/quantity_source_context"
                ),
                "material_ledger": (
                    "produced/consumed/reserved/balance 台账；每个 batch_plan "
                    "consumer 都必须有匹配的独立 consumed allocation；非根 batch "
                    "entry 必须有与 transition 一致的 processing_step_refs；parent edge "
                    "必须以 consumer_id=material_transition:<id> 扣减，child produced "
                    "必须以 source_refs=[material_transition:<id>] 入账，且数量与 edge "
                    "allocation 精确一致"
                ),
                "plan_changes": [
                    {
                        "change_type": "parameter_update | operation_split | operation_merge | operation_decomposition",
                        "field": "可修改的 Device 字段",
                        "before_step_ids": [1],
                        "after_step_ids": [101, 102],
                        "before": "修改前",
                        "after": "修改后",
                        "reason": "修改理由",
                        "preserved_invariants": [
                            "route",
                            "reagent_identity",
                            "macro_source_coverage",
                        ],
                    }
                ],
                "device_plan_each_step": (
                    "每个步骤必须保留 source_macro_step(s)、source_material_identity_ids、"
                    "material_event_kind 和 material_transition_ids；不得只回显自然语言 "
                    "source_reagent_identity 代替冻结 identity IDs"
                ),
                "change_rationale": "总体最小改写理由",
                "expected_resolved_errors": ["预计解决的结构化错误"],
                "research_plan_signature": state.feasibility_certificate.get(
                    "research_plan_signature", ""
                ),
                "route_changed": False,
            },
        }
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=json.dumps(task, ensure_ascii=False, indent=2)),
        ]
        print("[single-device-agent] LLM step start: device_plan_minimal_rewrite", flush=True)
        parsed = self._invoke_json_object_with_format_retry(
            state,
            messages,
            step_name="device_plan_minimal_rewrite",
            workstation_tools=True,
            skill_codes=self._workstation_skill_session().referenced_codes({
                "device_plan": current_plan.get("device_plan", []),
                "reagent_slot_plan": current_plan.get("reagent_slot_plan", []),
            }),
        )
        print("[single-device-agent] LLM step done: device_plan_minimal_rewrite", flush=True)
        return parsed

    def _validate_repaired_device_plan(
        self,
        state: SingleDeviceAgentState,
        previous_plan: Dict[str, Any],
        repaired_plan: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], List[str]]:
        errors: List[str] = []
        if not isinstance(repaired_plan, dict):
            return {}, ["计划级 Device LLM 未返回 JSON object。"]
        repaired = copy.deepcopy(repaired_plan)
        repaired = self._normalize_plan_handoff_steps(state, repaired)
        source_binding_errors = self._repaired_plan_source_binding_errors(
            previous_plan, repaired
        )
        errors.extend(source_binding_errors)
        if not source_binding_errors and not self._active_semantic_analysis:
            inherited_identity_count = self._inherit_missing_repaired_reagent_identities(
                state.research_handoff,
                previous_plan,
                repaired,
            )
            if inherited_identity_count:
                state.add_log(
                    "mechanically inherited missing source_reagent_identity for "
                    f"{inherited_identity_count} repaired Device-plan step(s); "
                    "this does not count as an LLM modification"
                )
        if str(repaired.get("status", "")).strip().lower() == "human_review_required":
            errors.append("计划级 Device LLM 判定未知收率/合批等需要人工判断。")
        if not self._plan_is_accepted(repaired):
            errors.append("计划级 Device LLM 未返回 status=device_plan。")
        if not isinstance(repaired.get("device_plan"), list) or not repaired.get("device_plan"):
            errors.append("计划级 Device LLM 未返回完整非空 device_plan。")
        errors.extend(self._declared_route_change_errors(repaired))
        claimed_signature = repaired.get("research_plan_signature")
        expected_signature = state.feasibility_certificate.get(
            "research_plan_signature", ""
        )
        if claimed_signature not in (None, "", expected_signature):
            errors.append("计划级 Device LLM 改变了 Research plan signature。")
        echoed_certificate = repaired.get("feasibility_certificate")
        if isinstance(echoed_certificate, dict) and echoed_certificate.get(
            "protected_digest"
        ) != state.feasibility_certificate.get("protected_digest"):
            errors.append("计划级 Device LLM 返回的证书与冻结证书不一致。")

        expected_macro_steps = {
            source
            for item in previous_plan.get("device_plan", []) or []
            if isinstance(item, dict)
            for source in self._source_macro_step_ids(item)
        }
        actual_macro_steps = {
            source
            for item in repaired.get("device_plan", []) or []
            if isinstance(item, dict)
            for source in self._source_macro_step_ids(item)
        }
        if expected_macro_steps and not expected_macro_steps.issubset(actual_macro_steps):
            errors.append(
                "计划级 Device LLM 删除了 macro step coverage："
                f"missing={sorted(expected_macro_steps - actual_macro_steps)}。"
            )
        expected_ids = sorted(
            str(item)
            for item in state.feasibility_certificate.get("device_sample_ids", []) or []
        )
        actual_ids = self._extract_sample_ids(repaired)
        if expected_ids and actual_ids != expected_ids:
            errors.append(
                "计划级 Device LLM 改变了冻结样品/对照矩阵："
                f"expected={expected_ids}, actual={actual_ids}。"
            )
        expected_matrix = state.feasibility_certificate.get(
            "sample_control_matrix", []
        )
        repaired_matrix = repaired.get("sample_control_matrix")
        if expected_matrix and repaired_matrix != expected_matrix:
            errors.append(
                "计划级 Device LLM 未逐字保留冻结的 sample_control_matrix。"
            )
        errors.extend(self._forbidden_plan_change_claims(repaired))
        if not isinstance(repaired.get("plan_changes"), list):
            errors.append("计划级 Device LLM 缺少逐项 before/after 的 plan_changes。")
        if not str(repaired.get("change_rationale", "")).strip():
            errors.append("计划级 Device LLM 缺少 change_rationale。")
        if not isinstance(repaired.get("expected_resolved_errors"), list):
            errors.append("计划级 Device LLM 缺少 expected_resolved_errors。")
        errors.extend(
            self._device_plan_research_alignment_errors(
                state.research_handoff,
                repaired,
                reference_plan=previous_plan,
                semantic_analysis=self._active_semantic_analysis,
            )
        )
        errors.extend(
            "计划级 Device 重写未通过确定性校验 "
            f"[{finding.get('type', 'plan_level_finding')}]："
            f"{finding.get('message', finding)}"
            for finding in self._plan_level_findings(state, repaired)
        )

        repaired["feasibility_accepted"] = True
        repaired["feasibility_certificate"] = copy.deepcopy(
            state.feasibility_certificate
        )
        repaired.setdefault(
            "feasibility", previous_plan.get(
                "feasibility", {"is_feasible": True, "blocking_constraints": []}
            )
        )
        repaired.setdefault(
            "macro_plan_summary", previous_plan.get("macro_plan_summary", "")
        )
        repaired.setdefault(
            "device_self_check", previous_plan.get("device_self_check", {})
        )
        return repaired, errors

    def run_state(
        self,
        research_handoff: Dict[str, Any],
        *,
        exp_id: Optional[str] = None,
        iteration_id: int = 0,
        workflow_id: int = 0,
        checkpoint_dir: Optional[str] = None,
        resume_checkpoints: bool = True,
        device_plan_override: Optional[Dict[str, Any]] = None,
        prior_repair_request: Optional[Dict[str, Any]] = None,
        human_quantity_approval_bundle: Optional[
            ValidatedHumanQuantityApprovalBundle
        ] = None,
    ) -> SingleDeviceAgentState:
        explicit_exp_id = exp_id
        exp_id = exp_id or self._default_exp_id()
        self._active_trusted_human_quantity_approvals = []
        self._active_semantic_analysis = {}
        self._checkpoint_store = None
        research_handoff, _ = self._strip_untrusted_approval_fields(
            research_handoff
        )
        approval_bundle_payload: Optional[
            Tuple[Dict[str, Any], List[Dict[str, Any]]]
        ] = None
        approval_bundle_invalid = False
        if human_quantity_approval_bundle is not None:
            if device_plan_override is None:
                approval_bundle_invalid = True
            else:
                approval_bundle_payload = (
                    consume_validated_human_quantity_approval_bundle(
                        human_quantity_approval_bundle
                    )
                )
                approval_bundle_invalid = approval_bundle_payload is None
        state = SingleDeviceAgentState(
            research_handoff=research_handoff,
            exp_id=exp_id,
            iteration_id=iteration_id,
            workflow_id=workflow_id,
            txt_format_reference=self._txt_format_reference,
            json_format_reference=self._json_format_reference,
        )
        state.add_log("SingleDeviceAgent started")
        try:
            self._refresh_workstation_snapshot()
            skill_session = self._workstation_skill_session()
            state.workstation_descriptions = skill_session.discovery_context()
            state.device_truth_sha256 = skill_session.truth_digest()
            state.txt_format_reference = self._txt_format_reference
            state.json_format_reference = self._json_format_reference
            self._assert_workstation_snapshot_current(state)
            resumed_from_manual = device_plan_override is not None
            if checkpoint_dir and not resumed_from_manual:
                fragment_contract_path = Path(__file__).with_name(
                    "feasibility_fragments.py"
                )
                self._checkpoint_store = DeviceCheckpointStore(
                    checkpoint_dir,
                    {
                        "research_handoff_sha256": checkpoint_digest(
                            state.research_handoff
                        ),
                        "device_truth_sha256": state.device_truth_sha256,
                        "contract_version": self._contract_version,
                        "semantic_analysis_enabled": (
                            self._llm_semantic_analysis_enabled()
                        ),
                        "feasibility_mode": os.getenv(
                            "CHEM_DEVICE_FEASIBILITY_MODE", ""
                        ).strip().lower(),
                        "fragment_contract_sha256": checkpoint_file_digest(
                            fragment_contract_path
                        ),
                        "implementation_sha256": implementation_digest(),
                        "planning_model": self._planning_model_checkpoint_binding(
                            self._model
                        ),
                    },
                    resume=resume_checkpoints,
                    metadata={"exp_id": exp_id},
                )
                if explicit_exp_id is None:
                    state.exp_id = str(
                        self._checkpoint_store.metadata.get("exp_id") or exp_id
                    )
                state.checkpoints = self._checkpoint_store.summary()
            if self._llm_semantic_analysis_enabled():
                frozen_semantics: Any = None
                if resumed_from_manual:
                    request_certificate = (
                        prior_repair_request or {}
                    ).get("feasibility_certificate")
                    if isinstance(request_certificate, dict):
                        frozen_semantics = request_certificate.get("semantic_analysis")
                if isinstance(frozen_semantics, dict):
                    semantic_errors = self._semantic_analysis_errors(
                        state.research_handoff, frozen_semantics
                    )
                    if semantic_errors:
                        raise ValueError(
                            "frozen semantic analysis is invalid: "
                            + "; ".join(semantic_errors)
                        )
                    self._active_semantic_analysis = copy.deepcopy(frozen_semantics)
                    state.add_log("restored frozen LLM semantic analysis from certificate")
                else:
                    self._active_semantic_analysis = self._checkpoint_stage(
                        state,
                        "semantic_analysis",
                        state.research_handoff,
                        lambda: self._invoke_semantic_analysis(state),
                    )
                state.semantic_analysis = copy.deepcopy(self._active_semantic_analysis)
                state.research_handoff = self._apply_semantic_analysis_to_handoff(
                    state.research_handoff
                )
                research_handoff = state.research_handoff
            if resumed_from_manual:
                plan_result, override_error = self._prepare_device_plan_override(
                    state,
                    device_plan_override or {},
                    prior_repair_request or {},
                    approval_bundle_payload=approval_bundle_payload,
                    approval_bundle_invalid=approval_bundle_invalid,
                )
                if override_error:
                    result = self._manual_override_rejected_result(
                        state, plan_result, override_error
                    )
                else:
                    result = self._run_accepted_device_plan(
                        state,
                        plan_result,
                        allow_plan_rewrite=False,
                        resumed_from_manual=True,
                    )
            else:
                # Stage 1: route feasibility + device-level plan.  This is the
                # only point at which a true chemistry/equipment route gap may
                # be returned to Research.
                # Prove frozen Research joint requirements before consulting
                # the mapping LLM.  A candidate must not evade a real route
                # gap by deleting the macro, calling it offline, or assigning
                # it to a station that only satisfies the temperature half.
                plan_result = self._verified_stage1_core_route_gap_result(
                    state, {}
                )
                if plan_result is not None:
                    state.add_log(
                        "pre-certificate deterministic route gate proved a "
                        "frozen Research joint-capability gap"
                    )
                else:
                    plan_result = self._invoke_feasibility_plan(state)
                    plan_result = self._promote_feasible_quantity_human_plan(
                        plan_result
                    )
                    plan_result = self._remove_implicit_connectivity_blockers(
                        state, plan_result
                    )
                    plan_result = self._retry_adaptable_feedback(
                        state, plan_result, research_handoff
                    )
                    plan_result = self._remove_implicit_connectivity_blockers(
                        state, plan_result
                    )
                    plan_result = self._normalize_plan_handoff_steps(
                        state, plan_result
                    )
                    plan_result = self._repair_plan_level_findings(
                        state, plan_result
                    )
                    plan_result = self._normalize_plan_handoff_steps(
                        state, plan_result
                    )

                if self._plan_is_accepted(plan_result):
                    plan_result = self._accept_feasibility_plan(state, plan_result)
                    if self._plan_is_accepted(plan_result):
                        result = self._run_accepted_device_plan(
                            state,
                            plan_result,
                            allow_plan_rewrite=self._contract_version != "v2",
                            resumed_from_manual=False,
                        )
                    else:
                        result = plan_result
                else:
                    result = (
                        self._stage1_device_local_manual_result(
                            state, plan_result
                        )
                        or plan_result
                    )

            state.raw_llm_output = result
            self._assert_workstation_snapshot_current(state)
            self._sync_skill_load_state(state)
            result["loaded_workstation_skills"] = state.loaded_workstation_skills
            result["skill_load_events"] = state.skill_load_events
            package = self._normalize_terminal_package(state, result)
            if self._contract_version == "v2":
                package = self._attach_v2_contract(state, package)
            self._assert_workstation_snapshot_current(state)
            state.terminal_package = package
            if package.get("feedback_type") == "device_internal_error" and result.get("llm_diagnostics"):
                error_package = package.setdefault("error_package", {})
                error_package["llm_diagnostics"] = copy.deepcopy(result["llm_diagnostics"])
                error_package["llm_failure_step"] = result["llm_failure_step"]
                error_package["llm_exception_type"] = result["llm_exception_type"]
            package_status = str(package.get("status", ""))
            if package_status in {"feasibility_error", "terminal_unmappable"}:
                state.status = "feasibility_error"
            elif package_status == "manual_required":
                state.status = "manual_required"
            elif package_status == "failed":
                state.status = "failed"
            else:
                state.status = "completed"
            state.workflow_txt = str(package.get("workflow_txt", ""))
            workflow_json = package.get("workflow_json")
            state.workflow_json = workflow_json if isinstance(workflow_json, dict) else {}
            self._finalize_checkpoint_run(state, package)
            state.add_log(f"SingleDeviceAgent completed with status={state.status}")
            return state
        except Exception as exc:
            diagnostics = get_responses_diagnostics(exc)
            error_text = (
                f"{type(exc).__name__}: "
                f"{_safe_model_failure_text(exc)}"
            )
            configuration_error = isinstance(exc, DeviceConfigurationError)
            if self._skill_session is not None:
                self._sync_skill_load_state(state)
            state.add_error(f"SingleDeviceAgent failed: {error_text}")
            state.status = "failed"
            state.terminal_package = {
                "status": "failed",
                "feedback_type": "device_configuration_error" if configuration_error else "device_internal_error",
                "feedback_route": "device",
                "failure_scope": "device_internal",
                "failure_stage": "device_configuration" if configuration_error else "device_internal_error",
                "exp_id": state.exp_id,
                "iteration_id": state.iteration_id,
                "workflow_id": state.workflow_id,
                "device_snapshot_id": self._device_snapshot_id(),
                "loaded_workstation_skills": state.loaded_workstation_skills,
                "skill_load_events": state.skill_load_events,
                "feasibility_accepted": state.feasibility_accepted,
                "checkpoints": copy.deepcopy(state.checkpoints),
                "feasibility_certificate": copy.deepcopy(
                    state.feasibility_certificate
                ),
                "workflow_txt": "",
                "workflow_json": {},
                "requires_scientific_review": False,
                "error_package": {
                    "type": "device_configuration_error" if configuration_error else "device_internal_error",
                    "assessment_source": "single_device_agent_runtime",
                    "blocking_constraints": [error_text],
                    "message": error_text if configuration_error else (
                        "Device Agent 在生成或验证工作流时发生运行错误；"
                        "未产生可下发工作流，不得伪装为设备可行性结论。"
                    ),
                },
            }
            if diagnostics is not None:
                record = next(
                    (
                        item for item in reversed(state.llm_diagnostics)
                        if item.get("diagnostics") == diagnostics
                    ),
                    None,
                )
                if record is None:
                    record = {
                        "task_name": "device_runtime",
                        "exception_type": type(exc).__name__,
                        "diagnostics": diagnostics,
                    }
                    state.llm_diagnostics.append(record)
                state.terminal_package["error_package"].update({
                    "llm_diagnostics": copy.deepcopy(diagnostics),
                    "llm_failure_step": record["task_name"],
                    "llm_exception_type": record["exception_type"],
                })
            if state.feasibility_progress and state.feasibility_progress[-1].get("status") == "failed":
                progress = state.feasibility_progress[-1]
                state.terminal_package["error_package"]["planning_progress"] = {
                    "failed_step": progress.get("active_step"),
                    "failed_macro_id": progress.get("active_macro_id"),
                    "completed_chunks": len(progress.get("completed_chunks", [])),
                    "partial_candidate_dispatchable": False,
                }
            # Runtime/model failures deliberately leave the run active.  A
            # retry with the same frozen binding can then restore the last
            # fully accepted fragment, while no partial candidate is granted
            # dispatch authority.
            self._finalize_checkpoint_run(state, state.terminal_package)
            state.add_log("SingleDeviceAgent returned a terminal runtime-error package")
            if _is_gateway_failure(exc):
                # An SDK HTTP exception may retain its raw body and headers.
                logger.error("SingleDeviceAgent failed: %s", error_text)
            else:
                logger.exception("SingleDeviceAgent failed")
            return state

    def run(self, research_handoff: Dict[str, Any]) -> Dict[str, Any]:
        return self.run_state(research_handoff).terminal_package

    @staticmethod
    def _checkpoint_terminal_outcome(package: Dict[str, Any]) -> bool:
        """Return whether a package closes, rather than interrupts, a run."""
        status = str(package.get("status", "")).strip().lower()
        return bool(
            package.get("dispatchable") is True
            or status in {
                "success",
                "manual_required",
                "feasibility_error",
                "terminal_unmappable",
            }
        )

    def _finalize_checkpoint_run(
        self,
        state: SingleDeviceAgentState,
        package: Dict[str, Any],
    ) -> None:
        """Publish lifecycle state without changing workflow authority."""
        store = getattr(self, "_checkpoint_store", None)
        if store is None:
            return
        status = str(package.get("status", "")).strip().lower()
        dispatchable = bool(
            package.get("dispatchable") is True or status == "success"
        )
        if self._checkpoint_terminal_outcome(package):
            store.complete(
                terminal_status=status or "dispatchable",
                dispatchable=dispatchable,
            )
        state.checkpoints = store.summary()
        package["checkpoints"] = copy.deepcopy(state.checkpoints)

    def _restore_checkpoint_skill_state(
        self,
        state: SingleDeviceAgentState,
        payload: Dict[str, Any],
    ) -> None:
        """Restore only workstation reads needed by a later uncached stage."""
        loaded = payload.get("loaded_workstation_skills")
        if not isinstance(loaded, list) or not loaded:
            return
        session = self._workstation_skill_session()
        for item in loaded:
            if not isinstance(item, dict):
                continue
            station_code = str(item.get("station_code") or "").strip()
            if station_code and station_code not in session.loaded:
                session.load(station_code, origin="checkpoint_restore")
        self._sync_skill_load_state(state)

    def _checkpoint_stage(
        self,
        state: SingleDeviceAgentState,
        stage: str,
        inputs: Any,
        compute: Any,
    ) -> Dict[str, Any]:
        """Checkpoint one completed JSON-producing stage.

        This wrapper deliberately does not checkpoint exceptions or terminal
        runtime-error sentinels.  Its payload carries no dispatch authority.
        """
        store = getattr(self, "_checkpoint_store", None)
        if store is None:
            return compute()
        self._assert_workstation_snapshot_current(state)
        key = store.key(stage, inputs)
        cached = store.read(key, record_reuse=False)
        if (
            isinstance(cached, dict)
            and cached.get("schema_version") == 1
            and isinstance(cached.get("result"), dict)
            and cached.get("result_sha256")
            == checkpoint_digest(cached.get("result"))
            and cached.get("dispatchable") is False
        ):
            store.confirm_reuse(key, stage)
            self._restore_checkpoint_skill_state(state, cached)
            state.checkpoints = store.summary()
            state.add_log(f"resumed Device checkpoint: {stage}")
            print(
                f"[single-device-agent] checkpoint restored: {stage}",
                flush=True,
            )
            return copy.deepcopy(cached["result"])

        with store.scope(key):
            result = compute()
        if not isinstance(result, dict):
            raise TypeError(f"checkpoint stage {stage} must return one JSON object")
        self._assert_workstation_snapshot_current(state)
        self._sync_skill_load_state(state)
        store.write(
            key,
            stage,
            {
                "schema_version": 1,
                "result": copy.deepcopy(result),
                "result_sha256": checkpoint_digest(result),
                "loaded_workstation_skills": copy.deepcopy(
                    state.loaded_workstation_skills
                ),
                "dispatchable": False,
            },
        )
        state.checkpoints = store.summary()
        state.add_log(f"saved Device checkpoint: {stage}")
        return result

    def _use_full_workstation_prompt(self) -> bool:
        """Discovery is complete; full contracts are progressively disclosed."""
        return False

    def _workstation_selection_text(self, research_handoff: Dict[str, Any]) -> str:
        """Focus workstation retrieval on executable intent, not long feedback history."""
        task = research_handoff.get("task")
        if not isinstance(task, dict):
            task = {}
        focused = {
            "query": task.get("query", ""),
            "current_stage": task.get("current_stage", ""),
            "macro_action_steps": research_handoff.get("macro_action_steps", []),
        }
        return json.dumps(focused, ensure_ascii=False)

    @staticmethod
    def _llm_semantic_analysis_enabled() -> bool:
        """Production always uses LLM semantics.

        The old keyword path is retained only so narrowly scoped legacy unit
        tests can exercise historical validators.  Disabling the LLM requires
        a second, explicitly test-only gate; a normal campaign cannot
        accidentally fall back by setting one environment variable.
        """
        requested_off = os.getenv(
            "CHEM_DEVICE_LLM_SEMANTIC_ANALYSIS", "on"
        ).strip().lower() in {
            "0",
            "false",
            "off",
            "no",
        }
        test_only_authorized = os.getenv(
            "CHEM_DEVICE_ALLOW_LEGACY_SEMANTICS_FOR_TESTS", ""
        ).strip().lower() in {"1", "true", "yes"}
        return not (requested_off and test_only_authorized)

    @staticmethod
    def _semantic_macro_id(step: Dict[str, Any], index: int) -> str:
        return str(step.get("步骤序号", step.get("step", index))).strip()

    @classmethod
    def _semantic_analysis_errors(
        cls,
        research_handoff: Dict[str, Any],
        analysis: Any,
    ) -> List[str]:
        """Validate completeness and references, never chemistry semantics."""
        errors: List[str] = []
        if not isinstance(analysis, dict):
            return ["semantic analysis must be one JSON object"]
        if str(analysis.get("status") or "").strip() != "semantic_analysis":
            errors.append("semantic analysis status must equal semantic_analysis")
        assessments = analysis.get("macro_step_assessments")
        if not isinstance(assessments, list):
            return errors + ["semantic analysis must contain macro_step_assessments[]"]
        macros = [
            item
            for item in research_handoff.get("macro_action_steps", []) or []
            if isinstance(item, dict)
        ]
        expected_ids = [
            cls._semantic_macro_id(item, index)
            for index, item in enumerate(macros, start=1)
        ]
        records_by_id: Dict[str, List[Dict[str, Any]]] = {}
        for raw in assessments:
            if not isinstance(raw, dict):
                errors.append("macro_step_assessments entries must be objects")
                continue
            macro_id = str(raw.get("source_macro_step") or "").strip()
            records_by_id.setdefault(macro_id, []).append(raw)
        if set(records_by_id) != set(expected_ids):
            errors.append(
                "semantic analysis macro ids must exactly cover frozen Research steps: "
                f"expected={expected_ids}, actual={sorted(records_by_id)}"
            )
        for macro, macro_id in zip(macros, expected_ids):
            matches = records_by_id.get(macro_id, [])
            if len(matches) != 1:
                errors.append(f"macro step {macro_id} must have exactly one semantic assessment")
                continue
            record = matches[0]
            if not str(record.get("reason") or "").strip():
                errors.append(f"macro step {macro_id} semantic reason is required")
            core = record.get("core_chemistry")
            observation = record.get("observation_only")
            for name, value in (("core_chemistry", core), ("observation_only", observation)):
                if not isinstance(value, dict) or not isinstance(value.get("value"), bool):
                    errors.append(f"macro step {macro_id} {name}.value must be boolean")
            if (
                isinstance(core, dict)
                and isinstance(observation, dict)
                and core.get("value") is True
                and observation.get("value") is True
            ):
                errors.append(f"macro step {macro_id} cannot be core chemistry and observation-only")

            capabilities = record.get("required_capabilities")
            if not isinstance(capabilities, list):
                errors.append(f"macro step {macro_id} required_capabilities must be an array")
            else:
                seen_categories: Set[str] = set()
                for capability in capabilities:
                    if not isinstance(capability, dict):
                        errors.append(f"macro step {macro_id} capability must be an object")
                        continue
                    category = str(capability.get("category") or "").strip()
                    if category not in _SEMANTIC_CAPABILITY_CATEGORIES:
                        errors.append(f"macro step {macro_id} has invalid capability category={category!r}")
                    if category in seen_categories:
                        errors.append(f"macro step {macro_id} duplicates capability category={category}")
                    seen_categories.add(category)
                if (
                    isinstance(core, dict)
                    and core.get("value") is True
                    and not seen_categories.intersection(
                        {"reaction", "drying", "calcination", "purification"}
                    )
                ):
                    errors.append(
                        f"macro step {macro_id} is core chemistry but declares no state-changing capability"
                    )

            raw_requirements = macro.get("quantity_requirements")
            raw_requirements = raw_requirements if isinstance(raw_requirements, list) else []
            quantity_semantics = record.get("quantity_semantics")
            if not isinstance(quantity_semantics, list):
                errors.append(f"macro step {macro_id} quantity_semantics must be an array")
                quantity_semantics = []
            semantic_indices: List[int] = []
            for item in quantity_semantics:
                if not isinstance(item, dict) or not isinstance(item.get("requirement_index"), int):
                    errors.append(f"macro step {macro_id} quantity semantic index must be integer")
                    continue
                semantic_indices.append(item["requirement_index"])
                kind = str(item.get("kind") or "").strip()
                if kind not in _SEMANTIC_QUANTITY_KINDS:
                    errors.append(f"macro step {macro_id} has invalid quantity kind={kind!r}")
                if not str(item.get("material_identity_id") or "").strip():
                    errors.append(f"macro step {macro_id} quantity semantic lacks material_identity_id")
            if sorted(semantic_indices) != list(range(len(raw_requirements))):
                errors.append(
                    f"macro step {macro_id} quantity semantics must cover indices "
                    f"0..{len(raw_requirements) - 1} exactly"
                )

            identities = record.get("material_identities")
            if not isinstance(identities, list):
                errors.append(f"macro step {macro_id} material_identities must be an array")
                identities = []
            if str(macro.get("试剂/对象", macro.get("reagent_or_object", "")) or "").strip() and not identities:
                errors.append(f"macro step {macro_id} must declare at least one material identity")
            local_identity_ids: Set[str] = set()
            for identity in identities:
                if not isinstance(identity, dict):
                    errors.append(f"macro step {macro_id} material identity must be an object")
                    continue
                identity_id = str(identity.get("identity_id") or "").strip()
                canonical_name = str(identity.get("canonical_name") or "").strip()
                if not identity_id or not canonical_name:
                    errors.append(f"macro step {macro_id} identity_id/canonical_name are required")
                    continue
                if identity_id in local_identity_ids:
                    errors.append(f"macro step {macro_id} duplicates identity_id={identity_id}")
                local_identity_ids.add(identity_id)
            for item in quantity_semantics:
                if isinstance(item, dict) and str(item.get("material_identity_id") or "").strip() not in local_identity_ids:
                    errors.append(f"macro step {macro_id} quantity semantic references unknown material identity")

            joint = record.get("joint_requirements")
            if not isinstance(joint, list):
                errors.append(f"macro step {macro_id} joint_requirements must be an array")
            else:
                for requirement in joint:
                    if not isinstance(requirement, dict):
                        errors.append(f"macro step {macro_id} joint requirement must be an object")
                        continue
                    if str(requirement.get("kind") or "") != "temperature_atmosphere":
                        errors.append(f"macro step {macro_id} has unsupported joint requirement kind")
                    temperature = requirement.get("temperature_c")
                    if not isinstance(temperature, (int, float)) or isinstance(temperature, bool) or not math.isfinite(float(temperature)):
                        errors.append(f"macro step {macro_id} joint requirement needs finite temperature_c")
                    if not isinstance(requirement.get("required"), bool):
                        errors.append(f"macro step {macro_id} joint requirement required must be boolean")
        return list(dict.fromkeys(errors))

    @classmethod
    def _normalize_semantic_analysis_contract(
        cls,
        research_handoff: Dict[str, Any],
        analysis: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Add transparent audit metadata without inventing semantics.

        ``identity_id`` is the cross-step primary key.  Surface names may
        legitimately change with state (for example wet solid vs. dried
        powder), so retain every LLM-provided spelling for audit instead of
        treating byte-for-byte name equality as chemistry truth.
        """
        normalized = copy.deepcopy(analysis)
        macros = [
            item
            for item in research_handoff.get("macro_action_steps", []) or []
            if isinstance(item, dict)
        ]
        macro_indices = {
            cls._semantic_macro_id(item, index): index - 1
            for index, item in enumerate(macros, start=1)
        }
        registry: Dict[str, Dict[str, Set[str]]] = {}
        for assessment in normalized.get("macro_step_assessments", []) or []:
            if not isinstance(assessment, dict):
                continue
            macro_id = str(assessment.get("source_macro_step") or "").strip()
            macro_index = macro_indices.get(macro_id)
            explicit_refs = [
                str(ref).strip()
                for ref in assessment.get("evidence_refs", []) or []
                if isinstance(ref, str)
                and ref.strip().startswith("macro_action_steps[")
            ]
            if macro_index is not None and not explicit_refs:
                assessment["evidence_refs"] = [f"macro_action_steps[{macro_index}]"]
                assessment["evidence_binding"] = (
                    "mechanically_bound_to_frozen_macro_step"
                )
            elif explicit_refs:
                assessment["evidence_refs"] = explicit_refs
            for identity in assessment.get("material_identities", []) or []:
                if not isinstance(identity, dict):
                    continue
                identity_id = str(identity.get("identity_id") or "").strip()
                canonical_name = str(identity.get("canonical_name") or "").strip()
                if not identity_id or not canonical_name:
                    continue
                entry = registry.setdefault(
                    identity_id,
                    {"canonical_name_variants": set(), "roles": set(), "aliases": set()},
                )
                entry["canonical_name_variants"].add(canonical_name)
                role = str(identity.get("role") or "").strip()
                if role:
                    entry["roles"].add(role)
                for alias in identity.get("aliases", []) or []:
                    alias_text = str(alias or "").strip()
                    if alias_text:
                        entry["aliases"].add(alias_text)
        normalized["contract_version"] = "minimal-auditable-v2"
        normalized["material_identity_registry"] = [
            {
                "identity_id": identity_id,
                "canonical_name_variants": sorted(values["canonical_name_variants"]),
                "roles": sorted(values["roles"]),
                "aliases": sorted(values["aliases"]),
            }
            for identity_id, values in sorted(registry.items())
        ]
        return normalized

    def _invoke_semantic_analysis(
        self,
        state: SingleDeviceAgentState,
    ) -> Dict[str, Any]:
        prompt = SEMANTIC_ANALYSIS_TASK_PROMPT.replace(
            "{research_handoff_json}",
            json.dumps(state.research_handoff, ensure_ascii=False, indent=2),
        ).replace("{workstation_descriptions}", state.workstation_descriptions)
        messages = [
            SystemMessage(content=SEMANTIC_ANALYSIS_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]
        print("[single-device-agent] LLM step start: semantic_analysis", flush=True)
        analysis = self._invoke_json_object_with_format_retry(
            state,
            messages,
            step_name="semantic_analysis",
        )
        errors = self._semantic_analysis_errors(state.research_handoff, analysis)
        for repair_attempt in range(
            1, DEFAULT_SEMANTIC_CONTRACT_REPAIR_LIMIT + 1
        ):
            if not errors:
                break
            state.add_log(
                "semantic analysis contract repair "
                f"{repair_attempt}/{DEFAULT_SEMANTIC_CONTRACT_REPAIR_LIMIT}: "
                f"{len(errors)} structural error(s)"
            )
            repair_prompt = (
                prompt
                + "\n\n## 上一份候选 JSON\n"
                + json.dumps(analysis, ensure_ascii=False, indent=2)
                + "\n\n## 仍需修复的最小接口错误\n"
                + "\n".join(f"- {error}" for error in errors)
                + "\n请保留上一候选中已经完成的语义判断，只修复上述结构、覆盖或"
                "一致性问题。重新阅读上方完整上下文并输出一个完整 JSON object；"
                "不要依赖此前调用的隐藏记忆。"
            )
            analysis = self._invoke_json_object_with_format_retry(
                state,
                [
                    SystemMessage(content=SEMANTIC_ANALYSIS_SYSTEM_PROMPT),
                    HumanMessage(content=repair_prompt),
                ],
                step_name=(
                    "semantic_analysis_contract_repair_"
                    f"{repair_attempt}"
                ),
            )
            errors = self._semantic_analysis_errors(state.research_handoff, analysis)
        print("[single-device-agent] LLM step done: semantic_analysis", flush=True)
        if errors:
            raise ValueError("LLM semantic analysis contract invalid: " + "; ".join(errors))
        normalized = self._normalize_semantic_analysis_contract(
            state.research_handoff, analysis
        )
        normalized["analysis_digest"] = self._stable_digest(
            normalized, prefix="semantic_analysis"
        )
        return normalized

    def _semantic_assessment(self, macro_id: Any) -> Dict[str, Any]:
        target = str(macro_id).strip()
        for item in self._active_semantic_analysis.get("macro_step_assessments", []) or []:
            if isinstance(item, dict) and str(item.get("source_macro_step") or "").strip() == target:
                return item
        return {}

    def _semantic_material_ids(self, macro_id: Any) -> Set[str]:
        assessment = self._semantic_assessment(macro_id)
        return {
            str(item.get("identity_id") or "").strip()
            for item in assessment.get("material_identities", []) or []
            if isinstance(item, dict) and str(item.get("identity_id") or "").strip()
        }

    def _apply_semantic_analysis_to_handoff(
        self, research_handoff: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Overlay LLM semantic labels while preserving every raw value."""
        updated = copy.deepcopy(research_handoff)
        for index, macro in enumerate(
            updated.get("macro_action_steps", []) or [], start=1
        ):
            if not isinstance(macro, dict):
                continue
            macro_id = self._semantic_macro_id(macro, index)
            assessment = self._semantic_assessment(macro_id)
            semantics = {
                item.get("requirement_index"): item
                for item in assessment.get("quantity_semantics", []) or []
                if isinstance(item, dict)
                and isinstance(item.get("requirement_index"), int)
            }
            requirements = macro.get("quantity_requirements")
            if not isinstance(requirements, list):
                continue
            for requirement_index, requirement in enumerate(requirements):
                if not isinstance(requirement, dict):
                    continue
                semantic = semantics.get(requirement_index)
                if not isinstance(semantic, dict):
                    continue
                original_kind = str(requirement.get("kind") or "").strip()
                semantic_kind = str(semantic.get("kind") or "").strip()
                requirement["llm_original_kind"] = original_kind
                requirement["kind"] = semantic_kind
                requirement["material_identity_id"] = str(
                    semantic.get("material_identity_id") or ""
                ).strip()
                requirement["semantic_reason"] = str(
                    semantic.get("reason") or assessment.get("reason") or ""
                ).strip()
                requirement["semantic_evidence_refs"] = copy.deepcopy(
                    semantic.get("evidence_refs")
                    or assessment.get("evidence_refs", [])
                )
                if semantic_kind == "whole_batch":
                    requirement.update(
                        {
                            "owner": "process_flow",
                            "required_by": "llm_semantic_review",
                            "device_policy": "preserve_whole_batch",
                            "scientifically_fixed": False,
                        }
                    )
                elif semantic_kind == "runtime_measured_inventory":
                    requirement.update(
                        {
                            "owner": "runtime_observation",
                            "required_by": "llm_semantic_review",
                            "device_policy": "runtime_only",
                            "scientifically_fixed": False,
                        }
                    )
                elif semantic_kind == "target_dose" and original_kind == "semantic_classification_required":
                    requirement.update(
                        {
                            "owner": "device_execution",
                            "required_by": "llm_semantic_review",
                            "device_policy": "device_semantic_decision",
                            "scientifically_fixed": False,
                        }
                    )
                elif semantic_kind == "scientific_input_setpoint" and original_kind == "semantic_classification_required":
                    requirement.update(
                        {
                            "owner": "research_scientific",
                            "required_by": "llm_semantic_review",
                            "device_policy": "scientific_review_before_change",
                            "scientifically_fixed": False,
                        }
                    )
        updated["llm_semantic_analysis_digest"] = self._active_semantic_analysis.get(
            "analysis_digest", ""
        )
        return updated

    def _semantic_joint_hydrogen_requirement(
        self, macro_id: Any
    ) -> Optional[Dict[str, Any]]:
        assessment = self._semantic_assessment(macro_id)
        for item in assessment.get("joint_requirements", []) or []:
            if not isinstance(item, dict) or item.get("required") is not True:
                continue
            if str(item.get("process") or "").strip() == "hydrogen_reduction":
                return item
        return None

    @staticmethod
    def _stations_for_semantic_category(category: str) -> frozenset[str]:
        return {
            "reaction": _REACTION_STATE_CHANGE_WORKSTATIONS,
            "drying": _DRYING_STATE_CHANGE_WORKSTATIONS,
            "calcination": _CALCINATION_STATE_CHANGE_WORKSTATIONS,
            "purification": _PURIFICATION_STATE_CHANGE_WORKSTATIONS,
            "liquid_handling": _LIQUID_HANDLING_WORKSTATIONS,
            "stirring": _STIRRING_PROCESS_WORKSTATIONS,
            "liquid_pouring": _LIQUID_POURING_WORKSTATIONS,
            "ultrasonic_liquid_handling": _ULTRASONIC_LIQUID_HANDLING_WORKSTATIONS,
            "ultrasonic_dispersion": _ULTRASONIC_DISPERSION_WORKSTATIONS,
        }.get(category, frozenset())

    def _fragment_checkpoint_inputs(
        self,
        state: SingleDeviceAgentState,
        *,
        macro_id: str,
        macro_ids: List[str],
        matrix: Any,
        aggregate: Dict[str, Any],
        index: int,
        extra_instruction: str,
    ) -> Dict[str, Any]:
        """Freeze every value that can change one fragment or its merge."""
        return {
            "schema_version": 1,
            "research_handoff_sha256": checkpoint_digest(
                state.research_handoff
            ),
            "semantic_analysis_sha256": checkpoint_digest(
                self._active_semantic_analysis
            ),
            "device_truth_sha256": state.device_truth_sha256,
            "macro_id": macro_id,
            "macro_ids": list(macro_ids),
            "chunk_index": index,
            "chunk_count": len(macro_ids),
            "sample_control_matrix_sha256": checkpoint_digest(matrix),
            "accepted_prefix_sha256": checkpoint_digest(aggregate),
            "extra_instruction_sha256": checkpoint_digest(extra_instruction),
        }

    def _restore_fragment_checkpoint(
        self,
        state: SingleDeviceAgentState,
        *,
        key: str,
        checkpoint_inputs: Dict[str, Any],
        step_name: str,
        macro_id: str,
        macro_ids: List[str],
        matrix: Any,
        aggregate: Dict[str, Any],
        index: int,
    ) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]]:
        """Return a contract-revalidated completed fragment, never a partial."""
        store = getattr(self, "_checkpoint_store", None)
        if store is None:
            return None
        cached = store.read(key, record_reuse=False)
        if not isinstance(cached, dict):
            return None
        fragment = cached.get("fragment")
        candidate = cached.get("candidate")
        if (
            cached.get("schema_version") != 1
            or cached.get("dispatchable") is not False
            or cached.get("feasibility_accepted") is not False
            or cached.get("workflow_json") != {}
            or cached.get("macro_id") != macro_id
            or cached.get("chunk_index") != index
            or cached.get("chunk_count") != len(macro_ids)
            or cached.get("step_name") != step_name
            or cached.get("fragment_binding") != checkpoint_inputs
            or cached.get("fragment_binding_sha256")
            != checkpoint_digest(checkpoint_inputs)
            or cached.get("prefix_sha256") != checkpoint_digest(aggregate)
            or not isinstance(fragment, dict)
            or not isinstance(candidate, dict)
            or cached.get("fragment_sha256") != checkpoint_digest(fragment)
            or cached.get("candidate_sha256") != checkpoint_digest(candidate)
            or candidate.get("status")
            not in {
                "device_plan",
                "success",
                "manual_required",
                "human_review_required",
            }
        ):
            return None
        try:
            recomputed = merge_fragment(
                copy.deepcopy(aggregate),
                copy.deepcopy(fragment),
                macro_id,
                macro_ids,
                matrix,
            )
        except FeasibilityFragmentError:
            return None
        if checkpoint_digest(recomputed) != checkpoint_digest(candidate):
            return None
        completed = cached.get("completed_chunk")
        if not isinstance(completed, dict) or completed != {
            "macro_id": macro_id,
            "step_name": step_name,
            "fragment_digest": self._stable_digest(fragment),
            "candidate_digest": self._stable_digest(recomputed),
            "plan_step_count": len(recomputed.get("device_plan", [])),
        }:
            return None
        store.confirm_reuse(key, step_name)
        self._restore_checkpoint_skill_state(state, cached)
        state.checkpoints = store.summary()
        state.add_log(f"{step_name}: restored completed fragment checkpoint")
        print(
            f"[single-device-agent] checkpoint restored: {step_name}",
            flush=True,
        )
        return copy.deepcopy(fragment), recomputed, copy.deepcopy(completed)

    def _save_fragment_checkpoint(
        self,
        state: SingleDeviceAgentState,
        *,
        key: str,
        checkpoint_inputs: Dict[str, Any],
        step_name: str,
        macro_id: str,
        macro_ids: List[str],
        index: int,
        prefix: Dict[str, Any],
        fragment: Dict[str, Any],
        candidate: Dict[str, Any],
        completed: Dict[str, Any],
    ) -> None:
        """Atomically save only a successfully merged non-dispatchable prefix."""
        store = getattr(self, "_checkpoint_store", None)
        if store is None or candidate.get("status") not in {
            "device_plan",
            "success",
            "manual_required",
            "human_review_required",
        }:
            return
        self._assert_workstation_snapshot_current(state)
        self._sync_skill_load_state(state)
        store.write(
            key,
            step_name,
            {
                "schema_version": 1,
                "macro_id": macro_id,
                "chunk_index": index,
                "chunk_count": len(macro_ids),
                "step_name": step_name,
                "fragment_binding": copy.deepcopy(checkpoint_inputs),
                "fragment_binding_sha256": checkpoint_digest(
                    checkpoint_inputs
                ),
                "prefix_sha256": checkpoint_digest(prefix),
                "fragment": copy.deepcopy(fragment),
                "fragment_sha256": checkpoint_digest(fragment),
                "candidate": copy.deepcopy(candidate),
                "candidate_sha256": checkpoint_digest(candidate),
                "completed_chunk": copy.deepcopy(completed),
                "loaded_workstation_skills": copy.deepcopy(
                    state.loaded_workstation_skills
                ),
                "feasibility_accepted": False,
                "workflow_json": {},
                "dispatchable": False,
            },
        )
        state.checkpoints = store.summary()
        state.add_log(f"{step_name}: saved completed fragment checkpoint")

    def _invoke_feasibility_plan(
        self,
        state: SingleDeviceAgentState,
        *,
        extra_instruction: str = "",
    ) -> Dict[str, Any]:
        """Build one complete candidate before any global approval or translation."""
        mode = os.getenv("CHEM_DEVICE_FEASIBILITY_MODE", "").strip().lower()
        if mode not in {"", "single", "fragmented"}:
            raise DeviceConfigurationError(
                "CHEM_DEVICE_FEASIBILITY_MODE must be single or fragmented"
            )
        fragmented = mode == "fragmented" or (
            not mode and self._contract_version == "v2"
        )
        if not fragmented:
            return self._invoke_feasibility_request(
                state, extra_instruction=extra_instruction,
            )
        macros = state.research_handoff.get("macro_action_steps")
        if not isinstance(macros, list) or not macros or any(
            not isinstance(step, dict) for step in macros
        ):
            raise FeasibilityFragmentError("macro_action_steps must be a nonempty object array")
        macro_ids = [self._semantic_macro_id(step, i) for i, step in enumerate(macros, 1)]
        if any(not value for value in macro_ids) or len(set(macro_ids)) != len(macro_ids):
            raise FeasibilityFragmentError("macro_action_steps must have unique nonempty source IDs")
        matrix = self._sample_matrix_contract_value(state.research_handoff)
        aggregate: Dict[str, Any] = {}
        progress: Dict[str, Any] = {
            "candidate_number": len(state.feasibility_progress) + 1,
            "status": "running",
            "research_handoff_digest": self._stable_digest(state.research_handoff),
            "semantic_analysis_digest": self._stable_digest(self._active_semantic_analysis),
            "device_truth_sha256": state.device_truth_sha256,
            "completed_chunks": [],
            "candidate": {},
        }
        state.feasibility_progress.append(progress)
        try:
            for index, macro_id in enumerate(macro_ids, 1):
                step_name = f"feasibility_device_plan_chunk_{index}_of_{len(macros)}"
                progress["active_macro_id"] = macro_id
                progress["active_step"] = step_name
                prefix = copy.deepcopy(aggregate)
                fragment_context = build_fragment_request_context(
                    state.research_handoff,
                    self._active_semantic_analysis,
                    aggregate,
                    macro_id,
                    macro_ids,
                    matrix,
                )
                fragment_context["observation_evidence_catalog"] = (
                    self._observation_evidence_catalog(state.research_handoff)
                )
                instruction = extra_instruction + "\n\n" + build_fragment_instruction(
                    aggregate, macro_id, macro_ids, matrix,
                )
                store = getattr(self, "_checkpoint_store", None)
                checkpoint_key = ""
                checkpoint_inputs: Dict[str, Any] = {}
                if store is not None:
                    checkpoint_inputs = self._fragment_checkpoint_inputs(
                        state,
                        macro_id=macro_id,
                        macro_ids=macro_ids,
                        matrix=matrix,
                        aggregate=aggregate,
                        index=index,
                        extra_instruction=extra_instruction,
                    )
                    checkpoint_key = store.key(
                        step_name,
                        checkpoint_inputs,
                    )
                restored = (
                    self._restore_fragment_checkpoint(
                        state,
                        key=checkpoint_key,
                        checkpoint_inputs=checkpoint_inputs,
                        step_name=step_name,
                        macro_id=macro_id,
                        macro_ids=macro_ids,
                        matrix=matrix,
                        aggregate=aggregate,
                        index=index,
                    )
                    if checkpoint_key
                    else None
                )
                if restored is not None:
                    fragment, merged, completed = restored
                else:
                    # A merge failure is a local fragment-contract repair. It
                    # never mutates the accepted prefix or consumes a Research
                    # iteration. The scope also prevents nested checkpoint-key
                    # collisions if fragment planning gains sub-stages later.
                    scope = (
                        store.scope(checkpoint_key)
                        if store is not None
                        else nullcontext()
                    )
                    with scope:
                        for fragment_attempt in range(2):
                            fragment = self._invoke_feasibility_request(
                                state,
                                extra_instruction=instruction,
                                step_name=step_name,
                                fragment_context=fragment_context,
                            )
                            try:
                                merged = merge_fragment(
                                    aggregate,
                                    fragment,
                                    macro_id,
                                    macro_ids,
                                    matrix,
                                )
                                break
                            except FeasibilityFragmentError as exc:
                                if fragment_attempt:
                                    raise
                                state.add_log(
                                    f"{step_name}: fragment contract rejected; "
                                    f"one scoped repair: {exc}"
                                )
                                instruction += (
                                    "\n\n本块未通过合并合同，前序 candidate 未变。"
                                    "只重写当前块："
                                    + str(exc)
                                    + "\n被拒绝的本块："
                                    + json.dumps(
                                        fragment,
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    )
                                )
                    completed = {
                        "macro_id": macro_id,
                        "step_name": step_name,
                        "fragment_digest": self._stable_digest(fragment),
                        "candidate_digest": self._stable_digest(merged),
                        "plan_step_count": len(merged.get("device_plan", [])),
                    }
                    self._save_fragment_checkpoint(
                        state,
                        key=checkpoint_key,
                        checkpoint_inputs=checkpoint_inputs,
                        step_name=step_name,
                        macro_id=macro_id,
                        macro_ids=macro_ids,
                        index=index,
                        prefix=prefix,
                        fragment=fragment,
                        candidate=merged,
                        completed=completed,
                    )
                aggregate = merged
                self._assert_workstation_snapshot_current(state)
                progress["candidate"] = copy.deepcopy(aggregate)
                progress["completed_chunks"].append(completed)
                self._sync_skill_load_state(state)
                state.add_log(f"{step_name}: merged; {len(aggregate.get('device_plan', []))} total plan steps; global approval pending")
                if aggregate.get("status") not in {"device_plan", "success", "manual_required", "human_review_required"}:
                    progress["status"] = "blocked"
                    return aggregate
            progress["status"] = "assembled_pending_global_audit"
            progress["active_macro_id"] = None
            return aggregate
        except Exception:
            progress["status"] = "failed"
            # Partial candidates remain diagnostic state, never workflow input.
            raise

    def _invoke_feasibility_request(
        self,
        state: SingleDeviceAgentState,
        *,
        extra_instruction: str = "",
        step_name: str = "feasibility_device_plan",
        fragment_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Run one Stage-1 request with full or bounded fragment context."""
        prompt = FEASIBILITY_PLAN_TASK_PROMPT
        if fragment_context is None:
            prompt_handoff = copy.deepcopy(state.research_handoff)
            prompt_handoff["observation_evidence_catalog"] = (
                self._observation_evidence_catalog(state.research_handoff)
            )
            prompt_semantic = copy.deepcopy(self._active_semantic_analysis)
        else:
            prompt_handoff = copy.deepcopy(fragment_context)
            prompt_semantic = {
                "context_contract": fragment_context.get("context_contract"),
                "current_macro_id": fragment_context.get("current_macro_id"),
                "semantic_assessment": copy.deepcopy(
                    fragment_context.get("semantic_assessment", [])
                ),
                "material_identity_registry": copy.deepcopy(
                    fragment_context.get("material_identity_registry", [])
                ),
                "semantic_analysis_sha256": fragment_context.get(
                    "semantic_analysis_sha256"
                ),
            }
        replacements = {
            "{research_handoff_json}": json.dumps(
                prompt_handoff,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "{workstation_descriptions}": "完整能力目录、全局规则和所选合同见本轮原生工具上下文。",
            "{semantic_analysis_json}": json.dumps(
                prompt_semantic,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        for placeholder, value in replacements.items():
            prompt = prompt.replace(placeholder, value)
        if _has_temporal_addition_stirring(state.research_handoff):
            prompt += (
                "\n\n## 时间语义适配提示（不是硬阻塞）\n"
                "research macro action 包含加液与搅拌的重叠时间语义。先检查液体进样站和磁力搅拌站是否"
                "共同接受同一容器；若接受，必须用同一容器的交替节拍实现，而不是返回 physical_infeasible。"
                "至少规划两批：小份加液 -> 固定时间搅拌 -> 下一批加液，并记录原始要求、近似等级、节拍和需科学复核。"
            )
        if extra_instruction:
            prompt += f"\n\n{extra_instruction}"
        messages = [
            SystemMessage(content=FEASIBILITY_PLAN_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]
        effort_override = os.getenv(
            "CHEM_DEVICE_FEASIBILITY_REASONING_EFFORT", ""
        ).strip()
        effort_attr = ""
        original_effort: Any = None
        if effort_override:
            for candidate in ("reasoning_effort", "_reasoning_effort"):
                if hasattr(self._model, candidate):
                    effort_attr = candidate
                    original_effort = getattr(self._model, candidate)
                    setattr(self._model, candidate, effort_override)
                    state.add_log(
                        "feasibility-plan reasoning effort override: "
                        f"{original_effort} -> {effort_override}"
                    )
                    break
        print(f"[single-device-agent] LLM step start: {step_name}", flush=True)
        try:
            result = self._invoke_json_object_with_format_retry(
                state,
                messages,
                step_name=step_name,
                workstation_tools=True,
            )
        finally:
            if effort_attr:
                setattr(self._model, effort_attr, original_effort)
        print(f"[single-device-agent] LLM step done: {step_name}", flush=True)
        return result

    def _invoke_translation_chunk(
        self,
        state: SingleDeviceAgentState,
        plan_result: Dict[str, Any],
        chunk_steps: List[Dict[str, Any]],
        chunk_index: int,
        total_chunks: int,
        carryover: Dict[str, Any],
        *,
        extra_instruction: str = "",
    ) -> Dict[str, Any]:
        """Translate ONE chunk of the frozen device_plan into workflow steps.

        The FULL plan is passed as read-only context (compact) so cross-chunk
        container choreography is visible, but the model only EMITS steps for
        this chunk's plan_step ids — keeping each call's OUTPUT bounded so a
        large plan never truncates into empty JSON (the D02 failure)."""
        plan_view = {
            "device_plan": plan_result.get("device_plan", []),
            "reagent_slot_plan": plan_result.get("reagent_slot_plan", []),
            "container_plan": plan_result.get("container_plan", []),
            "macro_action": (
                state.research_handoff.get("macro_action")
                if isinstance(state.research_handoff.get("macro_action"), dict)
                else {}
            ),
        }
        chunk_ids = [step.get("plan_step") for step in chunk_steps]
        chunk_stations: List[str] = []
        for step in chunk_steps:
            name = str(step.get("workstation", "")).strip()
            if name and name not in chunk_stations:
                chunk_stations.append(name)
        directive = (
            f"本次是第 {chunk_index + 1}/{total_chunks} 块。"
            f"只输出 plan_step 编号属于 {chunk_ids} 的那些 plan 步骤对应的 workflow 步骤，"
            "范围外的步骤一律不要输出。"
        )
        carryover_text = (
            json.dumps(carryover, ensure_ascii=False)
            if carryover
            else "（首块，无前序容器状态）"
        )
        prompt = TRANSLATION_TASK_PROMPT
        replacements = {
            "{device_plan_json}": json.dumps(plan_view, ensure_ascii=False, indent=2),
            "{chunk_directive}": directive,
            "{carryover_state}": carryover_text,
            "{station_parameter_tables}": self._station_parameter_tables(chunk_stations),
            "{txt_format_reference}": state.txt_format_reference,
            "{json_format_reference}": state.json_format_reference,
        }
        for placeholder, value in replacements.items():
            prompt = prompt.replace(placeholder, value)
        if extra_instruction:
            prompt += f"\n\n{extra_instruction}"
        messages = [
            SystemMessage(content=TRANSLATION_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]
        print(
            f"[single-device-agent] LLM step start: workflow_translation "
            f"(chunk {chunk_index + 1}/{total_chunks})",
            flush=True,
        )
        result = self._invoke_json_object_with_format_retry(
            state,
            messages,
            step_name=(
                f"workflow_translation_chunk_{chunk_index + 1}_of_{total_chunks}"
            ),
        )
        state.add_log(
            f"translation chunk {chunk_index + 1}/{total_chunks}: "
            "received one unambiguous JSON object"
        )
        print(
            f"[single-device-agent] LLM step done: workflow_translation "
            f"(chunk {chunk_index + 1}/{total_chunks})",
            flush=True,
        )
        return result

    def _station_parameter_tables(self, stations: List[str]) -> str:
        """Complete selected contracts: field names are never character-truncated."""
        session = self._workstation_skill_session()
        codes: List[str] = []
        for name in stations:
            code = session.resolve(name)
            if code is None:
                raise ValueError(f"Unknown workstation in translation plan: {name}")
            if code not in codes:
                codes.append(code)
        return (
            "# 全局工作站规则\n" + session.loader.global_rules_for_prompt()
            + "\n\n" + session.format_contracts(codes, origin="translation_plan")
        )

    def _retry_adaptable_feedback(
        self,
        state: SingleDeviceAgentState,
        result: Dict[str, Any],
        research_handoff: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Give any non-hard feasibility complaint one adaptation retry.

        The retry is generic: whatever the exact wording of the complaint, if
        no hard capability gap is evidenced, the model is asked once more to
        derive the requirement from supported steps (or dispose of it as a
        fixed capability / offline condition) instead of rejecting the plan.
        """
        classification = classify_feasibility_result(result, research_handoff)
        hard_items = classification.get("hard", []) or []
        hard_is_device_local = bool(
            hard_items
            and all(self._constraint_is_device_local(item) for item in hard_items)
            and not any(
                self._constraint_is_true_route_gap(item) for item in hard_items
            )
        )
        blocking = self._canonical_stage1_blockers(result)
        has_true_route_gap = any(
            self._constraint_is_true_route_gap(item) for item in blocking
        )
        needs_retry = not has_true_route_gap and (
            (
                classification["is_error"]
                and classification["overall"] != "hard"
            )
            or hard_is_device_local
            or _needs_temporal_mapping_retry(result, research_handoff)
        )
        if not needs_retry:
            return result

        complaints = (
            classification["adaptable"]
            + classification["unverifiable"]
            + (hard_items if hard_is_device_local else [])
        )
        state.add_log(
            "device feedback contained no hard capability gap "
            f"(buckets: adaptable={len(classification['adaptable'])}, "
            f"unverifiable={len(classification['unverifiable'])}); "
            "retrying with derived-constraint instructions"
        )
        instruction = (
            "## 强制重试要求\n"
            "上一轮返回的 feasibility_error 中没有任何硬设备能力缺口证据"
            "（硬缺口仅限必要化学操作缺失、强制安全条件无法满足，或必需设备离线且"
            "完整真源确认无替代）。容量、剂量、分批、料位、容器和配平全部属于 "
            "Device-local，不得回 Research。\n"
            "被拒绝的原因是：\n"
            + "\n".join(f"- {item}" for item in complaints[:6])
            + "\n请重新检查真源并按下列优先级处理每一条原因，然后输出 device_plan（情况 A）：\n"
            "1. 若是过程/时间语义（如边滴入边搅拌、缓慢滴加、分批），在同一兼容容器上派生为"
            "受支持步骤的组合（分批加液、批次间固定转速搅拌等），保持总量、顺序和近似总时长，"
            "并在 temporal_adaptations 中写明 original_requirement、"
            "execution_fidelity=approximated、adaptation_schedule 和 requires_scientific_review=true。\n"
            "2. 若是设备固有能力（如仪器固定辐射源、常温环境），在 feasibility."
            "device_layer_adaptations 中说明依据后按固定能力处理，不作为阻塞。\n"
            "3. 若为观测数据回传（送样测试后返回图谱/判读）或真源确实无容器路径的操作"
            "（如向 96位石英孔板 定量装粉），写入 offline_handoffs；"
            "固体称量/分装必须映射到固体转移+称量工作站，禁止写成人工步骤。\n"
            "4. 跨站运输和 A→B 物料转移默认联通；优先同容器贯穿，只插入最少的必要换瓶步骤，"
            "禁止无意义溶剂位置变化或 A→B→A 往返。某 operation 已明确接受的专用容器（例如"
            "高温高压微反应平台的10ml耐压反应管）视为可供给输入，不能因缺少单独容器物料站而"
            "拒绝；在 container_plan/offline_handoffs 声明预装输入即可。只有确认存在硬设备能力缺口时才保留 "
            "feasibility_error，并在 blocking_constraints 中写明缺失的必要化学操作、"
            "不可满足的强制安全条件，或必需设备离线且无替代的完整硬证据；无法证明满足"
            "但也无硬缺口的条件，"
            "映射后在 temporal_adaptations 或 device_layer_adaptations 中标记 requires_scientific_review=true。"
        )
        try:
            retry_result = self._invoke_feasibility_plan(state, extra_instruction=instruction)
        except Exception as exc:  # pragma: no cover - remote model dependent
            state.add_log(
                "adaptation retry failed with Device internal error: "
                f"{_safe_model_failure_text(exc)}"
            )
            raise
        if isinstance(retry_result, dict):
            return retry_result
        return result

    @staticmethod
    def _canonical_stage1_blockers(result: Dict[str, Any]) -> List[str]:
        """Collect Stage-1 evidence without treating requirement prose as failure.

        ``unsupported_items[].requirement`` describes what Research requested;
        it is not evidence that the request is impossible.  The evidence lives
        in ``reason`` and (when substantive) ``missing_device_capability``.
        Derived ``constraint_classification`` buckets are deliberately ignored
        here so stale/duplicated LLM categories cannot affect routing.
        """

        collected: List[str] = []
        seen: Set[str] = set()
        evidence_keys = (
            "reason",
            "missing_device_capability",
            "message",
            "constraint",
            "blocking_constraint",
            "failure_reason",
            "detail",
            "evidence",
            "text",
            "description",
        )

        def add_text(value: Any) -> None:
            text = str(value or "").strip()
            if not text or text in seen:
                return
            seen.add(text)
            collected.append(text)

        def add_active_evidence(value: Any) -> None:
            """Extract evidence through arbitrary dict/list wrapper depth."""

            if isinstance(value, list):
                for item in value:
                    add_active_evidence(item)
                return
            if isinstance(value, dict):
                # Wrapper objects may add arbitrary nesting. Traverse every
                # dict/list edge, but promote scalar values only when their key
                # is a standard active-evidence field.
                for key, nested in value.items():
                    if key in evidence_keys or isinstance(nested, (dict, list)):
                        add_active_evidence(nested)
                return
            add_text(value)

        def add(value: Any) -> None:
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, dict):
                    # Structured blocking records must contribute only their
                    # active evidence, never ``str(dict)`` (which would keep
                    # metadata-only records routable after evidence cleanup).
                    add_active_evidence(item)
                    continue
                add_text(item)

        def add_unsupported(value: Any) -> None:
            if not isinstance(value, list):
                return
            for item in value:
                if not isinstance(item, dict):
                    add(item)
                    continue
                reason = str(item.get("reason", "") or "").strip()
                missing = str(
                    item.get("missing_device_capability", "") or ""
                ).strip()
                missing_is_sentinel = bool(
                    re.fullmatch(
                        r"(?:无|none|n/?a|not\s+applicable)[。；;,，\s]*",
                        missing,
                        re.I,
                    )
                )
                evidence: List[str] = []
                if reason:
                    evidence.append(reason)
                if (
                    missing
                    and not missing_is_sentinel
                    and missing not in reason
                ):
                    evidence.append(missing)
                if evidence:
                    add(" ".join(evidence))

        feasibility = (
            result.get("feasibility")
            if isinstance(result.get("feasibility"), dict)
            else {}
        )
        add(feasibility.get("blocking_constraints", []))
        add(result.get("blocking_constraints", []))
        if not collected:
            add_unsupported(feasibility.get("unsupported_items", []))

        # Some legacy callers put the only concrete evidence under
        # ``error_package``.  Use it only as a fallback; otherwise it merely
        # duplicates the canonical feasibility fields above.
        if not collected:
            error_package = (
                result.get("error_package")
                if isinstance(result.get("error_package"), dict)
                else {}
            )
            add(error_package.get("blocking_constraints", []))
            add_unsupported(error_package.get("unsupported_items", []))
        return collected

    def _remove_implicit_connectivity_blockers(
        self,
        state: SingleDeviceAgentState,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Drop LLM blockers that contradict the connected-platform contract.

        This is deliberately narrow: it only removes claims that a station,
        container, or sample cannot move because an explicit edge/path is absent.
        Operation capability, target input state, capacity, safety, and availability
        constraints remain untouched.
        """
        def is_feasibility_error_node(node: Any) -> bool:
            return bool(
                isinstance(node, dict)
                and (
                    str(node.get("status", "")).strip().lower()
                    in {"feasibility_error", "unsupported", "not_feasible"}
                    or node.get("feedback_type") == "device_feasibility_error"
                )
            )

        def contains_feasibility_error_node(node: Any) -> bool:
            if is_feasibility_error_node(node):
                return True
            if isinstance(node, dict):
                return any(
                    contains_feasibility_error_node(value)
                    for value in node.values()
                )
            if isinstance(node, list):
                return any(contains_feasibility_error_node(item) for item in node)
            return False

        # A normalized terminal envelope is ``manual_required`` at its root,
        # while the routable Stage-1 evidence lives under
        # ``feasibility_assessment`` and is duplicated in ``error_package``.
        # Do not return before inspecting those nested error nodes.
        if not contains_feasibility_error_node(result):
            return result

        removed: List[str] = []

        def clean_text(value: Any, *, capability_field: bool = False) -> str:
            cleaned, changed = scrub_implicit_connectivity_constraint(
                value,
                capability_field=capability_field,
            )
            if changed:
                removed.append(str(value or ""))
            return cleaned

        evidence_specs = {
            "reason": False,
            "missing_device_capability": True,
            "message": False,
            "constraint": False,
            "blocking_constraint": False,
            "failure_reason": False,
            "detail": False,
            "evidence": False,
            "text": False,
            "description": False,
        }

        def clean_active_evidence(
            value: Any,
            *,
            capability_field: bool = False,
            strings_are_evidence: bool = True,
        ) -> Tuple[Any, bool, bool, bool]:
            """Recursively scrub values nested below an active evidence key.

            Returns ``(cleaned, changed, evidence_seen, evidence_remaining)``.
            Unknown scalar metadata inside wrapper objects is not promoted to
            routing evidence; unknown evidence shapes otherwise remain
            fail-closed.
            """

            if isinstance(value, str) or value in (None, ""):
                if not strings_are_evidence:
                    return value, False, False, bool(value)
                before = str(value or "")
                after = clean_text(
                    before,
                    capability_field=capability_field,
                )
                return after, after != before.strip(), True, bool(after)
            if isinstance(value, list):
                kept: List[Any] = []
                changed = False
                evidence_seen = False
                evidence_remaining = False
                for item in value:
                    if isinstance(item, str):
                        if not strings_are_evidence:
                            kept.append(item)
                            continue
                        cleaned, item_changed, seen_item, remaining_item = (
                            clean_active_evidence(
                                item,
                                capability_field=capability_field,
                                strings_are_evidence=True,
                            )
                        )
                    elif isinstance(item, (dict, list)):
                        cleaned, item_changed, seen_item, remaining_item = (
                            clean_active_evidence(
                                item,
                                capability_field=capability_field,
                                strings_are_evidence=strings_are_evidence,
                            )
                        )
                    else:
                        kept.append(item)
                        evidence_remaining = True
                        continue
                    changed = changed or item_changed
                    evidence_seen = evidence_seen or seen_item
                    evidence_remaining = evidence_remaining or remaining_item
                    if remaining_item or not seen_item:
                        kept.append(cleaned)
                return kept, changed, evidence_seen, evidence_remaining
            if isinstance(value, dict):
                candidate = copy.deepcopy(value)
                changed = False
                evidence_seen = False
                evidence_remaining = False
                for key, nested in list(candidate.items()):
                    if key in evidence_specs:
                        cleaned, item_changed, seen_item, remaining_item = (
                            clean_active_evidence(
                                nested,
                                capability_field=evidence_specs[key],
                                strings_are_evidence=True,
                            )
                        )
                    elif isinstance(nested, (dict, list)):
                        cleaned, item_changed, seen_item, remaining_item = (
                            clean_active_evidence(
                                nested,
                                capability_field=capability_field,
                                strings_are_evidence=False,
                            )
                        )
                    else:
                        continue
                    changed = changed or item_changed
                    evidence_seen = evidence_seen or seen_item
                    evidence_remaining = evidence_remaining or remaining_item
                    if seen_item and not remaining_item:
                        candidate.pop(key, None)
                    else:
                        candidate[key] = cleaned
                if not evidence_seen:
                    # An opaque object supplied directly as evidence remains
                    # fail-closed rather than being silently discarded.
                    evidence_remaining = bool(candidate)
                return candidate, changed, evidence_seen, evidence_remaining
            return value, False, True, True

        def clean_constraint_list(value: Any) -> Any:
            if not isinstance(value, list):
                if value in (None, ""):
                    return value
                cleaned = clean_text(value)
                return [cleaned] if cleaned else []
            kept: List[Any] = []
            for item in value:
                if isinstance(item, str):
                    cleaned = clean_text(item)
                    if cleaned:
                        kept.append(cleaned)
                elif isinstance(item, dict):
                    (
                        candidate,
                        evidence_changed,
                        evidence_seen,
                        evidence_remaining,
                    ) = clean_active_evidence(
                        copy.deepcopy(item),
                        strings_are_evidence=False,
                    )
                    if not evidence_seen:
                        # A structured blocker with only macro_step/category/
                        # requirement metadata contains no routable evidence.
                        continue
                    if evidence_changed and not evidence_remaining:
                        # Remaining fields such as macro_step, requirement and
                        # constraint_category are context/metadata, not proof of
                        # a failure.  Do not let canonical routing stringify it.
                        continue
                    walk(candidate)
                    kept.append(candidate)
                else:
                    walk(item)
                    kept.append(item)
            return kept

        def clean_unsupported(value: Any) -> Any:
            if not isinstance(value, list):
                return value
            kept: List[Any] = []
            for item in value:
                if isinstance(item, str):
                    cleaned = clean_text(item)
                    if cleaned:
                        kept.append(cleaned)
                    continue
                if not isinstance(item, dict):
                    kept.append(item)
                    continue
                candidate = copy.deepcopy(item)
                evidence_changed = False
                for key, capability_field in (
                    ("reason", False),
                    ("missing_device_capability", True),
                ):
                    if key not in candidate:
                        continue
                    before = str(candidate.get(key, "") or "")
                    after = clean_text(
                        before,
                        capability_field=capability_field,
                    )
                    if after != before.strip():
                        evidence_changed = True
                    if after:
                        candidate[key] = after
                    else:
                        candidate.pop(key, None)
                if evidence_changed:
                    # A recommendation based on a disproved transfer premise
                    # must not survive and tell the orchestrator/user to alter
                    # Research. Independent evidence remains in reason/missing.
                    candidate.pop("suggested_research_revision", None)
                reason = str(candidate.get("reason", "") or "").strip()
                missing = str(
                    candidate.get("missing_device_capability", "") or ""
                ).strip()
                missing_is_sentinel = bool(
                    re.fullmatch(
                        r"(?:无|none|n/?a|not\s+applicable)[。；;,，\s]*",
                        missing,
                        re.I,
                    )
                )
                if evidence_changed and not reason and (
                    not missing or missing_is_sentinel
                ):
                    continue
                walk(candidate)
                kept.append(candidate)
            return kept

        def clean_classification(value: Any) -> Any:
            if not isinstance(value, dict):
                return value
            for bucket in ("hard", "adaptable", "unverifiable"):
                if bucket in value:
                    value[bucket] = clean_constraint_list(value[bucket])
            for key, nested in list(value.items()):
                if key not in {"hard", "adaptable", "unverifiable"}:
                    walk(nested)
            return value

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in list(node.items()):
                    if key == "blocking_constraints":
                        node[key] = clean_constraint_list(value)
                    elif key == "unsupported_items":
                        node[key] = clean_unsupported(value)
                    elif key == "constraint_classification":
                        node[key] = clean_classification(value)
                    else:
                        walk(value)
                if (
                    is_feasibility_error_node(node)
                    and not self._canonical_stage1_blockers(node)
                ):
                    node.pop("recommendation_to_research_agent", None)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(result)

        if removed:
            # If every canonical Stage-1 evidence item was disproved by the
            # connected-platform contract, its Research-facing recommendation
            # is stale as well.  Leaving that prose behind is harmless to the
            # classifier but dangerous to downstream diagnostics and humans.
            if not self._canonical_stage1_blockers(result):
                result.pop("recommendation_to_research_agent", None)
            feasibility = result.get("feasibility")
            if not isinstance(feasibility, dict):
                feasibility = {}
                result["feasibility"] = feasibility
            adaptations = feasibility.get("device_layer_adaptations", [])
            adaptations = list(adaptations) if isinstance(adaptations, list) else []
            adaptation = (
                "已按平台默认联通契约消除纯跨站/容器转移路径阻塞；"
                "同容器跨站直接移动，必要 A→B 换瓶使用最少转移并保留样品谱系。"
            )
            if adaptation not in adaptations:
                adaptations.append(adaptation)
            feasibility["device_layer_adaptations"] = adaptations
            feasibility["connected_platform_guard"] = {
                "status": "applied",
                "removed_or_trimmed_constraint_count": len(removed),
                "non_routing_history_note": (
                    "device_capability_summary.not_supported 与既有 "
                    "device_layer_adaptations 仅作为历史 LLM 诊断保留；"
                    "不参与当前路由或可行性判定。"
                ),
            }
            state.add_log(
                "connected-platform guard removed "
                f"{len(removed)} implicit transport/transfer blocker(s)"
            )
        return result

    @staticmethod
    def _is_core_chemistry_macro(text: Any) -> bool:
        return bool(
            re.search(
                r"还原|氧化|煅烧|焙烧|退火|水热|溶剂热|热处理|"
                r"(?:化学|催化|合成|聚合|沉淀|络合).{0,12}反应|"
                r"reduc(?:e|ed|tion)|oxid(?:ize|ation)|calcination|anneal|"
                r"hydrothermal|solvothermal|chemical\s+reaction",
                str(text or ""),
                re.I,
            )
        )

    @staticmethod
    def _is_observation_only_macro(text: Any) -> bool:
        """Return true when the *action* is characterization/data return.

        Research characterization steps often describe the experimental
        matrix they compare (for example, an ``XRD`` step may mention a
        ``reduction-temperature matrix``).  Looking for chemistry words in the
        fully serialized step therefore mistakes an observation for a second
        reduction operation.  Prefer the action/name fields; only fall back to
        the whole value for legacy strings.
        """
        value = _json_text(text) if isinstance(text, (dict, list)) else str(text or "")
        action_value = value
        if isinstance(text, dict):
            action_parts = [
                str(text.get(key, "") or "")
                for key in (
                    "操作",
                    "operation",
                    "action",
                    "objective",
                    "name",
                    "handoff_type",
                    "boundary_type",
                    "stage_completion_rule",
                )
                if text.get(key) not in (None, "")
            ]
            if action_parts:
                action_value = "\n".join(action_parts)
        characterization = bool(
            re.search(
                r"XRD|PXRD|XPS|SEM|TEM|Raman|XAS|FTIR|UV[- ]?Vis|"
                r"图谱|光谱|衍射|显微|表征|观测|观察|数据回传|采集",
                action_value,
                re.I,
            )
        )
        return characterization and not SingleDeviceAgent._is_core_chemistry_macro(
            action_value
        )

    @classmethod
    def _handoff_executes_core_chemistry(cls, handoff: Any) -> bool:
        """Distinguish an offline reaction from a post-reaction transfer.

        A container handoff can legitimately refer to a ``hydrothermal
        reaction liquid`` without executing the hydrothermal reaction itself.
        Core chemistry is illegal offline only when the handoff action actually
        performs that chemistry.  The frozen Research-step capability gate
        below independently catches omitted or mis-mapped reactions.
        """
        if not isinstance(handoff, dict):
            value = str(handoff or "")
        else:
            value = _json_text(handoff)
        if cls._is_observation_only_macro(handoff):
            return False
        if cls._requires_controlled_hydrogen_reduction(value):
            return True
        core = (
            r"还原|氧化|煅烧|焙烧|退火|水热|溶剂热|热处理|"
            r"reduc(?:e|ed|tion)|oxid(?:ize|ation)|calcination|anneal|"
            r"hydrothermal|solvothermal"
        )
        explicit_action = bool(
            re.search(
                rf"(?:离线|外部|人工|送至|进行|执行|完成|实施|开展)"
                rf".{{0,48}}(?:{core})|(?:{core}).{{0,28}}"
                r"(?:处理|反应|保温|加热|工序|步骤)",
                value,
                re.I,
            )
        )
        if not explicit_action:
            return False
        # A handoff whose action is solely moving a named post-reaction sample
        # is not an offline reaction, even if the material name contains
        # ``水热反应液`` or a similar history label.
        transfer_only = bool(
            re.search(r"转移|换瓶|分装|移入|移至|容器路径|container\s+transfer", value, re.I)
        ) and not bool(
            re.search(
                rf"(?:离线|外部|人工).{{0,32}}(?:{core})|"
                rf"(?:{core}).{{0,20}}(?:保温|加热|处理)",
                value,
                re.I,
            )
        )
        return explicit_action and not transfer_only

    def _audit_core_chemistry_offline_handoffs(
        self,
        state: SingleDeviceAgentState,
        plan_result: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Reject Research-owned core chemistry disguised as a handoff.

        Observation/data return and a genuine input boundary remain legal.
        A hard route proof is attached only when the handoff is bound to a
        frozen Research macro step and the complete truth source proves the
        requested joint capability absent.
        """
        if self._active_semantic_analysis:
            valid_classes = {
                "observation_data_return",
                "input_boundary",
                "post_process_transfer",
                "core_chemistry_execution",
                "unsupported_external_operation",
            }
            semantic_findings: List[Dict[str, Any]] = []
            known_macro_ids = {
                str(item.get("source_macro_step") or "").strip()
                for item in self._active_semantic_analysis.get(
                    "macro_step_assessments", []
                ) or []
                if isinstance(item, dict)
            }
            for index, handoff in enumerate(
                plan_result.get("offline_handoffs", []) or [], start=1
            ):
                if not isinstance(handoff, dict):
                    continue
                classification = str(
                    handoff.get("semantic_classification") or ""
                ).strip()
                reason = str(handoff.get("semantic_reason") or "").strip()
                evidence_refs = handoff.get("semantic_evidence_refs")
                sources = self._source_macro_step_ids(handoff)
                if (
                    classification not in valid_classes
                    or not reason
                    or not isinstance(evidence_refs, list)
                    or not evidence_refs
                ):
                    semantic_findings.append(
                        {
                            "type": "missing_llm_handoff_semantics",
                            "handoff_index": index,
                            "message": (
                                f"offline_handoff[{index}] 缺少完整 LLM 语义分类；"
                                "必须给 semantic_classification/semantic_reason/"
                                "semantic_evidence_refs，确定性层不再从文字猜测。"
                            ),
                        }
                    )
                    continue
                if not sources or any(source not in known_macro_ids for source in sources):
                    semantic_findings.append(
                        {
                            "type": "invalid_llm_handoff_binding",
                            "handoff_index": index,
                            "message": (
                                f"offline_handoff[{index}] 的 LLM 语义分类未完整绑定"
                                "冻结 Research macro step。"
                            ),
                        }
                    )
                    continue
                if classification == "core_chemistry_execution":
                    semantic_findings.append(
                        {
                            "type": "illegal_core_chemistry_offline_handoff",
                            "handoff_index": index,
                            "source_macro_steps": sources,
                            "message": (
                                f"offline_handoff[{index}] 经 LLM 完整上下文审查被分类为"
                                " core_chemistry_execution；核心化学不得由离线交接替代。"
                            ),
                        }
                    )
            return semantic_findings

        research_steps: Dict[str, Dict[str, Any]] = {}
        for index, step in enumerate(
            state.research_handoff.get("macro_action_steps", []) or [], start=1
        ):
            if not isinstance(step, dict):
                continue
            source = str(step.get("步骤序号", step.get("step", index)))
            research_steps[source] = step

        findings: List[Dict[str, Any]] = []
        for index, handoff in enumerate(
            plan_result.get("offline_handoffs", []) or [], start=1
        ):
            if not isinstance(handoff, dict):
                continue
            sources = self._source_macro_step_ids(handoff)
            bound = [research_steps[source] for source in sources if source in research_steps]
            handoff_text = _json_text(handoff)
            handoff_executes_core = self._handoff_executes_core_chemistry(handoff)
            if not sources:
                # An unbound core handoff is auditable, but never strong enough
                # to trigger Research.  The local repair must bind or remove it.
                if handoff_executes_core:
                    findings.append(
                        {
                            "type": "illegal_core_chemistry_offline_handoff",
                            "handoff_index": index,
                            "source_macro_steps": [],
                            "binding_status": "unverified",
                            "proof": {
                                "status": "unverified",
                                "reason": "offline core chemistry has no reliable source_macro_step binding",
                            },
                            "message": (
                                f"offline_handoff[{index}] 包含核心化学处理，但未可靠绑定 "
                                "Research source_macro_step；不得签发证书，也不得凭此"
                                "返回 Research，必须在 Device 同层修复或人工确认。"
                            ),
                        }
                    )
                continue
            if not handoff_executes_core:
                # The handoff may be bound to a chemistry macro only because
                # it moves that macro's product.  Core capability is audited
                # directly from every frozen Research step below, so a plain
                # post-reaction transfer must not be relabelled as chemistry.
                continue
            if len(bound) != len(sources):
                findings.append(
                    {
                        "type": "illegal_core_chemistry_offline_handoff",
                        "handoff_index": index,
                        "source_macro_steps": sources,
                        "binding_status": "invalid",
                        "proof": {
                            "status": "unverified",
                            "reason": "handoff references a nonexistent Research macro step",
                        },
                        "message": (
                            f"offline_handoff[{index}] 的 source_macro_steps={sources} "
                            "无法完整绑定冻结的 Research macro plan。"
                        ),
                    }
                )
                continue
            # Audit each binding independently.  Aggregating source=[1..6]
            # used to let an XRD/data handoff inherit an unrelated earlier H2
            # step and fabricate a route gap.
            for source, macro in zip(sources, bound):
                if self._is_observation_only_macro(macro):
                    continue
                macro_text = _json_text(macro)
                if not self._is_core_chemistry_macro(macro_text):
                    continue
                proof = self._prove_high_temperature_hydrogen_gap(
                    macro_text + "\n" + handoff_text
                )
                findings.append(
                    {
                        "type": "illegal_core_chemistry_offline_handoff",
                        "handoff_index": index,
                        "source_macro_steps": [source],
                        "binding_status": "bound",
                        "proof": proof,
                        "message": (
                            f"offline_handoff[{index}] 将 Research macro step "
                            f"{source} 要求的核心化学处理改写为离线交接。"
                            "核心还原/氧化/煅烧/退火/反应不是 observation 数据回传；"
                            "证书前必须映射到单一兼容工作站，或以完整真源证明联合"
                            "能力硬缺口。"
                        ),
                    }
                )
        return findings

    def _verified_stage1_core_route_gap_result(
        self,
        state: SingleDeviceAgentState,
        plan_result: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Build a proof-only pre-certificate feasibility error.

        The proof is derived directly from every frozen Research macro step,
        never from the candidate's ``offline_handoffs``.  Consequently an LLM
        cannot evade a true gap by deleting the step, mapping it to an
        incompatible workstation, or omitting the handoff.  Local quantity,
        container, and schema findings are intentionally not copied into the
        Research package.
        """
        if state.feasibility_accepted or state.feasibility_certificate:
            return None
        proved: List[Dict[str, Any]] = []
        for index, macro in enumerate(
            state.research_handoff.get("macro_action_steps", []) or [], start=1
        ):
            if not isinstance(macro, dict):
                continue
            source = str(
                macro.get("macro_step_id")
                or macro.get("logical_step_id")
                or macro.get("步骤序号", macro.get("step", index))
            )
            if self._active_semantic_analysis:
                assessment = self._semantic_assessment(source)
                if (
                    isinstance(assessment.get("observation_only"), dict)
                    and assessment["observation_only"].get("value") is True
                ):
                    continue
                structured_requirement = self._semantic_joint_hydrogen_requirement(
                    source
                )
                if not structured_requirement:
                    continue
                proof = self._prove_high_temperature_hydrogen_gap(
                    f"{structured_requirement.get('temperature_c')} °C H2 reduction"
                )
                proof["semantic_requirement"] = copy.deepcopy(
                    structured_requirement
                )
            else:
                if self._is_observation_only_macro(macro):
                    continue
                macro_text = _json_text(macro)
                if not self._requires_controlled_hydrogen_reduction(macro_text):
                    continue
                proof = self._prove_high_temperature_hydrogen_gap(macro_text)
            if proof.get("status") != "proved_gap":
                continue
            proved.append(
                {
                    "source_macro_steps": [source],
                    "proof": copy.deepcopy(proof),
                }
            )
        if not proved:
            return None
        blocking: List[str] = []
        unsupported: List[Dict[str, Any]] = []
        for finding in proved:
            proof = finding["proof"]
            sources = finding.get("source_macro_steps", [])
            temperature = proof.get("required_temperature_c")
            hydrogen_stations = proof.get("hydrogen_reduction_workstations", [])
            station_limits = ", ".join(
                f"{item.get('workstation')} max {item.get('temperature_ceiling_c')}°C"
                for item in hydrogen_stations
                if isinstance(item, dict)
            ) or "no hydrogen-reduction station declared"
            message = (
                f"必要化学操作 macro step(s) {sources} 需要 {temperature:g}°C "
                "受控 H2/Ar 还原；完整 lab-design-all 真源不存在任何单一"
                "工作站同时满足该必要温度和氢气还原能力，且没有可替代工作站；"
                f"氢气还原站上限证据：{station_limits}。"
            )
            blocking.append(message)
            unsupported.append(
                {
                    "macro_steps": sources,
                    "requirement": (
                        f"{temperature:g}°C controlled 5 vol% H2/Ar reduction"
                    ),
                    "reason": "complete truth proves no single compatible workstation",
                    "missing_device_capability": (
                        "joint high-temperature + controlled hydrogen-reduction capability"
                    ),
                    "deterministic_proof": copy.deepcopy(proof),
                }
            )
        return {
            "status": "feasibility_error",
            "feedback_type": "device_feasibility_error",
            "feasibility": {
                "is_feasible": False,
                "blocking_constraints": blocking,
                "unsupported_items": unsupported,
                "deterministic_route_gap_proof": [
                    copy.deepcopy(finding["proof"]) for finding in proved
                ],
            },
            "recommendation_to_research_agent": (
                "请将受控氢气还原梯度改为当前完整设备真源可执行的"
                "科学路线；不得把核心化学反应伪装成 offline_handoff。"
            ),
            "assessment_source": "deterministic_complete_workstation_joint_capability_audit",
        }

    def _audit_research_core_joint_capability_mapping(
        self,
        state: SingleDeviceAgentState,
        plan_result: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Fail closed on unproved or incorrectly mapped joint requirements.

        A proved absence is routed by
        :meth:`_verified_stage1_core_route_gap_result` before any certificate.
        This audit handles the other two safe states: incomplete truth stays
        Device/human, while a satisfiable requirement must be mapped to one of
        the truth-proven compatible workstations.
        """
        if state.feasibility_accepted or state.feasibility_certificate:
            return []
        plan_steps = [
            item
            for item in plan_result.get("device_plan", []) or []
            if isinstance(item, dict)
        ]
        entries, _ = self._complete_lab_design_truth()
        labels_by_code: Dict[str, Set[str]] = {}
        for entry in entries:
            code = str(entry.get("station_name", "")).strip()
            if not code:
                continue
            labels_by_code[code] = {
                str(entry.get(key, "")).strip().casefold()
                for key in ("station_name", "display_name")
                if str(entry.get(key, "")).strip()
            }

        findings: List[Dict[str, Any]] = []
        for index, macro in enumerate(
            state.research_handoff.get("macro_action_steps", []) or [], start=1
        ):
            if not isinstance(macro, dict):
                continue
            source = str(macro.get("步骤序号", macro.get("step", index)))
            if self._active_semantic_analysis:
                assessment = self._semantic_assessment(source)
                if (
                    isinstance(assessment.get("observation_only"), dict)
                    and assessment["observation_only"].get("value") is True
                ):
                    continue
                structured_requirement = self._semantic_joint_hydrogen_requirement(
                    source
                )
                if not structured_requirement:
                    continue
                proof = self._prove_high_temperature_hydrogen_gap(
                    f"{structured_requirement.get('temperature_c')} °C H2 reduction"
                )
                proof["semantic_requirement"] = copy.deepcopy(
                    structured_requirement
                )
            else:
                if self._is_observation_only_macro(macro):
                    continue
                macro_text = _json_text(macro)
                if not self._requires_controlled_hydrogen_reduction(macro_text):
                    continue
                proof = self._prove_high_temperature_hydrogen_gap(macro_text)
            status = str(proof.get("status", "unverified"))
            if status == "proved_gap":
                findings.append(
                    {
                        "type": "research_core_joint_capability_gap",
                        "source_macro_steps": [source],
                        "proof": copy.deepcopy(proof),
                        "message": (
                            f"Research macro step {source} 的必要温度与受控 H2/Ar "
                            "还原联合能力已由完整真源证明不存在；证书前必须走"
                            " research_replan_required，不能删除、离线化或错映射该步骤。"
                        ),
                    }
                )
                continue
            if status != "satisfied":
                findings.append(
                    {
                        "type": "research_core_joint_capability_unverified",
                        "source_macro_steps": [source],
                        "proof": copy.deepcopy(proof),
                        "message": (
                            f"Research macro step {source} 的必要温度与受控 H2/Ar "
                            "还原联合能力真源不完整，不能签发证书；仅可在 Device/"
                            "人工范围补证据，不得据此返回 Research。"
                        ),
                    }
                )
                continue
            compatible = {
                str(item).strip()
                for item in proof.get("compatible_workstations", []) or []
                if str(item).strip()
            }
            compatible_labels = {
                label
                for code in compatible
                for label in labels_by_code.get(code, {code.casefold()})
            }
            mapped = [
                step
                for step in plan_steps
                if source in self._source_macro_step_ids(step)
            ]
            mapped_stations = {
                str(step.get("workstation", "")).strip()
                for step in mapped
                if str(step.get("workstation", "")).strip()
            }
            if not any(
                station.casefold() in compatible_labels
                for station in mapped_stations
            ):
                findings.append(
                    {
                        "type": "research_core_joint_capability_mapping_mismatch",
                        "source_macro_steps": [source],
                        "proof": copy.deepcopy(proof),
                        "message": (
                            f"Research macro step {source} 必须映射到同时满足必要温度"
                            f"与受控 H2/Ar 还原的工作站 {sorted(compatible)}；"
                            f"当前在线映射为 {sorted(mapped_stations) or ['<missing>']}。"
                        ),
                    }
                )
        return findings

    @staticmethod
    def _candidate_claims_same_core_gap(candidate: Dict[str, Any]) -> bool:
        feasibility = candidate.get("feasibility")
        if not isinstance(feasibility, dict):
            return False
        text = _json_text(feasibility.get("blocking_constraints", []))
        return bool(
            SingleDeviceAgent._requires_controlled_hydrogen_reduction(text)
            and re.search(r"缺少|不存在|没有|不支持|无法|no\s+alternative|missing", text, re.I)
        )

    def _truth_workstation_code(self, station_name: Any) -> str:
        """Resolve a plan station only when it exists in the active truth set.

        Production ``WorkstationLoader`` exposes ``get_by_code`` and its
        alias resolver.  Minimal legacy/fake loaders used by isolated tests do
        not, so they retain exact-name behavior; they cannot introduce a code
        into the production truth registry.
        """

        station_text = str(station_name or "").strip()
        if not station_text:
            return ""
        loader = self._workstation_loader
        get_by_code = getattr(loader, "get_by_code", None)
        if not callable(get_by_code):
            return station_text
        if isinstance(get_by_code(station_text), dict):
            return station_text
        # Code-shaped tokens are an exact namespace.  Never let the loader's
        # fuzzy display-name resolver turn a nonexistent code such as
        # ``Magnetic_Stirring_Workstation_V1`` into the longer, real
        # ``Heating_Magnetic_Stirring_Workstation_V1`` by substring match.
        if re.fullmatch(
            r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*_V\d+", station_text
        ):
            return ""
        resolver = getattr(loader, "_map_station_name_to_code", None)
        resolved = resolver(station_text) if callable(resolver) else None
        resolved_text = str(resolved or "").strip()
        if resolved_text and isinstance(get_by_code(resolved_text), dict):
            return resolved_text
        return ""

    def _frozen_material_transition_coverage_findings(
        self,
        research_handoff: Dict[str, Any],
        plan_result: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Audit frozen state-changing macros before signing feasibility.

        This deliberately does not run quantity/yield judgement.  It only
        proves that every frozen material-state macro has at least one mapped
        Device step and an existing bidirectional transition record, so an LLM
        cannot replace a reaction with mere reagent pickup and receive a
        feasibility certificate.
        """

        plan_steps = [
            step
            for step in plan_result.get("device_plan", []) or []
            if isinstance(step, dict)
        ]
        transitions = {
            str(item.get("transition_id") or "").strip(): item
            for item in plan_result.get("material_transitions", []) or []
            if isinstance(item, dict)
            and str(item.get("transition_id") or "").strip()
        }
        findings: List[Dict[str, Any]] = []
        for index, macro in enumerate(
            research_handoff.get("macro_action_steps", []) or [], start=1
        ):
            if not isinstance(macro, dict):
                continue
            macro_id = str(
                macro.get("步骤序号", macro.get("step", index))
            ).strip()
            frozen_operation = str(
                macro.get("操作", macro.get("operation", ""))
            )
            if self._active_semantic_analysis:
                assessment = self._semantic_assessment(macro_id)
                declared_categories = [
                    str(item.get("category") or "").strip()
                    for item in assessment.get("required_capabilities", []) or []
                    if isinstance(item, dict)
                ]
                state_category_requirements = {
                    category: self._stations_for_semantic_category(category)
                    for category in declared_categories
                    if category in {"reaction", "drying", "calcination", "purification"}
                }
                non_state_category_requirements = {
                    category: self._stations_for_semantic_category(category)
                    for category in declared_categories
                    if category not in {"reaction", "drying", "calcination", "purification"}
                }
                frozen_processing_text = "LLM semantic analysis"
            else:
                # Explicit legacy/offline diagnostic mode only.  Production
                # uses the frozen LLM semantic contract above.
                frozen_processing_text = _frozen_required_processing_text(macro)
                state_category_requirements = (
                    _required_state_change_workstation_categories(
                        frozen_processing_text
                    )
                )
                non_state_category_requirements = (
                    _required_non_state_workstation_categories(
                        frozen_processing_text
                    )
                )
            if not (
                state_category_requirements
                or non_state_category_requirements
            ):
                continue
            external_distribution_only = bool(
                not self._active_semantic_analysis
                and
                _FROZEN_EXTERNAL_MATERIAL_PATTERN.search(_json_text(macro))
                and _FROZEN_EXTERNAL_DISTRIBUTION_OPERATION_PATTERN.search(
                    frozen_operation
                )
                and not _FROZEN_HARD_STATE_CHANGE_OPERATION_PATTERN.search(
                    frozen_processing_text
                )
                and not non_state_category_requirements
            )
            if external_distribution_only:
                continue
            category_requirements = {
                **state_category_requirements,
                **non_state_category_requirements,
            }
            mapped_steps = [
                step
                for step in plan_steps
                if macro_id in self._source_macro_step_ids(step)
            ]
            missing_categories: List[str] = []
            for category, allowed_stations in category_requirements.items():
                requires_state_change = (
                    category in state_category_requirements
                )
                category_covered = False
                for step in mapped_steps:
                    plan_step_id = str(
                        step.get("plan_step") or ""
                    ).strip()
                    event_kind = str(
                        step.get("material_event_kind") or ""
                    ).strip()
                    workstation = str(
                        step.get("workstation") or ""
                    ).strip()
                    truth_workstation_code = self._truth_workstation_code(
                        workstation
                    )
                    transition_ids = {
                        str(value).strip()
                        for value in step.get(
                            "material_transition_ids", []
                        ) or []
                        if str(value).strip()
                    } if isinstance(
                        step.get("material_transition_ids"), list
                    ) else set()
                    if (
                        truth_workstation_code not in allowed_stations
                    ):
                        continue
                    if not requires_state_change:
                        category_covered = True
                        break
                    if event_kind != "state_change" or not transition_ids:
                        continue
                    for transition_id in transition_ids:
                        transition = transitions.get(transition_id)
                        if not isinstance(transition, dict):
                            continue
                        if str(
                            transition.get("transition_kind") or ""
                        ).strip() != "state_change":
                            continue
                        transition_plan_refs = {
                            str(value).strip()
                            for value in transition.get(
                                "source_plan_steps", []
                            ) or []
                            if str(value).strip()
                        } if isinstance(
                            transition.get("source_plan_steps"), list
                        ) else set()
                        transition_macro_refs = {
                            str(value).strip()
                            for value in transition.get(
                                "source_macro_steps", []
                            ) or []
                            if str(value).strip()
                        } if isinstance(
                            transition.get("source_macro_steps"), list
                        ) else set()
                        if (
                            plan_step_id in transition_plan_refs
                            and macro_id in transition_macro_refs
                        ):
                            category_covered = True
                            break
                    if category_covered:
                        break
                if not category_covered:
                    missing_categories.append(category)
            if missing_categories:
                findings.append(
                    {
                        "type": "frozen_material_transition_coverage_missing",
                        "source_macro_steps": [macro_id],
                        "missing_processing_categories": missing_categories,
                        "message": (
                            f"冻结物料状态变化 macro step {macro_id}（{frozen_operation}）"
                            "必须在签发 feasibility certificate 前逐类由真源支持"
                            "的 Device workstation 覆盖；物料状态类别还必须有"
                            " state_change transition 双向绑定。"
                            "不能只覆盖复合操作中的一类、只拿取原料或伪造"
                            " process_same_material。缺失类别="
                            f"{missing_categories}；逐类允许工作站="
                            f"{{{', '.join(f'{category}: {sorted(category_requirements[category])}' for category in missing_categories)}}}。"
                        ),
                    }
                )
        return findings

    @staticmethod
    def _device_adaptable_target(requirement: Dict[str, Any]) -> bool:
        return bool(
            str(requirement.get("kind") or "").strip() == "target_dose"
            and not bool(requirement.get("scientifically_fixed"))
            and (
                str(requirement.get("device_policy") or "").strip()
                in {"device_semantic_decision", "omit_if_not_skill_required"}
                or (
                    str(requirement.get("source") or "").strip()
                    == "agent_proposed"
                    and str(requirement.get("adjustability") or "").strip()
                    == "scientific_review_required"
                )
            )
        )

    def _semantically_classified_quantity_requirement(
        self,
        macro_id: str,
        requirement_index: int,
        requirement: Dict[str, Any],
    ) -> Dict[str, Any]:
        classified = copy.deepcopy(requirement)
        if not self._active_semantic_analysis:
            return classified
        assessment = self._semantic_assessment(macro_id)
        match = next(
            (
                item
                for item in assessment.get("quantity_semantics", []) or []
                if isinstance(item, dict)
                and item.get("requirement_index") == requirement_index
            ),
            None,
        )
        if not isinstance(match, dict):
            return classified
        classified["kind"] = str(match.get("kind") or "").strip()
        classified["material_identity_id"] = str(
            match.get("material_identity_id") or ""
        ).strip()
        classified["semantic_reason"] = str(
            match.get("reason") or assessment.get("reason") or ""
        ).strip()
        classified["semantic_evidence_refs"] = copy.deepcopy(
            match.get("evidence_refs") or assessment.get("evidence_refs", [])
        )
        if str(requirement.get("kind") or "") == "semantic_classification_required":
            classified["owner"] = "device_execution"
            classified["required_by"] = "llm_semantic_review"
            classified["device_policy"] = "device_semantic_decision"
            classified["scientifically_fixed"] = False
        return classified

    @classmethod
    def _payload_contains_quantity(
        cls,
        payload: Any,
        requirement: Dict[str, Any],
    ) -> bool:
        target_value, target_dimension, _ = cls._canonical_quantity(
            {
                "value": requirement.get("value"),
                "unit": requirement.get("unit"),
            }
        )
        if target_value is None or target_dimension.startswith("unknown"):
            return False

        def walk(value: Any) -> bool:
            if isinstance(value, dict):
                if "value" in value and ("unit" in value or "单位" in value):
                    actual_value, actual_dimension, _ = cls._canonical_quantity(value)
                    if (
                        actual_value is not None
                        and actual_dimension == target_dimension
                        and math.isclose(
                            actual_value,
                            target_value,
                            rel_tol=1e-9,
                            abs_tol=1e-12,
                        )
                    ):
                        return True
                return any(walk(nested) for nested in value.values())
            if isinstance(value, list):
                return any(walk(item) for item in value)
            if isinstance(value, str):
                return any(
                    item.get("dimension") == target_dimension
                    and math.isclose(
                        float(item.get("value", float("nan"))),
                        target_value,
                        rel_tol=1e-9,
                        abs_tol=1e-12,
                    )
                    for item in cls._extract_explicit_quantities(value)
                )
            return False

        return walk(payload)

    @staticmethod
    def _capability_index_workstations() -> Dict[str, Dict[str, Any]]:
        try:
            parsed = json.loads(
                (chem_resources_root() / "workstation_capability_index.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, json.JSONDecodeError):
            return {}
        return {
            str(item.get("station_code") or "").strip(): item
            for item in parsed.get("workstations", []) or []
            if isinstance(item, dict) and str(item.get("station_code") or "").strip()
        }

    def _quantity_requirement_disposition_findings(
        self,
        research_handoff: Dict[str, Any],
        plan_result: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Audit Device's limited authority over optional execution targets.

        A model-proposed target may be bound only to a real required Skill
        setpoint.  Otherwise Device must remove it (or carry the material as a
        one-to-one whole batch).  This prevents an optional 20 mg target from
        becoming an artificial request to know the dry batch inventory.
        """

        raw_dispositions = plan_result.get("quantity_requirement_dispositions")
        dispositions = raw_dispositions if isinstance(raw_dispositions, list) else []
        disposition_by_key: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
        for item in dispositions:
            if not isinstance(item, dict):
                continue
            macro_id = str(item.get("source_macro_step") or "").strip()
            index_value = item.get("requirement_index")
            if isinstance(index_value, bool) or not isinstance(index_value, int):
                continue
            disposition_by_key.setdefault((macro_id, index_value), []).append(item)

        plan_steps = [
            item
            for item in plan_result.get("device_plan", []) or []
            if isinstance(item, dict)
        ]
        plan_by_id = {
            str(item.get("plan_step") or "").strip(): item
            for item in plan_steps
            if str(item.get("plan_step") or "").strip()
        }
        truth = self._capability_index_workstations()
        findings: List[Dict[str, Any]] = []

        for fallback_macro_id, macro in enumerate(
            research_handoff.get("macro_action_steps", []) or [], start=1
        ):
            if not isinstance(macro, dict):
                continue
            macro_id = str(
                macro.get("步骤序号", macro.get("step", fallback_macro_id))
            ).strip()
            requirements = macro.get("quantity_requirements")
            if not isinstance(requirements, list):
                continue
            mapped_steps = [
                step
                for step in plan_steps
                if macro_id in self._source_macro_step_ids(step)
            ]
            for requirement_index, requirement in enumerate(requirements):
                if not isinstance(requirement, dict):
                    continue
                requirement = self._semantically_classified_quantity_requirement(
                    macro_id, requirement_index, requirement
                )
                if not self._device_adaptable_target(requirement):
                    continue
                matches = disposition_by_key.get((macro_id, requirement_index), [])
                if len(matches) != 1:
                    findings.append(
                        {
                            "type": "missing_execution_quantity_disposition",
                            "source_macro_steps": [macro_id],
                            "requirement_index": requirement_index,
                            "message": (
                                f"Research macro step {macro_id} quantity_requirements["
                                f"{requirement_index}] 是可调 Device 执行目标；必须恰好给出一个"
                                " quantity_requirement_dispositions 处置，不能直接保留并要求"
                                "人工证明未知整批库存。"
                            ),
                        }
                    )
                    continue
                disposition = matches[0]
                decision = str(disposition.get("decision") or "").strip()
                reason = str(disposition.get("reason") or "").strip()
                evidence_refs = disposition.get("evidence_refs")
                if not reason or not isinstance(evidence_refs, list) or not evidence_refs:
                    findings.append(
                        {
                            "type": "execution_quantity_decision_missing_rationale",
                            "source_macro_steps": [macro_id],
                            "requirement_index": requirement_index,
                            "message": (
                                f"Device 对执行目标 {macro_id}[{requirement_index}] 的模型判断"
                                "必须给出非空 reason 和 evidence_refs；确定性层不替模型补科学理由。"
                            ),
                        }
                    )
                    continue
                if decision == "bind_skill_setpoint":
                    plan_step_id = str(disposition.get("plan_step") or "").strip()
                    plan_step = plan_by_id.get(plan_step_id)
                    if plan_step is None or plan_step not in mapped_steps:
                        findings.append(
                            {
                                "type": "invalid_execution_quantity_skill_binding",
                                "source_macro_steps": [macro_id],
                                "requirement_index": requirement_index,
                                "message": (
                                    f"可调执行目标 {macro_id}[{requirement_index}] 的 bind_skill_setpoint "
                                    "未绑定到服务该 macro step 的真实 plan_step。"
                                ),
                            }
                        )
                        continue
                    station_code = self._truth_workstation_code(
                        plan_step.get("workstation")
                    )
                    station = truth.get(station_code, {})
                    declared_station_code = self._truth_workstation_code(
                        disposition.get("workstation")
                    )
                    operation_name = str(disposition.get("operation") or "").strip()
                    parameter_name = str(disposition.get("parameter") or "").strip()
                    matching_operations = [
                        operation
                        for operation in station.get("operations", []) or []
                        if isinstance(operation, dict)
                        and operation_name
                        and operation.get("name") == operation_name
                    ]
                    matching_parameters = [
                        parameter
                        for operation in matching_operations
                        for parameter in operation.get("parameter_contracts", []) or []
                        if isinstance(parameter, dict)
                        and parameter.get("required") is True
                        and parameter.get("role") == "target_material_quantity"
                        and parameter_name
                        and parameter.get("name") == parameter_name
                    ]
                    target_value, target_dimension, _ = self._canonical_quantity(
                        {
                            "value": requirement.get("value"),
                            "unit": requirement.get("unit"),
                        }
                    )
                    dimension_compatible = any(
                        self._canonical_quantity(
                            {"value": 1, "unit": parameter.get("unit")}
                        )[1]
                        == target_dimension
                        for parameter in matching_parameters
                    )
                    recipe_target_compatible = any(
                        parameter_name == str(target.get("name") or "").strip()
                        and target.get("semantics")
                        == "recipe_defined_target_quantities"
                        and target_dimension == "mass"
                        for operation in matching_operations
                        for target in (
                            operation.get("quantity_semantics", {}).get(
                                "target_setpoints", []
                            )
                            or []
                        )
                        if isinstance(target, dict)
                    )
                    if (
                        not station_code
                        or declared_station_code != station_code
                        or not operation_name
                        or not parameter_name
                        or target_value is None
                        or not (dimension_compatible or recipe_target_compatible)
                        or not self._payload_contains_quantity(
                        {
                            "key_values": plan_step.get("key_values", {}),
                            "notes": plan_step.get("notes", ""),
                        },
                        requirement,
                        )
                    ):
                        findings.append(
                            {
                                "type": "invalid_execution_quantity_skill_binding",
                                "source_macro_steps": [macro_id],
                                "requirement_index": requirement_index,
                                "message": (
                                    f"可调执行目标 {macro_id}[{requirement_index}] 声称由 Skill 必需，"
                                    "但绑定 operation 没有匹配的必填 material-quantity 参数，或"
                                    "plan_step 未实际携带该目标值。"
                                ),
                            }
                        )
                elif decision in {"omit_as_nonessential", "replace_with_whole_batch"}:
                    if any(
                        self._payload_contains_quantity(
                            {
                                "objective": step.get("objective", ""),
                                "operation_intent": step.get("operation_intent", ""),
                                "key_values": step.get("key_values", {}),
                                "notes": step.get("notes", ""),
                            },
                            requirement,
                        )
                        for step in mapped_steps
                    ):
                        findings.append(
                            {
                                "type": "omitted_execution_quantity_still_present",
                                "source_macro_steps": [macro_id],
                                "requirement_index": requirement_index,
                                "message": (
                                    f"可调执行目标 {macro_id}[{requirement_index}] 已声明 {decision}，"
                                    "但映射步骤仍携带该数值；必须删除，不能继续触发未知库存审核。"
                                ),
                            }
                        )
                    if decision == "replace_with_whole_batch":
                        whole_batch_bound = any(
                            str(batch.get("quantity_mode") or "") == "whole_batch"
                            and macro_id
                            in {
                                str(value).strip()
                                for value in batch.get("source_macro_steps", []) or []
                            }
                            for batch in plan_result.get("batch_plan", []) or []
                            if isinstance(batch, dict)
                        )
                        if not whole_batch_bound:
                            findings.append(
                                {
                                    "type": "execution_quantity_whole_batch_missing",
                                    "source_macro_steps": [macro_id],
                                    "requirement_index": requirement_index,
                                    "message": (
                                        f"可调执行目标 {macro_id}[{requirement_index}] 声明改为 whole_batch，"
                                        "但 batch_plan 没有以 source_macro_steps 绑定该 macro 的 whole_batch。"
                                    ),
                                }
                            )
                elif decision == "retain_as_scientific_target":
                    # Scientific necessity is a Device-model judgement.  The
                    # deterministic layer deliberately does not second-guess
                    # it; it only required an explicit, evidenced rationale.
                    pass
                elif decision == "adapt_within_device_bounds":
                    after = disposition.get("after")
                    after_value, after_dimension, _ = self._canonical_quantity(after)
                    before_value, before_dimension, _ = self._canonical_quantity(
                        {
                            "value": requirement.get("value"),
                            "unit": requirement.get("unit"),
                        }
                    )
                    if (
                        after_value is None
                        or after_dimension != before_dimension
                        or before_value is None
                        or disposition.get("requires_scientific_review") is not True
                    ):
                        findings.append(
                            {
                                "type": "invalid_execution_quantity_adaptation",
                                "source_macro_steps": [macro_id],
                                "requirement_index": requirement_index,
                                "message": (
                                    f"执行目标 {macro_id}[{requirement_index}] 的适配必须给同维度 after "
                                    "并显式 requires_scientific_review=true；确定性层不猜调整值。"
                                ),
                            }
                        )
                elif decision == "request_runtime_measurement":
                    plan_step_id = str(disposition.get("plan_step") or "").strip()
                    plan_step = plan_by_id.get(plan_step_id)
                    station_code = self._truth_workstation_code(
                        plan_step.get("workstation") if plan_step else ""
                    )
                    operation_name = str(disposition.get("operation") or "").strip()
                    station = truth.get(station_code, {})
                    reports = [
                        report
                        for operation in station.get("operations", []) or []
                        if isinstance(operation, dict)
                        and operation.get("name") == operation_name
                        for report in operation.get("reported_measurements", []) or []
                        if str(report).strip()
                    ]
                    if plan_step not in mapped_steps or not reports:
                        findings.append(
                            {
                                "type": "invalid_execution_quantity_runtime_measurement",
                                "source_macro_steps": [macro_id],
                                "requirement_index": requirement_index,
                                "message": (
                                    f"执行目标 {macro_id}[{requirement_index}] 请求运行时测量，"
                                    "但绑定 operation 的 Skill 未声明 Report 数值反馈。"
                                ),
                            }
                        )
                else:
                    findings.append(
                        {
                            "type": "invalid_execution_quantity_disposition",
                            "source_macro_steps": [macro_id],
                            "requirement_index": requirement_index,
                            "message": (
                                f"可调执行目标 {macro_id}[{requirement_index}] 的 decision={decision!r} "
                                "无效；必须由 Device 模型在 retain/adapt/bind/omit/whole_batch/"
                                "runtime measurement 中明确选择。"
                            ),
                        }
                    )
        return findings

    def _plan_level_findings(
        self,
        state: SingleDeviceAgentState,
        plan_result: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Return the complete, de-duplicated audit for the latest candidate."""
        plan_view = {
            "offline_handoffs": plan_result.get("offline_handoffs", []),
            "steps": plan_result.get("device_plan", []),
            "quantity_adjustments": plan_result.get("quantity_adjustments", []),
        }
        findings = audit_offline_handoffs(plan_view)
        findings += self._external_return_wait_findings(state.research_handoff, plan_result)
        findings += audit_connected_sample_container_chain(plan_view)
        findings += scan_manual_material_operations(plan_view, "")
        findings += self._audit_core_chemistry_offline_handoffs(
            state, plan_result
        )
        findings += self._audit_research_core_joint_capability_mapping(
            state, plan_result
        )
        findings += self._audit_plan_recipe_evidence(plan_result)
        if not self._active_semantic_analysis:
            # Legacy/offline diagnostics only.  In production the independent
            # semantic LLM contract, source coverage, material transitions and
            # workstation truth jointly decide whether a macro operation was
            # omitted; do not re-interpret prose with keyword regexes here.
            findings += self._audit_plan_semantic_omissions(plan_result)
        findings += self._audit_plan_sample_matrix(
            state.research_handoff, plan_result
        )
        findings += self._quantity_requirement_disposition_findings(
            state.research_handoff, plan_result
        )
        findings += self._frozen_material_transition_coverage_findings(
            state.research_handoff, plan_result
        )
        findings += [
            {
                "type": "frozen_research_route_drift",
                "message": message,
            }
            for message in self._device_plan_research_alignment_errors(
                state.research_handoff,
                plan_result,
                semantic_analysis=self._active_semantic_analysis,
            )
        ]
        findings += [
            {"type": "frozen_research_route_drift", "message": message}
            for message in self._declared_route_change_errors(plan_result)
        ]
        findings += [
            {"type": "frozen_research_route_drift", "message": message}
            for message in self._forbidden_plan_change_claims(plan_result)
        ]
        unique: List[Dict[str, Any]] = []
        seen: Set[Tuple[str, str]] = set()
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            normalized = copy.deepcopy(finding)
            message = str(normalized.get("message", normalized))
            key = (str(normalized.get("type", "plan_level_finding")), message)
            if key in seen:
                continue
            seen.add(key)
            normalized.setdefault("message", message)
            unique.append(normalized)
        return unique

    def _repair_plan_level_findings(
        self,
        state: SingleDeviceAgentState,
        plan_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Generate at most three new Stage-1 Device-plan candidates.

        Every candidate is audited from scratch and the next prompt contains
        the *complete latest* finding set plus that complete candidate.  This
        stays entirely inside Device and never spends a Research iteration.
        """
        current = copy.deepcopy(plan_result)
        if str(current.get("status", "")).strip().lower() not in {
            "device_plan",
            "success",
        }:
            return current

        for candidate_number in range(1, DEFAULT_STAGE1_PLAN_REPAIR_LIMIT + 1):
            current = self._normalize_plan_handoff_steps(state, current)
            wait_findings = self._external_return_wait_findings(state.research_handoff, current)
            if wait_findings:
                return self._external_return_wait_result(state, current, wait_findings)
            findings = self._plan_level_findings(state, current)
            if not findings:
                return current
            state.add_log(
                f"plan-level capability audit flagged {len(findings)} finding(s); "
                f"requesting Stage-1 local repair candidate "
                f"{candidate_number}/{DEFAULT_STAGE1_PLAN_REPAIR_LIMIT}"
            )
            instruction = (
                "## 计划级能力审计未通过\n"
                f"这是 Device 同层候选 {candidate_number}/"
                f"{DEFAULT_STAGE1_PLAN_REPAIR_LIMIT}。以下是对最新候选的全部"
                f" {len(findings)} 项 finding，必须逐项修复：\n"
                + "\n".join(
                    f"- [{finding.get('type', 'plan_level_finding')}] "
                    f"{finding.get('message', '')}"
                    for finding in findings
                )
                + "\n\n## 最新完整 Device-plan 候选\n"
                + json.dumps(current, ensure_ascii=False, indent=2)
                + "\n\n"
                + SOLID_WEIGHING_ROUTE_NOTE
                + "\n请重新输出完整 JSON：被标记的人工/离线物料操作改写为对应"
                "工作站步骤；文件传参称量补齐逐瓶确定质量(g)与料罐号。disallowed "
                "的人工物料操作和运行时未知配方均不得保留。macro plan 已要求的预混、"
                "搅拌、加料、洗涤、转移、干燥和反应步骤不得写成可省略；若目标专用"
                "容器不兼容前置工作站，先在兼容容器完成前置步骤，再经默认联通的最短"
                "转移链进入目标容器。sample_matrix_drift 必须通过逐字回显 Research 的"
                "完整 sample_control_matrix，并在 container_plan/batch_plan 中保留每个"
                " sample_id、对照身份和变量水平来修复；不得删除、合并或新增实验组。"
                "一个物理步骤若同时服务多个 Research macro step，只保留一个物理步骤："
                "source_macro_step 写首个 primary scalar，source_macro_steps 写全部来源"
                "且按 Research 顺序排列。source_reagent_identity 逐字保留对应来源的试剂/"
                "对象；悬浊液、湿固体、上清或沉淀只是同一样品的状态，不得改名成新样品。"
                "ordinary_transfer_misclassified_offline 或 ordinary_transfer_path_requires_"
                "lineage_evidence 必须删除该 offline_handoff，"
                "改为 Device 内一次最短普通容器转移，并补齐 sample_lineage 的"
                "sample_id/source_container/destination_container/transfer_reason/"
                "trace_complete=true。unverified_transfer_justification 必须让"
                "justification_evidence_refs 指向匹配的 quantity_adjustments[]."
                "adjustment_id，或删除无结构证据的必要性标签和冗余换瓶。"
                "若无法证明转移必要性，仅能留在 Device"
                "补证据或人工复核，不得构造 Research 路线缺口。"
                "redundant_container_round_trip/redundant_same_type_container_changes "
                "必须沿用原容器或压缩为一次有明确 transfer_reason 的必要转移。"
                "missing/invalid_execution_quantity_disposition 必须逐项补齐"
                " quantity_requirement_dispositions：Device 模型必须结合科学目标、下游 Skill、"
                "上游 Report 和 whole_batch 路径明确判断 retain/adapt/bind/omit/whole_batch/runtime，"
                "并写 reason/evidence_refs。若选择 omit/whole_batch，确保映射步骤不再携带该数值；"
                "若选择 bind，必须引用真实同维度必填参数；若选择 runtime，必须引用真实 Report。"
                "Research macro step 要求的还原、氧化、煅烧、退火、水热或其他核心化学"
                "反应不得写入 offline_handoffs；offline 仅可保留 observation 数据回传和"
                "真实输入/输出边界。若完整真源证明没有单一工作站同时满足核心反应的必要"
                "温度、气氛和安全条件，必须返回 feasibility_error，并只在 blocking_constraints "
                "写出该必要化学操作、完整真源逐站联合能力证据及无替代结论。"
            )
            try:
                retried = self._invoke_feasibility_plan(
                    state, extra_instruction=instruction
                )
            except Exception as exc:  # pragma: no cover - remote model dependent
                state.add_log(
                    "plan-level repair call failed with Device internal error: "
                    f"{_safe_model_failure_text(exc)}"
                )
                raise
            if not isinstance(retried, dict):
                continue
            candidate = self._promote_feasible_quantity_human_plan(retried)
            candidate = self._remove_implicit_connectivity_blockers(
                state, candidate
            )
            candidate = self._normalize_plan_handoff_steps(state, candidate)
            if not self._plan_is_accepted(candidate):
                # Promote a repair candidate's hard verdict only when the
                # current, bound plan independently proves that same core gap
                # from the complete truth source. Mixed dose/container errors
                # never enter the synthesized Research package.
                proved_gap = self._verified_stage1_core_route_gap_result(
                    state, current
                )
                if (
                    proved_gap is not None
                    and self._candidate_claims_same_core_gap(candidate)
                ):
                    state.add_log(
                        "Stage-1 repair candidate reported the same core route "
                        "gap independently proved by complete workstation truth; "
                        "promoting before certificate issuance"
                    )
                    return proved_gap
                state.add_log(
                    "Stage-1 local repair candidate did not return an accepted "
                    "device_plan; retaining Device scope and continuing"
                )
                continue
            current = candidate

        remaining = self._plan_level_findings(state, current)
        if self._plan_is_accepted(current) and not remaining:
            state.add_log(
                "Stage-1 local plan repair accepted the final allowed candidate"
            )
            return current
        proved_gap = self._verified_stage1_core_route_gap_result(state, current)
        if proved_gap is not None:
            state.add_log(
                "Stage-1 candidates exhausted while preserving core chemistry "
                "as offline_handoff; complete workstation truth proved a joint "
                "capability route gap, promoting before certificate issuance"
            )
            return proved_gap
        state.add_log(
            "Stage-1 local plan repair exhausted after "
            f"{DEFAULT_STAGE1_PLAN_REPAIR_LIMIT} new candidate(s); "
            f"remaining_findings={len(remaining)}; routing to human, not Research"
        )
        blocking_constraints = [
            str(finding.get("message", finding))
            for finding in remaining
            if isinstance(finding, dict)
        ]
        if not blocking_constraints:
            blocking_constraints = [
                "Stage-1 Device-plan repair did not return an accepted, "
                "fully auditable device_plan within the local candidate budget."
            ]
        current.update(
            {
                "status": "manual_required",
                "feedback_type": "human_review_required",
                "feedback_route": "human",
                "failure_scope": "device_plan",
                "feasibility_accepted": False,
                "feasibility_certificate": {},
                "workflow_txt": "",
                "workflow_json": {},
                "error_package": {
                    "type": "stage1_plan_repair_exhausted",
                    "blocking_constraints": blocking_constraints,
                    "findings": copy.deepcopy(remaining),
                    "candidate_limit": DEFAULT_STAGE1_PLAN_REPAIR_LIMIT,
                    "message": (
                        "Stage-1 Device 同层计划修复候选已耗尽；等待人工修订，"
                        "不得返回 Research。"
                    ),
                },
            }
        )
        return current

    def _audit_plan_sample_matrix(
        self,
        research_handoff: Dict[str, Any],
        plan_result: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        return [
            {
                "type": "sample_matrix_drift",
                "message": message,
            }
            for message in self._sample_matrix_drift_errors(
                research_handoff, plan_result
            )
        ]

    def _audit_plan_recipe_evidence(
        self,
        plan_result: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Require executable recipe values before translating file dosing.

        A ratio or a future measured yield cannot be materialized into the
        XLSX required by the multi-channel solid-weighing workstation. Catch
        that under-specification at the plan boundary, where the LLM can still
        choose and justify an exact batch mass.
        """
        bottle_keys = {
            "瓶号",
            "瓶编号",
            "目标瓶号",
            "bottle_id",
            "vial_id",
        }
        mass_keys = {
            "加样量(g)",
            "加样质量(g)",
            "质量(g)",
            "mass_g",
        }
        hopper_keys = {
            "料罐号",
            "料罐编号",
            "料斗号",
            "料斗编号",
            "hopper_id",
        }
        recipe_row_keys = {
            "配方行",
            "逐瓶配方",
            "recipe_rows",
        }

        def normalized_key(value: Any) -> str:
            return (
                str(value)
                .strip()
                .lower()
                .replace("（", "(")
                .replace("）", ")")
                .replace(" ", "")
            )

        def field_value(row: Dict[str, Any], aliases: Set[str]) -> Any:
            for key, value in row.items():
                if normalized_key(key) in aliases:
                    return value
            return None

        def positive_number(value: Any) -> bool:
            # Recipe files require JSON numbers.  Strings such as "0.005",
            # "按实测质量" or "运行时确定" are deliberately not coercible:
            # accepting them here would only postpone the same ambiguity to
            # XLSX materialization.
            return (
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(float(value))
                and float(value) > 0
            )

        def positive_integer(value: Any) -> bool:
            return positive_number(value) and float(value).is_integer()

        def structured_rows(value: Any) -> Tuple[bool, List[Any]]:
            """Return whether a structured recipe was declared and its rows.

            Rows may be nested under file/batch lists.  An explicit 配方行
            collection is authoritative even when it is empty or contains a
            non-object, so malformed structured data cannot fall back to a
            permissive prose match.
            """
            declared = False
            rows: List[Any] = []

            def walk(node: Any) -> None:
                nonlocal declared
                if isinstance(node, list):
                    for item in node:
                        walk(item)
                    return
                if not isinstance(node, dict):
                    return

                normalized = {normalized_key(key): item for key, item in node.items()}
                categories = sum(
                    bool(set(normalized) & aliases)
                    for aliases in (bottle_keys, mass_keys, hopper_keys)
                )
                # Also support a direct row object without a 配方行 wrapper.
                if categories >= 2:
                    declared = True
                    rows.append(node)
                    return

                for key, item in node.items():
                    if normalized_key(key) in recipe_row_keys:
                        declared = True
                        if isinstance(item, list):
                            rows.extend(item)
                        else:
                            rows.append(item)
                    else:
                        walk(item)

            walk(value)
            return declared, rows

        def valid_structured_row(row: Any) -> bool:
            if not isinstance(row, dict):
                return False
            return (
                positive_integer(field_value(row, bottle_keys))
                and positive_number(field_value(row, mass_keys))
                and positive_integer(field_value(row, hopper_keys))
            )

        def valid_legacy_text_recipe(value: Any) -> bool:
            """Conservative compatibility path for old prose-only plans."""
            if not isinstance(value, str) or not value.strip():
                return False
            candidate_lines = [
                part.strip()
                for part in re.split(r"[；;\n]+", value)
                if re.search(r"瓶(?:号)?\s*\d+|料(?:罐|斗)(?:号|编号)?", part)
            ]
            if not candidate_lines:
                return False
            number = r"(?:\d+(?:\.\d+)?|\.\d+)"
            for line in candidate_lines:
                if not re.search(r"瓶(?:号)?\s*[:=]?\s*\d+", line, re.I):
                    return False
                if not re.search(
                    rf"加样(?:量|质量)(?:\s*\(g\))?\s*[:=]\s*{number}\s*g\b",
                    line,
                    re.I,
                ):
                    return False
                if not re.search(
                    r"料(?:罐|斗)(?:号|编号)?\s*[:=]\s*\d+", line, re.I
                ):
                    return False
            return True

        findings: List[Dict[str, Any]] = []
        for step in plan_result.get("device_plan") or []:
            if not isinstance(step, dict):
                continue
            station = str(step.get("workstation", ""))
            intent = str(step.get("operation_intent", ""))
            if not (
                "Multi_Channel_Solid_Weighing_Workstation_V1" in station
                or "多通道固体称量工作站_V1" in station
                or "文件传参" in intent
            ):
                continue
            declared, rows = structured_rows(step.get("key_values", {}))
            if declared and rows and all(valid_structured_row(row) for row in rows):
                # Structured rows are the executable source of truth.  Do not
                # scan unrelated prose for bare words such as “运行时”: a note
                # like “不使用运行时未知配方” is a guarantee, not a blocker.
                continue
            if not declared and valid_legacy_text_recipe(step.get("notes", "")):
                continue
            findings.append(
                {
                    "type": "missing_concrete_recipe_evidence",
                    "plan_step": step.get("plan_step"),
                    "message": (
                        f"device_plan[{step.get('plan_step', '?')}] 文件传参固体称量缺少"
                        "可物化的逐瓶配方。必须给出每行瓶号、确定加样量(g)和料罐号；"
                        "不能使用按实测质量/按比例/适量等运行时未知值。"
                    ),
                }
            )
        return findings

    def _audit_plan_semantic_omissions(
        self,
        plan_result: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Reject plans that conditionally discard a declared chemical step.

        A device adaptation may change containers or split a transfer, but it
        cannot label a macro-plan operation as optional merely because the
        initially selected workstation/container pair is incompatible.
        """
        findings: List[Dict[str, Any]] = []
        operation_terms = r"预混|混合|搅拌|加料|滴加|洗涤|转移|干燥|反应|熟化|macro|步骤"
        omission_patterns = (
            rf"(?:省略|跳过|删除|不执行).{{0,48}}(?:{operation_terms})",
            rf"(?:{operation_terms}).{{0,48}}(?:省略|跳过|删除|不执行)",
            r"(?:后续|反应平台|高温反应).{0,32}搅拌.{0,24}(?:替代|完成)",
        )
        for step in plan_result.get("device_plan") or []:
            if not isinstance(step, dict):
                continue
            evidence = _json_text(
                {
                    "objective": step.get("objective", ""),
                    "operation_intent": step.get("operation_intent", ""),
                    "key_values": step.get("key_values", {}),
                    "notes": step.get("notes", ""),
                }
            )
            if not any(re.search(pattern, evidence, re.I) for pattern in omission_patterns):
                continue
            findings.append(
                {
                    "type": "conditional_macro_semantic_omission",
                    "plan_step": step.get("plan_step"),
                    "message": (
                        f"device_plan[{step.get('plan_step', '?')}] 含有条件性省略/替代"
                        "化学步骤的指令。设备层只能更换兼容容器、工作站或拆分转移，不能"
                        "删除 macro plan 已要求的前置混合、搅拌、加料、洗涤、转移、干燥"
                        "或反应时序。"
                    ),
                }
            )
        return findings

    def _normalize_plan_handoff_steps(
        self,
        state: SingleDeviceAgentState,
        plan_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Normalize plan trace fields and move pseudo-station handoffs.

        ``offline_handoff`` is a workflow boundary, never a dispatchable
        workstation.  Keeping it inside ``device_plan`` wastes a translation
        or reviewer rewrite on an unavoidable ``unknown_workstation`` error.
        A multi-source physical step is represented by one primary scalar plus
        ``source_macro_steps``; it is never cloned merely for traceability.
        """
        normalized = copy.deepcopy(plan_result)
        raw_steps = normalized.get("device_plan")
        if not isinstance(raw_steps, list):
            return plan_result

        kept_steps: List[Dict[str, Any]] = []
        pseudo_steps: List[Dict[str, Any]] = []
        normalized_source_fields = 0
        for step in raw_steps:
            if not isinstance(step, dict):
                continue
            if self._normalize_source_macro_fields(step):
                normalized_source_fields += 1
            station = str(step.get("workstation", "")).strip()
            if re.search(r"(?:offline|离线).{0,8}handoff|handoff", station, re.I):
                pseudo_steps.append(step)
            else:
                kept_steps.append(step)
        normalized["device_plan"] = kept_steps
        handoffs = list(normalized.get("offline_handoffs") or [])
        existing_text = _json_text(handoffs)
        for step in pseudo_steps:
            objective = str(step.get("objective", "")).strip()
            notes = str(step.get("notes", "")).strip()
            # Feasibility plans normally already provide a richer top-level
            # handoff. Add a lossless fallback only when no matching boundary
            # is present there.
            if objective and objective.lower() in existing_text.lower():
                for existing_handoff in handoffs:
                    if not isinstance(existing_handoff, dict):
                        continue
                    if objective.lower() in _json_text(existing_handoff).lower():
                        existing_handoff.setdefault("source_macro_step", step.get("source_macro_step"))
                        existing_handoff.setdefault("source_macro_steps", step.get("source_macro_steps", []))
                        existing_handoff.setdefault(
                            "source_reagent_identity",
                            step.get("source_reagent_identity", ""),
                        )
                        existing_handoff.setdefault(
                            "source_material_identity_ids",
                            step.get("source_material_identity_ids", []),
                        )
                        break
                continue
            key_values = step.get("key_values")
            containers = step.get("containers")
            return_data: List[str] = []
            if isinstance(key_values, dict):
                return_data.extend(f"{key}={value}" for key, value in key_values.items())
            if isinstance(containers, dict) and containers:
                return_data.append(
                    "容器状态=" + json.dumps(containers, ensure_ascii=False)
                )
            if notes:
                return_data.append(notes)
            handoffs.append(
                {
                    "name": objective or "设备流程离线边界",
                    "sample": notes or objective or "样品状态由离线边界建立",
                    "required_return_data": return_data,
                    "source_macro_step": step.get("source_macro_step"),
                    "source_macro_steps": step.get("source_macro_steps", []),
                    "source_reagent_identity": step.get(
                        "source_reagent_identity", ""
                    ),
                    "source_material_identity_ids": step.get(
                        "source_material_identity_ids", []
                    ),
                    "operation_intent": step.get("operation_intent", ""),
                }
            )

        research_steps = [
            step
            for step in state.research_handoff.get("macro_action_steps", []) or []
            if isinstance(step, dict)
        ]
        inferred_handoff_sources = 0
        for handoff in handoffs:
            if not isinstance(handoff, dict):
                continue
            if self._normalize_source_macro_fields(handoff):
                normalized_source_fields += 1
            if self._source_macro_step_ids(handoff):
                continue
            if self._active_semantic_analysis:
                # Production requires the planning LLM to bind every handoff
                # explicitly.  Do not guess a macro source from overlapping
                # chemical-name fragments.
                continue
            handoff_text = self._identity_text(_json_text(handoff))
            scores: Dict[str, int] = {}
            for index, research_step in enumerate(research_steps, start=1):
                source = str(
                    research_step.get("步骤序号", research_step.get("step", index))
                )
                operation = str(
                    research_step.get("操作", research_step.get("operation", ""))
                )
                normalized_operation = self._identity_text(operation)
                score = 8 if normalized_operation and normalized_operation in handoff_text else 0
                for acronym in re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,}", operation):
                    if self._identity_text(acronym) in handoff_text:
                        score += 4
                expected_tokens = self._reagent_identity_tokens(
                    research_step.get(
                        "试剂/对象", research_step.get("reagent_or_object", "")
                    )
                )
                score += sum(
                    2 for token in expected_tokens if token and token in handoff_text
                )
                if score:
                    scores[source] = score
            if scores:
                best = max(scores.values())
                winners = [source for source, score in scores.items() if score == best]
                if len(winners) == 1:
                    primary = self._source_macro_scalar(winners[0])
                    handoff["source_macro_step"] = primary
                    handoff["source_macro_steps"] = [primary]
                    inferred_handoff_sources += 1
        normalized["offline_handoffs"] = handoffs
        if pseudo_steps:
            state.add_log(
                "normalized "
                f"{len(pseudo_steps)} offline handoff pseudo-station step(s) into metadata"
            )
        if normalized_source_fields or inferred_handoff_sources:
            state.add_log(
                "normalized source macro trace fields: "
                f"records={normalized_source_fields}, "
                f"inferred_offline_handoffs={inferred_handoff_sources}"
            )
        return normalized

    def _translation_chunk_size(self) -> int:
        if self._contract_version == "v2":
            # One frozen Device-plan source step per repair unit.  A source
            # step may expand to several machine steps, but other source steps
            # remain byte-for-byte cached while this unit is repaired.
            return 1
        raw = os.getenv("CHEM_DEVICE_TRANSLATION_CHUNK_SIZE", "").strip()
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
        return 6

    def _lid_and_container_state_after(
        self, steps: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Deterministic carry-over: per-container lid state implied by the
        already-translated steps, so the next chunk does not re-open/re-close a
        container and the assembled lid continuity holds at chunk seams."""
        state: Dict[str, Any] = {}
        for step in steps:
            if not isinstance(step, dict):
                continue
            operation = str(step.get("operation", ""))
            params = step.get("parameters")
            params = params if isinstance(params, dict) else {}
            container_type = str(params.get("容器类型", "")).strip()
            ids = params.get("容器编号")
            id_list = ids if isinstance(ids, list) else []
            for cid in id_list:
                key = f"{container_type}#{cid}"
                if operation.startswith("开盖"):
                    state[key] = {"lid": "无盖"}
                elif operation.startswith("关盖"):
                    state[key] = {"lid": "有盖"}
        return state

    def _inherit_workflow_source_traces(
        self,
        workflow_steps: List[Dict[str, Any]],
        plan_steps: List[Dict[str, Any]],
    ) -> None:
        """Carry uniquely provable plan/macro trace metadata into workflow.

        A chunk may contain several plan steps for the same workstation and
        macro.  In that case a missing ``source_plan_step`` is intentionally
        left missing for the deterministic coverage gate; guessing would hide
        a dropped or cross-wired plan step.  One plan step may still expand to
        any number of workflow steps.
        """
        for workflow_step in workflow_steps:
            if not isinstance(workflow_step, dict):
                continue
            workflow_sources = self._source_macro_step_ids(workflow_step)
            workstation = str(workflow_step.get("workstation", "")).strip()
            workflow_truth_station = self._truth_workstation_code(workstation)
            candidates = [
                plan_step
                for plan_step in plan_steps
                if isinstance(plan_step, dict)
                and (
                    not workstation
                    or str(plan_step.get("workstation", "")).strip() == workstation
                    or (
                        workflow_truth_station
                        and self._truth_workstation_code(
                            plan_step.get("workstation", "")
                        )
                        == workflow_truth_station
                    )
                )
                and (
                    not workflow_sources
                    or workflow_sources[0]
                    in self._source_macro_step_ids(plan_step)
                )
            ]
            explicit_plan_source = workflow_step.get("source_plan_step")
            explicit_candidates = [
                plan_step
                for plan_step in plan_steps
                if isinstance(plan_step, dict)
                and str(plan_step.get("plan_step") or "").strip()
                == str(explicit_plan_source or "").strip()
            ]
            if explicit_plan_source not in (None, "") and len(
                explicit_candidates
            ) == 1:
                candidates = explicit_candidates
            if explicit_plan_source in (None, "") and len(candidates) == 1:
                candidate_plan_step = candidates[0].get("plan_step")
                if candidate_plan_step not in (None, ""):
                    workflow_step["source_plan_step"] = self._source_macro_scalar(
                        str(candidate_plan_step)
                    )
            source_shapes = {
                tuple(self._source_macro_step_ids(candidate))
                for candidate in candidates
                if self._source_macro_step_ids(candidate)
            }
            if len(source_shapes) == 1:
                sources = list(next(iter(source_shapes)))
                typed = [self._source_macro_scalar(source) for source in sources]
                workflow_step["source_macro_step"] = typed[0]
                workflow_step["source_macro_steps"] = typed
            else:
                self._normalize_source_macro_fields(workflow_step)

    @staticmethod
    def _is_translation_context_limit_error(exc: Exception) -> bool:
        """Recognize explicit provider context limits, not general API failures."""
        from agent_skills.native_tools import NativeToolConfigurationError, NativeToolProtocolError

        if isinstance(exc, (ValueError, TypeError, NativeToolConfigurationError, NativeToolProtocolError)):
            return False
        body = getattr(exc, "body", None)
        error = body.get("error", body) if isinstance(body, dict) else {}
        codes = [
            safe_gateway_error_code(exc),
            getattr(exc, "_chem_gateway_error_code", None),
            getattr(exc, "code", None),
        ]
        if isinstance(error, dict):
            codes.append(error.get("code"))
        if any(code in ("context_length_exceeded", "context_window_exceeded", "max_context_length_exceeded") for code in codes):
            return True
        message = str(exc).lower()
        return bool(re.search(
            r"\b(?:context_length_exceeded|context_window_exceeded|max_context_length_exceeded)\b"
            r"|\bcontext (?:length|window) (?:is )?exceeded\b"
            r"|\b(?:input|prompt) is too long\b"
            r"|\b(?:input|prompt)\b.{0,80}\bexceeds?\b.{0,80}\bcontext (?:window|length|limit)\b"
            r"|\bmaximum context length\b.{0,180}\b(?:requested|resulted in|exceeded)\b"
            r"|\binput token count\b.{0,100}\bexceeds\b.{0,80}\bmaximum\b",
            message, re.DOTALL,
        ))

    def _invoke_translation_chunk_bounded(
        self,
        state: SingleDeviceAgentState,
        plan_result: Dict[str, Any],
        chunk_steps: List[Dict[str, Any]],
        chunk_index: int,
        total_chunks: int,
        carryover: Dict[str, Any],
        *,
        extra_instruction: str = "",
    ) -> Dict[str, Any]:
        """Bisect only context-overflowed chunks, keeping their logical cache ID.

        Each child keeps the frozen full plan and complete selected contracts.
        A split strictly reduces the step count, bounding work to a binary tree
        of at most 2*N-1 calls; an overflowing single step fails without clipping.
        """
        try:
            return self._invoke_translation_chunk(
                state, plan_result, chunk_steps, chunk_index, total_chunks, carryover,
                extra_instruction=extra_instruction,
            )
        except Exception as exc:
            if not self._is_translation_context_limit_error(exc):
                raise
            if len(chunk_steps) <= 1:
                ids = [step.get("plan_step") for step in chunk_steps]
                raise RuntimeError(
                    f"translation chunk {chunk_index + 1}: context limit still exceeded "
                    f"at indivisible plan_step IDs {ids}; full contracts and frozen plan "
                    "were retained, so translation cannot proceed within this model's context window"
                ) from exc
            middle = len(chunk_steps) // 2
            state.add_log(
                f"translation chunk {chunk_index + 1}: explicit context limit; splitting "
                f"{len(chunk_steps)} plan steps into {middle}+{len(chunk_steps) - middle} "
                "without truncating contracts or changing the frozen plan"
            )
            left = self._invoke_translation_chunk_bounded(
                state, plan_result, chunk_steps[:middle], chunk_index, total_chunks, carryover,
                extra_instruction=extra_instruction,
            )
            left_steps = left.get("workflow_json", {}).get("steps", [])
            right_carryover = copy.deepcopy(carryover)
            for container, changes in self._lid_and_container_state_after(left_steps).items():
                previous = right_carryover.get(container, {})
                right_carryover[container] = {
                    **(previous if isinstance(previous, dict) else {}), **changes,
                }
            right = self._invoke_translation_chunk_bounded(
                state, plan_result, chunk_steps[middle:], chunk_index, total_chunks, right_carryover,
                extra_instruction=extra_instruction,
            )
            return {
                "workflow_json": {"steps": left_steps + right.get("workflow_json", {}).get("steps", [])},
                "workflow_txt": "\n".join(
                    str(result.get("workflow_txt", "")).strip()
                    for result in (left, right) if str(result.get("workflow_txt", "")).strip()
                ),
            }

    def _translate_plan_in_chunks(
        self,
        state: SingleDeviceAgentState,
        plan_result: Dict[str, Any],
        chunk_cache: Dict[int, Dict[str, Any]],
        *,
        only_chunks: Optional[Set[int]] = None,
        feedback_by_chunk: Optional[Dict[int, str]] = None,
        previous_steps_by_chunk: Optional[Dict[int, List[Dict[str, Any]]]] = None,
        mutable_device_ids_by_chunk: Optional[Dict[int, Set[str]]] = None,
    ) -> Tuple[Dict[str, Any], str, Dict[int, int], List[List[Dict[str, Any]]]]:
        """Translate the device_plan chunk-by-chunk and assemble.

        Each chunk's OUTPUT is bounded (≤ chunk_size plan steps expanded), so a
        large plan never truncates into empty JSON. ``chunk_cache`` holds each
        chunk's translated steps/txt; ``only_chunks`` re-translates just those
        chunk indices (targeted repair) and reuses the cache for the rest.

        Returns (workflow_json, workflow_txt, step_number→chunk_index map,
        chunk step groups)."""
        device_plan = [
            step for step in (plan_result.get("device_plan", []) or [])
            if isinstance(step, dict)
        ]
        chunk_size = self._translation_chunk_size()
        chunks = [
            device_plan[i : i + chunk_size]
            for i in range(0, len(device_plan), chunk_size)
        ] or [[]]
        total = len(chunks)
        feedback_by_chunk = feedback_by_chunk or {}
        previous_steps_by_chunk = previous_steps_by_chunk or {}
        mutable_device_ids_by_chunk = mutable_device_ids_by_chunk or {}

        carryover: Dict[str, Any] = {}
        assembled_steps: List[Dict[str, Any]] = []
        txt_fragments: List[str] = []
        step_to_chunk: Dict[int, int] = {}
        chunk_step_groups: List[List[Dict[str, Any]]] = [[] for _ in range(total)]

        for index, chunk_steps in enumerate(chunks):
            need = only_chunks is None or index in only_chunks
            if need or index not in chunk_cache:
                try:
                    translated = self._invoke_translation_chunk_bounded(
                        state, plan_result, chunk_steps, index, total, carryover,
                        extra_instruction=feedback_by_chunk.get(index, ""),
                    )
                    wf = translated.get("workflow_json")
                    wf = wf if isinstance(wf, dict) else {}
                    steps = [s for s in (wf.get("steps") or []) if isinstance(s, dict)]
                    self._inherit_workflow_source_traces(steps, chunk_steps)
                    if self._contract_version == "v2":
                        self._stamp_device_steps_with_macro_action(
                            {"steps": steps}, state.research_handoff
                        )
                        previous_steps = previous_steps_by_chunk.get(index)
                        mutable_ids = mutable_device_ids_by_chunk.get(index)
                        if previous_steps is not None and mutable_ids is not None:
                            steps, scope_errors = merge_scoped_device_step_repair(
                                previous_steps, steps, mutable_ids
                            )
                            if scope_errors:
                                raise ValueError("; ".join(scope_errors))
                    chunk_cache[index] = {
                        "steps": steps,
                        "txt": str(translated.get("workflow_txt", "")),
                    }
                except Exception as exc:  # pragma: no cover - remote dependent
                    state.add_error(
                        f"translation chunk {index + 1} internal failure: "
                        f"{type(exc).__name__}: {_safe_model_failure_text(exc)}"
                    )
                    # API/parse/runtime failures are Device-internal.  Do not
                    # disguise them as an empty workflow candidate and burn
                    # eight semantic repair slots.
                    raise

            cached = chunk_cache.get(index, {"steps": [], "txt": ""})
            chunk_step_groups[index] = cached["steps"]
            for step in cached["steps"]:
                assembled_steps.append(dict(step))
                step_to_chunk[len(assembled_steps)] = index  # 1-indexed final no.
            if cached["txt"].strip():
                txt_fragments.append(cached["txt"].strip())
            # carry-over reflects everything translated so far, in plan order
            carryover = self._lid_and_container_state_after(assembled_steps)

        # renumber assembled steps 1..N; rebuild step_to_chunk on final numbers
        final_map: Dict[int, int] = {}
        for final_no, step in enumerate(assembled_steps, start=1):
            step["step_number"] = final_no
            final_map[final_no] = step_to_chunk.get(final_no, 0)

        workflow_json = {
            "steps": assembled_steps,
            "temporal_adaptations": plan_result.get("temporal_adaptations", []),
            "offline_handoffs": plan_result.get("offline_handoffs", []),
        }
        workflow_txt = self._renumber_txt("\n".join(txt_fragments))
        return workflow_json, workflow_txt, final_map, chunk_step_groups

    @staticmethod
    def _renumber_txt(text: str) -> str:
        """Renumber `第N步` blocks sequentially across concatenated fragments."""
        counter = {"n": 0}

        def _sub(_match: "re.Match") -> str:
            counter["n"] += 1
            return f"第{counter['n']}步"

        return re.sub(r"第\s*\d+\s*步", _sub, text)

    def _force_retranslate_workflow_candidate(
        self,
        state: SingleDeviceAgentState,
        plan_result: Dict[str, Any],
        result: Dict[str, Any],
        deterministic_report: Dict[str, Any],
    ) -> None:
        """Create a real new candidate when a reviewer ignores hard errors."""
        plan_steps = [
            item
            for item in plan_result.get("device_plan", []) or []
            if isinstance(item, dict)
        ]
        chunk_count = max(
            1,
            (len(plan_steps) + self._translation_chunk_size() - 1)
            // self._translation_chunk_size(),
        )
        instruction = self._build_repair_instruction(
            result, deterministic_report
        )
        workflow_json, workflow_txt, _, _ = self._translate_plan_in_chunks(
            state,
            plan_result,
            {},
            feedback_by_chunk={
                index: instruction for index in range(chunk_count)
            },
        )
        result["workflow_json"] = workflow_json
        result["workflow_txt"] = workflow_txt
        self._stamp_device_steps_with_macro_action(
            workflow_json, state.research_handoff
        )
        self._apply_deterministic_completion(state, result)

    def _translate_and_verify(
        self,
        state: SingleDeviceAgentState,
        plan_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Stage 2 with chunked translation + bounded targeted repair.

        The plan is translated chunk-by-chunk (bounded output per call), then
        the full deterministic checks run on the assembled workflow. On
        failure, only the chunks that own the erroring steps are re-translated
        (issue #12: repairs never re-roll the chemistry, and never re-pay the
        whole plan's translation cost)."""
        if self._workflow_verification_mode() == "llm":
            return self._translate_and_review_with_skills(state, plan_result)

        max_modifications = self._workflow_repair_limit()
        max_rounds = 1 + max_modifications
        chunk_cache: Dict[int, Dict[str, Any]] = {}
        report: Dict[str, Any] = {"status": "failed", "errors": [], "warnings": []}
        result: Dict[str, Any] = {}
        only_chunks: Optional[Set[int]] = None
        feedback_by_chunk: Dict[int, str] = {}
        repair_rounds: List[Dict[str, Any]] = []
        expected_locked_hashes: Dict[str, str] = {}
        previous_steps_by_chunk: Dict[int, List[Dict[str, Any]]] = {}
        mutable_device_ids_by_chunk: Dict[int, Set[str]] = {}

        for round_index in range(1, max_rounds + 1):
            workflow_json, workflow_txt, step_map, chunk_groups = (
                self._translate_plan_in_chunks(
                    state, plan_result, chunk_cache,
                    only_chunks=only_chunks,
                    feedback_by_chunk=feedback_by_chunk,
                    previous_steps_by_chunk=previous_steps_by_chunk,
                    mutable_device_ids_by_chunk=mutable_device_ids_by_chunk,
                )
            )
            current_chunk_hashes = chunk_hashes(chunk_cache)
            current_lock_hashes = dict(current_chunk_hashes)
            for cached in chunk_cache.values():
                current_lock_hashes.update(
                    device_step_hashes(cached.get("steps", []))
                )
            lock_violations = locked_chunk_violations(
                expected_locked_hashes, current_lock_hashes
            )
            if lock_violations:
                report = {
                    "status": "failed",
                    "errors": lock_violations,
                    "warnings": [],
                    "_device_internal_error": True,
                    "assessment_source": "v2_repair_scope_guard",
                }
                result = dict(plan_result)
                result["workflow_json"] = workflow_json
                result["workflow_txt"] = workflow_txt
                result["dispatch_validation"] = report
                result["feedback_type"] = "device_internal_error"
                result["feedback_route"] = "device"
                return result
            result = self._merge_plan_and_translation(
                plan_result, {"workflow_txt": workflow_txt, "workflow_json": workflow_json}
            )
            self._stamp_device_steps_with_macro_action(
                workflow_json, state.research_handoff
            )
            self._apply_deterministic_completion(state, result)
            if not self._materialize_recipe_files(state, result):
                materialization = result.get("recipe_materialization", {})
                report = {
                    "status": "failed",
                    "errors": list(materialization.get("errors", [])),
                    "warnings": [],
                    "checked_steps": len(
                        (result.get("workflow_json") or {}).get("steps", [])
                    ),
                    "assessment_source": "deterministic_recipe_materializer",
                }
                result["dispatch_validation"] = report
                repair_rounds.append(
                    {
                        "round": round_index,
                        "candidate_kind": (
                            "initial" if round_index == 1 else "llm_modification"
                        ),
                        "status": "failed",
                        "errors": list(report["errors"]),
                        "issues": [],
                    }
                )
                result["workflow_repair_cycle"] = {
                    "initial_candidate_count": 1,
                    "modification_count": round_index - 1,
                    "max_modifications": max_modifications,
                    "status": "failed",
                    "rounds": repair_rounds,
                    "mechanical_completions_counted": False,
                }
                return result
            report = self._run_full_checks(result)
            report.setdefault(
                "assessment_source", "deterministic_workstation_validator"
            )
            result["dispatch_validation"] = report
            repair_rounds.append(
                {
                    "round": round_index,
                    "candidate_kind": (
                        "initial" if round_index == 1 else "llm_modification"
                    ),
                    "status": report.get("status", "failed"),
                    "errors": list(report.get("errors", [])),
                    "issues": (
                        build_validation_issues_v2(
                            report.get("errors", []), result.get("workflow_json")
                        )
                        if self._contract_version == "v2"
                        else []
                    ),
                    "locked_step_hashes": dict(expected_locked_hashes),
                }
            )
            if report.get("_device_internal_error"):
                result["feedback_type"] = "device_internal_error"
                result["feedback_route"] = "device"
                result["failure_scope"] = "device_internal"
                result["workflow_repair_cycle"] = {
                    "initial_candidate_count": 1,
                    "modification_count": round_index - 1,
                    "max_modifications": max_modifications,
                    "status": "failed",
                    "rounds": repair_rounds,
                    "mechanical_completions_counted": False,
                }
                return result
            if report["status"] != "failed":
                if round_index > 1:
                    state.add_log(
                        f"chunked translation repair round {round_index - 1} "
                        "passed deterministic checks"
                    )
                result["workflow_repair_cycle"] = {
                    "initial_candidate_count": 1,
                    "modification_count": round_index - 1,
                    "max_modifications": max_modifications,
                    "status": "passed",
                    "rounds": repair_rounds,
                    "mechanical_completions_counted": False,
                }
                return result

            # map failures back to chunks and re-translate only those
            structured = structure_validation_errors(
                report.get("errors", []), result.get("workflow_json")
            )
            erroring_chunks: Set[int] = set()
            mutable_device_ids_by_chunk = {}
            workflow_steps = {
                step.get("step_number"): step
                for step in (result.get("workflow_json") or {}).get("steps", [])
                if isinstance(step, dict)
            }
            for record in structured:
                step_no = record.get("step_number")
                if isinstance(step_no, int) and step_no in step_map:
                    chunk_index = step_map[step_no]
                    erroring_chunks.add(chunk_index)
                    step = workflow_steps.get(step_no, {})
                    device_step_id = str(step.get("device_step_id") or "")
                    if device_step_id:
                        mutable_device_ids_by_chunk.setdefault(
                            chunk_index, set()
                        ).add(device_step_id)
            empty_chunks = {
                idx for idx, group in enumerate(chunk_groups) if not group
            }
            erroring_chunks |= empty_chunks
            if not erroring_chunks:
                if self._contract_version == "v2":
                    # A V2 repair must have an exact step scope.  An unanchored
                    # global failure is retained for human review instead of
                    # authorizing a whole-workflow rewrite.
                    break
                erroring_chunks = set(range(len(chunk_groups)))

            if self._contract_version == "v2":
                previous_steps_by_chunk = {
                    idx: copy.deepcopy(chunk_cache[idx].get("steps", []))
                    for idx in erroring_chunks
                    if idx in chunk_cache
                }
                expected_locked_hashes = {
                    key: digest
                    for key, digest in current_chunk_hashes.items()
                    if int(key.rsplit("_", 1)[1]) not in erroring_chunks
                }
                for idx, steps in previous_steps_by_chunk.items():
                    expected_locked_hashes.update(
                        device_step_hashes(
                            steps,
                            exclude=mutable_device_ids_by_chunk.get(idx, set()),
                        )
                    )

            state.add_log(
                f"deterministic checks failed ({len(report['errors'])} errors); "
                f"candidate {round_index}/{max_rounds} re-translating chunks "
                f"{sorted(erroring_chunks)}"
            )
            instruction = self._build_repair_instruction(result, report)
            feedback_by_chunk = {
                idx: instruction
                + (
                    "\nV2 局部修复范围：只允许替换 device_step_id="
                    + json.dumps(
                        sorted(mutable_device_ids_by_chunk.get(idx, set())),
                        ensure_ascii=False,
                    )
                    + "；其余 Device Steps 已由哈希锁定，必须逐字保持。"
                    if self._contract_version == "v2"
                    else ""
                )
                for idx in erroring_chunks
            }
            only_chunks = erroring_chunks
            for idx in erroring_chunks:  # force re-translation of these chunks
                chunk_cache.pop(idx, None)

        if not result or not result.get("workflow_json", {}).get("steps"):
            # Exhausted repairs with empty output remain an honest Device-local
            # workflow failure; accepted route feasibility stays immutable.
            result = dict(plan_result)
            result["status"] = "success"
            result["workflow_txt"] = ""
            result["workflow_json"] = {}
            result["dispatch_validation"] = report if report.get("errors") else {
                "status": "failed",
                "errors": ["workflow translation 分块后仍未产出可校验的 workflow_json。"],
                "warnings": [],
                "assessment_source": "deterministic_workstation_validator",
            }
        result["workflow_repair_cycle"] = {
            "initial_candidate_count": 1,
            "modification_count": min(
                max_modifications, max(0, len(repair_rounds) - 1)
            ),
            "max_modifications": max_modifications,
            "status": "failed",
            "rounds": repair_rounds,
            "mechanical_completions_counted": False,
        }
        return result

    def _workflow_verification_mode(self) -> str:
        """Select the final workflow gate.

        Skill-grounded LLM review is the default. The former deterministic gate
        remains available as an explicit rollback while the new stage settles.
        """
        if self._contract_version == "v2":
            return "deterministic"
        raw = os.getenv("CHEM_DEVICE_WORKFLOW_VERIFICATION", "llm").strip().lower()
        return "deterministic" if raw in {"deterministic", "legacy", "format"} else "llm"

    def _translate_and_review_with_skills(
        self,
        state: SingleDeviceAgentState,
        plan_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Translate once, then accept at most eight new LLM candidates."""
        max_modifications = self._workflow_repair_limit()
        workflow_json, workflow_txt, _, _ = self._translate_plan_in_chunks(
            state, plan_result, {}
        )
        result = self._merge_plan_and_translation(
            plan_result, {"workflow_txt": workflow_txt, "workflow_json": workflow_json}
        )
        steps = workflow_json.get("steps") if isinstance(workflow_json, dict) else None
        translation_retry_rounds: List[Dict[str, Any]] = []
        translation_modification_count = 0
        if not isinstance(steps, list) or not steps:
            translation_retry_rounds.append(
                {
                    "round": 1,
                    "candidate_kind": "initial",
                    "status": "failed",
                    "errors": ["workflow translation 未产出可供 Skill 审核的步骤。"],
                    "issues": [],
                }
            )
            plan_steps = [
                item
                for item in plan_result.get("device_plan", []) or []
                if isinstance(item, dict)
            ]
            chunk_count = max(
                1,
                (len(plan_steps) + self._translation_chunk_size() - 1)
                // self._translation_chunk_size(),
            )
            while translation_modification_count < max_modifications:
                translation_modification_count += 1
                retry_instruction = (
                    "上一 workflow 候选为空或截断。必须忠实覆盖本 chunk 的全部 "
                    "device_plan step，输出完整可解析 workflow_json.steps；不得改变计划。"
                )
                workflow_json, workflow_txt, _, _ = self._translate_plan_in_chunks(
                    state,
                    plan_result,
                    {},
                    feedback_by_chunk={
                        index: retry_instruction for index in range(chunk_count)
                    },
                )
                result = self._merge_plan_and_translation(
                    plan_result,
                    {
                        "workflow_txt": workflow_txt,
                        "workflow_json": workflow_json,
                    },
                )
                steps = (
                    workflow_json.get("steps")
                    if isinstance(workflow_json, dict)
                    else None
                )
                produced = bool(isinstance(steps, list) and steps)
                translation_retry_rounds.append(
                    {
                        "round": translation_modification_count + 1,
                        "candidate_kind": "llm_modification",
                        "status": "produced" if produced else "failed",
                        "errors": (
                            []
                            if produced
                            else ["workflow translation 仍为空或截断。"]
                        ),
                        "issues": [],
                    }
                )
                if produced:
                    break

        if not isinstance(steps, list) or not steps:
            result["dispatch_validation"] = {
                "status": "failed",
                "errors": ["workflow translation 未产出可供 Skill 审核的步骤。"],
                "warnings": [],
                "checked_steps": 0,
                "assessment_source": "llm_workstation_skill_reviewer",
            }
            result["workflow_skill_review"] = {
                "status": "failed",
                "rounds": [],
                "rewritten": translation_modification_count > 0,
                "rewrite_count": translation_modification_count,
            }
            result["workflow_repair_cycle"] = {
                "initial_candidate_count": 1,
                "modification_count": translation_modification_count,
                "max_modifications": max_modifications,
                "status": "failed",
                "rounds": translation_retry_rounds,
                "mechanical_completions_counted": False,
            }
            return result

        self._stamp_device_steps_with_macro_action(workflow_json, state.research_handoff)
        self._apply_deterministic_completion(state, result)
        if not self._materialize_recipe_files(state, result):
            materialization = result.get("recipe_materialization", {})
            result["dispatch_validation"] = {
                "status": "failed",
                "errors": list(materialization.get("errors", [])),
                "warnings": [],
                "checked_steps": len(steps),
                "assessment_source": "deterministic_recipe_materializer",
            }
            result["workflow_skill_review"] = {
                "status": "not_run",
                "rounds": [],
                "rewritten": translation_modification_count > 0,
                "rewrite_count": translation_modification_count,
                "reason": "recipe materialization failed before Skill review",
            }
            result["workflow_repair_cycle"] = {
                "initial_candidate_count": 1,
                "modification_count": translation_modification_count,
                "max_modifications": max_modifications,
                "status": "failed",
                "rounds": translation_retry_rounds + [
                    {
                        "round": 1,
                        "candidate_kind": "initial",
                        "status": "failed",
                        "errors": list(materialization.get("errors", [])),
                        "issues": [],
                    }
                ],
                "mechanical_completions_counted": False,
            }
            return result

        reviews: List[Dict[str, Any]] = []
        deterministic_rounds: List[Dict[str, Any]] = []
        last_review: Dict[str, Any] = {
            "verdict": "not_executable",
            "summary": "workflow Skill review did not complete",
            "issues": [],
        }
        deterministic_report: Dict[str, Any] = {
            "status": "failed",
            "errors": [],
            "warnings": [],
            "checked_steps": len(steps),
        }
        verdict = "not_executable"
        rewrite_count = translation_modification_count
        review_internal_error = False

        # Review the initial candidate, then each accepted rewrite.  A final
        # review after rewrite #8 is required before release.  Deterministic
        # autofills/normalization do not increment ``rewrite_count``.
        remaining_modifications = max_modifications - rewrite_count
        for round_number in range(1, remaining_modifications + 2):
            deterministic_report = (
                self._run_full_checks(result)
                if self._contract_gate_enabled()
                else {
                    "status": "passed",
                    "errors": [],
                    "warnings": [],
                    "checked_steps": len(
                        (result.get("workflow_json") or {}).get("steps", [])
                    ),
                }
            )
            result["pre_review_deterministic_validation"] = deterministic_report
            deterministic_rounds.append(
                {
                    "round": round_number,
                    "status": deterministic_report.get("status", "failed"),
                    "errors": list(deterministic_report.get("errors", [])),
                    "warnings": list(deterministic_report.get("warnings", [])),
                }
            )
            deterministic_failed = deterministic_report.get("status") == "failed"
            if deterministic_report.get("_device_internal_error"):
                review_internal_error = True
                result["feedback_type"] = "device_internal_error"
                result["feedback_route"] = "device"
                result["failure_scope"] = "device_internal"
                last_review = {
                    "verdict": "not_executable",
                    "summary": deterministic_report.get("errors", [
                        "Device deterministic validation internal error"
                    ])[0],
                    "issues": [
                        {
                            "code": "device_validation_internal_error",
                            "severity": "error",
                            "reason": error,
                        }
                        for error in deterministic_report.get("errors", [])
                    ],
                }
                verdict = "not_executable"
                break
            allow_rewrite = rewrite_count < max_modifications
            review = self._invoke_workflow_skill_review(
                state,
                plan_result,
                result,
                allow_rewrite=allow_rewrite,
                round_number=round_number,
            )
            reviews.append(self._review_record(review, round_number))
            last_review = review
            verdict = str(review.get("verdict", "")).strip().lower()
            if review.get("_device_internal_error"):
                review_internal_error = True
                result["feedback_type"] = "device_internal_error"
                result["feedback_route"] = "device"
                result["failure_scope"] = "device_internal"
                if review.get("llm_diagnostics"):
                    result["llm_diagnostics"] = copy.deepcopy(review["llm_diagnostics"])
                    result["llm_failure_step"] = review["llm_failure_step"]
                    result["llm_exception_type"] = review["internal_error_type"]
                break

            if verdict != "rewritten":
                if verdict == "executable" and deterministic_failed:
                    last_review = {
                        "verdict": "not_executable",
                        "summary": (
                            "LLM reviewer returned executable but deterministic "
                            "workstation checks still failed."
                        ),
                        "issues": [
                            {
                                "code": "deterministic_validation_failed",
                                "severity": "error",
                                "reason": str(error),
                            }
                            for error in deterministic_report.get("errors", [])
                        ],
                    }
                    reviews[-1] = self._review_record(last_review, round_number)
                    verdict = "not_executable"
                    if allow_rewrite:
                        # Do not spend a repair slot on another review of the
                        # same workflow.  Force the translator to emit a real
                        # new candidate from the structured errors.
                        self._force_retranslate_workflow_candidate(
                            state,
                            plan_result,
                            result,
                            deterministic_report,
                        )
                        rewrite_count += 1
                        state.add_log(
                            "Skill reviewer ignored deterministic failures; "
                            f"forced a new translation candidate ({rewrite_count}/"
                            f"{max_modifications})"
                        )
                        if not self._materialize_recipe_files(state, result):
                            verdict = "not_executable"
                            break
                        continue
                if verdict == "not_executable" and allow_rewrite:
                    issue_errors = [
                        self._render_skill_review_issue(issue)
                        for issue in review.get("issues", [])
                        if isinstance(issue, dict)
                    ]
                    forced_report = {
                        "status": "failed",
                        "errors": (
                            list(deterministic_report.get("errors", []))
                            + issue_errors
                            + ([str(review.get("summary"))] if review.get("summary") else [])
                        ),
                        "warnings": list(
                            deterministic_report.get("warnings", [])
                        ),
                    }
                    self._force_retranslate_workflow_candidate(
                        state,
                        plan_result,
                        result,
                        forced_report,
                    )
                    rewrite_count += 1
                    state.add_log(
                        "Skill reviewer returned not_executable after route "
                        "acceptance; forced a Device-local workflow candidate "
                        f"({rewrite_count}/{max_modifications})"
                    )
                    if not self._materialize_recipe_files(state, result):
                        verdict = "not_executable"
                        break
                    continue
                break

            replacement_json = review.get("workflow_json")
            replacement_steps = (
                replacement_json.get("steps")
                if isinstance(replacement_json, dict)
                else None
            )
            if not allow_rewrite or not isinstance(replacement_steps, list) or not replacement_steps:
                if allow_rewrite:
                    self._force_retranslate_workflow_candidate(
                        state,
                        plan_result,
                        result,
                        deterministic_report,
                    )
                    rewrite_count += 1
                    state.add_log(
                        "Skill reviewer returned an incomplete rewrite; forced "
                        f"a new translation candidate ({rewrite_count}/"
                        f"{max_modifications})"
                    )
                    if not self._materialize_recipe_files(state, result):
                        verdict = "not_executable"
                        break
                    continue
                last_review = dict(review)
                last_review["verdict"] = "not_executable"
                last_review.setdefault("issues", []).append(
                    {
                        "code": "incomplete_or_disallowed_rewrite",
                        "severity": "error",
                        "reason": (
                            "Reviewer returned rewritten when no further rewrite was "
                            "allowed, or omitted the complete replacement workflow."
                        ),
                    }
                )
                reviews[-1] = self._review_record(last_review, round_number)
                verdict = "not_executable"
                break

            replacement = dict(replacement_json)
            replacement.setdefault(
                "temporal_adaptations", plan_result.get("temporal_adaptations", [])
            )
            replacement.setdefault(
                "offline_handoffs", plan_result.get("offline_handoffs", [])
            )
            self._inherit_workflow_source_traces(
                [
                    step
                    for step in replacement.get("steps", []) or []
                    if isinstance(step, dict)
                ],
                [
                    step
                    for step in plan_result.get("device_plan", []) or []
                    if isinstance(step, dict)
                ],
            )
            result["workflow_json"] = replacement
            result["workflow_txt"] = self._workflow_txt_from_json(replacement)
            self._stamp_device_steps_with_macro_action(
                replacement, state.research_handoff
            )
            self._apply_deterministic_completion(state, result)
            rewrite_count += 1
            state.add_log(
                "workflow Skill reviewer rewrote the complete workflow "
                f"(rewrite {rewrite_count}/{max_modifications})"
            )
            if not self._materialize_recipe_files(state, result):
                materialization = result.get("recipe_materialization", {})
                last_review = {
                    "verdict": "not_executable",
                    "summary": "recipe materialization failed after workflow rewrite",
                    "issues": [
                        {
                            "code": "recipe_materialization_failed",
                            "severity": "error",
                            "reason": str(error),
                        }
                        for error in materialization.get("errors", [])
                    ],
                }
                reviews.append(self._review_record(last_review, round_number + 1))
                verdict = "not_executable"
                break

        accepted = (
            verdict == "executable"
            and deterministic_report.get("status") != "failed"
        )
        materialization_failed = (
            isinstance(result.get("recipe_materialization"), dict)
            and result["recipe_materialization"].get("status") == "failed"
        )
        issues = (
            last_review.get("issues")
            if isinstance(last_review.get("issues"), list)
            else []
        )
        if materialization_failed:
            errors = list((result.get("recipe_materialization") or {}).get("errors", []))
        elif deterministic_report.get("status") == "failed":
            errors = list(deterministic_report.get("errors", []))
        else:
            errors = [
                self._render_skill_review_issue(issue)
                for issue in issues
                if isinstance(issue, dict)
            ]
        if not accepted and not errors:
            errors = [
                str(
                    last_review.get("summary")
                    or "LLM Skill reviewer 判定 workflow 不可执行。"
                )
            ]
        result["dispatch_validation"] = {
            "status": "passed" if accepted else "failed",
            "errors": [] if accepted else errors,
            "warnings": list(deterministic_report.get("warnings", [])),
            "checked_steps": len((result.get("workflow_json") or {}).get("steps", [])),
            "assessment_source": (
                "deterministic_recipe_materializer"
                if materialization_failed
                else (
                    "llm_workstation_skill_reviewer_internal"
                    if review_internal_error
                    else "llm_workstation_skill_reviewer"
                )
            ),
        }
        result["workflow_skill_review"] = {
            "status": "passed" if accepted else "failed",
            "rounds": reviews,
            "deterministic_rounds": deterministic_rounds,
            "rewritten": rewrite_count > 0,
            "rewrite_count": rewrite_count,
            "final_verdict": verdict or "not_executable",
        }
        cycle_rounds: List[Dict[str, Any]] = copy.deepcopy(
            translation_retry_rounds
        )
        for index, review_record in enumerate(reviews, start=1):
            deterministic = (
                deterministic_rounds[index - 1]
                if index - 1 < len(deterministic_rounds)
                else {}
            )
            cycle_rounds.append(
                {
                    "round": len(translation_retry_rounds) + index,
                    "candidate_kind": (
                        "initial_review"
                        if not translation_retry_rounds and index == 1
                        else "candidate_review"
                    ),
                    "status": (
                        "passed"
                        if review_record.get("verdict") == "executable"
                        and deterministic.get("status") != "failed"
                        else "failed"
                    ),
                    "errors": list(deterministic.get("errors", [])),
                    "issues": copy.deepcopy(review_record.get("issues", [])),
                    "verdict": review_record.get("verdict", "not_executable"),
                }
            )
        result["workflow_repair_cycle"] = {
            "initial_candidate_count": 1,
            "modification_count": rewrite_count,
            "max_modifications": max_modifications,
            "status": "passed" if accepted else "failed",
            "rounds": cycle_rounds,
            "mechanical_completions_counted": False,
        }
        return result

    def _invoke_workflow_skill_review(
        self,
        state: SingleDeviceAgentState,
        plan_result: Dict[str, Any],
        result: Dict[str, Any],
        *,
        allow_rewrite: bool,
        round_number: int,
    ) -> Dict[str, Any]:
        plan_view = {
            "research_handoff": state.research_handoff,
            "macro_plan_summary": plan_result.get("macro_plan_summary", ""),
            "device_plan": plan_result.get("device_plan", []),
            "reagent_slot_plan": plan_result.get("reagent_slot_plan", []),
            "container_plan": plan_result.get("container_plan", []),
            "temporal_adaptations": plan_result.get("temporal_adaptations", []),
            "offline_handoffs": plan_result.get("offline_handoffs", []),
            "deterministic_validation": result.get(
                "pre_review_deterministic_validation", {}
            ),
        }
        deterministic_failed = (
            isinstance(plan_view["deterministic_validation"], dict)
            and plan_view["deterministic_validation"].get("status") == "failed"
        )
        if allow_rewrite and deterministic_failed:
            policy = (
                "允许完整重写；deterministic_validation 已列出当前 workflow 的确定性"
                "错误，必须逐项修复。存在这些错误时禁止返回 executable。不得改变 macro "
                "plan 化学语义和数值。若说明文字与平台下发 schema 冲突，以确定性错误"
                "所依据的下发 schema 为准，并改用可映射的工作站组合。"
            )
        elif allow_rewrite:
            policy = "允许在不改变 macro plan 化学语义和数值的前提下完整重写一次。"
        else:
            policy = "这是重写后的最终复审。禁止再次重写；只能返回 executable 或 not_executable。"
        prompt = WORKFLOW_SKILL_REVIEW_TASK_PROMPT
        replacements = {
            "{review_policy}": policy,
            "{macro_plan_json}": json.dumps(plan_view, ensure_ascii=False, indent=2),
            "{workflow_txt}": str(result.get("workflow_txt", "")),
            "{workflow_json}": json.dumps(
                result.get("workflow_json", {}), ensure_ascii=False, indent=2
            ),
            "{all_workstation_skills}": "完整能力目录、全局规则和所选合同见本轮原生工具上下文；必要时继续加载其他工作站。",
        }
        for placeholder, value in replacements.items():
            prompt = prompt.replace(placeholder, value)
        messages = [
            SystemMessage(content=WORKFLOW_SKILL_REVIEW_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]
        print(
            f"[single-device-agent] LLM step start: workflow_skill_review "
            f"(round {round_number})",
            flush=True,
        )
        try:
            review = self._invoke_json_object_with_format_retry(
                state,
                messages,
                step_name=f"workflow_skill_review_round_{round_number}",
                workstation_tools=True,
                skill_codes=self._workstation_skill_session().referenced_codes(
                    {
                        "device_plan": plan_result.get("device_plan", []),
                        "reagent_slot_plan": plan_result.get("reagent_slot_plan", []),
                        "workflow": result.get("workflow_json", {}),
                    }
                ),
                retry_instruction=(
                    _JSON_FORMAT_RETRY_INSTRUCTION
                    + "\nverdict=rewritten 时只返回完整 workflow_json，不要返回 "
                    "workflow_txt 的重复文本。"
                ),
            )
            if str(review.get("verdict", "")).strip().lower() == "rewritten":
                replacement = review.get("workflow_json")
                if isinstance(replacement, dict) and isinstance(
                    replacement.get("steps"), list
                ):
                    review["workflow_txt"] = self._workflow_txt_from_json(replacement)
        except Exception as exc:
            failure_text = _safe_model_failure_text(exc)
            review = {
                "verdict": "not_executable",
                "summary": f"workflow Skill review failed: {type(exc).__name__}: {failure_text}",
                "_device_internal_error": True,
                "internal_error_type": type(exc).__name__,
                "issues": [
                    {
                        "code": "skill_review_failed",
                        "severity": "error",
                        "reason": failure_text,
                    }
                ],
            }
            diagnostics = get_responses_diagnostics(exc)
            if diagnostics is not None:
                review["llm_diagnostics"] = diagnostics
                review["llm_failure_step"] = f"workflow_skill_review_round_{round_number}"
        print(
            f"[single-device-agent] LLM step done: workflow_skill_review "
            f"(round {round_number})",
            flush=True,
        )
        return review

    @staticmethod
    def _workflow_txt_from_json(workflow_json: Dict[str, Any]) -> str:
        """Build the human-readable view from the dispatch source of truth."""
        lines: List[str] = []
        for index, step in enumerate(workflow_json.get("steps", []) or [], start=1):
            if not isinstance(step, dict):
                continue
            step["step_number"] = index
            workstation = str(step.get("workstation", "")).strip()
            operation = str(step.get("operation", "")).strip()
            parameters = step.get("parameters")
            lines.append(f"第{index}步 {workstation}：{operation}")
            if isinstance(parameters, dict):
                lines.append(
                    "参数：" + json.dumps(parameters, ensure_ascii=False, sort_keys=True)
                )
            notes = str(step.get("notes", "")).strip()
            if notes:
                lines.append(f"声明：{notes}")
            lines.append("")
        return "\n".join(lines).strip()

    @staticmethod
    def _review_record(review: Dict[str, Any], round_number: int) -> Dict[str, Any]:
        return {
            "round": round_number,
            "verdict": str(review.get("verdict", "not_executable")),
            "summary": str(review.get("summary", "")),
            "issues": review.get("issues", [])
            if isinstance(review.get("issues"), list)
            else [],
        }

    @staticmethod
    def _render_skill_review_issue(issue: Dict[str, Any]) -> str:
        code = str(issue.get("code") or "workstation_skill_violation")
        steps = issue.get("step_numbers")
        prefix = f"步骤 {steps}: " if isinstance(steps, list) and steps else ""
        reason = str(issue.get("reason") or issue.get("message") or code)
        return f"{prefix}{reason}（{code}）。"

    def _merge_plan_and_translation(
        self,
        plan_result: Dict[str, Any],
        translated: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Compose the terminal result: plan fields from stage 1, workflow
        fields from stage 2. temporal_adaptations/offline_handoffs live inside
        workflow_json per the established contract, sourced from the plan."""
        workflow_json = translated.get("workflow_json")
        workflow_json = dict(workflow_json) if isinstance(workflow_json, dict) else {}
        workflow_json.setdefault(
            "temporal_adaptations", plan_result.get("temporal_adaptations", [])
        )
        workflow_json.setdefault(
            "offline_handoffs", plan_result.get("offline_handoffs", [])
        )
        merged = {
            "status": "success",
            "feasibility_accepted": bool(
                plan_result.get("feasibility_accepted", False)
            ),
            "feasibility_certificate": copy.deepcopy(
                plan_result.get("feasibility_certificate", {})
            ),
            "feasibility": plan_result.get("feasibility", {}),
            "macro_plan_summary": plan_result.get("macro_plan_summary", ""),
            "device_self_check": plan_result.get("device_self_check", {}),
            "reagent_slot_plan": plan_result.get("reagent_slot_plan", []),
            "container_plan": plan_result.get("container_plan", []),
            "device_plan": plan_result.get("device_plan", []),
            "quantity_adjustments": copy.deepcopy(
                plan_result.get("quantity_adjustments", [])
            ),
            "batch_plan": copy.deepcopy(plan_result.get("batch_plan", [])),
            "material_ledger": copy.deepcopy(
                plan_result.get("material_ledger", {})
            ),
            "quantity_audit": copy.deepcopy(
                plan_result.get("quantity_audit", {})
            ),
            "plan_changes": copy.deepcopy(plan_result.get("plan_changes", [])),
            "change_rationale": plan_result.get("change_rationale", ""),
            "expected_resolved_errors": copy.deepcopy(
                plan_result.get("expected_resolved_errors", [])
            ),
            "workflow_txt": str(translated.get("workflow_txt", "")),
            "workflow_json": workflow_json,
        }
        return merged

    def _contract_gate_enabled(self) -> bool:
        """Skill-contract normalization+audit gate. On by default; legacy tests
        that pin the pre-contract behaviour set CHEM_DEVICE_CONTRACT_AUDIT=off."""
        if self._contract_engine is None:
            return False
        raw = os.getenv("CHEM_DEVICE_CONTRACT_AUDIT", "").strip().lower()
        return raw not in {"0", "off", "false", "no"}

    def _run_full_checks(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Schema validation + txt↔json consistency + capability audit, merged
        into one report so the repair loop sees every deterministic finding."""
        workflow_json = result.get("workflow_json")
        checked_steps = len(
            workflow_json.get("steps", [])
            if isinstance(workflow_json, dict)
            and isinstance(workflow_json.get("steps"), list)
            else []
        )

        def internal_report(stage: str, exc: Exception) -> Dict[str, Any]:
            return {
                "status": "failed",
                "errors": [
                    f"Device validation internal error at {stage}: "
                    f"{type(exc).__name__}: {exc}"
                ],
                "warnings": [],
                "checked_steps": checked_steps,
                "assessment_source": "deterministic_device_validation_internal",
                "_device_internal_error": True,
            }

        if self._contract_gate_enabled() and isinstance(workflow_json, dict):
            # Deterministic normalization first: inject Skill station ids and
            # coerce declared scalar types/enum labels. Never changes chemistry.
            try:
                notes = self._contract_engine.normalize_workflow(workflow_json)
            except Exception as exc:  # pragma: no cover - injected in tests
                return internal_report("contract_normalization", exc)
            if notes:
                result.setdefault("normalization_notes", []).extend(notes)
        try:
            report = self._workflow_validator.validate(workflow_json)
        except Exception as exc:  # pragma: no cover - injected in tests
            return internal_report("workflow_validator", exc)
        errors = list(report.get("errors", []))
        warnings = list(report.get("warnings", []))
        errors.extend(self._workflow_plan_step_trace_errors(result))
        quantity_audit = result.get("quantity_audit")
        if (
            isinstance(quantity_audit, dict)
            and quantity_audit.get("status") == "failed"
        ):
            for issue in quantity_audit.get("issues", []) or []:
                if isinstance(issue, dict):
                    message = str(issue.get("message") or issue.get("code") or issue)
                    code = str(issue.get("code") or "device_local_quantity_error")
                else:
                    message = str(issue)
                    code = "device_local_quantity_error"
                errors.append(f"数量审计失败：{message}（{code}）")
        recipe_materialization = result.get("recipe_materialization")
        if (
            isinstance(recipe_materialization, dict)
            and recipe_materialization.get("status") == "failed"
        ):
            errors.extend(
                str(item)
                for item in recipe_materialization.get("errors", [])
                if str(item).strip()
            )

        consistency = self._workflow_validator.validate_consistency(
            result.get("workflow_txt", ""), workflow_json
        )
        errors.extend(consistency.get("errors", []))
        warnings.extend(consistency.get("warnings", []))

        audit_findings = audit_offline_handoffs(workflow_json)
        audit_findings += audit_connected_sample_container_chain(workflow_json)
        audit_findings += scan_manual_material_operations(
            workflow_json, str(result.get("workflow_txt", ""))
        )
        errors.extend(finding["message"] for finding in audit_findings)
        result["capability_audit"] = (
            {"status": "flagged", "findings": audit_findings}
            if audit_findings
            else {"status": "clean", "findings": []}
        )

        # Issue #4 hard gate: every step must map into the platform's exact
        # dispatch form — an unmapped step means the workflow cannot actually
        # be dispatched, so it is an error, not an FYI.
        try:
            dispatch_preview = format_dispatch_payload(
                workflow_json, self._dispatch_catalog
            )
            unmapped = int(dispatch_preview.get("unmapped_steps", 0) or 0)
            if unmapped > 0:
                preview_warnings = dispatch_preview.get("warnings", []) or []
                detail = "; ".join(str(item) for item in preview_warnings[:3])
                errors.append(
                    f"{unmapped} 个步骤无法映射为平台下发形式"
                    f"（formatter_unmapped_step）。{detail}"
                )
            # dropped platform parameters: the workflow values are lost from
            # the dispatch payload, so treat as a hard error to trigger the
            # repair loop instead of silently degrading the run (issue #17).
            preview_warnings = dispatch_preview.get("warnings", []) or []
            dropped = [
                w for w in preview_warnings
                if isinstance(w, dict) and w.get("code") == "dispatch_parameter_dropped"
            ] if self._contract_gate_enabled() else []
            for item in dropped:
                errors.append(
                    "步骤 {step} 下发参数被省略（dispatch_parameter_dropped）："
                    "{params} 不在 {ws}/{op} 平台参数中".format(
                        step=item.get("step_number"),
                        params="、".join(str(p) for p in item.get("parameters") or []),
                        ws=item.get("workstation"),
                        op=item.get("operation"),
                    )
                )
            # The formatter's public contract historically used readable
            # string warnings.  Do not accidentally bypass this hard gate just
            # because the warning has not yet migrated to a structured dict.
            for warning in preview_warnings if self._contract_gate_enabled() else []:
                if isinstance(warning, str) and "已从下发 payload 省略" in warning:
                    errors.append(
                        "下发参数被省略（dispatch_parameter_dropped）：" + warning
                    )
            if checked_steps and not dispatch_preview.get("payload"):
                return internal_report(
                    "dispatch_formatter_empty_payload",
                    RuntimeError("formatter returned an empty payload for non-empty workflow"),
                )
        except Exception as exc:  # pragma: no cover - injected in tests
            return internal_report("dispatch_formatter", exc)

        if self._contract_gate_enabled() and isinstance(workflow_json, dict):
            # Evaluation-grade Skill contract audit (same engine the external
            # reviewers run): types, enums, ranges, dynamic bottle numbers,
            # per-transfer and cumulative liquid limits, nesting, id match.
            try:
                contract_errors = self._contract_engine.contract_audit_errors(
                    workflow_json
                )
            except Exception as exc:  # pragma: no cover - injected in tests
                return internal_report("contract_audit", exc)
            known = set(errors)
            errors.extend(e for e in contract_errors if e not in known)

        return {
            "status": "failed" if errors else "passed",
            "errors": errors,
            "warnings": warnings,
            "checked_steps": report.get("checked_steps", 0),
        }

    def _workflow_plan_step_trace_errors(
        self,
        result: Dict[str, Any],
    ) -> List[str]:
        """Require an exact workflow-to-Device-plan provenance cover.

        ``source_plan_step`` is metadata and is never dispatched.  It is a
        scalar because each physical workflow step is translated from exactly
        one stable plan step; one plan step may expand to many workflow steps.
        Missing/unknown references or duplicate plan ids fail closed in both
        deterministic and Skill-contract modes.
        """

        plan_steps = result.get("device_plan")
        if not isinstance(plan_steps, list) or not plan_steps:
            return []
        errors: List[str] = []
        plan_ids: List[str] = []
        plan_by_id: Dict[str, Dict[str, Any]] = {}
        for index, step in enumerate(plan_steps, start=1):
            if not isinstance(step, dict):
                errors.append(
                    f"device_plan[{index}] 不是 object（invalid_device_plan_step）。"
                )
                continue
            plan_id = str(step.get("plan_step") or "").strip()
            if not plan_id:
                errors.append(
                    f"device_plan[{index}] 缺少稳定 plan_step "
                    "（missing_device_plan_step_id）。"
                )
                continue
            plan_ids.append(plan_id)
            plan_by_id.setdefault(plan_id, step)
        duplicate_plan_ids = sorted(
            {plan_id for plan_id in plan_ids if plan_ids.count(plan_id) > 1}
        )
        if duplicate_plan_ids:
            errors.append(
                "device_plan 含重复 plan_step，workflow 无法唯一追踪 "
                f"（duplicate_device_plan_step_id）：{duplicate_plan_ids}。"
            )
        expected = set(plan_ids)
        workflow_json = result.get("workflow_json")
        workflow_steps = (
            workflow_json.get("steps", [])
            if isinstance(workflow_json, dict)
            else []
        )
        if not isinstance(workflow_steps, list):
            workflow_steps = []
        covered: Set[str] = set()
        valid_workflow_refs: Dict[str, List[Dict[str, Any]]] = {}
        for index, step in enumerate(workflow_steps, start=1):
            if not isinstance(step, dict):
                continue
            step_number = step.get("step_number", index)
            raw_source = step.get("source_plan_step")
            if isinstance(raw_source, (list, dict, tuple, set)):
                errors.append(
                    f"workflow 步骤 {step_number} 的 source_plan_step 必须是"
                    " scalar（invalid_workflow_plan_step_trace）。"
                )
                continue
            source = str(raw_source or "").strip()
            if not source:
                errors.append(
                    f"workflow 步骤 {step_number} 缺少 source_plan_step "
                    "（missing_workflow_plan_step_trace）。"
                )
                continue
            if source not in expected:
                errors.append(
                    f"workflow 步骤 {step_number} 引用了不存在的 plan_step="
                    f"{source}（unknown_workflow_plan_step_trace）。"
                )
                continue
            source_plan = plan_by_id[source]
            workflow_station = str(step.get("workstation") or "").strip()
            plan_station = str(
                source_plan.get("workstation") or ""
            ).strip()
            resolved_workflow_station = (
                self._truth_workstation_code(workflow_station)
                or workflow_station
            )
            resolved_plan_station = (
                self._truth_workstation_code(plan_station) or plan_station
            )
            if (
                not workflow_station
                or not plan_station
                or resolved_workflow_station != resolved_plan_station
            ):
                errors.append(
                    f"workflow 步骤 {step_number} 的 workstation="
                    f"{workflow_station or '<missing>'} 与 source_plan_step="
                    f"{source} 冻结工作站 {plan_station or '<missing>'} 不一致"
                    "（workflow_plan_step_workstation_mismatch）。"
                )
                continue
            expected_macro_sources = set(
                self._source_macro_step_ids(source_plan)
            )
            actual_macro_sources = set(
                self._source_macro_step_ids(step)
            )
            if expected_macro_sources != actual_macro_sources:
                errors.append(
                    f"workflow 步骤 {step_number} 的 source_macro_steps="
                    f"{sorted(actual_macro_sources)} 与 source_plan_step="
                    f"{source} 的冻结来源 {sorted(expected_macro_sources)} 不一致"
                    "（workflow_plan_step_macro_trace_mismatch）。"
                )
                continue
            valid_workflow_refs.setdefault(source, []).append(step)
        auxiliary_operation_pattern = re.compile(
            r"^(?:开盖|关盖|物料拿取|物料放置|容器(?:拿取|放置|转移|中转)|"
            r"运输|transfer\s+container)$",
            re.I,
        )

        def exact_or_curated_operation(
            platform_station: Optional[str], operation: str
        ) -> Optional[str]:
            """Resolve only truth-exact names and explicit curated aliases.

            ``DispatchCatalog.resolve_operation`` intentionally has a len-one
            fallback for legacy formatting.  That is unsafe for provenance:
            on a one-operation stirrer it could map arbitrary ``开盖`` to
            ``开始搅拌`` and falsely claim the chemical operation was covered.
            """

            operations = self._dispatch_catalog.stations.get(
                str(platform_station or ""), {}
            )
            text = str(operation or "").strip()
            if not text or not operations:
                return None
            if text in operations:
                return text
            for candidate in CURATED_OPERATION_ALIASES.get(text, []):
                if candidate in operations:
                    return candidate
            normalized = text.replace("_", "").replace(" ", "").lower()
            for candidate in operations:
                if (
                    candidate.replace("_", "").replace(" ", "").lower()
                    == normalized
                ):
                    return candidate
            return None

        for plan_id in sorted(expected):
            traced_steps = valid_workflow_refs.get(plan_id, [])
            if not traced_steps:
                continue
            source_plan = plan_by_id[plan_id]
            plan_station = str(source_plan.get("workstation") or "").strip()
            plan_intent = str(
                source_plan.get("operation_intent") or ""
            ).strip()
            dispatch_station = self._dispatch_catalog.resolve_station(
                plan_station
            )
            resolved_plan_operation = exact_or_curated_operation(
                dispatch_station, plan_intent
            )
            if resolved_plan_operation:
                operation_covered = any(
                    exact_or_curated_operation(
                        dispatch_station,
                        str(step.get("operation") or "").strip(),
                    )
                    == resolved_plan_operation
                    for step in traced_steps
                )
            else:
                operation_covered = any(
                    str(step.get("operation") or "").strip()
                    and not auxiliary_operation_pattern.fullmatch(
                        str(step.get("operation") or "").strip()
                    )
                    for step in traced_steps
                )
            if not operation_covered:
                errors.append(
                    f"workflow 对 plan_step={plan_id} 只有开关盖/容器中转"
                    "辅助步骤，或遗漏了可由真源解析的 operation_intent="
                    f"{plan_intent or '<missing>'}"
                    "（workflow_plan_step_operation_missing）。"
                )
                continue
            covered.add(plan_id)
        missing = sorted(expected - covered)
        if missing:
            errors.append(
                "workflow 未覆盖完整 device_plan "
                f"（missing_device_plan_workflow_coverage）：{missing}。"
            )
        return errors

    def _build_repair_instruction(
        self,
        result: Dict[str, Any],
        report: Dict[str, Any],
    ) -> str:
        stations_in_errors: List[str] = []
        for step in (result.get("workflow_json") or {}).get("steps", []):
            if isinstance(step, dict):
                station = str(step.get("workstation", "")).strip()
                if station and station not in stations_in_errors:
                    stations_in_errors.append(station)
        allowed_lines = []
        for station in stations_in_errors[:8]:
            allowed = self._workflow_validator.allowed_params_for(station)
            if allowed:
                allowed_lines.append(f"- {station} 可下发参数：{', '.join(allowed)}")
        audit = result.get("capability_audit") or {}
        route_note = ""
        if any(
            finding.get("type") == "unnecessary_offline_handoff"
            for finding in audit.get("findings", [])
        ):
            route_note = "\n\n" + SOLID_WEIGHING_ROUTE_NOTE
        return (
            "## 下发参数修复要求\n"
            "上一轮 workflow 未通过确定性校验（含参数 schema、txt/json 一致性、"
            "能力反查），错误如下：\n"
            + "\n".join(f"- {item}" for item in report["errors"][:12])
            + (
                "\n\n各工作站真源允许的参数字段：\n" + "\n".join(allowed_lines)
                if allowed_lines
                else ""
            )
            + route_note
            + "\n请只使用真源参数表中列出的工作站、操作和参数字段（保持真源中的层级字段名，"
            "不要发明聚合字段），移除臆造字段，补全必填参数。不得直接篡改 device_plan 的"
            "化学数值；若单次参数越界，使用合法工作站、分次、分瓶或容量拆批使每次下发落在"
            "范围内，并保持累计量。需要改变总量/浓度/摩尔比时留给本周期耗尽后的计划级修复；"
            "被标记为 unnecessary_offline_handoff 或人工物料操作的内容必须改写为对应"
            "工作站的设备步骤（不允许保留人工称量/转移），然后重新输出完整 JSON。"
        )

    def _apply_deterministic_completion(
        self,
        state: SingleDeviceAgentState,
        result: Dict[str, Any],
    ) -> None:
        """Fill mechanically-derivable required fields in place (audit-logged).

        Runs on BOTH the first mapping and the self-repair output so neither can
        fail validation on omissions the harness can fill deterministically.
        Accumulates into result['dispatch_completion'] across calls.
        """
        if self._contract_version == "v2":
            # V2 requires the binding LLM to emit every station id, operation,
            # container field and machine parameter.  The deterministic layer
            # reports omissions and never turns them into apparently valid
            # commands by choosing defaults.
            return
        recipe_filled = self._restore_recipe_evidence_from_device_plan(result)
        id_filled = self._fill_workstation_ids(result.get("workflow_json"))
        completed, filled_log = complete_required_fields(
            result.get("workflow_json"), self._workflow_validator
        )
        lid_filled = self._normalize_lid_retention(completed)
        all_filled = recipe_filled + id_filled + filled_log + lid_filled
        if not all_filled:
            return
        result["workflow_json"] = completed
        prior = (result.get("dispatch_completion") or {}).get("filled", [])
        result["dispatch_completion"] = {"filled": list(prior) + all_filled}
        state.add_log(
            f"deterministic completion filled {len(all_filled)} required "
            "field(s) before dispatch validation"
        )

    @staticmethod
    def _normalize_lid_retention(
        workflow_json: Any,
    ) -> List[Dict[str, Any]]:
        """Make open-lid retention agree with the later close sequence."""
        if not isinstance(workflow_json, dict):
            return []
        steps = workflow_json.get("steps")
        if not isinstance(steps, list):
            return []
        filled: List[Dict[str, Any]] = []

        def _identity(step: Dict[str, Any]) -> Tuple[str, Set[Any]]:
            params = step.get("parameters")
            params = params if isinstance(params, dict) else {}
            values = params.get("容器编号")
            return (
                str(params.get("容器类型", "")).strip(),
                set(values) if isinstance(values, list) else set(),
            )

        for index, step in enumerate(steps):
            if not isinstance(step, dict) or str(step.get("operation", "")) != "开盖":
                continue
            params = step.get("parameters")
            if not isinstance(params, dict) or "保留瓶盖" not in params:
                continue
            container_type, container_ids = _identity(step)
            later_close = False
            for candidate in steps[index + 1 :]:
                if not isinstance(candidate, dict) or str(candidate.get("operation", "")) != "关盖":
                    continue
                other_type, other_ids = _identity(candidate)
                if container_type == other_type and container_ids and container_ids <= other_ids:
                    later_close = True
                    break
            expected = 1 if later_close else 0
            if params.get("保留瓶盖") == expected:
                continue
            previous = params.get("保留瓶盖")
            params["保留瓶盖"] = expected
            filled.append(
                {
                    "step_number": step.get("step_number"),
                    "field": "parameters.保留瓶盖",
                    "param": "保留瓶盖",
                    "value": expected,
                    "previous": previous,
                    "source": "deterministic future-close sequence",
                }
            )
        return filled

    def _fill_workstation_ids(
        self,
        workflow_json: Any,
    ) -> List[Dict[str, Any]]:
        """Stamp the exact Skill workstation id onto every workflow step."""
        if not isinstance(workflow_json, dict):
            return []
        steps = workflow_json.get("steps")
        if not isinstance(steps, list):
            return []
        filled: List[Dict[str, Any]] = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            station = str(step.get("workstation", "")).strip()
            if not station:
                continue
            platform = self._dispatch_catalog.resolve_station(station)
            station_id = self._dispatch_catalog.station_ids.get(station)
            if station_id is None and platform:
                station_id = self._dispatch_catalog.station_ids.get(platform)
            if station_id is None:
                continue
            if step.get("id") == station_id:
                continue
            previous = step.get("id")
            step["id"] = station_id
            filled.append(
                {
                    "step_number": step.get("step_number"),
                    "field": "id",
                    "param": "id",
                    "value": station_id,
                    "previous": previous,
                    "source": "lab-design-all/SKILL.md workstation code",
                }
            )
        return filled

    @staticmethod
    def _restore_recipe_evidence_from_device_plan(
        result: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Restore solid-recipe evidence that translation dropped.

        The stage-1 device plan already fixes the target mass and hopper.  The
        stage-2 translator occasionally emits the correct file-backed solid
        weighing operation but omits those two values because they are not
        dispatch parameters.  Recipe XLSX generation still needs them.  Match
        plan/workflow weighing steps by station, macro step, container and
        occurrence order, then append the uniquely determined values to notes.
        No chemistry or quantity is inferred here.
        """
        workflow = result.get("workflow_json")
        plan = result.get("device_plan")
        if not isinstance(workflow, dict) or not isinstance(plan, list):
            return []
        steps = workflow.get("steps")
        if not isinstance(steps, list):
            return []

        def _containers(item: Dict[str, Any]) -> Tuple[Any, ...]:
            payload = item.get("containers")
            if not isinstance(payload, dict):
                payload = item.get("parameters")
            if not isinstance(payload, dict):
                return ()
            values = payload.get("容器编号")
            return tuple(values) if isinstance(values, list) else ()

        candidates: List[Tuple[int, Dict[str, Any]]] = []
        for index, item in enumerate(plan):
            if not isinstance(item, dict):
                continue
            key_values = item.get("key_values")
            if not isinstance(key_values, dict):
                continue
            has_mass = any(
                key in key_values
                for key in ("目标质量", "加样量", "每瓶加样量", "质量")
            )
            has_hopper = any(
                key in key_values for key in ("料罐号", "料斗号", "料罐", "料斗")
            )
            if has_mass and has_hopper:
                candidates.append((index, item))

        used: Set[int] = set()
        filled: List[Dict[str, Any]] = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            if step.get("operation") != "固体进样-文件传参-机器人":
                continue
            evidence = json.dumps(
                {
                    "notes": step.get("notes", ""),
                    "parameters": step.get("parameters", {}),
                },
                ensure_ascii=False,
            )
            has_mass = bool(
                re.search(r"(?<![\d.])\d+(?:\.\d+)?\s*(?:mg|g)(?![A-Za-z])", evidence, re.I)
            )
            has_hopper = bool(re.search(r"料(?:罐|斗)(?:号|编号)?", evidence))
            if has_mass and has_hopper:
                continue

            station = str(step.get("workstation", "")).strip()
            source_macros = set(
                SingleDeviceAgent._source_macro_step_ids(step)
            )
            container_ids = _containers(step)
            matches = [
                (index, item)
                for index, item in candidates
                if index not in used
                and str(item.get("workstation", "")).strip() == station
                and bool(
                    source_macros
                    & set(SingleDeviceAgent._source_macro_step_ids(item))
                )
                and (not container_ids or not _containers(item) or _containers(item) == container_ids)
            ]
            if not matches:
                continue
            index, source = matches[0]
            key_values = source["key_values"]
            mass = next(
                (
                    key_values[key]
                    for key in ("目标质量", "加样量", "每瓶加样量", "质量")
                    if key in key_values
                ),
                None,
            )
            hopper = next(
                (
                    key_values[key]
                    for key in ("料罐号", "料斗号", "料罐", "料斗")
                    if key in key_values
                ),
                None,
            )
            if mass in (None, "") or hopper in (None, ""):
                continue
            addition = (
                "配方依据（由device_plan确定性补全）："
                f"每瓶加样量={mass}；料罐号={hopper}。"
            )
            prior_notes = str(step.get("notes", "")).strip()
            step["notes"] = f"{prior_notes}\n{addition}".strip()
            used.add(index)
            filled.append(
                {
                    "step_number": step.get("step_number"),
                    "field": "notes.recipe_evidence",
                    "value": {"mass": mass, "hopper": hopper},
                    "source": f"device_plan[{source.get('plan_step', index + 1)}].key_values",
                }
            )
        return filled

    def _materialize_recipe_files(
        self,
        state: SingleDeviceAgentState,
        result: Dict[str, Any],
    ) -> bool:
        """Turn file parameters into concrete task-scoped artifacts before review."""
        workflow_json = result.get("workflow_json")
        if not isinstance(workflow_json, dict):
            return True
        prior_materialization = result.get("recipe_materialization")
        known_artifacts = (
            prior_materialization.get("artifacts", [])
            if isinstance(prior_materialization, dict)
            else []
        )
        try:
            records = materialize_workflow_recipe_files(
                workflow_json,
                exp_id=state.exp_id,
                known_artifacts=known_artifacts,
            )
        except RecipeMaterializationError as exc:
            message = f"recipe materialization failed: {exc}"
            result["recipe_materialization"] = {
                "status": "failed",
                "errors": [message],
                "artifacts": [],
            }
            state.add_error(message)
            return False
        result["recipe_materialization"] = {
            "status": "passed",
            "errors": [],
            "artifacts": records,
        }
        if records:
            result["workflow_txt"] = self._workflow_txt_from_json(workflow_json)
            state.add_log(
                f"materialized {len(records)} recipe file(s) before workflow review"
            )
        return True

    def _normalize_terminal_package(
        self,
        state: SingleDeviceAgentState,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        wait_findings = self._external_return_wait_findings(state.research_handoff, result)
        if wait_findings:
            return self._external_return_wait_result(state, result, wait_findings)
        if str(result.get("status", "")).strip().lower() == "manual_required":
            package = copy.deepcopy(result)
            certificate_accepted = bool(
                state.feasibility_accepted
                and isinstance(state.feasibility_certificate, dict)
                and state.feasibility_certificate.get("accepted") is True
            )
            allowed_scopes = {
                "device_plan",
                "device_quantity",
                "device_workflow",
                "device_internal",
            }
            scope = str(package.get("failure_scope", "") or "").strip()
            if scope not in allowed_scopes:
                scope = "device_workflow" if certificate_accepted else "device_plan"
            # ``manual_required`` is terminal human/Device state by definition;
            # caller-supplied Research fields may never override that, and a
            # signed certificate is restored from immutable runtime state.
            package["feedback_type"] = "human_review_required"
            package["feedback_route"] = "human"
            package["failure_scope"] = scope
            package["feasibility_accepted"] = certificate_accepted
            package["feasibility_certificate"] = (
                copy.deepcopy(state.feasibility_certificate)
                if certificate_accepted
                else {}
            )
            error_package = package.get("error_package")
            if not isinstance(error_package, dict):
                error_package = {}
            else:
                error_package = copy.deepcopy(error_package)
            if str(error_package.get("type", "")) in {
                "research_replan_required",
                "physical_infeasible",
                "device_feasibility_error",
            }:
                error_package["original_type"] = error_package.get("type")
                error_package["type"] = "human_review_required"
            error_package["feedback_route"] = "human"
            error_package["failure_scope"] = scope
            package["error_package"] = error_package
            package.setdefault("exp_id", state.exp_id)
            package.setdefault("iteration_id", state.iteration_id)
            package.setdefault("workflow_id", state.workflow_id)
            package.setdefault("device_snapshot_id", self._device_snapshot_id())
            package.setdefault("workflow_txt", "")
            package.setdefault("workflow_json", {})
            package.setdefault("agent_mode", "single_device_agent")
            return package

        if (
            result.get("status") in {"feasibility_error", "unsupported", "not_feasible"}
            or result.get("feedback_type") == "device_feasibility_error"
        ) and not state.feasibility_accepted:
            # Final fail-closed defense: repair/retry helpers may return a
            # differently nested error shape. Reapply the idempotent connected-
            # platform scrub before any classification or Research routing.
            result = self._remove_implicit_connectivity_blockers(
                state, copy.deepcopy(result)
            )
            verified_complete_joint_proof = (
                self._has_verified_complete_joint_capability_proof(state, result)
            )
            if verified_complete_joint_proof:
                # Canonicalize the entire assessment before routing.  This
                # strips any dose/container/schema blockers appended to a
                # genuine route proof, so only the independently proved frozen
                # chemistry gap can enter Research or its cumulative deadlock.
                canonical_route_gap = self._verified_stage1_core_route_gap_result(
                    state, {}
                )
                if isinstance(canonical_route_gap, dict):
                    result = canonical_route_gap
            feasibility = result.get("feasibility") if isinstance(result.get("feasibility"), dict) else {}
            blocking = self._canonical_stage1_blockers(result)
            if not blocking:
                blocking = ["single device agent 判定当前 macro action 无法映射，但未返回具体阻塞原因。"]
            classification = classify_feasibility_result(result, state.research_handoff)
            overall = classification["overall"] or "unverifiable"
            truth_contradicted_constraints: List[str] = []
            proved_route_gaps = [
                item
                for item in blocking
                if self._constraint_is_true_route_gap(item)
            ]
            if proved_route_gaps:
                # The generic lexical classifier may call a safety/container
                # sentence "unverifiable" because it also contains a local
                # container word.  The strict route predicate has priority.
                overall = "hard"
                classification["hard"] = list(
                    dict.fromkeys(
                        list(classification.get("hard", [])) + proved_route_gaps
                    )
                )
                classification["unverifiable"] = [
                    item
                    for item in classification.get("unverifiable", [])
                    if item not in proved_route_gaps
                ]
            if overall == "hard" and not verified_complete_joint_proof:
                hard_items = classification.get("hard", []) or []
                ungrounded = [
                    str(item)
                    for item in hard_items
                    if not self._constraint_grounded_in_frozen_research(
                        item, state.research_handoff
                    )
                ]
                if ungrounded:
                    ungrounded_set = set(ungrounded)
                    classification["hard"] = [
                        item
                        for item in hard_items
                        if str(item) not in ungrounded_set
                    ]
                    classification["unverifiable"] = list(
                        dict.fromkeys(
                            list(classification.get("unverifiable", []))
                            + ungrounded
                        )
                    )
                    overall = (
                        "hard"
                        if classification["hard"]
                        else (
                            "adaptable"
                            if classification.get("adaptable")
                            else "unverifiable"
                        )
                    )
                    state.add_log(
                        "frozen-research grounding guard: moved "
                        f"{len(ungrounded)} unrelated/vague hard claim(s) "
                        "to unverifiable"
                    )
            # A bare LLM assertion that a named station does not exist is not
            # a route proof when the complete lab-design-all roster contains
            # that exact station/capability.  Move only the contradicted item
            # to the human-review bucket; independent hard constraints remain
            # hard.  The program-generated high-temperature + H2 conjunction
            # is recomputed above and deliberately bypasses this existence
            # guard because simple roster membership cannot satisfy it.
            if overall == "hard" and not verified_complete_joint_proof:
                hard_items = classification.get("hard", []) or []
                truth_contradicted_constraints = [
                    str(item)
                    for item in hard_items
                    if self._constraint_contradicted_by_workstation_truth(item)
                ]
                if truth_contradicted_constraints:
                    contradicted_set = set(truth_contradicted_constraints)
                    classification["hard"] = [
                        item
                        for item in hard_items
                        if str(item) not in contradicted_set
                    ]
                    classification["unverifiable"] = list(
                        dict.fromkeys(
                            list(classification.get("unverifiable", []))
                            + truth_contradicted_constraints
                        )
                    )
                    if classification["hard"]:
                        overall = "hard"
                    elif classification.get("adaptable"):
                        overall = "adaptable"
                    else:
                        overall = "unverifiable"
                    state.add_log(
                        "workstation-truth contradiction guard: moved "
                        f"{len(truth_contradicted_constraints)} false named-"
                        "station absence claim(s) from hard to unverifiable"
                    )
            # Issue #9 guard: a hard verdict built on "缺少固体称量能力" is
            # disproved by the truth source (the solid weighing chain exists);
            # downgrade to unverifiable/needs_human_review. Constraints citing
            # the true gap (石英孔板/马弗炉 path) keep their hard class.
            if overall == "hard":
                hard_items = classification.get("hard", []) or []
                if hard_items and all(
                    weighing_false_hard_guard(str(item)) for item in hard_items
                ):
                    overall = "unverifiable"
                    state.add_log(
                        "weighing false-hard guard: hard verdict rested only on "
                        "weighing-capability claims disproved by the truth "
                        "source; downgraded to needs_human_review"
                    )
                elif hard_items and any(
                    self._constraint_is_true_route_gap(item)
                    for item in hard_items
                ):
                    # A proved route/safety gap wins even if the same sentence
                    # mentions a container; e.g. a mandatory inert sealed
                    # reactor condition with no alternative.
                    pass
                elif hard_items and all(
                    self._constraint_is_device_local(item) for item in hard_items
                ):
                    overall = "adaptable"
                    state.add_log(
                        "route-gate guard: dose/container/capacity/schema findings "
                        "are Device-local and cannot be routed to Research"
                    )
                elif hard_items:
                    overall = "unverifiable"
                    state.add_log(
                        "route-gate guard: no blocker proved a missing necessary "
                        "chemical operation, unsatisfied mandatory safety condition, "
                        "or offline required station without an alternative"
                    )
            if overall == "hard":
                error_type = "physical_infeasible"
                message = result.get("recommendation_to_research_agent", "")
                assessment_source = str(
                    result.get("assessment_source", "")
                    or "single_device_agent_llm"
                )
                requires_review = False
            elif overall == "adaptable":
                error_type = "temporal_adaptation_required"
                message = (
                    "当前反馈仅说明过程/时间语义无法单站原子化执行；"
                    "应优先重试同一容器的分批/间隔节拍。"
                )
                assessment_source = "single_device_agent_llm_soft_temporal"
                requires_review = True
            else:
                error_type = "needs_human_review"
                message = (
                    "设备真源无法证明相关条件被满足，但也没有硬设备能力缺口证据；"
                    "请人工审核该条件，而不是直接判定实验不可执行。"
                    + (
                        f" 原始建议：{result.get('recommendation_to_research_agent', '')}"
                        if result.get("recommendation_to_research_agent")
                        else ""
                    )
                )
                assessment_source = "single_device_agent_llm_unverifiable"
                requires_review = True
            macro_action = (
                state.research_handoff.get("macro_action")
                if isinstance(state.research_handoff.get("macro_action"), dict)
                else {}
            )
            if overall != "hard":
                local_only = bool(
                    blocking
                    and all(self._constraint_is_device_local(item) for item in blocking)
                    and not any(
                        self._constraint_is_true_route_gap(item) for item in blocking
                    )
                )
                local_quantity = bool(
                    local_only
                    and re.search(
                        r"剂量|物质的量|质量不足|用量|料位|累计取液|重复消费|"
                        r"重复计量|容量|体积|分批|拆批|分瓶|配平",
                        _json_text(blocking),
                        re.I,
                    )
                )
                if local_only:
                    error_type = (
                        "device_local_quantity_error"
                        if local_quantity
                        else "device_workflow_error"
                    )
                    message = (
                        "该阻塞属于 Device 层数量/批次/容器/参数规划；"
                        "不得进入 Research，需人工修订 device plan 后续跑。"
                    )
                return {
                    "feedback_type": "human_review_required",
                    "feedback_route": "human",
                    "failure_scope": (
                        "device_quantity"
                        if local_quantity
                        else "device_plan"
                    ),
                    "status": "manual_required",
                    "failure_stage": "route_feasibility_unverifiable",
                    "exp_id": state.exp_id,
                    "iteration_id": state.iteration_id,
                    "workflow_id": state.workflow_id,
                    "device_snapshot_id": self._device_snapshot_id(),
                    "macro_plan": state.research_handoff,
                    "macro_plan_summary": result.get("macro_plan_summary", ""),
                    "macro_action": macro_action,
                    "device_capabilities": result.get("device_capability_summary", {}),
                    "feasibility_assessment": result,
                    "feasibility_accepted": False,
                    "feasibility_certificate": {},
                    "requires_scientific_review": True,
                    "workflow_txt": "",
                    "workflow_json": {},
                    "error_package": {
                        "type": error_type,
                        "device_snapshot_id": self._device_snapshot_id(),
                        "macro_action_id": macro_action.get("macro_action_id", ""),
                        "observation_point_id": macro_action.get("observation_point_id", ""),
                        "constraint_classification": {
                            "hard": classification["hard"],
                            "adaptable": classification["adaptable"],
                            "unverifiable": classification["unverifiable"],
                        },
                        "blocking_constraints": blocking,
                        "assessment_source": assessment_source,
                        "message": message,
                    },
                    "agent_mode": "single_device_agent",
                }

            return {
                "feedback_type": "research_replan_required",
                "legacy_feedback_type": "device_feasibility_error",
                "feedback_route": "research",
                "failure_scope": "route_feasibility",
                "status": "feasibility_error",
                "failure_stage": "route_feasibility_gate",
                "exp_id": state.exp_id,
                "iteration_id": state.iteration_id,
                "workflow_id": state.workflow_id,
                "device_snapshot_id": self._device_snapshot_id(),
                "macro_plan": state.research_handoff,
                "macro_plan_summary": result.get("macro_plan_summary", ""),
                "macro_action": macro_action,
                "device_capabilities": result.get("device_capability_summary", {}),
                "feasibility_assessment": result,
                "feasibility_accepted": False,
                "feasibility_certificate": {},
                "requires_scientific_review": requires_review,
                "error_package": {
                    "type": "research_replan_required",
                    "reason_category": error_type,
                    "device_snapshot_id": self._device_snapshot_id(),
                    "macro_action_id": macro_action.get("macro_action_id", ""),
                    "observation_point_id": macro_action.get("observation_point_id", ""),
                    "observation_point": macro_action.get("observation_point", ""),
                    "constraint_classification": {
                        "hard": classification["hard"],
                        "adaptable": classification["adaptable"],
                        "unverifiable": classification["unverifiable"],
                    },
                    "blocking_constraints": [
                        item
                        for item in blocking
                        if item not in set(truth_contradicted_constraints)
                    ]
                    or blocking,
                    "message": message,
                    "last_workflow_txt": result.get("workflow_txt", ""),
                    "unsupported_items": feasibility.get("unsupported_items", []),
                    "assessment_source": assessment_source,
                },
            }

        quantity_audit_guard = result.get("quantity_audit")
        if (
            state.feasibility_accepted
            and (
                (
                    isinstance(quantity_audit_guard, dict)
                    and quantity_audit_guard.get("status")
                    in {"failed", "human_review_required"}
                )
                or (
                    result.get("quantity_contract_required")
                    and (
                        not isinstance(quantity_audit_guard, dict)
                        or quantity_audit_guard.get("status") != "passed"
                    )
                )
            )
        ):
            return self._build_manual_result(
                state,
                result,
                reason=(
                    "quantity audit is not passed; fail-closed before dispatch "
                    "and keep the repair at Device/human layer"
                ),
                error_type="device_quantity_human_review_required",
            )

        workflow_txt = str(result.get("workflow_txt", "")).strip()
        workflow_json = result.get("workflow_json")
        self_check = result.get("device_self_check", {})
        dispatch_validation = result.get("dispatch_validation")
        if not isinstance(dispatch_validation, dict):
            dispatch_validation = self._workflow_validator.validate(workflow_json)
        # A pre-computed failed report (including empty/truncated translation)
        # must take the structured Device-local failure path below, not the
        # hard-raise guards.  It is never reclassified as route infeasibility.
        if dispatch_validation.get("status") != "failed":
            if not workflow_txt:
                raise ValueError("single device agent success output missing workflow_txt")
            if not isinstance(workflow_json, dict) or not isinstance(workflow_json.get("steps"), list):
                raise ValueError("single device agent success output missing workflow_json.steps")
            self._raise_if_self_check_failed(self_check)

        if not isinstance(workflow_json, dict):
            workflow_json = {}
        if dispatch_validation.get("status") == "failed":
            # Never let an unvalidated dispatch payload leave as success.  Once
            # feasibility is accepted, this branch is Device-local by contract.
            raw_errors = dispatch_validation.get("errors", []) or []
            structured = structure_validation_errors(raw_errors, workflow_json)
            macro_action_view = state.research_handoff.get("macro_action")
            macro_action_view = (
                macro_action_view if isinstance(macro_action_view, dict) else {}
            )
            blocking = [str(err) for err in raw_errors[:8]]
            skill_review = result.get("workflow_skill_review")
            skill_review = skill_review if isinstance(skill_review, dict) else {}
            assessment_source = str(
                dispatch_validation.get(
                    "assessment_source", "deterministic_workstation_validator"
                )
            )
            is_skill_review_failure = (
                assessment_source == "llm_workstation_skill_reviewer"
            )
            is_recipe_failure = assessment_source == "deterministic_recipe_materializer"
            is_internal_failure = bool(
                is_recipe_failure
                or result.get("feedback_type") == "device_internal_error"
                or assessment_source == "llm_workstation_skill_reviewer_internal"
                or assessment_source == "deterministic_device_validation_internal"
                or assessment_source == "final_dispatch_formatter_internal"
            )
            quantity_audit = result.get("quantity_audit")
            quantity_failed = bool(
                isinstance(quantity_audit, dict)
                and quantity_audit.get("status") == "failed"
            )
            return {
                "status": "failed",
                "feedback_type": (
                    "device_internal_error"
                    if is_internal_failure
                    else (
                        "device_local_quantity_error"
                        if quantity_failed
                        else "device_workflow_error"
                    )
                ),
                "feedback_route": "device",
                "failure_scope": (
                    "device_internal"
                    if is_internal_failure
                    else ("device_quantity" if quantity_failed else "device_workflow")
                ),
                "failure_stage": (
                    "recipe_materialization"
                    if is_recipe_failure
                    else (
                        "workflow_skill_review"
                        if is_skill_review_failure
                        else (
                            "device_internal_error"
                            if is_internal_failure
                            else "dispatch_validation"
                        )
                    )
                ),
                "exp_id": state.exp_id,
                "iteration_id": state.iteration_id,
                "workflow_id": state.workflow_id,
                "device_snapshot_id": self._device_snapshot_id(),
                "feasibility_accepted": state.feasibility_accepted,
                "feasibility_certificate": copy.deepcopy(
                    state.feasibility_certificate
                ),
                "macro_plan": state.research_handoff,
                "macro_plan_summary": result.get("macro_plan_summary", ""),
                "dispatch_validation": dispatch_validation,
                "workflow_skill_review": skill_review,
                "recipe_materialization": result.get(
                    "recipe_materialization", {}
                ),
                "capability_audit": result.get(
                    "capability_audit", {"status": "clean", "findings": []}
                ),
                "device_plan": result.get("device_plan", []),
                "quantity_adjustments": result.get("quantity_adjustments", []),
                "batch_plan": result.get("batch_plan", []),
                "material_ledger": result.get("material_ledger", {}),
                "quantity_audit": result.get("quantity_audit", {}),
                "workflow_repair": result.get("workflow_repair", {}),
                "error_package": {
                    "type": (
                        "workflow_skill_review_failed"
                        if is_skill_review_failure
                        else (
                            "recipe_materialization_failed"
                            if is_recipe_failure
                            else (
                                "device_internal_error"
                                if is_internal_failure
                                else "workflow_translation_failed"
                            )
                        )
                    ),
                    "assessment_source": assessment_source,
                    "blocking_constraints": blocking,
                    "structured_errors": structured,
                    "device_snapshot_id": self._device_snapshot_id(),
                    "macro_action_id": str(macro_action_view.get("macro_action_id", "")),
                    "observation_point_id": str(
                        macro_action_view.get("observation_point_id", "")
                    ),
                    "failed_plan_signature": self._plan_signature(state.research_handoff),
                    "message": (
                        "workflow 经完整 Workstation Skill 审核和最多8个修改候选后仍不可执行。"
                        if is_skill_review_failure
                        else (
                            "workflow 引用的设备配方文件未能在 Skill 审核前生成。"
                            if is_recipe_failure
                            else (
                                "workflow 翻译未能通过确定性工作站校验（含有界修复轮）。"
                                "阻塞多为参数字段/结构/范围问题；必须继续留在 Device 层修复。"
                            )
                        )
                    ),
                },
                "message": (
                    "workflow_json 未通过完整 Workstation Skill 可执行性审核，"
                    "该 workflow 不得下发执行。"
                    if is_skill_review_failure
                    else (
                        "设备配方文件未成功物化，该 workflow 不得下发执行。"
                        if is_recipe_failure
                        else (
                            "workflow_json 未通过严格下发参数校验（含自我修复重试），"
                            "该 workflow 不得下发执行。"
                        )
                    )
                ),
                "workflow_txt": workflow_txt,
                "workflow_json": workflow_json,
                "agent_mode": "single_device_agent",
            }

        temporal_adaptations = workflow_json.get("temporal_adaptations", [])
        requires_review = any(
            isinstance(item, dict) and item.get("requires_scientific_review")
            for item in temporal_adaptations
            if isinstance(temporal_adaptations, list)
        )
        quantity_audit = result.get("quantity_audit")
        if isinstance(quantity_audit, dict):
            requires_review = bool(
                requires_review
                or quantity_audit.get("requires_scientific_review")
            )

        macro_action = self._stamp_device_steps_with_macro_action(
            workflow_json, state.research_handoff
        )

        # Harness output → the platform's EXACT parameter form (station names,
        # operation names, per-version parameter keys, declared types, station
        # ids, dispatch envelope). The semantic workflow_json stays untouched.
        try:
            dispatch = format_dispatch_payload(
                workflow_json,
                self._dispatch_catalog,
                plan_name=state.exp_id,
            )
            if workflow_json.get("steps") and not dispatch.get("payload"):
                raise RuntimeError(
                    "formatter returned an empty payload for a non-empty workflow"
                )
            if int(dispatch.get("unmapped_steps", 0) or 0) > 0:
                raise RuntimeError(
                    f"formatter left {dispatch.get('unmapped_steps')} workflow steps unmapped"
                )
        except Exception as exc:  # pragma: no cover - injected in tests
            internal = copy.deepcopy(result)
            internal["feedback_type"] = "device_internal_error"
            internal["feedback_route"] = "device"
            internal["failure_scope"] = "device_internal"
            internal["dispatch_validation"] = {
                "status": "failed",
                "errors": [
                    f"final dispatch formatting failed: {type(exc).__name__}: {exc}"
                ],
                "warnings": [],
                "checked_steps": len(workflow_json.get("steps", [])),
                "assessment_source": "final_dispatch_formatter_internal",
                "_device_internal_error": True,
            }
            return self._normalize_terminal_package(state, internal)

        return {
            "status": "success",
            "feedback_route": "none",
            "failure_scope": "none",
            "exp_id": state.exp_id,
            "iteration_id": state.iteration_id,
            "workflow_id": state.workflow_id,
            "device_snapshot_id": self._device_snapshot_id(),
            "feasibility_accepted": state.feasibility_accepted,
            "feasibility_certificate": copy.deepcopy(
                state.feasibility_certificate
            ),
            "macro_plan": state.research_handoff,
            "macro_plan_summary": result.get("macro_plan_summary", ""),
            "macro_action": macro_action,
            "verification_summary": {
                "result": "accepted",
                "category": "",
                "blocking_constraints": [],
                "message": "single device agent mapped macro action to workstation workflow",
            },
            "feasibility": result.get("feasibility", {}),
            "device_self_check": self_check,
            "dispatch_validation": dispatch_validation,
            "workflow_skill_review": result.get("workflow_skill_review", {}),
            "loaded_workstation_skills": state.loaded_workstation_skills,
            "skill_load_events": state.skill_load_events,
            "recipe_materialization": result.get("recipe_materialization", {}),
            "dispatch_completion": result.get("dispatch_completion", {}),
            "capability_audit": result.get(
                "capability_audit", {"status": "clean", "findings": []}
            ),
            "dispatch_payload": dispatch.get("payload", {}),
            "dispatch_formatting": {
                "mapped_steps": dispatch.get("mapped_steps", 0),
                "unmapped_steps": dispatch.get("unmapped_steps", 0),
                "warnings": dispatch.get("warnings", []),
            },
            "requires_scientific_review": requires_review,
            "reagent_slot_plan": result.get("reagent_slot_plan", []),
            "container_plan": result.get("container_plan", []),
            "device_plan": result.get("device_plan", []),
            "quantity_adjustments": result.get("quantity_adjustments", []),
            "batch_plan": result.get("batch_plan", []),
            "material_ledger": result.get("material_ledger", {}),
            "quantity_audit": result.get("quantity_audit", {}),
            "workflow_repair": result.get("workflow_repair", {}),
            "plan_level_repair": result.get("plan_level_repair", {}),
            "temporal_adaptations": temporal_adaptations,
            "workflow_txt": workflow_txt,
            "workflow_json": workflow_json,
            "agent_mode": "single_device_agent",
        }

    def _attach_v2_contract(
        self,
        state: SingleDeviceAgentState,
        package: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Attach the canonical V2 package while retaining the legacy view."""

        from chem_agent_contracts.adapters import attach_device_v2_contract, research_state_to_v2
        from chem_agent_contracts.v2 import ResearchActionPackageV2

        raw = state.research_handoff.get("research_action_package_v2")
        if isinstance(raw, dict):
            research = ResearchActionPackageV2.model_validate(raw)
        else:
            task = (
                state.research_handoff.get("task")
                if isinstance(state.research_handoff.get("task"), dict)
                else {}
            )
            research = research_state_to_v2(
                {
                    "campaign_id": state.research_handoff.get("campaign_id", ""),
                    "current_stage": task.get("current_stage", "current stage"),
                    "current_stage_plan": task.get("current_stage_plan", "current stage"),
                    "macro_plan": state.research_handoff.get("macro_action_steps", []),
                    "macro_action": state.research_handoff.get("macro_action", {}),
                    "current_evidence_bundle": state.research_handoff.get(
                        "current_evidence_bundle", {}
                    ),
                    "event": {"query": task.get("query", "")},
                }
            )
        return attach_device_v2_contract(package, research)

    def _stamp_device_steps_with_macro_action(
        self,
        workflow_json: Dict[str, Any],
        research_handoff: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Trace each device step to the observation point / macro action it serves.

        Issue 6: device steps must carry observation_point_id + macro_action_id +
        source_macro_step so the UI can show observation → macro action → device
        steps. Ids are inherited from the source macro step (which the research
        layer already stamped); the macro-action descriptor is echoed back.
        """
        macro_action = (
            research_handoff.get("macro_action")
            if isinstance(research_handoff.get("macro_action"), dict)
            else {}
        )
        step_id_map: Dict[Any, Dict[str, str]] = {}
        for macro_step in research_handoff.get("macro_action_steps", []) or []:
            if not isinstance(macro_step, dict):
                continue
            number = macro_step.get("步骤序号")
            ids = {}
            if macro_step.get("macro_action_id"):
                ids["macro_action_id"] = macro_step["macro_action_id"]
            if macro_step.get("observation_point_id"):
                ids["observation_point_id"] = macro_step["observation_point_id"]
            macro_step_id = str(
                macro_step.get("macro_step_id")
                or macro_step.get("logical_step_id")
                or ""
            ).strip()
            if macro_step_id:
                ids["source_macro_step_id"] = macro_step_id
            if number is not None and ids:
                step_id_map[number] = ids
                step_id_map[str(number)] = ids
            if macro_step_id:
                step_id_map[macro_step_id] = ids

        default_ids = {}
        if macro_action.get("macro_action_id"):
            default_ids["macro_action_id"] = macro_action["macro_action_id"]
        if macro_action.get("observation_point_id"):
            default_ids["observation_point_id"] = macro_action["observation_point_id"]

        try:
            device_id_ordinals: Dict[Tuple[str, str], int] = {}
            for position, step in enumerate(workflow_json.get("steps", []) or [], start=1):
                if not isinstance(step, dict):
                    continue
                self._normalize_source_macro_fields(step)
                source = step.get("source_macro_step_id") or step.get("source_macro_step")
                ids = step_id_map.get(source, default_ids)
                for key, value in ids.items():
                    step.setdefault(key, value)
                stable_macro_id = str(step.get("source_macro_step_id") or "").strip()
                if stable_macro_id:
                    source_plan = str(step.get("source_plan_step") or "P").strip()
                    ordinal_key = (stable_macro_id, source_plan)
                    device_id_ordinals[ordinal_key] = (
                        device_id_ordinals.get(ordinal_key, 0) + 1
                    )
                    safe_macro = re.sub(r"[^A-Za-z0-9_-]+", "_", stable_macro_id)
                    safe_plan = re.sub(r"[^A-Za-z0-9_-]+", "_", source_plan)
                    generated_id = (
                        f"DS_{safe_macro}_{safe_plan}_"
                        f"{device_id_ordinals[ordinal_key]:03d}"
                    )
                    if self._contract_version == "v2":
                        step["device_step_id"] = generated_id
                    else:
                        step.setdefault("device_step_id", generated_id)
                station = str(step.get("station_code") or step.get("workstation") or "").strip()
                station_code = self._workstation_skill_session().resolve(station)
                if station_code:
                    step.setdefault("station_code", station_code)
                    platform = self._dispatch_catalog.resolve_station(station_code)
                    if platform:
                        step.setdefault("platform_name", platform)
        except Exception:  # pragma: no cover - stamping must not break success
            pass
        return macro_action

    @staticmethod
    def _looks_like_json_value_start(text: str, offset: int) -> bool:
        """Whether ``text[offset]`` plausibly starts a JSON object/array.

        Markdown citations such as ``[source]`` and prose openings such as
        ``{example`` are not decoded as JSON fragments.  Unmatched closing
        delimiters outside the one complete object are still rejected later.
        A quote, closing delimiter, nested value, number, or JSON literal after
        the opening delimiter is structural enough that a failed decode must
        make the whole response ambiguous instead of selecting a later object.
        """
        opening = text[offset]
        index = offset + 1
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            return True
        next_char = text[index]
        if opening == "{":
            return next_char in {'"', "}"}
        return next_char in {'{', '[', '"', "]", "-"} or next_char.isdigit() or next_char in {
            "t",
            "f",
            "n",
        }

    @staticmethod
    def _json_response_diagnostic(content: str, exc: BaseException) -> str:
        """Return non-sensitive parse diagnostics; never include raw output."""
        digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
        offset = getattr(exc, "pos", None)
        if not isinstance(offset, int):
            match = re.search(r"\boffset=(\d+)", str(exc))
            offset = int(match.group(1)) if match else None
        return (
            f"response_len={len(content)}, sha256={digest}, "
            f"error_class={type(exc).__name__}, "
            f"offset={offset if offset is not None else 'unknown'}"
        )

    def _invoke_json_object_with_format_retry(
        self,
        state: SingleDeviceAgentState,
        messages: List[Any],
        *,
        step_name: str,
        retry_instruction: str = "",
        workstation_tools: bool = False,
        skill_codes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Record failures with the business step; native upstream retry is scoped below."""
        try:
            return self._invoke_json_object_with_format_retry_impl(
                state, messages, step_name=step_name,
                retry_instruction=retry_instruction,
                workstation_tools=workstation_tools, skill_codes=skill_codes,
            )
        except Exception as exc:
            diagnostics = get_responses_diagnostics(exc)
            if diagnostics is not None and not any(
                item.get("task_name") == step_name and item.get("diagnostics") == diagnostics
                for item in state.llm_diagnostics
            ):
                state.llm_diagnostics.append({
                    "task_name": step_name,
                    "exception_type": type(exc).__name__,
                    "diagnostics": diagnostics,
                })
            raise

    def _invoke_json_object_with_format_retry_impl(
        self,
        state: SingleDeviceAgentState,
        messages: List[Any],
        *,
        step_name: str,
        retry_instruction: str = "",
        workstation_tools: bool = False,
        skill_codes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Invoke one Device LLM step with bounded format-only retries.

        An ambiguous response is discarded in full.  We never choose the
        first/last object from multiple top-level JSON values.  The retry is a
        Device-local serialization retry and therefore does not consume a
        Stage-1 plan candidate or workflow-modification slot.  If every bounded
        retry also fails, the caller receives a safe exception and the normal
        Device-internal fail-closed route remains in force.
        """
        if workstation_tools:
            return self._invoke_json_with_workstation_skills(
                state, messages, step_name=step_name,
                retry_instruction=retry_instruction, initial_codes=skill_codes or [],
            )
        base_messages = list(messages)
        retry_message = retry_instruction.strip() or _JSON_FORMAT_RETRY_INSTRUCTION
        last_exc: Optional[BaseException] = None
        for attempt in range(DEFAULT_JSON_FORMAT_RETRY_LIMIT + 1):
            if attempt == 0:
                current_messages = base_messages
            else:
                instruction = retry_message
                if attempt == DEFAULT_JSON_FORMAT_RETRY_LIMIT:
                    instruction += "\n\n" + _JSON_FORMAT_FINAL_RETRY_INSTRUCTION
                current_messages = base_messages + [
                    HumanMessage(content=instruction)
                ]
            structured_invoke = getattr(self._model, "invoke_json_object", None)
            self._assert_workstation_snapshot_current(state)

            def invoke_model() -> Any:
                return (
                    structured_invoke(current_messages)
                    if callable(structured_invoke)
                    else self._model.invoke(current_messages)
                )

            if getattr(self._model, "handles_transport_retries", False) is True:
                response = invoke_model()
            else:
                def timed_invoke() -> Any:
                    if getattr(self._model, "handles_request_timing", False) is True:
                        return invoke_model()
                    with measure_llm_request(
                        component=os.getenv("CHEM_LLM_COMPONENT", "device"),
                        model=str(
                            getattr(self._model, "model_name", "")
                            or type(self._model).__name__
                        ),
                        transport="device_injected_model",
                    ):
                        return invoke_model()

                configured_retries = 0
                for attribute in (
                    "_transport_max_retries", "_chem_gateway_max_retries",
                ):
                    value = getattr(self._model, attribute, None)
                    if isinstance(value, int) and not isinstance(value, bool):
                        configured_retries = max(0, value)
                        break
                    if isinstance(value, str):
                        try:
                            configured_retries = max(0, int(value))
                            break
                        except ValueError:
                            continue
                wall_timeout = getattr(
                    self._model, "_chem_wall_timeout_seconds", None
                )
                if not isinstance(wall_timeout, (int, float)) or isinstance(
                    wall_timeout, bool
                ):
                    wall_timeout = None
                response = call_with_gateway_retry(
                    timed_invoke,
                    max_retries=configured_retries,
                    wall_timeout_seconds=wall_timeout,
                    operation_name=f"Device {step_name}",
                )
            self._assert_workstation_snapshot_current(state)
            content = self._coerce_text(getattr(response, "content", response))
            try:
                parsed = self._parse_json(content)
                if not isinstance(parsed, dict):
                    raise ValueError(
                        "response root must be exactly one JSON object"
                    )
                return parsed
            except (ValueError, json.JSONDecodeError) as exc:
                last_exc = exc
                diagnostic = self._json_response_diagnostic(content, exc)
                if attempt < DEFAULT_JSON_FORMAT_RETRY_LIMIT:
                    state.add_log(
                        f"{step_name} returned malformed or ambiguous JSON; "
                        "discarded the whole response and retrying format "
                        f"{attempt + 1}/{DEFAULT_JSON_FORMAT_RETRY_LIMIT} "
                        "inside Device without consuming a plan/workflow "
                        f"candidate ({diagnostic})"
                    )
                    continue
                state.add_log(
                    f"{step_name} JSON format retry exhausted; keeping failure "
                    f"inside Device ({diagnostic})"
                )
                raise ValueError(
                    f"{step_name} did not return exactly one complete JSON "
                    "object after "
                    f"{DEFAULT_JSON_FORMAT_RETRY_LIMIT} Device-local format "
                    "retries; "
                    f"{diagnostic}"
                ) from exc
        raise ValueError(
            f"{step_name} JSON format retry exhausted: "
            f"{type(last_exc).__name__ if last_exc else 'unknown error'}"
        )

    def _invoke_json_with_workstation_skills(
        self,
        state: SingleDeviceAgentState,
        messages: List[Any],
        *,
        step_name: str,
        retry_instruction: str,
        initial_codes: List[str],
    ) -> Dict[str, Any]:
        """Native discovery/read loop, followed by a loaded-contract coverage gate.

        A model may discover a new station while drafting a candidate. That
        candidate is not accepted until the station's complete contract has
        been read and the model has reviewed the candidate against it. Negative
        capability conclusions escalate to complete-catalog evidence instead
        of treating an unseen station as an absent capability.
        """
        session = self._workstation_skill_session()
        visible = set(initial_codes)
        base_messages = list(messages)
        candidate_feedback = ""
        remaining_tool_calls = max(2, len(session.codes) + 4)

        def record_attempt(event: Dict[str, Any]) -> None:
            # The native helper emits metadata only, never requests or output.
            record = {"task_name": step_name, **copy.deepcopy(event)}
            state.llm_request_attempts.append(record)
            if event.get("event") == "model_attempt_failed":
                diagnostic = event.get("responses_diagnostics")
                if diagnostic is not None:
                    state.llm_diagnostics.append({
                        "task_name": step_name,
                        "exception_type": event.get("exception_type", "ResponsesTerminalError"),
                        "diagnostics": copy.deepcopy(diagnostic),
                        "retry_scheduled": event.get("retry_scheduled", False),
                        "model_turn": event.get("model_turn"),
                        "attempt": event.get("attempt"),
                    })
                if event.get("retry_scheduled"):
                    state.add_log(f"{step_name}: upstream_error; retrying only the failed model turn with completed tool results")
                    print(f"[single-device-agent] {step_name}: upstream_error; scoped retry", flush=True)
            self._assert_workstation_snapshot_current(state)

        def record_load(request: Dict[str, Any], output: Dict[str, Any]) -> None:
            nonlocal remaining_tool_calls
            remaining_tool_calls = max(0, remaining_tool_calls - 1)
            if request.get("name") == "load_workstation_skill" and output.get("station_code") in session.codes:
                visible.add(output["station_code"])
                if session.events:
                    session.events[-1]["tool_call_id"] = request.get("id", "")
            self._sync_skill_load_state(state)

        for coverage_round in range(3):
            context = session.discovery_context()
            if visible:
                context += "\n\n# 本轮已选择的完整合同\n" + session.format_contracts(
                    sorted(visible), origin="selected_context"
                )
            if candidate_feedback:
                context += "\n\n" + candidate_feedback
            current_messages = base_messages + [HumanMessage(content=context)]
            parsed: Optional[Dict[str, Any]] = None
            for format_attempt in range(DEFAULT_JSON_FORMAT_RETRY_LIMIT + 1):
                attempt_messages = list(current_messages)
                if format_attempt:
                    format_instruction = retry_instruction or _JSON_FORMAT_RETRY_INSTRUCTION
                    if format_attempt == DEFAULT_JSON_FORMAT_RETRY_LIMIT:
                        format_instruction += "\n\n" + _JSON_FORMAT_FINAL_RETRY_INSTRUCTION
                    attempt_messages.append(HumanMessage(
                        content=format_instruction
                        + "\n完整输出一个 JSON object；保留已加载的工作站合同。"
                        + session.format_contracts(sorted(visible), origin="format_retry")
                    ))
                session.assert_current()
                retry_options: Dict[str, Any] = {}
                if step_name == "feasibility_device_plan" or step_name.startswith("feasibility_device_plan_chunk_"):
                    retry_options = {
                        "retry_upstream_errors": True,
                        "max_upstream_retries": 1,
                        "on_model_attempt": record_attempt,
                    }
                response = invoke_with_tools(
                    self._model, attempt_messages, [session.tool()],
                    max_rounds=remaining_tool_calls,
                    on_tool_result=record_load,
                    **retry_options,
                )
                session.assert_current()
                content = self._coerce_text(getattr(response, "content", response))
                try:
                    parsed = self._parse_json(content)
                    if not isinstance(parsed, dict):
                        raise ValueError("response root must be exactly one JSON object")
                    break
                except (ValueError, json.JSONDecodeError) as exc:
                    if format_attempt == DEFAULT_JSON_FORMAT_RETRY_LIMIT:
                        raise ValueError(
                            f"{step_name} did not return exactly one complete JSON object after "
                            f"{DEFAULT_JSON_FORMAT_RETRY_LIMIT} Device-local format retries; "
                            + self._json_response_diagnostic(content, exc)
                        ) from exc
                    state.add_log(
                        f"{step_name}: native JSON format retry {format_attempt + 1}; "
                        + self._json_response_diagnostic(content, exc)
                    )

            assert isinstance(parsed, dict)
            missing = set(session.referenced_codes(parsed)) - visible
            feasibility = parsed.get("feasibility", {})
            negative = (
                parsed.get("verdict") == "not_executable"
                or parsed.get("status") in {"feasibility_error", "device_feasibility_error"}
                or (isinstance(feasibility, dict) and feasibility.get("is_feasible") is False)
            )
            if negative:
                missing.update(session.codes - visible)
            if not missing:
                self._sync_skill_load_state(state)
                return parsed
            # Loading a final candidate's station reference is safe and read-only;
            # the subsequent model turn must still reconsider the whole candidate.
            for code in sorted(missing):
                session.load(code, origin="negative_capability_audit" if negative else "candidate_reference")
            visible.update(missing)
            state.add_log(
                f"{step_name}: loaded {len(missing)} additional complete station contracts "
                "and withheld the candidate pending evidence-grounded reconsideration"
            )
            candidate_feedback = (
                "# 尚未接受的上一候选\n" + json.dumps(parsed, ensure_ascii=False)
                + "\n上一候选引用未读合同或作出能力缺口结论；现在已补齐相应完整真源。"
                "必须重新审核容器、参数和辅助操作的可行路径，再输出完整候选。"
                "不能把目录筛选遗漏当成能力缺失；不得改变冻结的 Research 科学语义。"
            )
        self._sync_skill_load_state(state)
        raise ValueError(f"{step_name}: workstation contract coverage did not converge after 3 bounded rounds")

    def _parse_json(self, text: str) -> Any:
        """Parse exactly one JSON value without guessing among alternatives.

        A single object may be wrapped by a Markdown fence or ordinary prose
        on separate lines.  Multiple complete top-level values, a
        second malformed JSON-looking fragment, or same-line trailing junk is
        rejected so the caller can request one clean format retry.
        """
        stripped = text.strip()
        if not stripped:
            raise ValueError("empty JSON response")
        try:
            exact = json.loads(stripped)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(exact, dict):
                return exact
            raise ValueError(
                "response root must be exactly one JSON object; offset=0"
            )

        decoder = json.JSONDecoder()
        candidates: List[Tuple[int, int, Any]] = []
        malformed_offsets: List[int] = []
        index = 0
        while index < len(stripped):
            if stripped[index] not in "{[":
                index += 1
                continue
            plausible = self._looks_like_json_value_start(stripped, index)
            try:
                value, end = decoder.raw_decode(stripped, index)
            except json.JSONDecodeError as exc:
                if plausible:
                    malformed_offsets.append(
                        exc.pos if isinstance(exc.pos, int) else index
                    )
                index += 1
                continue
            if isinstance(value, (dict, list)):
                candidates.append((index, end, value))
                index = end
                continue
            index += 1

        if len(candidates) > 1:
            raise ValueError(
                "ambiguous JSON response: found multiple complete top-level "
                f"values (count={len(candidates)}, "
                f"offset={candidates[1][0]})"
            )
        if not candidates:
            offset = malformed_offsets[0] if malformed_offsets else 0
            raise ValueError(
                "could not parse one complete JSON value from device response; "
                f"offset={offset}"
            )

        start, end, value = candidates[0]
        if not isinstance(value, dict):
            raise ValueError(
                "response root must be exactly one JSON object; "
                f"offset={start}"
            )
        prefix = stripped[:start]
        suffix = stripped[end:]
        if malformed_offsets:
            raise ValueError(
                "JSON response contains an additional malformed JSON-like "
                f"fragment; offset={malformed_offsets[0]}"
            )
        unmatched: List[Tuple[int, str]] = []
        for segment, base_offset in ((prefix, 0), (suffix, end)):
            delimiter_stack: List[str] = []
            pairs = {"}": "{", "]": "["}
            for local_offset, delimiter in enumerate(segment):
                if delimiter in "{[":
                    delimiter_stack.append(delimiter)
                    continue
                if delimiter not in pairs:
                    continue
                if delimiter_stack and delimiter_stack[-1] == pairs[delimiter]:
                    delimiter_stack.pop()
                    continue
                unmatched.append((base_offset + local_offset, delimiter))
                break
        if unmatched:
            outside_offset, delimiter = min(unmatched)
            raise ValueError(
                "JSON response contains an unmatched closing delimiter "
                f"{delimiter!r} outside the complete object; "
                f"offset={outside_offset}"
            )

        # A second top-level JSON scalar does not contain ``{``/``[`` and must
        # not be mistaken for ordinary prose.  Treat a line that consists
        # solely of any valid JSON value (true/false/null/number/string as well
        # as a collection) as another top-level value.  Normal explanatory
        # prose such as "End of result." remains allowed.
        for segment, base_offset in ((prefix, 0), (suffix, end)):
            cursor = 0
            for line in segment.splitlines(keepends=True):
                token = line.strip()
                leading = len(line) - len(line.lstrip())
                token_offset = base_offset + cursor + leading
                cursor += len(line)
                if not token or token.startswith("```"):
                    continue
                try:
                    extra_value = json.loads(token)
                except json.JSONDecodeError:
                    continue
                raise ValueError(
                    "ambiguous JSON response: found an additional complete "
                    f"top-level {type(extra_value).__name__}; "
                    f"offset={token_offset}"
                )

        if (
            suffix
            and not suffix.startswith(("\n", "\r"))
            and not suffix.lstrip().startswith("```")
        ):
            raise ValueError(
                "JSON response contains same-line trailing content; "
                f"offset={end}"
            )
        return value

    def _coerce_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    # Responses/v1 returns typed content blocks.  Reasoning and
                    # function-call blocks can themselves contain JSON-looking
                    # text; stringifying those blocks contaminates the actual
                    # assistant output and makes a single JSON object appear to
                    # be multiple top-level values.  Only textual blocks belong
                    # in the payload passed to the strict JSON parser.
                    block_type = str(item.get("type") or "").strip().lower()
                    text_value = item.get("text")
                    content_value = item.get("content")
                    if isinstance(text_value, str) and block_type in {
                        "",
                        "text",
                        "output_text",
                    }:
                        parts.append(text_value)
                    elif (
                        isinstance(content_value, str)
                        and block_type in {"", "text", "output_text"}
                    ):
                        parts.append(content_value)
                elif item is not None:
                    parts.append(str(item))
            return "\n".join(part.strip() for part in parts if part).strip()
        if content is None:
            return ""
        return str(content).strip()

    def _clean_list(self, value: Any) -> List[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    def _raise_if_self_check_failed(self, self_check: Any) -> None:
        if not isinstance(self_check, dict):
            return

        failed_items = []
        for key, value in self_check.items():
            text = str(value or "").strip().lower()
            if re.search(r"\bfail\b|失败|不通过|未通过", text):
                failed_items.append(f"{key}: {value}")
        if failed_items:
            joined = "; ".join(failed_items)
            raise ValueError(f"single device agent self-check failed: {joined}")

    def _read_text(self, path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def _default_exp_id(self) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        # A terminal checkpoint must never cause a later run to inherit the
        # previous workflow identity, including two invocations in one clock
        # tick or from concurrent workers.
        return f"single_device_{timestamp}_{uuid.uuid4().hex[:8]}"
