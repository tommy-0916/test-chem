"""Single-agent device mapper.

This is the simplified device layer used after research-agent planning:
research macro actions + device truth -> workstation workflow.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from utils.paths import format_reference_path
from utils.workstation_loader import WorkstationLoader

from feasibility_rules import (
    classify_feasibility_result,
    has_temporal_addition_stirring as _has_temporal_addition_stirring,
    json_text as _json_text,
    soft_temporal_mapping_error as _soft_temporal_mapping_error,
)
from dispatch_formatter import (
    DispatchCatalog,
    complete_required_fields,
    format_dispatch_payload,
)
from capability_audit import (
    SOLID_WEIGHING_ROUTE_NOTE,
    audit_offline_handoffs,
    scan_manual_material_operations,
    weighing_false_hard_guard,
)
from workflow_validator import WorkflowValidator, structure_validation_errors

logger = logging.getLogger(__name__)


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
7a. **45 个工作站在物理上是联通的**，工作站之间的物料和样品转移不需要人类参与。
    禁止生成任何 human handoff / manual handoff / 人工拿取 / 人工搬运 / 人工转移 /
    人工称取 / 人工装载 / 人工重新装载步骤（"人工审核/复核/确认"属于审查语义，不受此限）。
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
8. 不要改变研究目标、目标材料、当前 stage 或目标 observation point。
9. 计划中的每个步骤都必须能从给定工作站 USAGE/AUDIT-RULES 找到依据；不要臆造不存在的工作站。
10. 维护容器身份台账：同一个容器编号在整个计划中必须保持同一种容器类型，除非计划中显式存在受支持的转移/换瓶步骤。
11. 维护体积台账：每次加液、洗涤、去上清、保留清洗液、干燥前后都要避免让任一容器的液体体积超过当前工作站/容器真源限制。若宏观体积过大，优先等比例缩小体系或拆分到多个等价样品容器，并在 device_layer_adaptations 中说明；不能静默超限。
12. 离心/纯化需要配平时，进入同一次离心的容器数量、容器类型和液体体积必须可配平。若使用配平瓶，配平瓶体积应与主样瓶接近；若无法配平，返回 device_feasibility_error。
13. 保留化学上有意义的加料方式：若 macro action 明确要求滴加、缓慢加入、分批加入或给出加入速率，必须在计划中写成设备支持的分批/多次加液节拍；不要擅自改成一次性加入，除非 macro action 明确允许。
    “边滴入边搅拌/边搅拌边滴加/同步搅拌加液”默认是可适配的时间语义：若液体进样站和磁力搅拌站支持同一容器，
    应规划“预搅拌 -> 小份加液 -> 固定时间搅拌 -> 重复 -> 最终搅拌”的交替节拍，并在 temporal_adaptations 中标明
    `execution_fidelity=approximated`、原始要求和分批计划；不得仅因没有单站原子化并行动作就返回 feasibility_error。
    只有明确要求不可中断的连续流/恒定流速/微流控进料，或不存在共享容器和合法转移路径时，才可返回 feasibility_error。
14. 如果某个 macro action 会造成“反应/静置/暂存所需容器”与“后续离心/洗涤/干燥/测试所需容器”之间没有受支持的连续路径，不能通过更改容器名称或跳过转移来伪装可执行；必须返回 device_feasibility_error，并在 suggested_research_revision 中说明需要 research layer 改成可连续容器路径的化学语义路线。
15. 输出必须是一个 JSON object，不要 Markdown，不要代码块。
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
   workflow 步骤必须继承来源 plan 步骤的 source_macro_step，并携带 macro_action_id /
   observation_point_id（如 device_plan 提供）。
4. 必填参数（参数表 是否必填=是）必须全部填写；容器数量必须等于容器编号数组长度。
5. workflow_txt 严格仿照 TXT 格式参考（第N步 工作站：…），且与 workflow_json 步骤
   一一对应（块数相同、每块出现该步工作站名）。
6. temporal_adaptations 与 offline_handoffs 原样搬运 device_plan 中的内容，不新增不删除。
7. 输出必须是一个 JSON object：{"workflow_txt": "...", "workflow_json": {...}}，
   不要 Markdown，不要代码块，不要输出其它键。
""".strip()


