"""
Workflow Generator workflow.
"""

import json
import logging
import os
import re
from typing import Optional, Tuple

from core import BaseAgent
from utils.llm_factory import LLMFactory
from utils.log_manager import LogManager
from utils.workstation_loader import WorkstationLoader

from .state import WorkflowGeneratorTestState
from .prompts import (
    SYSTEM_PROMPT,
    FORWARD_SKELETON_PROMPT,
    FORWARD_PARAMETER_FILL_PROMPT,
    BACKWARD_SKELETON_PROMPT,
    BACKWARD_PARAMETER_FILL_PROMPT,
)
from .prompts.task_prompts import COMMON_CONSTRAINTS

logger = logging.getLogger(__name__)


class WorkflowGenerator(BaseAgent):
    """Translate prepared context into a workstation-level txt workflow."""

    KNOWLEDGE_CHAR_LIMIT = 1800
    RELATED_WORKFLOW_CHAR_LIMIT = 1400
    REFERENCE_CHAR_LIMIT = 1800
    PARAMETER_FILL_VALIDATION_RETRIES = 3

    def __init__(
        self,
        model=None,
        use_knowledge_agent: bool = False,
        use_research_agent: bool = False,
        exp_log_path: Optional[str] = None,
        use_new_format: bool = True,
    ):
        if model is None:
            model = LLMFactory.create()

        super().__init__(
            model=model,
            system_prompt=SYSTEM_PROMPT,
            task_prompt="",
            parse_json=False,
        )

        self._exp_log_path = exp_log_path
        self._workstation_loader = WorkstationLoader(use_new_format=use_new_format)
        self._log_manager = None
        if exp_log_path:
            exp_id = os.path.basename(os.path.dirname(exp_log_path))
            self._log_manager = LogManager(exp_id)

    def run(self, state: WorkflowGeneratorTestState) -> WorkflowGeneratorTestState:
        logger.info("Starting Workflow Generator Task1 (forward)")
        if self._log_manager is None and state.exp_log_path:
            exp_id = os.path.basename(os.path.dirname(state.exp_log_path))
            self._log_manager = LogManager(exp_id)

        try:
            self._step1_get_inputs(state)
            self._step2_forward_skeleton(state)
            self._step3_forward_parameter_fill(state)
            self._step4_append_workflow(state)
            state.status = "success"
        except Exception as exc:
            error_msg = f"Workflow Generator failed: {exc}"
            logger.exception(error_msg)
            state.errors.append(error_msg)
            state.status = "failed"
            raise
        return state

    def run_task2(self, state: WorkflowGeneratorTestState, source_workflow_txt: str, verification_suggestion: str) -> WorkflowGeneratorTestState:
        if not verification_suggestion or not verification_suggestion.strip():
            raise ValueError("Task2 requires non-empty verification_suggestion")

        logger.info("Starting Workflow Generator Task2 (backward)")
        if self._log_manager is None and state.exp_log_path:
            exp_id = os.path.basename(os.path.dirname(state.exp_log_path))
            self._log_manager = LogManager(exp_id)

        try:
            self._step1_get_inputs(state)
            state.source_workflow_txt = source_workflow_txt
            self._step2_backward_skeleton(state, source_workflow_txt, verification_suggestion)
            self._step3_backward_parameter_fill(state, verification_suggestion)
            self._step4_append_workflow(state)
            state.status = "success"
        except Exception as exc:
            error_msg = f"Workflow Generator Task2 failed: {exc}"
            logger.exception(error_msg)
            state.errors.append(error_msg)
            state.status = "failed"
            raise
        return state

    def _step1_get_inputs(self, state: WorkflowGeneratorTestState) -> None:
        state.macro_plan = state.macro_plan or state.final_goal
        state.final_goal = state.final_goal or state.macro_plan
        prompt_query_text = "\n".join([
            state.macro_plan,
            state.goal_in_this_iteration,
            state.related_workflows_txt,
            state.knowledge,
        ])
        workstation_descriptions = self._workstation_loader.format_relevant_for_prompt(prompt_query_text)
        state._input_data = {
            "macro_plan": state.macro_plan,
            "macro_plan_summary": state.macro_plan_summary,
            "observation_requirements": state.observation_requirements,
            "goal_in_this_iteration": self._trim_text(state.goal_in_this_iteration, 800),
            "knowledge": self._trim_text(state.knowledge, self.KNOWLEDGE_CHAR_LIMIT),
            "related_workflows_txt": self._trim_text(state.related_workflows_txt, self.RELATED_WORKFLOW_CHAR_LIMIT),
            "workstation_descriptions": workstation_descriptions,
            "reference_format": self._trim_text(state.txt_format_reference, self.REFERENCE_CHAR_LIMIT),
        }

    def _step2_forward_skeleton(self, state: WorkflowGeneratorTestState) -> None:
        input_data = state._input_data
        task_prompt = FORWARD_SKELETON_PROMPT.format(
            macro_plan=input_data["macro_plan"],
            macro_plan_summary=input_data["macro_plan_summary"],
            observation_requirements=self._format_observation_requirements(input_data["observation_requirements"]),
            goal_in_this_iteration=input_data["goal_in_this_iteration"],
            knowledge=input_data["knowledge"],
            related_workflows_txt=input_data["related_workflows_txt"],
            workstation_descriptions=input_data["workstation_descriptions"],
            common_constraints=COMMON_CONSTRAINTS,
        )
        state.forward_skeleton_prompt = f"System Prompt:\n{SYSTEM_PROMPT}\n\nTask Prompt:\n{task_prompt}"
        raw_response = self._invoke_with_retry_direct([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task_prompt},
        ])
        state.workflow_skeleton_txt = self._extract_workflow_text(raw_response, ("实验方案骨架",))

    def _step3_forward_parameter_fill(self, state: WorkflowGeneratorTestState) -> None:
        input_data = state._input_data
        task_prompt = FORWARD_PARAMETER_FILL_PROMPT.format(
            macro_plan=input_data["macro_plan"],
            goal_in_this_iteration=input_data["goal_in_this_iteration"],
            observation_requirements=self._format_observation_requirements(input_data["observation_requirements"]),
            knowledge=input_data["knowledge"],
            related_workflows_txt=input_data["related_workflows_txt"],
            workstation_descriptions=input_data["workstation_descriptions"],
            reference_format=input_data["reference_format"],
            workflow_skeleton_txt=state.workflow_skeleton_txt,
            common_constraints=COMMON_CONSTRAINTS,
        )
        state.forward_parameter_prompt = f"System Prompt:\n{SYSTEM_PROMPT}\n\nTask Prompt:\n{task_prompt}"
        state.workflow_txt = self._generate_complete_workflow_text(
            task_prompt=task_prompt,
            skeleton_txt=state.workflow_skeleton_txt,
            label="forward",
        )

    def _step2_backward_skeleton(self, state: WorkflowGeneratorTestState, source_workflow_txt: str, verification_suggestion: str) -> None:
        input_data = state._input_data
        task_prompt = BACKWARD_SKELETON_PROMPT.format(
            macro_plan=input_data["macro_plan"],
            goal_in_this_iteration=input_data["goal_in_this_iteration"],
            observation_requirements=self._format_observation_requirements(input_data["observation_requirements"]),
            knowledge=input_data["knowledge"],
            related_workflows_txt=input_data["related_workflows_txt"],
            workstation_descriptions=input_data["workstation_descriptions"],
            source_workflow_txt=self._trim_text(source_workflow_txt, 2200),
            verification_suggestion=verification_suggestion,
            common_constraints=COMMON_CONSTRAINTS,
        )
        state.backward_skeleton_prompt = f"System Prompt:\n{SYSTEM_PROMPT}\n\nTask Prompt:\n{task_prompt}"
        raw_response = self._invoke_with_retry_direct([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task_prompt},
        ])
        state.workflow_skeleton_txt = self._extract_workflow_text(raw_response, ("修正后的实验方案骨架", "实验方案骨架"))

    def _step3_backward_parameter_fill(self, state: WorkflowGeneratorTestState, verification_suggestion: str) -> None:
        input_data = state._input_data
        task_prompt = BACKWARD_PARAMETER_FILL_PROMPT.format(
            macro_plan=input_data["macro_plan"],
            goal_in_this_iteration=input_data["goal_in_this_iteration"],
            observation_requirements=self._format_observation_requirements(input_data["observation_requirements"]),
            knowledge=input_data["knowledge"],
            related_workflows_txt=input_data["related_workflows_txt"],
            workstation_descriptions=input_data["workstation_descriptions"],
            reference_format=input_data["reference_format"],
            workflow_skeleton_txt=state.workflow_skeleton_txt,
            verification_suggestion=verification_suggestion,
            common_constraints=COMMON_CONSTRAINTS,
        )
        state.backward_parameter_prompt = f"System Prompt:\n{SYSTEM_PROMPT}\n\nTask Prompt:\n{task_prompt}"
        state.workflow_txt = self._generate_complete_workflow_text(
            task_prompt=task_prompt,
            skeleton_txt=state.workflow_skeleton_txt,
            label="backward",
        )

    def _generate_complete_workflow_text(self, task_prompt: str, skeleton_txt: str, label: str) -> str:
        prompt_for_attempt = task_prompt
        last_workflow_txt = ""

        for attempt in range(1, self.PARAMETER_FILL_VALIDATION_RETRIES + 1):
            raw_response = self._invoke_with_retry_direct([
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt_for_attempt},
            ])
            workflow_txt = self._extract_workflow_text(raw_response, ("实验方案",))
            last_workflow_txt = workflow_txt
            if self._is_complete_workflow_text(workflow_txt, skeleton_txt):
                return workflow_txt

            feedback = self._build_completeness_feedback(workflow_txt, skeleton_txt)
            logger.warning(
                "%s parameter fill produced incomplete workflow on validation attempt %s/%s: %s",
                label,
                attempt,
                self.PARAMETER_FILL_VALIDATION_RETRIES,
                feedback,
            )
            prompt_for_attempt = self._build_validation_retry_prompt(task_prompt, workflow_txt, feedback)

        raise ValueError(f"{label} parameter fill did not produce a complete workflow: {self._build_completeness_feedback(last_workflow_txt, skeleton_txt)}")

    def _build_validation_retry_prompt(self, base_prompt: str, previous_workflow_txt: str, feedback: str) -> str:
        return (
            f"{base_prompt}\n\n"
            "## 上一版输出未通过完整性校验\n"
            f"- 问题：{feedback}\n"
            "- 你必须重新输出完整实验方案全文。\n"
            "- 不要只输出修改点。\n"
            "- 不要省略骨架中的任何主要步骤。\n"
            "- 如果上一版少了步骤，就把缺失步骤补齐后重新输出整份 workflow。\n\n"
            "### 上一版输出（仅供纠错参考）\n"
            f"{self._trim_text(previous_workflow_txt, 1800)}"
        )

    def _build_completeness_feedback(self, workflow_txt: str, skeleton_txt: str) -> str:
        workflow_len = len((workflow_txt or "").strip())
        workflow_steps = self._count_steps(workflow_txt)
        skeleton_steps = self._count_steps(skeleton_txt)
        minimum_steps = max(3, skeleton_steps - 2) if skeleton_steps else 3

        problems = []
        if workflow_len < 200:
            problems.append(f"文本长度过短（当前 {workflow_len} 字，至少需要 200 字）")
        if workflow_steps < minimum_steps:
            problems.append(
                f"步骤数量不足（骨架约 {skeleton_steps} 步，最终至少需要 {minimum_steps} 步，当前只有 {workflow_steps} 步）"
            )
        if not problems:
            problems.append("输出未通过结构完整性校验")
        return "；".join(problems)

    def _is_complete_workflow_text(self, workflow_txt: str, skeleton_txt: str) -> bool:
        if len((workflow_txt or "").strip()) < 200:
            return False
        workflow_steps = self._count_steps(workflow_txt)
        skeleton_steps = self._count_steps(skeleton_txt)
        minimum_steps = max(3, skeleton_steps - 2) if skeleton_steps else 3
        return workflow_steps >= minimum_steps

    def _count_steps(self, text: str) -> int:
        numbered_steps = re.findall(r"^\s*\d+\.\s*第\d+步", text or "", re.MULTILINE)
        if numbered_steps:
            return len(numbered_steps)
        station_steps = re.findall(r"^\s*---工作站[:：]\s*", text or "", re.MULTILINE)
        return len(station_steps)

    def _step4_append_workflow(self, state: WorkflowGeneratorTestState) -> None:
        if self._log_manager is None:
            return
        state.workflow_id = self._log_manager.add_workflow(
            state.iteration_id,
            {
                "workflow_skeleton_txt": state.workflow_skeleton_txt,
                "workflow_txt": state.workflow_txt,
            },
        )

    def _extract_workflow_text(self, output: str, headings: Tuple[str, ...]) -> str:
        code_blocks = re.findall(r"```(?:text)?\s*([\s\S]*?)\s*```", output)
        if code_blocks:
            return code_blocks[-1].strip()

        for heading in headings:
            match = re.search(rf"###\s*{re.escape(heading)}\s*(.*)", output, re.DOTALL)
            if match:
                return match.group(1).strip()

        first_step = re.search(r"(^.*?第\d+步[\s\S]*)", output, re.MULTILINE)
        if first_step:
            return first_step.group(1).strip()

        return output.strip()

    def _format_observation_requirements(self, observation_requirements) -> str:
        if not observation_requirements:
            return "无"
        return json.dumps(observation_requirements, ensure_ascii=False, indent=2)

    def _normalize_text(self, text: str) -> str:
        return re.sub(r"\n{3,}", "\n\n", str(text or "").strip())

    def _trim_text(self, text: str, max_chars: int) -> str:
        normalized = self._normalize_text(text)
        if len(normalized) <= max_chars:
            return normalized
        return normalized[:max_chars].rstrip() + "\n...[truncated]"

    def _coerce_text_content(self, content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                        continue
                    if isinstance(text, list):
                        parts.extend(str(sub) for sub in text if sub)
                        continue
                    if item.get("type") == "text" and item.get("content"):
                        parts.append(str(item["content"]))
                        continue
                if item is not None:
                    parts.append(str(item))
            return "\n".join(part for part in parts if part).strip()
        if content is None:
            return ""
        return str(content)

    def _invoke_with_retry_direct(self, messages: list, max_retries: int = 8) -> str:
        import time

        wait_schedule = [5, 10, 20, 30, 60, 60, 90, 120]
        last_error = None
        for attempt in range(max_retries):
            try:
                response = self._model.invoke(messages)
                content = self._coerce_text_content(response.content if response is not None else "")
                if not content:
                    raise ValueError("LLM returned empty content")
                return content
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "LLM call failed (attempt %s/%s, %s): %s",
                    attempt + 1,
                    max_retries,
                    type(exc).__name__,
                    exc,
                )
                if attempt < max_retries - 1:
                    time.sleep(wait_schedule[min(attempt, len(wait_schedule) - 1)])
        raise RuntimeError(
            f"LLM call failed after {max_retries} retries: {type(last_error).__name__}: {last_error}"
        ) from last_error
