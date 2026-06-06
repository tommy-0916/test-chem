"""
Format Translate Agent 工作流
==============================
"""

import json
import logging
import os
import re
from typing import Optional

from core import BaseAgent
from utils.llm_factory import LLMFactory
from utils.log_manager import LogManager
from utils.workstation_loader import WorkstationLoader

from .state import FormatTranslateAgentTestState
from .prompts import SYSTEM_PROMPT, FORWARD_FORMAT_TRANSLATE_PROMPT

logger = logging.getLogger(__name__)


class FormatTranslateAgent(BaseAgent):
    KNOWLEDGE_CHAR_LIMIT = 600
    JSON_REFERENCE_CHAR_LIMIT = 2200

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
            parse_json=True,
        )

        self._exp_log_path = exp_log_path
        self._workstation_loader = WorkstationLoader(use_new_format=use_new_format)
        self._log_manager = None
        if exp_log_path:
            exp_id = os.path.basename(os.path.dirname(exp_log_path))
            self._log_manager = LogManager(exp_id)

    def run(self, state: FormatTranslateAgentTestState) -> FormatTranslateAgentTestState:
        logger.info("Starting Format Translate Agent")
        if self._log_manager is None and state.exp_log_path:
            exp_id = os.path.basename(os.path.dirname(state.exp_log_path))
            self._log_manager = LogManager(exp_id)

        try:
            self._step1_get_inputs(state)
            self._step2_forward_format_translate(state)
            self._step3_update_log(state)
            state.status = "success"
        except Exception as exc:
            error_msg = f"Format Translate Agent failed: {exc}"
            logger.exception(error_msg)
            state.errors.append(error_msg)
            state.status = "failed"
            raise
        return state

    def _step1_get_inputs(self, state: FormatTranslateAgentTestState) -> None:
        state.macro_plan = state.macro_plan or state.final_goal
        state.final_goal = state.final_goal or state.macro_plan
        workstation_descriptions = self._workstation_loader.format_workflow_specific_for_prompt(
            state.workflow_txt, include_audit=False
        )
        state._input_data = {
            "final_goal": state.final_goal,
            "goal_in_this_iteration": self._trim_text(state.goal_in_this_iteration, 600),
            "workflow_txt": state.workflow_txt,
            "knowledge": self._trim_text(state.knowledge, self.KNOWLEDGE_CHAR_LIMIT),
            "workstation_descriptions": workstation_descriptions,
            "json_format_reference": self._trim_text(state.json_format_reference, self.JSON_REFERENCE_CHAR_LIMIT),
        }

    def _step2_forward_format_translate(self, state: FormatTranslateAgentTestState) -> None:
        input_data = state._input_data
        task_prompt = FORWARD_FORMAT_TRANSLATE_PROMPT.format(
            final_goal=input_data["final_goal"],
            goal_in_this_iteration=input_data["goal_in_this_iteration"],
            workflow_txt=input_data["workflow_txt"],
            workstation_descriptions=input_data["workstation_descriptions"],
            knowledge=input_data["knowledge"],
            json_format_reference=input_data["json_format_reference"],
        )
        state.forward_format_translate_prompt = f"System Prompt:\n{SYSTEM_PROMPT}\n\nTask Prompt:\n{task_prompt}"
        raw_response = self._invoke_with_retry_direct([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task_prompt},
        ])
        self._parse_forward_format_translate_output(state, raw_response)

    def _step3_update_log(self, state: FormatTranslateAgentTestState) -> None:
        if self._log_manager is None:
            return
        self._log_manager.update_workflow(
            state.iteration_id,
            state.workflow_id,
            {"workflow_json": state.workflow_json},
        )

    def _parse_forward_format_translate_output(self, state: FormatTranslateAgentTestState, output: str) -> None:
        logger.info("Raw LLM response (first 500 chars): %s", output[:500])
        try:
            if isinstance(output, dict):
                state.workflow_json = output
            elif isinstance(output, str):
                matches = re.findall(r'```(?:json)?\s*([\s\S]*?)\s*```', output)
                if matches:
                    state.workflow_json = json.loads(matches[-1].strip())
                else:
                    state.workflow_json = json.loads(output)
            else:
                raise ValueError(f"Unexpected output type: {type(output)}")
            if "steps" not in state.workflow_json:
                raise ValueError("Missing 'steps' field in output JSON")
        except Exception as exc:
            raise ValueError(f"Failed to parse workflow output: {exc}") from exc

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
