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
from workflow_validator import WorkflowValidator

logger = logging.getLogger(__name__)


def _needs_temporal_mapping_retry(result: Dict[str, Any], handoff: Dict[str, Any]) -> bool:
    """Success result whose self-check still complains about addition timing."""
    if str(result.get("status", "")).strip().lower() != "success":
        return False
    if not _has_temporal_addition_stirring(handoff):
        return False
    self_check = result.get("device_self_check", {})
    return bool(
        isinstance(self_check, dict)
        and re.search(r"fail|失败|不通过|未通过", _json_text(self_check))
        and re.search(r"addition|加液|滴加|搅拌|节拍|同步", _json_text(self_check))
    )


SINGLE_DEVICE_SYSTEM_PROMPT = """
你是化学自动化平台的 single device agent。
你只做一件事：把 research agent 输出的化学语义 macro action 映射成当前设备可执行的 workstation workflow。

职责边界：
1. 输入包含 query、stage、macro_action_steps、设备/器材真源、TXT/JSON 格式参考。
2. research agent 已经根据设备和器材信息尽量规划可做的化学路线，但它仍只输出 macro action，不负责工作站细节。
3. 你负责选择具体工作站、容器类型、容器编号、原液编号、开盖/关盖、分瓶/配平、重复洗涤、干燥和离线 handoff。
4. 不再调用 pre-flow agent、workflow generator、verify agent、format translate agent；你一次性完成可行性判断和 workflow 生成。
5. 只有当当前设备真源无法实现某个必要化学动作或强制科学条件时，才返回 device_feasibility_error。
   先把 macro action 的每个实验参数分类，再决定它是不是阻塞：
   - device_dispatch_field（设备下发字段）：真源参数表中存在同义字段，直接映射并严格取值；
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
   (3) 设备开放参数——Skill 参数表列出的字段，只有这一类需要你填写并严格满足取值范围。
6. 不要因为 macro action 缺少容器编号、工作站名、原液瓶位、开盖/关盖、偶数进样瓶配平、重复洗涤子步骤而拒绝；这些都由你补全。
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
9. 所有设备动作必须能从给定工作站 USAGE/AUDIT-RULES 找到依据；不要臆造不存在的工作站或参数。
10. 维护容器身份台账：同一个容器编号在整个 workflow 中必须保持同一种容器类型，除非 workflow 中显式存在受支持的转移/换瓶操作；不能只在 notes 中声称容器仍是另一种类型。
11. 维护体积台账：每次加液、洗涤、去上清、保留清洗液、干燥前后都要避免让任一容器的液体体积超过当前工作站/容器真源限制。若宏观体积过大，优先等比例缩小体系或拆分到多个等价样品容器，并在 device_layer_adaptations 中说明；不能静默超限。
12. 离心/纯化需要配平时，进入同一次离心的容器数量、容器类型和液体体积必须可配平。若使用配平瓶，配平瓶体积应与主样瓶接近；若无法配平，返回 device_feasibility_error。
13. 保留化学上有意义的加料方式：若 macro action 明确要求滴加、缓慢加入、分批加入或给出加入速率，必须映射成设备支持的分批/多次加液或在 workflow 中记录可执行的近似节拍；不要擅自改成一次性加入，除非 macro action 明确允许。
    “边滴入边搅拌/边搅拌边滴加/同步搅拌加液”默认是可适配的时间语义：若液体进样站和磁力搅拌站支持同一容器，
    应生成“预搅拌 -> 小份加液 -> 固定时间搅拌 -> 重复 -> 最终搅拌”的交替节拍，并在 adaptations 中标明
    `execution_fidelity=approximated`、原始要求和分批计划；不得仅因没有单站原子化并行动作就返回 feasibility_error。
    只有明确要求不可中断的连续流/恒定流速/微流控进料，或不存在共享容器和合法转移路径时，才可返回 feasibility_error。
14. 如果某个 macro action 会造成“反应/静置/暂存所需容器”与“后续离心/洗涤/干燥/测试所需容器”之间没有受支持的连续路径，不能通过更改容器名称、notes 解释或跳过转移来伪装可执行；必须返回 device_feasibility_error，并在 suggested_research_revision 中说明需要 research layer 改成可连续容器路径的化学语义路线。
15. 成功输出前必须做一次内部设备自检：工作站/操作/参数受真源支持，容器类型连续，开盖/关盖状态合理，累计体积不超限，离心配平成立，workflow_txt 与 workflow_json 一致。自检不通过时先修复，无法修复时返回 device_feasibility_error。
16. 输出必须是一个 JSON object，不要 Markdown，不要代码块。
""".strip()