FEASIBILITY_PLAN_TASK_PROMPT = """
请判断 research handoff 能否由当前设备完成，并产出 device_plan（设备级计划，不含 workflow_json）。

## research handoff
{research_handoff_json}

## 当前设备/器材真源
下面是每个工作站的 USAGE.md 和 AUDIT-RULES.md 摘要/全文。只能使用这里出现的工作站、容器和操作。
{workstation_descriptions}

## 输出 JSON 格式
必须返回一个 JSON object，二选一：

### 情况 A：可以由设备完成 → 输出 device_plan
{{
  "status": "device_plan",
  "feasibility": {{
    "is_feasible": true,
    "blocking_constraints": [],
    "device_layer_adaptations": ["你在计划级补全了哪些设备映射决策"]
  }},
  "macro_plan_summary": "一句话说明从 macro action 到设备计划的映射策略",
  "device_self_check": {{
    "container_continuity": "pass/fail + 简短说明",
    "volume_and_capacity": "pass/fail + 简短说明",
    "centrifuge_balancing": "pass/fail/not_applicable + 简短说明",
    "addition_mode_preserved": "pass/fail/not_applicable + 简短说明",
    "workstation_constraints": "pass/fail + 简短说明"
  }},
  "reagent_slot_plan": [
    {{
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
      "用途": "用途"
    }}
  ],
  "device_plan": [
    {{
      "plan_step": 1,
      "workstation": "工作站名称（真源中的名称）",
      "objective": "这一步要达成什么（化学语义）",
      "operation_intent": "操作意图（如 拿取容器/开盖/分批加液/搅拌/离心/烘干/固体转移/定量称量）",
      "key_values": {{"体积/质量/温度/时间/转速等化学数值": "值+单位"}},
      "containers": {{"容器类型": "进样瓶", "容器编号": [1, 2]}},
      "source_macro_step": 1,
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
      "required_return_data": ["XRD 图谱", "判读结果"]
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

计划要求：
- device_plan 步骤按执行顺序排列；开盖/关盖/配平/重复洗涤在计划级写成显式步骤或 notes 说明。
- 涉及洗涤时，展开成固定次数；涉及干燥时，使用固定温度和固定时间；不写“至……为止”。
- 若无法确定原液编号，自己分配并在 reagent_slot_plan 说明；需要偶数配平时自己选偶数容器并贯穿。
- 对每个容器维护台账（类型/体积/带盖/用途）；换容器必须有真源支持的转移步骤。
- 时间语义（边滴入边搅拌等）在计划级展开为交替节拍并记入 temporal_adaptations。
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
        "source_macro_step": 1,
        "notes": "可选说明"
      }}
    ]
  }}
}}

翻译要求：
- **只输出上述范围内 plan 步骤的 workflow 步骤**，不要输出范围外的步骤，也不要输出
  temporal_adaptations / offline_handoffs（这些由程序从 device_plan 统一附加）。
- 每个 plan 步骤可展开为多个 workflow 步骤（如 开盖→加液→关盖），每个 workflow 步骤继承
  该 plan 步骤的 source_macro_step（以及 macro_action_id/observation_point_id，若提供）。
- 参数字段名/嵌套/类型/枚举/单位/范围严格来自参数表；必填参数全部填写；
  容器数量 == len(容器编号)。
- 化学数值（体积/质量/温度/时间/转速）从 device_plan.key_values 原样搬运，只做单位换算。
- temporal_adaptations 与 offline_handoffs 从 device_plan 原样搬运。
- workflow_txt 与 workflow_json 一一对应（块数相同、每块出现该步工作站名）。
""".strip()


