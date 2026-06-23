"""Single-agent device mapper.

This is the simplified device layer used after research-agent planning:
research macro actions + device truth -> workstation workflow.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from utils.paths import format_reference_path
from utils.workstation_loader import WorkstationLoader

logger = logging.getLogger(__name__)


SINGLE_DEVICE_SYSTEM_PROMPT = """
你是化学自动化平台的 single device agent。
你只做一件事：把 research agent 输出的化学语义 macro action 映射成当前设备可执行的 workstation workflow。

职责边界：
1. 输入包含 query、stage、macro_action_steps、设备/器材真源、TXT/JSON 格式参考。
2. research agent 已经根据设备和器材信息尽量规划可做的化学路线，但它仍只输出 macro action，不负责工作站细节。
3. 你负责选择具体工作站、容器类型、容器编号、原液编号、开盖/关盖、分瓶/配平、重复洗涤、干燥和离线 handoff。
4. 不再调用 pre-flow agent、workflow generator、verify agent、format translate agent；你一次性完成可行性判断和 workflow 生成。
5. 只有当当前设备真源无法实现某个必要化学动作或强制科学条件时，才返回 device_feasibility_error。
6. 不要因为 macro action 缺少容器编号、工作站名、原液瓶位、开盖/关盖、偶数进样瓶配平、重复洗涤子步骤而拒绝；这些都由你补全。
7. 如果 macro action 明确把 XRD/PXRD/SEM/TEM/Raman/XAS 等写成离线 observation/handoff/数据回传，保留为 handoff note，不作为当前设备不可执行原因。
8. 不要改变研究目标、目标材料、当前 stage 或目标 observation point。
9. 所有设备动作必须能从给定工作站 USAGE/AUDIT-RULES 找到依据；不要臆造不存在的工作站或参数。
10. 维护容器身份台账：同一个容器编号在整个 workflow 中必须保持同一种容器类型，除非 workflow 中显式存在受支持的转移/换瓶操作；不能只在 notes 中声称容器仍是另一种类型。
11. 维护体积台账：每次加液、洗涤、去上清、保留清洗液、干燥前后都要避免让任一容器的液体体积超过当前工作站/容器真源限制。若宏观体积过大，优先等比例缩小体系或拆分到多个等价样品容器，并在 device_layer_adaptations 中说明；不能静默超限。
12. 离心/纯化需要配平时，进入同一次离心的容器数量、容器类型和液体体积必须可配平。若使用配平瓶，配平瓶体积应与主样瓶接近；若无法配平，返回 device_feasibility_error。
13. 保留化学上有意义的加料方式：若 macro action 明确要求滴加、缓慢加入、分批加入或给出加入速率，必须映射成设备支持的分批/多次加液或在 workflow 中记录可执行的近似节拍；不要擅自改成一次性加入，除非 macro action 明确允许。
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
        "reason": "为什么真源不支持",
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
- 对每个容器编号建立隐式台账：容器类型、当前体积、是否带盖、当前样品用途。后续步骤使用该容器编号时必须与台账一致；如需换容器，必须加入真源支持的转移/重新取样步骤，否则不能换。
- 对每个加液步骤检查单次加液量、累计体积和后续工作站体积限制。若宏观计划体积超过设备真源或接近不可执行边界，可以等比例缩小所有相关试剂体积以保持摩尔比/浓度关系，或拆分为多个并行样品瓶；必须在 device_layer_adaptations 中说明缩放或拆分理由。
- 若进入离心/纯化工作站的样品需要配平，所有参与该次离心的容器应使用同一种容器类型，并具有接近的液体体积；不能只用低体积配平瓶去配高体积主样瓶。
- 若 macro action 包含滴加、缓慢加入、分批加入、加入速率或时间依赖混合，workflow 必须体现为多次加液、分批加液或明确的可执行节拍。若设备真源不支持连续滴加，使用保守分批近似并说明；若分批近似也不可行，返回 device_feasibility_error。
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
    ) -> None:
        self._model = model
        self._workstation_loader = workstation_loader or WorkstationLoader(
            use_new_format=use_new_format
        )
        self._txt_format_reference = self._read_text(format_reference_path("txt"))
        self._json_format_reference = self._read_text(format_reference_path("json"))

    def run_state(
        self,
        research_handoff: Dict[str, Any],
        *,
        exp_id: Optional[str] = None,
        iteration_id: int = 0,
        workflow_id: int = 0,
    ) -> SingleDeviceAgentState:
        exp_id = exp_id or self._default_exp_id()
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
            state.raw_llm_output = result
            package = self._normalize_terminal_package(state, result)
            state.terminal_package = package
            state.status = "feasibility_error" if package.get("status") == "feasibility_error" else "completed"
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

    def _invoke_mapping(self, state: SingleDeviceAgentState) -> Dict[str, Any]:
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

    def _normalize_terminal_package(
        self,
        state: SingleDeviceAgentState,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        if result.get("status") == "feasibility_error" or result.get("feedback_type") == "device_feasibility_error":
            feasibility = result.get("feasibility") if isinstance(result.get("feasibility"), dict) else {}
            blocking = self._clean_list(feasibility.get("blocking_constraints", []))
            if not blocking:
                blocking = ["single device agent 判定当前 macro action 无法映射，但未返回具体阻塞原因。"]
            return {
                "feedback_type": "device_feasibility_error",
                "status": "feasibility_error",
                "exp_id": state.exp_id,
                "iteration_id": state.iteration_id,
                "workflow_id": state.workflow_id,
                "macro_plan": state.research_handoff,
                "macro_plan_summary": result.get("macro_plan_summary", ""),
                "device_capabilities": result.get("device_capability_summary", {}),
                "feasibility_assessment": result,
                "error_package": {
                    "type": "physical_infeasible",
                    "blocking_constraints": blocking,
                    "message": result.get("recommendation_to_research_agent", ""),
                    "last_workflow_txt": result.get("workflow_txt", ""),
                    "unsupported_items": feasibility.get("unsupported_items", []),
                    "assessment_source": "single_device_agent_llm",
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

        return {
            "status": "success",
            "exp_id": state.exp_id,
            "iteration_id": state.iteration_id,
            "workflow_id": state.workflow_id,
            "macro_plan": state.research_handoff,
            "macro_plan_summary": result.get("macro_plan_summary", ""),
            "verification_summary": {
                "result": "accepted",
                "category": "",
                "blocking_constraints": [],
                "message": "single device agent mapped macro action to workstation workflow",
            },
            "feasibility": result.get("feasibility", {}),
            "device_self_check": self_check,
            "reagent_slot_plan": result.get("reagent_slot_plan", []),
            "container_plan": result.get("container_plan", []),
            "workflow_txt": workflow_txt,
            "workflow_json": workflow_json,
            "agent_mode": "single_device_agent",
        }

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