SINGLE_DEVICE_TASK_PROMPT = """
请把 research handoff 映射为当前设备可执行的 workflow。

## research handoff
{research_handoff_json}

## 当前设备/器材真源
下面是每个工作站的 USAGE.md 和 AUDIT-RULES.md 摘要/全文。只能使用这里出现的工作站、容器、操作和参数。
{workstation_descriptions}

## TXT workflow 格式参考
{txt_format_reference}

## JSON workflow 格式参考
{json_format_reference}

## 输出 JSON 格式
必须返回一个 JSON object，二选一：

### 情况 A：可以映射
{{
  "status": "success",
  "feasibility": {{
    "is_feasible": true,
    "blocking_constraints": [],
    "device_layer_adaptations": ["你补全了哪些设备层映射"]
  }},
  "macro_plan_summary": "一句话说明从 macro action 到设备 workflow 的映射策略",
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
  "workflow_txt": "严格仿照 TXT workflow 格式参考的工作流文本",
  "workflow_json": {{
    "steps": [
      {{
        "step_number": 1,
        "workstation": "工作站名称",
        "operation": "操作名称",
        "parameters": {{}},
        "source_macro_step": 1,
        "notes": "可选说明"
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

生成要求：
- workflow_txt 必须是工作站步骤，不是 macro action 复述。
- workflow_json.steps 必须和 workflow_txt 表达同一套步骤。
- 每个设备步骤尽量标注 source_macro_step。
- 涉及洗涤时，展开成固定次数或工作站支持的清洗次数，不写“洗涤至”。
- 涉及干燥时，使用固定温度和固定时间，不写“干燥至”。
- 离线 observation 可以放在 workflow_json.offline_handoffs，也可以在 workflow_txt 末尾写成“离线 handoff”，但不得伪造成设备内工作站。
- 如果无法确定某个原液编号，自己分配编号并在 reagent_slot_plan 中说明。
- 如果需要偶数容器配平，自己选择偶数容器并贯穿后续步骤。
- 如果 macro action 含“边滴入边搅拌/边搅拌边滴加”等时间语义，优先在同一容器上交替调用液体进样站和磁力搅拌站；
  `workflow_json.steps` 必须显式展开至少两批加液与批次间搅拌（或记录等价固定节拍），并在 `workflow_json.temporal_adaptations`
  中保留 `original_requirement`、`execution_fidelity`、`adaptation_schedule` 和 `requires_scientific_review`。
- 对每个容器编号建立隐式台账：容器类型、当前体积、是否带盖、当前样品用途。后续步骤使用该容器编号时必须与台账一致；如需换容器，必须加入真源支持的转移/重新取样步骤，否则不能换。
- 对每个加液步骤检查单次加液量、累计体积和后续工作站体积限制。若宏观计划体积超过设备真源或接近不可执行边界，可以等比例缩小所有相关试剂体积以保持摩尔比/浓度关系，或拆分为多个并行样品瓶；必须在 device_layer_adaptations 中说明缩放或拆分理由。
- 若进入离心/纯化工作站的样品需要配平，所有参与该次离心的容器应使用同一种容器类型，并具有接近的液体体积；不能只用低体积配平瓶去配高体积主样瓶。
- 若 macro action 包含滴加、缓慢加入、分批加入、加入速率或时间依赖混合，workflow 必须体现为多次加液、分批加液或明确的可执行节拍。若设备真源不支持连续滴加，使用保守分批近似并说明；只有分批近似也不可行或原始要求明确不可中断时，才返回 device_feasibility_error。
- 不要把“建议”“范围”“上限”当作可以贴边或超过的默认值；优先选择留有余量的参数。若必须贴近边界，必须在 feasibility/device_layer_adaptations 中解释为什么仍可执行。
- 成功输出必须包含 device_self_check，且其中任一项为 fail 时不得返回 success；应先修改 workflow，若无法修改则返回 device_feasibility_error。
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
            result = self._invoke_mapping(state)
            result = self._retry_adaptable_feedback(state, result, research_handoff)
            result = self._repair_validation_failures(state, result)
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

    def _invoke_mapping(
        self,
        state: SingleDeviceAgentState,
        *,
        extra_instruction: str = "",
    ) -> Dict[str, Any]:
        prompt = SINGLE_DEVICE_TASK_PROMPT
        replacements = {
            "{research_handoff_json}": json.dumps(
                state.research_handoff,
                ensure_ascii=False,
                indent=2,
            ),
            "{workstation_descriptions}": state.workstation_descriptions,
            "{txt_format_reference}": state.txt_format_reference,
            "{json_format_reference}": state.json_format_reference,
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
            SystemMessage(content=SINGLE_DEVICE_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]
        print("[single-device-agent] LLM step start: map_macro_to_workflow", flush=True)
        response = self._model.invoke(messages)
        print("[single-device-agent] LLM step done: map_macro_to_workflow", flush=True)
        content = self._coerce_text(getattr(response, "content", response))
        result = self._parse_json(content)
        if not isinstance(result, dict):
            raise ValueError("single device LLM did not return a JSON object")
        return result

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
            + "\n请重新检查真源并按下列优先级处理每一条原因，然后输出 success：\n"
            "1. 若是过程/时间语义（如边滴入边搅拌、缓慢滴加、分批），在同一兼容容器上派生为"
            "受支持步骤的组合（分批加液、批次间固定转速搅拌等），保持总量、顺序和近似总时长，"
            "并在 workflow_json.temporal_adaptations 中写明 original_requirement、"
            "execution_fidelity=approximated、adaptation_schedule 和 requires_scientific_review=true。\n"
            "2. 若是设备固有能力（如仪器固定辐射源、常温环境），在 feasibility."
            "device_layer_adaptations 中说明依据后按固定能力处理，不作为阻塞。\n"
            "3. 若为观测数据回传（送样测试后返回图谱/判读）或真源确实无容器路径的操作"
            "（如向 96位石英孔板 定量装粉），写入 workflow_json.offline_handoffs；"
            "固体称量/分装必须映射到固体转移+称量工作站，禁止写成人工步骤。\n"
            "4. 只有确认存在硬设备能力缺口时才保留 feasibility_error，并在 blocking_constraints "
            "中写明缺失的工作站/转移路径/容量等硬证据；无法证明满足但也无硬缺口的条件，"
            "映射后在 temporal_adaptations 或 device_layer_adaptations 中标记 requires_scientific_review=true。"
        )
        try:
            retry_result = self._invoke_mapping(state, extra_instruction=instruction)
        except Exception as exc:  # pragma: no cover - remote model dependent
            state.add_log(
                f"adaptation retry failed; retaining original feedback: {exc}"
            )
            return result
        if isinstance(retry_result, dict):
            return retry_result
        return result

    def _repair_validation_failures(
        self,
        state: SingleDeviceAgentState,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """One self-repair round when strict dispatch validation fails."""
        if str(result.get("status", "")).strip().lower() != "success":
            return result

        # Deterministic pre-validation completion: fill mechanically-derivable
        # required fields (容器数量=len(容器编号), 开盖/关盖编号, SKILL 默认值 like
        # 保留瓶盖=1) so the Device layer never fails validation — or burns an
        # LLM self-repair round — on omissions the harness can fill itself.
        # Never overwrites LLM-written values; never synthesizes structure.
        self._apply_deterministic_completion(state, result)

        report = self._run_full_checks(result)
        if report["status"] != "failed":
            result.setdefault("dispatch_validation", report)
            return result

        # Bounded self-repair: up to TWO repair rounds (issue #12 — repair →
        # re-validate → repair → re-validate; still failing → failed package).
        max_repair_rounds = 2
        for repair_round in range(1, max_repair_rounds + 1):
            state.add_log(
                f"deterministic checks failed ({len(report['errors'])} errors); "
                f"self-repair round {repair_round}/{max_repair_rounds}"
            )
            instruction = self._build_repair_instruction(result, report)
            try:
                repaired = self._invoke_mapping(state, extra_instruction=instruction)
            except Exception as exc:  # pragma: no cover - remote model dependent
                state.add_log(f"validation repair call failed: {exc}")
                break
            if not (
                isinstance(repaired, dict)
                and str(repaired.get("status", "")).strip().lower() == "success"
            ):
                break
            # the repaired output also gets deterministic completion before its
            # validation — otherwise a repair round can reintroduce mechanical
            # omissions (e.g. 保留瓶盖) that the harness can fill itself.
            self._apply_deterministic_completion(state, repaired)
            second_report = self._run_full_checks(repaired)
            repaired["dispatch_validation"] = second_report
            result = repaired
            report = second_report
            if report["status"] != "failed":
                state.add_log(
                    f"self-repair round {repair_round} passed deterministic checks"
                )
                return result
        result["dispatch_validation"] = report
        return result

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
        if not workflow_txt:
            raise ValueError("single device agent success output missing workflow_txt")
        if not isinstance(workflow_json, dict) or not isinstance(workflow_json.get("steps"), list):
            raise ValueError("single device agent success output missing workflow_json.steps")
        self_check = result.get("device_self_check", {})
        self._raise_if_self_check_failed(self_check)

        dispatch_validation = result.get("dispatch_validation")
        if not isinstance(dispatch_validation, dict):
            dispatch_validation = self._workflow_validator.validate(workflow_json)
        if dispatch_validation.get("status") == "failed":
            # Never let an unvalidated dispatch payload leave as success —
            # downgrade with the full validation report attached.
            return {
                "status": "failed",
                "feedback_type": "device_internal_error",
                "failure_stage": "dispatch_validation",
                "exp_id": state.exp_id,
                "iteration_id": state.iteration_id,
                "workflow_id": state.workflow_id,
                "macro_plan": state.research_handoff,
                "macro_plan_summary": result.get("macro_plan_summary", ""),
                "dispatch_validation": dispatch_validation,
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