@dataclass
class SingleDeviceAgentState:
    research_handoff: Dict[str, Any]
    exp_id: str
    iteration_id: int = 0
    workflow_id: int = 0
    workstation_descriptions: str = ""
    txt_format_reference: str = ""
    json_format_reference: str = ""
    status: str = "running"
    workflow_txt: str = ""
    workflow_json: Dict[str, Any] = field(default_factory=dict)
    terminal_package: Dict[str, Any] = field(default_factory=dict)
    raw_llm_output: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
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

    def __init__(
        self,
        model: Any,
        *,
        use_new_format: bool = True,
        workstation_loader: Optional[WorkstationLoader] = None,
        workflow_validator: Optional[WorkflowValidator] = None,
    ) -> None:
        self._model = model
        self._workstation_loader = workstation_loader or WorkstationLoader(
            use_new_format=use_new_format
        )
        self._workflow_validator = workflow_validator or WorkflowValidator(
            self._workstation_loader
        )
        self._dispatch_catalog = DispatchCatalog.load(self._workstation_loader)
        self._txt_format_reference = self._read_text(format_reference_path("txt"))
        self._json_format_reference = self._read_text(format_reference_path("json"))

    def _device_snapshot_id(self) -> str:
        loader = self._workstation_loader
        if hasattr(loader, "snapshot_id"):
            try:
                return loader.snapshot_id()
            except Exception:
                return ""
        return ""

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

    def run_state(
        self,
        research_handoff: Dict[str, Any],
        *,
        exp_id: Optional[str] = None,
        iteration_id: int = 0,
        workflow_id: int = 0,
    ) -> SingleDeviceAgentState:
        exp_id = exp_id or self._default_exp_id()
        handoff_text = json.dumps(research_handoff, ensure_ascii=False)
        workstation_selection_text = self._workstation_selection_text(research_handoff)
        if self._use_full_workstation_prompt():
            workstation_descriptions = self._workstation_loader.format_for_prompt()
        elif hasattr(self._workstation_loader, "format_relevant_for_prompt"):
            workstation_descriptions = self._workstation_loader.format_relevant_for_prompt(
                workstation_selection_text
            )
        else:
            workstation_descriptions = self._workstation_loader.format_for_prompt()
        state = SingleDeviceAgentState(
            research_handoff=research_handoff,
            exp_id=exp_id,
            iteration_id=iteration_id,
            workflow_id=workflow_id,
            workstation_descriptions=workstation_descriptions,
            txt_format_reference=self._txt_format_reference,
            json_format_reference=self._json_format_reference,
        )
        state.add_log("SingleDeviceAgent started")
        try:
            # Stage 1: feasibility + device-level plan (no workflow_json).
            plan_result = self._invoke_feasibility_plan(state)
            plan_result = self._retry_adaptable_feedback(state, plan_result, research_handoff)
            plan_result = self._repair_plan_level_findings(state, plan_result)

            if str(plan_result.get("status", "")).strip().lower() in {
                "device_plan", "success",
            }:
                # Stage 2: dedicated translation into strict workflow form;
                # the chemistry plan is frozen from here on — repairs only
                # re-invoke translation.
                result = self._translate_and_verify(state, plan_result)
            else:
                result = plan_result

            state.raw_llm_output = result
            package = self._normalize_terminal_package(state, result)
            state.terminal_package = package
            package_status = str(package.get("status", ""))
            if package_status == "feasibility_error":
                state.status = "feasibility_error"
            elif package_status == "failed":
                state.status = "failed"
            else:
                state.status = "completed"
            state.workflow_txt = str(package.get("workflow_txt", ""))
            workflow_json = package.get("workflow_json")
            state.workflow_json = workflow_json if isinstance(workflow_json, dict) else {}
            state.add_log(f"SingleDeviceAgent completed with status={state.status}")
            return state
        except Exception as exc:
            state.add_error(f"SingleDeviceAgent failed: {type(exc).__name__}: {exc}")
            state.status = "failed"
            logger.exception("SingleDeviceAgent failed")
            raise

    def run(self, research_handoff: Dict[str, Any]) -> Dict[str, Any]:
        return self.run_state(research_handoff).terminal_package

    def _use_full_workstation_prompt(self) -> bool:
        raw = os.getenv("CHEM_DEVICE_AGENT_FULL_WORKSTATIONS", "")
        return raw.strip().lower() in {"1", "true", "yes", "on", "full"}

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

    def _invoke_feasibility_plan(
        self,
        state: SingleDeviceAgentState,
        *,
        extra_instruction: str = "",
    ) -> Dict[str, Any]:
        """Stage 1: feasibility verdict + device_plan (no workflow_json)."""
        prompt = FEASIBILITY_PLAN_TASK_PROMPT
        replacements = {
            "{research_handoff_json}": json.dumps(
                state.research_handoff,
                ensure_ascii=False,
                indent=2,
            ),
            "{workstation_descriptions}": state.workstation_descriptions,
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
        print("[single-device-agent] LLM step start: feasibility_device_plan", flush=True)
        response = self._model.invoke(messages)
        print("[single-device-agent] LLM step done: feasibility_device_plan", flush=True)
        content = self._coerce_text(getattr(response, "content", response))
        result = self._parse_json(content)
        if not isinstance(result, dict):
            raise ValueError("feasibility-plan LLM did not return a JSON object")
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
        response = self._model.invoke(messages)
        content = self._coerce_text(getattr(response, "content", response))
        state.add_log(
            f"translation chunk {chunk_index + 1}/{total_chunks}: "
            f"raw {len(content)} chars"
        )
        result = self._parse_json(content)
        print(
            f"[single-device-agent] LLM step done: workflow_translation "
            f"(chunk {chunk_index + 1}/{total_chunks})",
            flush=True,
        )
        if not isinstance(result, dict):
            raise ValueError(
                f"translation chunk {chunk_index + 1} did not return a JSON object"
            )
        return result

    def _station_parameter_tables(self, stations: List[str]) -> str:
        """SKILL parameter-table excerpts for a given station list — the
        translation stage's only legal source of field names."""
        stations = [s for s in stations if s]
        if not stations:
            return self._workstation_loader.format_for_prompt()
        sections: List[str] = []
        try:
            all_stations = {
                str(entry.get("station_name", "")): entry
                for entry in self._workstation_loader.get_all()
                if isinstance(entry, dict)
            }
        except Exception:
            all_stations = {}
        seen: List[str] = []
        for name in stations:
            if name in seen:
                continue
            seen.append(name)
            entry = all_stations.get(name)
            if entry is None:
                for key, candidate in all_stations.items():
                    if name in key or key in name or name == str(candidate.get("display_name", "")):
                        entry = candidate
                        break
            allowed = self._workflow_validator.allowed_params_for(name)
            header = f"### {name}\n可下发参数：{', '.join(allowed) if allowed else '(未解析)'}"
            body = ""
            if isinstance(entry, dict):
                content = str(entry.get("skill_content", "") or entry.get("usage_content", ""))
                body = content[:4000]
            sections.append(header + ("\n" + body if body else ""))
        return "\n\n".join(sections)

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
        needs_retry = (
            classification["is_error"] and classification["overall"] != "hard"
        ) or _needs_temporal_mapping_retry(result, research_handoff)
        if not needs_retry:
            return result

        complaints = classification["adaptable"] + classification["unverifiable"]
        state.add_log(
            "device feedback contained no hard capability gap "
            f"(buckets: adaptable={len(classification['adaptable'])}, "
            f"unverifiable={len(classification['unverifiable'])}); "
            "retrying with derived-constraint instructions"
        )
        instruction = (
            "## 强制重试要求\n"
            "上一轮返回的 feasibility_error 中没有任何硬设备能力缺口证据"
            "（缺失工作站/无转移路径/容器不兼容/容量超限/站点离线/明确不可中断连续流）。\n"
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
            "4. 只有确认存在硬设备能力缺口时才保留 feasibility_error，并在 blocking_constraints "
            "中写明缺失的工作站/转移路径/容量等硬证据；无法证明满足但也无硬缺口的条件，"
            "映射后在 temporal_adaptations 或 device_layer_adaptations 中标记 requires_scientific_review=true。"
        )
        try:
            retry_result = self._invoke_feasibility_plan(state, extra_instruction=instruction)
        except Exception as exc:  # pragma: no cover - remote model dependent
            state.add_log(
                f"adaptation retry failed; retaining original feedback: {exc}"
            )
            return result
        if isinstance(retry_result, dict):
            return retry_result
        return result

    def _repair_plan_level_findings(
        self,
        state: SingleDeviceAgentState,
        plan_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """One stage-1 re-invoke when the PLAN itself carries audit findings
        (weighing handoffs / manual material ops live in the plan's
        offline_handoffs — translation cannot fix them)."""
        if str(plan_result.get("status", "")).strip().lower() not in {
            "device_plan", "success",
        }:
            return plan_result
        plan_view = {
            "offline_handoffs": plan_result.get("offline_handoffs", []),
            "steps": plan_result.get("device_plan", []),
        }
        findings = audit_offline_handoffs(plan_view)
        findings += scan_manual_material_operations(plan_view, "")
        if not findings:
            return plan_result
        state.add_log(
            f"plan-level capability audit flagged {len(findings)} finding(s); "
            "re-invoking feasibility-plan stage once"
        )
        instruction = (
            "## 计划级能力审计未通过\n"
            + "\n".join(f"- {finding['message']}" for finding in findings[:6])
            + "\n\n" + SOLID_WEIGHING_ROUTE_NOTE
            + "\n请把上述被标记的人工/离线内容改写为对应工作站的计划步骤"
            "（disallowed 的人工物料操作不得保留），重新输出完整 JSON。"
        )
        try:
            retried = self._invoke_feasibility_plan(state, extra_instruction=instruction)
        except Exception as exc:  # pragma: no cover - remote model dependent
            state.add_log(f"plan-level repair call failed: {exc}")
            return plan_result
        if isinstance(retried, dict) and str(retried.get("status", "")).strip().lower() in {
            "device_plan", "success", "feasibility_error",
        }:
            return retried
        return plan_result

    def _translation_chunk_size(self) -> int:
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

    def _translate_plan_in_chunks(
        self,
        state: SingleDeviceAgentState,
        plan_result: Dict[str, Any],
        chunk_cache: Dict[int, Dict[str, Any]],
        *,
        only_chunks: Optional[Set[int]] = None,
        feedback_by_chunk: Optional[Dict[int, str]] = None,
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

        carryover: Dict[str, Any] = {}
        assembled_steps: List[Dict[str, Any]] = []
        txt_fragments: List[str] = []
        step_to_chunk: Dict[int, int] = {}
        chunk_step_groups: List[List[Dict[str, Any]]] = [[] for _ in range(total)]

        for index, chunk_steps in enumerate(chunks):
            need = only_chunks is None or index in only_chunks
            if need or index not in chunk_cache:
                try:
                    translated = self._invoke_translation_chunk(
                        state, plan_result, chunk_steps, index, total, carryover,
                        extra_instruction=feedback_by_chunk.get(index, ""),
                    )
                    wf = translated.get("workflow_json")
                    wf = wf if isinstance(wf, dict) else {}
                    steps = [s for s in (wf.get("steps") or []) if isinstance(s, dict)]
                    chunk_cache[index] = {
                        "steps": steps,
                        "txt": str(translated.get("workflow_txt", "")),
                    }
                except Exception as exc:  # pragma: no cover - remote dependent
                    state.add_log(f"translation chunk {index + 1} failed: {exc}")
                    chunk_cache[index] = {"steps": [], "txt": ""}

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
        max_rounds = 3  # initial + two repair rounds
        chunk_cache: Dict[int, Dict[str, Any]] = {}
        report: Dict[str, Any] = {"status": "failed", "errors": [], "warnings": []}
        result: Dict[str, Any] = {}
        only_chunks: Optional[Set[int]] = None
        feedback_by_chunk: Dict[int, str] = {}

        for round_index in range(1, max_rounds + 1):
            workflow_json, workflow_txt, step_map, chunk_groups = (
                self._translate_plan_in_chunks(
                    state, plan_result, chunk_cache,
                    only_chunks=only_chunks,
                    feedback_by_chunk=feedback_by_chunk,
                )
            )
            result = self._merge_plan_and_translation(
                plan_result, {"workflow_txt": workflow_txt, "workflow_json": workflow_json}
            )
            self._apply_deterministic_completion(state, result)
            report = self._run_full_checks(result)
            result["dispatch_validation"] = report
            if report["status"] != "failed":
                if round_index > 1:
                    state.add_log(
                        f"chunked translation repair round {round_index - 1} "
                        "passed deterministic checks"
                    )
                return result

            # map failures back to chunks and re-translate only those
            structured = structure_validation_errors(
                report.get("errors", []), result.get("workflow_json")
            )
            erroring_chunks: Set[int] = set()
            for record in structured:
                step_no = record.get("step_number")
                if isinstance(step_no, int) and step_no in step_map:
                    erroring_chunks.add(step_map[step_no])
            empty_chunks = {
                idx for idx, group in enumerate(chunk_groups) if not group
            }
            erroring_chunks |= empty_chunks
            if not erroring_chunks:
                # global error with no step anchor — re-translate everything
                erroring_chunks = set(range(len(chunk_groups)))

            state.add_log(
                f"deterministic checks failed ({len(report['errors'])} errors); "
                f"round {round_index}/{max_rounds} re-translating chunks "
                f"{sorted(erroring_chunks)}"
            )
            instruction = self._build_repair_instruction(result, report)
            feedback_by_chunk = {idx: instruction for idx in erroring_chunks}
            only_chunks = erroring_chunks
            for idx in erroring_chunks:  # force re-translation of these chunks
                chunk_cache.pop(idx, None)

        if not result or not result.get("workflow_json", {}).get("steps"):
            # exhausted repairs with empty output — honest failed result that
            # the terminal package reflows to Research (issue #4).
            result = dict(plan_result)
            result["status"] = "success"
            result["workflow_txt"] = ""
            result["workflow_json"] = {}
            result["dispatch_validation"] = report if report.get("errors") else {
                "status": "failed",
                "errors": ["workflow translation 分块后仍未产出可校验的 workflow_json。"],
                "warnings": [],
            }
        return result

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
            "feasibility": plan_result.get("feasibility", {}),
            "macro_plan_summary": plan_result.get("macro_plan_summary", ""),
            "device_self_check": plan_result.get("device_self_check", {}),
            "reagent_slot_plan": plan_result.get("reagent_slot_plan", []),
            "container_plan": plan_result.get("container_plan", []),
            "device_plan": plan_result.get("device_plan", []),
            "workflow_txt": str(translated.get("workflow_txt", "")),
            "workflow_json": workflow_json,
        }
        return merged

    def _run_full_checks(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Schema validation + txt↔json consistency + capability audit, merged
        into one report so the repair loop sees every deterministic finding."""
        workflow_json = result.get("workflow_json")
        report = self._workflow_validator.validate(workflow_json)
        errors = list(report.get("errors", []))
        warnings = list(report.get("warnings", []))

        consistency = self._workflow_validator.validate_consistency(
            result.get("workflow_txt", ""), workflow_json
        )
        errors.extend(consistency.get("errors", []))
        warnings.extend(consistency.get("warnings", []))

        audit_findings = audit_offline_handoffs(workflow_json)
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
        except Exception:  # pragma: no cover - formatter must not break checks
            pass

        return {
            "status": "failed" if errors else "passed",
            "errors": errors,
            "warnings": warnings,
            "checked_steps": report.get("checked_steps", 0),
        }

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
            "不要发明聚合字段），移除臆造字段，补全必填参数，让数值落在真源允许范围内；"
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
        completed, filled_log = complete_required_fields(
            result.get("workflow_json"), self._workflow_validator
        )
        if not filled_log:
            return
        result["workflow_json"] = completed
        prior = (result.get("dispatch_completion") or {}).get("filled", [])
        result["dispatch_completion"] = {"filled": list(prior) + filled_log}
        state.add_log(
            f"deterministic completion filled {len(filled_log)} required "
            "field(s) before dispatch validation"
        )

    def _normalize_terminal_package(
        self,
        state: SingleDeviceAgentState,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        if (
            result.get("status") in {"feasibility_error", "unsupported", "not_feasible"}
            or result.get("feedback_type") == "device_feasibility_error"
        ):
            feasibility = result.get("feasibility") if isinstance(result.get("feasibility"), dict) else {}
            blocking = self._clean_list(feasibility.get("blocking_constraints", []))
            if not blocking:
                blocking = ["single device agent 判定当前 macro action 无法映射，但未返回具体阻塞原因。"]
            classification = classify_feasibility_result(result, state.research_handoff)
            overall = classification["overall"] or "unverifiable"
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
            if overall == "hard":
                error_type = "physical_infeasible"
                message = result.get("recommendation_to_research_agent", "")
                assessment_source = "single_device_agent_llm"
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
            return {
                "feedback_type": "device_feasibility_error",
                "status": "feasibility_error",
                "exp_id": state.exp_id,
                "iteration_id": state.iteration_id,
                "workflow_id": state.workflow_id,
                "device_snapshot_id": self._device_snapshot_id(),
                "macro_plan": state.research_handoff,
                "macro_plan_summary": result.get("macro_plan_summary", ""),
                "macro_action": macro_action,
                "device_capabilities": result.get("device_capability_summary", {}),
                "feasibility_assessment": result,
                "requires_scientific_review": requires_review,
                "error_package": {
                    "type": error_type,
                    "device_snapshot_id": self._device_snapshot_id(),
                    "macro_action_id": macro_action.get("macro_action_id", ""),
                    "observation_point_id": macro_action.get("observation_point_id", ""),
                    "observation_point": macro_action.get("observation_point", ""),
                    "constraint_classification": {
                        "hard": classification["hard"],
                        "adaptable": classification["adaptable"],
                        "unverifiable": classification["unverifiable"],
                    },
                    "blocking_constraints": blocking,
                    "message": message,
                    "last_workflow_txt": result.get("workflow_txt", ""),
                    "unsupported_items": feasibility.get("unsupported_items", []),
                    "assessment_source": assessment_source,
                },
            }

        workflow_txt = str(result.get("workflow_txt", "")).strip()
        workflow_json = result.get("workflow_json")
        self_check = result.get("device_self_check", {})
        dispatch_validation = result.get("dispatch_validation")
        if not isinstance(dispatch_validation, dict):
            dispatch_validation = self._workflow_validator.validate(workflow_json)
        # A pre-computed failed report (incl. empty/truncated translation) must
        # take the structured-reflow path below, NOT the hard-raise guards —
        # otherwise an exhausted chunked translation would crash instead of
        # reflowing to Research (issue #4).
        if dispatch_validation.get("status") != "failed":
            if not workflow_txt:
                raise ValueError("single device agent success output missing workflow_txt")
            if not isinstance(workflow_json, dict) or not isinstance(workflow_json.get("steps"), list):
                raise ValueError("single device agent success output missing workflow_json.steps")
            self._raise_if_self_check_failed(self_check)

        if not isinstance(workflow_json, dict):
            workflow_json = {}
        if dispatch_validation.get("status") == "failed":
            # Never let an unvalidated dispatch payload leave as success.
            # Issue #4: the exhausted-repair failure flows BACK to Research as
            # structured feedback (workflow_translation_failed) through the
            # same channel as feasibility errors — the orchestrator's
            # feasibility branch, deadlock counting, constraint accumulation
            # and research B2 routing all pick it up unchanged.
            raw_errors = dispatch_validation.get("errors", []) or []
            structured = structure_validation_errors(raw_errors, workflow_json)
            macro_action_view = state.research_handoff.get("macro_action")
            macro_action_view = (
                macro_action_view if isinstance(macro_action_view, dict) else {}
            )
            blocking = [str(err) for err in raw_errors[:8]]
            return {
                "status": "failed",
                "feedback_type": "device_feasibility_error",
                "failure_stage": "dispatch_validation",
                "exp_id": state.exp_id,
                "iteration_id": state.iteration_id,
                "workflow_id": state.workflow_id,
                "device_snapshot_id": self._device_snapshot_id(),
                "macro_plan": state.research_handoff,
                "macro_plan_summary": result.get("macro_plan_summary", ""),
                "dispatch_validation": dispatch_validation,
                "capability_audit": result.get(
                    "capability_audit", {"status": "clean", "findings": []}
                ),
                "device_plan": result.get("device_plan", []),
                "error_package": {
                    "type": "workflow_translation_failed",
                    "assessment_source": "deterministic_workstation_validator",
                    "blocking_constraints": blocking,
                    "structured_errors": structured,
                    "device_snapshot_id": self._device_snapshot_id(),
                    "macro_action_id": str(macro_action_view.get("macro_action_id", "")),
                    "observation_point_id": str(
                        macro_action_view.get("observation_point_id", "")
                    ),
                    "failed_plan_signature": self._plan_signature(state.research_handoff),
                    "message": (
                        "workflow 翻译未能通过确定性工作站校验（含有界修复轮）。"
                        "阻塞多为参数字段/结构/范围问题；research 层可考虑简化步骤结构、"
                        "缩小单步参数复杂度或拆分 macro action 后重试。"
                    ),
                },
                "message": (
                    "workflow_json 未通过严格下发参数校验（含自我修复重试），"
                    "该 workflow 不得下发执行。"
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
        except Exception as exc:  # pragma: no cover - must not break success
            dispatch = {
                "payload": {},
                "warnings": [f"dispatch formatting failed: {exc}"],
                "mapped_steps": 0,
                "unmapped_steps": 0,
            }

        return {
            "status": "success",
            "exp_id": state.exp_id,
            "iteration_id": state.iteration_id,
            "workflow_id": state.workflow_id,
            "device_snapshot_id": self._device_snapshot_id(),
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
            "temporal_adaptations": temporal_adaptations,
            "workflow_txt": workflow_txt,
            "workflow_json": workflow_json,
            "agent_mode": "single_device_agent",
        }

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
            if number is not None and ids:
                step_id_map[number] = ids

        default_ids = {}
        if macro_action.get("macro_action_id"):
            default_ids["macro_action_id"] = macro_action["macro_action_id"]
        if macro_action.get("observation_point_id"):
            default_ids["observation_point_id"] = macro_action["observation_point_id"]

        try:
            for step in workflow_json.get("steps", []) or []:
                if not isinstance(step, dict):
                    continue
                source = step.get("source_macro_step")
                ids = step_id_map.get(source, default_ids)
                for key, value in ids.items():
                    step.setdefault(key, value)
        except Exception:  # pragma: no cover - stamping must not break success
            pass
        return macro_action

    def _parse_json(self, text: str) -> Any:
        stripped = text.strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

        matches = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", stripped)
        for candidate in reversed(matches):
            try:
                return json.loads(candidate.strip())
            except json.JSONDecodeError:
                continue

        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            return json.loads(stripped[start : end + 1])
        raise ValueError("could not parse JSON from single device agent response")

    def _coerce_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or item))
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
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"single_device_{timestamp}"
