"""
Top-level main workflow orchestration.
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from utils.llm_factory import LLMFactory
from utils.log_manager import LogManager
from utils.paths import format_reference_path
from utils.workstation_loader import WorkstationLoader
from state import WorkflowState

from pre_flow_agent.workflow import PreFlowAgent
from workflow_generator.workflow import WorkflowGenerator
from verify_agent.workflow import VerifyAgent
from format_translate_agent.workflow import FormatTranslateAgent

logger = logging.getLogger(__name__)


DEVICE_FEASIBILITY_SYSTEM_PROMPT = """
你是化学自动化实验平台的设备适应层审查器。
你的任务是根据当前设备/工作站描述，判断上游 research agent 给出的 macro_plan 是否能被当前设备层执行。

判断原则：
1. 只依据给定的设备描述、工作站能力、容器类型、参数范围、审查规则进行判断。
2. 不要因为化学路线本身可能不优而拒绝；只判断设备层是否能执行。
3. 如果 macro_plan 中某个步骤需要设备描述中不存在的容器、工作站、动作、传感器、在线表征、温压条件或人工判断闭环，应判定为不可执行。
4. 如果某个宏观实验目标可以通过设备支持的替代动作完成，但 macro_plan 当前写法仍包含不支持动作，应判定当前 macro_plan 不可执行，并说明需要上游重新规划。
5. 如果 macro_plan 明确把 XRD/PXRD/FTIR/SEM 等表征写成“离线 observation / 外部送样 / handoff / 数据回传”，并明确不由当前设备层工作站执行，不要因为该离线交接步骤拒绝整段设备内流程；只需在 supported_parts 或 recommendation 中标注这是外部 handoff。
6. 如果表征被写成当前设备内执行、XRD 工作站执行、自动采集或在线判读，而设备描述没有对应能力，则必须判定不可执行。
7. 只能依据设备描述中明确写出的容器状态/盖状态限制来拒绝；不要推断“盖子保留在某平台”“开盖容器不能转移”“烘干机不接受开盖进样瓶”等设备描述未明确写出的物流限制。
8. 如果某个后续工作站的描述没有声明盖状态限制，只要容器类型、参数范围和前序必要开盖/关盖动作可满足，就不要仅因盖状态不明而拒绝。
9. 输出必须是一个 JSON object，不要输出 Markdown、解释段落或代码块。
""".strip()


DEVICE_FEASIBILITY_TASK_PROMPT = """
请判断下面的 macro_plan 是否能由当前设备层完成。

## macro_plan
{macro_plan}

## 当前设备/工作站描述
{workstation_descriptions}

## 输出 JSON 格式
必须返回一个 JSON object，字段如下：
{{
  "is_feasible": true 或 false,
  "status": "supported" 或 "unsupported",
  "blocking_constraints": [
    "如果不可执行，逐条说明当前 macro_plan 与设备能力不匹配的硬约束；如果可执行则为空数组"
  ],
  "unsupported_items": [
    {{
      "macro_step": "对应的 macro action/步骤",
      "requirement": "该步骤要求的容器、设备、动作、参数或传感能力",
      "reason": "为什么当前设备描述不支持",
      "missing_device_capability": "缺失的具体设备能力",
      "suggested_research_revision": "建议上游 research agent 如何改写该 macro action"
    }}
  ],
  "supported_parts": [
    "当前 macro_plan 中设备层可以执行的部分"
  ],
  "device_capability_summary": {{
    "supported_containers": ["从设备描述中抽取出的支持容器"],
    "supported_workstations": ["从设备描述中抽取出的支持工作站"],
    "not_supported": ["macro_plan 要求但设备描述中没有的能力"]
  }},
  "recommendation_to_research_agent": "给上游 research agent 的一句或几句改写建议"
}}

