"""
Verify Agent 工作流
====================
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

from .state import VerifyAgentTestState
from .prompts import (
    SYSTEM_PROMPT,
    FORWARD_CONSTRAINT_VERIFYING_PROMPT,
    FORWARD_FEASIBILITY_VERIFYING_PROMPT,
)
from .prompts.task_prompts import COMMON_VERIFY_CONSTRAINTS, OUTPUT_FORMAT

logger = logging.getLogger(__name__)


class VerifyAgent(BaseAgent):
    """对 workflow txt 进行约束审核和 feasibility 审核。"""

    KNOWLEDGE_CHAR_LIMIT = 800
    RELATED_WORKFLOW_CHAR_LIMIT = 800
    CATEGORY_PRIORITY = [
        "physical_infeasible",
        "safety_risk",
        "description_constraint_error",
        "format_or_parameter_error",
    ]

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

    def run(self, state: VerifyAgentTestState) -> VerifyAgentTestState:
        logger.info("Starting Verify Agent")
        if self._log_manager is None and state.exp_log_path:
            exp_id = os.path.basename(os.path.dirname(state.exp_log_path))
            self._log_manager = LogManager(exp_id)

        try:
            self._step1_get_inputs(state)
            self._step2_forward_constraint_verifying(state)
            if state.verification_result == "refused":
                self._step4_update_log(state)
                state.status = "success"
                return state
            self._step3_forward_feasibility_verifying(state)
            self._step4_update_log(state)
            state.status = "success"
        except Exception as exc:
            error_msg = f"Verify Agent failed: {exc}"
            logger.exception(error_msg)
            state.errors.append(error_msg)
            state.status = "failed"
            raise
        return state

    def _step1_get_inputs(self, state: VerifyAgentTestState) -> None:
        state.macro_plan = state.macro_plan or state.final_goal
        state.final_goal = state.final_goal or state.macro_plan
        workstation_descriptions = self._workstation_loader.format_workflow_specific_for_prompt(
            state.workflow_txt, include_audit=True
        )
        state._input_data = {
            "macro_plan": state.macro_plan,
            "macro_plan_summary": state.macro_plan_summary,
            "observation_requirements": state.observation_requirements,
            "goal_in_this_iteration": self._trim_text(state.goal_in_this_iteration, 800),
            "knowledge": self._trim_text(state.knowledge, self.KNOWLEDGE_CHAR_LIMIT),
            "related_workflows_txt": self._trim_text(state.related_workflows_txt, self.RELATED_WORKFLOW_CHAR_LIMIT),
            "workflow_txt": state.workflow_txt,
            "workstation_descriptions": workstation_descriptions,
        }

    def _step2_forward_constraint_verifying(self, state: VerifyAgentTestState) -> None:
        input_data = state._input_data
        task_prompt = FORWARD_CONSTRAINT_VERIFYING_PROMPT.format(
            macro_plan=input_data["macro_plan"],
            goal_in_this_iteration=input_data["goal_in_this_iteration"],
            observation_requirements=self._format_observation_requirements(input_data["observation_requirements"]),
            knowledge=input_data["knowledge"],
            related_workflows_txt=input_data["related_workflows_txt"],
            workstation_descriptions=input_data["workstation_descriptions"],
            workflow_txt=input_data["workflow_txt"],
            common_constraints=COMMON_VERIFY_CONSTRAINTS,
            output_format=OUTPUT_FORMAT,
        )
        state.forward_constraint_verifying_prompt = f"System Prompt:\n{SYSTEM_PROMPT}\n\nTask Prompt:\n{task_prompt}"
        raw_response = self._invoke_with_retry_direct([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task_prompt},
        ])
        result, category, constraints, suggestion = self._parse_verifying_output(
            raw_response,
            default_category="description_constraint_error",
        )
        state.verification_result = result
        state.verification_category = category
        state.blocking_constraints = constraints
        state.verification_suggestion = suggestion

    def _step3_forward_feasibility_verifying(self, state: VerifyAgentTestState) -> None:
        input_data = state._input_data
        task_prompt = FORWARD_FEASIBILITY_VERIFYING_PROMPT.format(
            macro_plan=input_data["macro_plan"],
            macro_plan_summary=input_data["macro_plan_summary"],
            observation_requirements=self._format_observation_requirements(input_data["observation_requirements"]),
            workstation_descriptions=input_data["workstation_descriptions"],
            workflow_txt=input_data["workflow_txt"],
            common_constraints=COMMON_VERIFY_CONSTRAINTS,
            output_format=OUTPUT_FORMAT,
        )
        state.forward_feasibility_verifying_prompt = f"System Prompt:\n{SYSTEM_PROMPT}\n\nTask Prompt:\n{task_prompt}"
        raw_response = self._invoke_with_retry_direct([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task_prompt},
        ])
        result, category, constraints, suggestion = self._parse_verifying_output(
            raw_response,
            default_category="physical_infeasible",
        )
        state.verification_result = result
        state.verification_category = category
        state.blocking_constraints = constraints
        state.verification_suggestion = suggestion

    def _step4_update_log(self, state: VerifyAgentTestState) -> None:
        if self._log_manager is None:
            return
        self._log_manager.update_workflow(
            state.iteration_id,
            state.workflow_id,
            {
                "verification_result": state.verification_result,
                "verification_category": state.verification_category,
                "blocking_constraints": state.blocking_constraints,
                "verification_suggestion": state.verification_suggestion,
            },
        )

    def _parse_verifying_output(self, output: str, default_category: str):
        result = self._extract_first_available_section(
            output,
            ("审核结果", "审核结论"),
            ("失败类型", "错误类型", "阻塞约束", "问题列表", "修改建议", "正确部分"),
        ).lower().strip()
        if result not in {"accepted", "refused"}:
            lowered = output.lower()
            if "accepted" in lowered:
                result = "accepted"
            elif "refused" in lowered:
                result = "refused"
            else:
                raise ValueError(f"Failed to parse verification result from output: {output}")

        category_text = self._extract_first_available_section(
            output,
            ("失败类型", "错误类型"),
            ("阻塞约束", "问题列表", "修改建议", "正确部分"),
        ).strip()
        category = self._normalize_category(category_text, default_category, result)

        blocking_text = self._extract_first_available_section(
            output,
            ("阻塞约束", "问题列表"),
            ("修改建议", "正确部分"),
        ).strip()
        blocking_constraints = self._parse_blocking_constraints(blocking_text)
        if result == "accepted":
            blocking_constraints = []
            category = "none"

        suggestion = self._extract_first_available_section(
            output,
            ("修改建议",),
            ("正确部分",),
        ).strip()
        if result == "refused" and not suggestion:
            raise ValueError("Verifier refused the workflow but returned no 修改建议 section")
        return result, category, blocking_constraints, suggestion or "无"

    def _normalize_category(self, category_text: str, default_category: str, result: str) -> str:
        if result == "accepted":
            return "none"
        if not category_text:
            return default_category
        raw_items = re.split(r"[,，/\n]+", category_text)
        categories = [item.strip().lower() for item in raw_items if item.strip()]
        for category in self.CATEGORY_PRIORITY:
            if category in categories:
                return category
        return categories[0] if categories else default_category

    def _extract_first_available_section(self, text: str, headings: Tuple[str, ...], next_headings: Tuple[str, ...]) -> str:
        for heading in headings:
            section = self._extract_section(text, heading, next_headings)
            if section:
                return section
        return ""

    def _extract_section(self, text: str, heading: str, next_headings: Tuple[str, ...]) -> str:
        start_pattern = rf"###\s*{re.escape(heading)}\s*"
        start_match = re.search(start_pattern, text)
        if not start_match:
            return ""
        start = start_match.end()
        end = len(text)
        for next_heading in next_headings:
            next_match = re.search(rf"###\s*{re.escape(next_heading)}\s*", text[start:])
            if next_match:
                end = start + next_match.start()
                break
        return text[start:end].strip()

    def _parse_blocking_constraints(self, text: str):
        if not text or text in {"无", "none", "None"}:
            return []
        constraints = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped in {"无", "none", "None"}:
                continue
            cleaned = re.sub(r"^#+\s*", "", stripped)
            cleaned = re.sub(r"^[\-*]\s*", "", cleaned)
            cleaned = re.sub(r"^\d+\.\s*", "", cleaned)
            cleaned = cleaned.strip()
            if cleaned:
                constraints.append(cleaned)
        deduped = []
        for item in constraints:
            if item not in deduped:
                deduped.append(item)
        return deduped

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
                    if item.get("type") == "text" and item.get("content"):
                        parts.append(str(item["content"]))
                        continue
                if item is not None:
                    parts.append(str(item))
            return "\n".join(part for part in parts if part).strip()
        if content is None:
            return ""
        return str(content)

    def _invoke_with_retry_direct(self, messages: list, max_retries: int = 5) -> str:
        import time

        wait_schedule = [5, 10, 20, 30, 60]
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