要求：
- 如果判断不可执行，blocking_constraints 不能为空。
- 不要臆造设备描述中没有的能力。
- 不要自己生成新的完整实验路线；这里只做设备层可行性判断。
- 明确标注为“离线 observation/handoff/外部送样/数据回传，且不由当前设备层执行”的表征步骤，不应作为设备层不可执行的 blocking constraint。
- 如果离线表征前的样品制备、洗涤、开盖、干燥等设备内步骤不可执行，则仍应拒绝并说明这些设备内步骤的问题。
- 对容器盖状态的判断要保守：只有当设备描述明确要求有盖/无盖，而 macro_plan 没有满足时，才把它列为 blocking constraint。
""".strip()


class MainWorkflow:
    TXT_FORMAT_REFERENCE_PATH = str(format_reference_path("txt"))
    JSON_FORMAT_REFERENCE_PATH = str(format_reference_path("json"))

    def __init__(
        self,
        model=None,
        use_temp_data_flow: bool = True,
        max_verify_retries: int = 3,
        use_new_format: bool = True,
    ):
        if model is None:
            model = LLMFactory.create()

        self._model = model
        self._use_temp_data_flow = use_temp_data_flow
        self._max_verify_retries = max_verify_retries
        self._use_new_format = use_new_format

        self._pre_flow_agent = PreFlowAgent(model=model, use_new_format=use_new_format)
        self._workflow_generator = WorkflowGenerator(model=model, use_new_format=use_new_format)
        self._verify_agent = VerifyAgent(model=model, use_new_format=use_new_format)
        self._format_translate_agent = FormatTranslateAgent(model=model, use_new_format=use_new_format)
        self._workstation_loader = WorkstationLoader(use_new_format=use_new_format)
        self._txt_format_reference = self._load_txt_format_reference()
        self._json_format_reference = self._load_json_format_reference()

    def _load_txt_format_reference(self) -> str:
        if not os.path.exists(self.TXT_FORMAT_REFERENCE_PATH):
            return ""
        with open(self.TXT_FORMAT_REFERENCE_PATH, "r", encoding="utf-8") as f:
            return f.read()

    def _load_json_format_reference(self) -> str:
        if not os.path.exists(self.JSON_FORMAT_REFERENCE_PATH):
            return ""
        with open(self.JSON_FORMAT_REFERENCE_PATH, "r", encoding="utf-8") as f:
            return f.read()

    def run(self, macro_plan: str) -> Dict[str, Any]:
        state = self.run_state(macro_plan)
        return state.terminal_package or self._build_success_package(state)

    def run_state(self, macro_plan: str) -> WorkflowState:
        state: Optional[WorkflowState] = None
        try:
            state = self._step1_create_log_and_get_inputs(macro_plan)
            state = self._step_pre_feasibility_gate(state)
            if state.status == "feasibility_error":
                state.update_stage("completed")
                return state

            state = self._step2_pre_flow_agent(state)
            state = self._step3_workflow_generator(state)
            state = self._step4_verify_agent_with_retry(state)

            if state.verification_result == "refused" and state.verification_category == "physical_infeasible":
                state.terminal_package = self._build_feasibility_error_package(state)
                state.status = "feasibility_error"
                state.update_stage("completed")
                return state

            if state.verification_result != "accepted":
                state.terminal_package = self._build_verification_refused_package(state)
                state.status = "verification_refused"
                state.update_stage("completed")
                return state

            state = self._step6_format_translate_agent(state)
            self._validate_translated_workflow(state)
            state.terminal_package = self._build_success_package(state)
            state.status = "completed"
            state.update_stage("completed")
            return state
        except Exception as exc:
            if state is not None:
                state.add_error(str(exc))
            logger.exception("Main Workflow failed: %s", exc)
            raise

    def _step1_create_log_and_get_inputs(self, macro_plan: str) -> WorkflowState:
        log_manager = LogManager()
        exp_id = log_manager.exp_id
        exp_log_path = os.path.join(log_manager.exp_dir, "exp_log.json")
        log_manager.set_macro_plan(macro_plan)
        log_manager.ensure_iteration(0)

        workstation_descriptions = self._workstation_loader.get_all()
        state = WorkflowState(
            final_goal=macro_plan,
            macro_plan=macro_plan,
            exp_id=exp_id,
            exp_log_path=exp_log_path,
            iteration_id=0,
            workflow_id=0,
            workstation_descriptions=workstation_descriptions,
            txt_format_reference=self._txt_format_reference,
            json_format_reference=self._json_format_reference,
            use_temp_data_flow=self._use_temp_data_flow,
        )
        state.update_stage("init")
        state.add_log(f"Experiment ID: {exp_id}")
        state.add_log(f"Loaded {len(workstation_descriptions)} workstations")
        return state

    def _step_pre_feasibility_gate(self, state: WorkflowState) -> WorkflowState:
        """Ask the device layer LLM to reject macro plans absent from workstation truth."""
        state.update_stage("pre_feasibility")
        feasibility_report = self._evaluate_macro_plan_device_feasibility(state)
        state.pre_feasibility_report = feasibility_report
        state.pre_feasibility_source = str(feasibility_report.get("source", ""))

        if feasibility_report.get("is_feasible"):
            state.add_log(
                "Pre-feasibility gate passed by "
                + str(feasibility_report.get("source", "unknown"))
            )
            return state

        blocking_constraints = self._blocking_constraints_from_feasibility_report(feasibility_report)
        if not blocking_constraints:
            blocking_constraints = ["设备适应层判定当前 macro_plan 不可执行，但未返回具体阻塞原因。"]

        state.verification_result = "refused"
        state.verification_category = "physical_infeasible"
        state.blocking_constraints = blocking_constraints
        state.verification_suggestion = str(
            feasibility_report.get("recommendation_to_research_agent")
            or feasibility_report.get("suggestion")
            or (
                "当前 macro_plan 包含设备描述中不存在或未支持的能力。"
                "请上游 research agent 改写 macro_plan，使其只使用当前设备支持的容器、工作站和动作。"
            )
        )
        state.workflow_txt = ""
        state.terminal_package = self._build_feasibility_error_package(state)
        state.status = "feasibility_error"
        state.add_log(
            "Pre-feasibility gate refused macro_plan: "
            + " | ".join(blocking_constraints)
        )
        return state

    def _evaluate_macro_plan_device_feasibility(self, state: WorkflowState) -> Dict[str, Any]:
        try:
            return self._llm_evaluate_macro_plan_device_feasibility(state)
        except Exception as exc:
            logger.warning(
                "LLM device feasibility check failed without fallback: %s: %s",
                type(exc).__name__,
                exc,
            )
            raise RuntimeError(
                f"LLM device feasibility check failed without fallback: {type(exc).__name__}: {exc}"
            ) from exc

    def _llm_evaluate_macro_plan_device_feasibility(self, state: WorkflowState) -> Dict[str, Any]:
        model = getattr(self, "_model", None)
        if model is None:
            raise RuntimeError("No LLM model configured for device feasibility check")

        workstation_descriptions = self._format_workstation_descriptions_for_feasibility(state)
        task_prompt = DEVICE_FEASIBILITY_TASK_PROMPT.format(
            macro_plan=state.macro_plan,
            workstation_descriptions=workstation_descriptions,
        )
        state.pre_feasibility_prompt = (
            f"System Prompt:\n{DEVICE_FEASIBILITY_SYSTEM_PROMPT}\n\n"
            f"Task Prompt:\n{task_prompt}"
        )
        response = model.invoke(
            [
                {"role": "system", "content": DEVICE_FEASIBILITY_SYSTEM_PROMPT},
                {"role": "user", "content": task_prompt},
            ]
        )
        raw_response = self._coerce_text_content(response.content if response is not None else "")
        if not raw_response:
            raise ValueError("LLM returned empty device feasibility response")

        state.pre_feasibility_raw_response = raw_response
        parsed = self._parse_json_object(raw_response)
        report = self._normalize_device_feasibility_report(parsed)
        report["source"] = "llm"
        return report

    def _format_workstation_descriptions_for_feasibility(self, state: WorkflowState) -> str:
        loader = getattr(self, "_workstation_loader", None)
        if loader is not None:
            return loader.format_for_prompt()
        return self._workstation_descriptions_text(state.workstation_descriptions)

    def _parse_json_object(self, text: str) -> Dict[str, Any]:
        candidates: List[str] = []
        for match in re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE):
            candidates.append(match.strip())
        candidates.append(text.strip())

        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if 0 <= brace_start < brace_end:
            candidates.append(text[brace_start:brace_end + 1])

        last_error: Optional[Exception] = None
        for candidate in candidates:
            if not candidate:
                continue
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
                raise ValueError("Parsed JSON is not an object")
            except Exception as exc:
                last_error = exc
        raise ValueError(f"Failed to parse device feasibility JSON: {last_error}")

    def _normalize_device_feasibility_report(self, parsed: Dict[str, Any]) -> Dict[str, Any]:
        report = dict(parsed)
        blocking_constraints = self._as_string_list(
            report.get("blocking_constraints")
            or report.get("unsupported_reasons")
            or report.get("reasons")
            or []
        )
        unsupported_items = report.get("unsupported_items")
        if not isinstance(unsupported_items, list):
            unsupported_items = []

        is_feasible = self._coerce_feasibility_bool(report.get("is_feasible"))
        status = str(report.get("status") or "").strip().lower()
        if is_feasible is None:
            if status in {"supported", "feasible", "accepted", "pass", "passed"}:
                is_feasible = True
            elif status in {"unsupported", "infeasible", "refused", "fail", "failed"}:
                is_feasible = False
            else:
                is_feasible = not bool(blocking_constraints or unsupported_items)

        if not is_feasible and not blocking_constraints:
            blocking_constraints = self._blocking_constraints_from_unsupported_items(unsupported_items)

        report["is_feasible"] = bool(is_feasible)
        report["status"] = "supported" if report["is_feasible"] else "unsupported"
        report["blocking_constraints"] = blocking_constraints
        report["unsupported_items"] = unsupported_items
        report["supported_parts"] = self._as_string_list(report.get("supported_parts") or [])
        if not isinstance(report.get("device_capability_summary"), dict):
            report["device_capability_summary"] = {}
        return report

    def _coerce_feasibility_bool(self, value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if value is None:
            return None
        lowered = str(value).strip().lower()
        if lowered in {"true", "yes", "y", "1", "supported", "feasible", "accepted"}:
            return True
        if lowered in {"false", "no", "n", "0", "unsupported", "infeasible", "refused"}:
            return False
        return None

    def _as_string_list(self, value: Any) -> List[str]:
        if value in (None, "", "无", "none", "None"):
            return []
        if isinstance(value, str):
            lines = [value]
            if "\n" in value:
                lines = value.splitlines()
            return [
                re.sub(r"^[\-*\d.、\s]+", "", line).strip()
                for line in lines
                if re.sub(r"^[\-*\d.、\s]+", "", line).strip()
                and re.sub(r"^[\-*\d.、\s]+", "", line).strip() not in {"无", "none", "None"}
            ]
        if isinstance(value, list):
            items: List[str] = []
            for item in value:
                if isinstance(item, dict):
                    text = (
                        item.get("reason")
                        or item.get("requirement")
                        or item.get("missing_device_capability")
                        or item.get("message")
                    )
                    if text:
                        items.append(str(text).strip())
                    continue
                if item is not None:
                    items.extend(self._as_string_list(str(item)))
            return list(dict.fromkeys(item for item in items if item))
        return [str(value).strip()]

    def _blocking_constraints_from_unsupported_items(self, unsupported_items: List[Any]) -> List[str]:
        constraints: List[str] = []
        for item in unsupported_items:
            if isinstance(item, dict):
                requirement = str(item.get("requirement") or "").strip()
                reason = str(item.get("reason") or "").strip()
                missing = str(item.get("missing_device_capability") or "").strip()
                text = "；".join(part for part in [requirement, reason, missing] if part)
                if text:
                    constraints.append(text)
            elif item:
                constraints.append(str(item).strip())
        return list(dict.fromkeys(constraints))

    def _blocking_constraints_from_feasibility_report(self, report: Dict[str, Any]) -> List[str]:
        blocking_constraints = self._as_string_list(report.get("blocking_constraints") or [])
        if blocking_constraints:
            return blocking_constraints
        return self._blocking_constraints_from_unsupported_items(
            report.get("unsupported_items") if isinstance(report.get("unsupported_items"), list) else []
        )

    def _coerce_text_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, str):
                    text = item.strip()
                    if text:
                        parts.append(text)
                    continue
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        text = text.strip()
                        if text:
                            parts.append(text)
                        continue
                    if item.get("type") == "text" and item.get("content"):
                        text = str(item["content"]).strip()
                        if text:
                            parts.append(text)
                        continue
                if item is not None:
                    text = str(item).strip()
                    if text:
                        parts.append(text)
            return "\n".join(parts).strip()
        if content is None:
            return ""
        return str(content).strip()

    def _is_offline_observation_handoff(self, macro_plan: str) -> bool:
        text = macro_plan or ""
        lowered = text.lower()
        has_characterization = any(
            token in lowered for token in ["xrd", "pxrd", "ftir", "sem", "tem"]
        ) or any(token in text for token in ["表征", "送样", "数据回传"])
        has_offline_marker = any(
            token in lowered
            for token in ["offline", "handoff", "external", "not by current device"]
        ) or any(token in text for token in ["离线", "外部", "送样", "数据回传", "不由当前设备层"])
        return has_characterization and has_offline_marker

    def _workstation_descriptions_text(self, descriptions: List[Dict[str, Any]]) -> str:
        chunks = []
        for description in descriptions:
            chunks.extend(
                str(description.get(key, ""))
                for key in [
                    "station_name",
                    "usage_content",
                    "audit_rules_content",
                    "skill_content",
                ]
            )
        return "\n".join(chunks)

    def _summarize_device_capabilities(self, descriptions: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "supported_containers": [],
            "supported_workstations": [
                str(description.get("station_name", "")).strip()
                for description in descriptions
                if str(description.get("station_name", "")).strip()
            ],
        }

    def _step2_pre_flow_agent(self, state: WorkflowState) -> WorkflowState:
        from pre_flow_agent.state import PreFlowAgentTestState

        state.update_stage("pre_flow")
        pre_flow_state = PreFlowAgentTestState(
            final_goal=state.final_goal,
            macro_plan=state.macro_plan,
            workstation_descriptions=state.workstation_descriptions,
            txt_format_reference=state.txt_format_reference,
            use_knowledge_agent=False,
            use_research_agent=False,
            iteration_id=state.iteration_id,
            exp_log_path=state.exp_log_path,
        )
        pre_flow_state = self._pre_flow_agent.run(pre_flow_state)
        if pre_flow_state.status == "failed":
            raise RuntimeError(f"PreFlowAgent failed: {pre_flow_state.errors}")

        state.macro_plan_summary = pre_flow_state.macro_plan_summary
        state.observation_requirements = pre_flow_state.observation_requirements
        state.knowledge = pre_flow_state.knowledge
        state.related_workflows_unformatted = pre_flow_state.related_workflows_unformatted
        state.related_workflows_txt = pre_flow_state.related_workflows_txt
        state.goal_in_this_iteration = pre_flow_state.goal_in_this_iteration
        state.knowledge_raw_inputs = pre_flow_state.knowledge_raw_inputs
        state.memory_raw_inputs = pre_flow_state.memory_raw_inputs
        return state

    def _step3_workflow_generator(self, state: WorkflowState) -> WorkflowState:
        from workflow_generator.state import WorkflowGeneratorTestState

        state.update_stage("workflow_gen")
        workflow_gen_state = WorkflowGeneratorTestState(
            final_goal=state.final_goal,
            macro_plan=state.macro_plan,
            macro_plan_summary=state.macro_plan_summary,
            observation_requirements=state.observation_requirements,
            goal_in_this_iteration=state.goal_in_this_iteration,
            knowledge=state.knowledge,
            related_workflows_txt=state.related_workflows_txt,
            txt_format_reference=state.txt_format_reference,
            workstation_descriptions=state.workstation_descriptions,
            iteration_id=state.iteration_id,
            workflow_id=state.workflow_id,
            exp_log_path=state.exp_log_path,
        )
        workflow_gen_state = self._workflow_generator.run(workflow_gen_state)
        if workflow_gen_state.status == "failed":
            raise RuntimeError(f"WorkflowGenerator failed: {workflow_gen_state.errors}")

        state.workflow_skeleton_txt = workflow_gen_state.workflow_skeleton_txt
        state.workflow_txt = workflow_gen_state.workflow_txt
        state.workflow_id = workflow_gen_state.workflow_id
        return state

    def _step4_verify_single(self, state: WorkflowState) -> WorkflowState:
        from verify_agent.state import VerifyAgentTestState

        verify_state = VerifyAgentTestState(
            final_goal=state.final_goal,
            macro_plan=state.macro_plan,
            macro_plan_summary=state.macro_plan_summary,
            observation_requirements=state.observation_requirements,
            goal_in_this_iteration=state.goal_in_this_iteration,
            knowledge=state.knowledge,
            related_workflows_txt=state.related_workflows_txt,
            workflow_txt=state.workflow_txt,
            iteration_id=state.iteration_id,
            workflow_id=state.workflow_id,
            exp_log_path=state.exp_log_path,
        )
        verify_state = self._verify_agent.run(verify_state)
        if verify_state.status == "failed":
            raise RuntimeError(f"VerifyAgent failed: {verify_state.errors}")

        state.verification_result = verify_state.verification_result
        state.verification_category = verify_state.verification_category
        state.blocking_constraints = verify_state.blocking_constraints
        state.verification_suggestion = verify_state.verification_suggestion
        return state

    def _step4_verify_agent_with_retry(self, state: WorkflowState) -> WorkflowState:
        state.update_stage("verify")
        state = self._step4_verify_single(state)
        if state.verification_result == "accepted":
            return state
        if state.verification_category == "physical_infeasible":
            return state

        for attempt in range(1, self._max_verify_retries + 1):
            state.retry_count = attempt
            state.add_log(f"Verify refused, start task2 retry {attempt}/{self._max_verify_retries}")
            state = self._step5_workflow_generator_task2(state)
            state = self._step4_verify_single(state)
            if state.verification_result == "accepted":
                return state
            if state.verification_category == "physical_infeasible":
                return state

        return state

    def _step5_workflow_generator_task2(self, state: WorkflowState) -> WorkflowState:
        from workflow_generator.state import WorkflowGeneratorTestState

        task2_state = WorkflowGeneratorTestState(
            final_goal=state.final_goal,
            macro_plan=state.macro_plan,
            macro_plan_summary=state.macro_plan_summary,
            observation_requirements=state.observation_requirements,
            goal_in_this_iteration=state.goal_in_this_iteration,
            knowledge=state.knowledge,
            related_workflows_txt=state.related_workflows_txt,
            txt_format_reference=state.txt_format_reference,
            workstation_descriptions=state.workstation_descriptions,
            iteration_id=state.iteration_id,
            workflow_id=state.workflow_id + 1,
            exp_log_path=state.exp_log_path,
        )
        task2_state = self._workflow_generator.run_task2(
            state=task2_state,
            source_workflow_txt=state.workflow_txt,
            verification_suggestion=state.verification_suggestion,
        )
        if task2_state.status == "failed":
            raise RuntimeError(f"WorkflowGenerator task2 failed: {task2_state.errors}")

        state.workflow_skeleton_txt = task2_state.workflow_skeleton_txt
        state.workflow_txt = task2_state.workflow_txt
        state.workflow_id = task2_state.workflow_id
        return state

    def _step6_format_translate_agent(self, state: WorkflowState) -> WorkflowState:
        from format_translate_agent.state import FormatTranslateAgentTestState

        state.update_stage("format_translate")
        format_translate_state = FormatTranslateAgentTestState(
            final_goal=state.final_goal,
            macro_plan=state.macro_plan,
            observation_requirements=state.observation_requirements,
            goal_in_this_iteration=state.goal_in_this_iteration,
            workflow_txt=state.workflow_txt,
            workstation_descriptions=state.workstation_descriptions,
            knowledge=state.knowledge,
            json_format_reference=state.json_format_reference,
            iteration_id=state.iteration_id,
            workflow_id=state.workflow_id,
            exp_log_path=state.exp_log_path,
        )
        format_translate_state = self._format_translate_agent.run(format_translate_state)
        if format_translate_state.status == "failed":
            raise RuntimeError(f"FormatTranslateAgent failed: {format_translate_state.errors}")
        state.workflow_json = format_translate_state.workflow_json
        return state

    def _validate_translated_workflow(self, state: WorkflowState) -> None:
        workflow_json = state.workflow_json or {}
        unknown_steps = workflow_json.get("unknown_steps")
        if isinstance(unknown_steps, list) and unknown_steps:
            raise ValueError(f"Translated workflow contains unknown_steps: {unknown_steps}")
        if workflow_json.get("steps") is None:
            raise ValueError("Translated workflow missing steps")

    def _build_success_package(self, state: WorkflowState) -> Dict[str, Any]:
        return {
            "status": "success",
            "exp_id": state.exp_id,
            "iteration_id": state.iteration_id,
            "workflow_id": state.workflow_id,
            "macro_plan": state.macro_plan,
            "macro_plan_summary": state.macro_plan_summary,
            "observation_plan": state.observation_requirements,
            "verification_summary": {
                "result": state.verification_result,
                "category": state.verification_category,
                "blocking_constraints": state.blocking_constraints,
                "message": state.verification_suggestion,
            },
            "workflow_txt": state.workflow_txt,
            "workflow_json": state.workflow_json,
        }

    def _build_feasibility_error_package(self, state: WorkflowState) -> Dict[str, Any]:
        feasibility_report = state.pre_feasibility_report or {}
        return {
            "feedback_type": "device_feasibility_error",
            "status": "feasibility_error",
            "exp_id": state.exp_id,
            "iteration_id": state.iteration_id,
            "workflow_id": state.workflow_id,
            "macro_plan": state.macro_plan,
            "macro_plan_summary": state.macro_plan_summary,
            "observation_plan": state.observation_requirements,
            "device_capabilities": self._device_capabilities_for_error_package(state),
            "feasibility_assessment": feasibility_report,
            "error_package": {
                "type": state.verification_category or "physical_infeasible",
                "blocking_constraints": state.blocking_constraints,
                "message": state.verification_suggestion,
                "last_workflow_txt": state.workflow_txt,
                "unsupported_items": feasibility_report.get("unsupported_items", []),
                "assessment_source": feasibility_report.get("source", state.pre_feasibility_source),
            },
        }

    def _device_capabilities_for_error_package(self, state: WorkflowState) -> Dict[str, Any]:
        base_summary = self._summarize_device_capabilities(state.workstation_descriptions)
        summary = (state.pre_feasibility_report or {}).get("device_capability_summary")
        if not isinstance(summary, dict):
            return base_summary

        merged = dict(base_summary)
        for key in ["supported_containers", "supported_workstations", "not_supported"]:
            value = summary.get(key)
            if isinstance(value, list):
                merged[key] = value
            elif isinstance(value, str) and value.strip():
                merged[key] = [value.strip()]
        return merged

    def _build_verification_refused_package(self, state: WorkflowState) -> Dict[str, Any]:
        return {
            "status": "verification_refused",
            "exp_id": state.exp_id,
            "iteration_id": state.iteration_id,
            "workflow_id": state.workflow_id,
            "macro_plan": state.macro_plan,
            "macro_plan_summary": state.macro_plan_summary,
            "observation_plan": state.observation_requirements,
            "error_package": {
                "type": state.verification_category or "verification_refused",
                "blocking_constraints": state.blocking_constraints,
                "message": state.verification_suggestion,
                "last_workflow_txt": state.workflow_txt,
            },
        }
