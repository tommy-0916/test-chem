"""
Pre-Flow Agent workflow.
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from core import BaseAgent
from utils.llm_factory import LLMFactory
from utils.log_manager import LogManager
from utils.workstation_loader import WorkstationLoader

from .state import PreFlowAgentTestState
from .tools import KnowledgeQuery, ResearchQuery
from .prompts import (
    SYSTEM_PROMPT,
    KNOWLEDGE_LOOP_PROMPT,
    CONTEXT_SUMMARIZING_PROMPT,
    FORWARD_TRANSLATING_PROMPT,
)

logger = logging.getLogger(__name__)


class PreFlowAgent(BaseAgent):
    """Prepare downstream context from macro_plan plus temporary knowledge/memory."""

    MAX_KNOWLEDGE_ROUNDS = 3
    CONTEXT_CHAR_LIMIT = 2400
    KNOWLEDGE_CONTEXT_CHAR_LIMIT = 4200
    RELATED_WORKFLOW_CHAR_LIMIT = 1800
    REFERENCE_PREVIEW_CHAR_LIMIT = 1200
    OBSERVATION_PRIMARY_WORKSTATION = "dual-station-electrochemical-workstation"
    OBSERVATION_FUTURE_XRD_WORKSTATION = "xrd-workstation"

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

        self._use_knowledge_agent = use_knowledge_agent
        self._use_research_agent = use_research_agent
        self._exp_log_path = exp_log_path
        self._use_new_format = use_new_format
        self._knowledge_query = KnowledgeQuery(use_knowledge_agent=use_knowledge_agent)
        self._research_query = ResearchQuery(use_research_agent=use_research_agent)
        self._workstation_loader = WorkstationLoader(use_new_format=use_new_format)
        self._log_manager = None
        if exp_log_path:
            exp_id = os.path.basename(os.path.dirname(exp_log_path))
            self._log_manager = LogManager(exp_id)

    def run(self, state: PreFlowAgentTestState) -> PreFlowAgentTestState:
        logger.info("Starting Pre-Flow Agent")

        if self._log_manager is None and state.exp_log_path:
            exp_id = os.path.basename(os.path.dirname(state.exp_log_path))
            self._log_manager = LogManager(exp_id)

        try:
            self._step1_get_inputs(state)
            self._step2_iterative_knowledge_collection(state)
            self._step3_context_summarization(state)
            self._step4_query_memory(state)
            self._step5_synthesize_context(state)
            self._step6_update_log(state)
            state.status = "success"
        except Exception as exc:
            error_msg = f"Pre-Flow Agent failed: {exc}"
            logger.exception(error_msg)
            state.errors.append(error_msg)
            state.status = "failed"
            raise

        return state

    def _step1_get_inputs(self, state: PreFlowAgentTestState) -> None:
        macro_plan = state.macro_plan or state.final_goal
        state.macro_plan = macro_plan
        state.final_goal = state.final_goal or macro_plan

        workstation_descriptions = self._workstation_loader.get_relevant_operation_summary(macro_plan)
        state._input_data = {
            "macro_plan": macro_plan,
            "workstation_descriptions": workstation_descriptions,
            "reference_format": state.txt_format_reference,
        }

    def _step2_iterative_knowledge_collection(self, state: PreFlowAgentTestState) -> None:
        input_data = state._input_data
        rounds: List[Dict[str, Any]] = []
        prompts: List[str] = []
        accumulated_knowledge = ""
        issued_queries: List[str] = []

        for round_index in range(1, self.MAX_KNOWLEDGE_ROUNDS + 1):
            existing_knowledge = accumulated_knowledge or "无（当前尚未检索到额外 knowledge 片段）"
            task_prompt = KNOWLEDGE_LOOP_PROMPT.format(
                round_index=round_index,
                max_rounds=self.MAX_KNOWLEDGE_ROUNDS,
                macro_plan=input_data["macro_plan"],
                existing_knowledge=self._trim_text(existing_knowledge, self.KNOWLEDGE_CONTEXT_CHAR_LIMIT),
            )
            full_prompt = f"System Prompt:\n{SYSTEM_PROMPT}\n\nTask Prompt:\n{task_prompt}"
            prompts.append(full_prompt)

            raw_response = self._invoke_with_retry_direct([
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": task_prompt},
            ])
            parsed = self._parse_knowledge_loop_output(raw_response)

            query = parsed["knowledge_query"]
            search_result = ""
            if not parsed["knowledge_sufficient"]:
                if not query:
                    query = self._trim_text(input_data["macro_plan"], 400)
                if query not in issued_queries:
                    issued_queries.append(query)
                    search_result = self._knowledge_query.search(query=query)
                    accumulated_knowledge = self._merge_knowledge_context(accumulated_knowledge, search_result)

            round_record = {
                "round_index": round_index,
                "prompt": full_prompt,
                "llm_output": raw_response,
                "knowledge_sufficient": parsed["knowledge_sufficient"],
                "knowledge_assessment": parsed["knowledge_assessment"],
                "knowledge_gap": parsed["knowledge_gap"],
                "knowledge_query": query,
                "search_result": search_result,
            }
            rounds.append(round_record)

            if parsed["knowledge_sufficient"]:
                break
            if not search_result:
                # No new retrieval signal, so continuing the loop would only repeat context.
                break

        state.knowledge_decision_prompts = prompts
        state.knowledge_raw_inputs = {
            "query": issued_queries[-1] if issued_queries else "",
            "queries": issued_queries,
            "rounds": rounds,
            "search_result": accumulated_knowledge,
            "sources": self._knowledge_query.get_all(),
            "knowledge_sufficient": bool(rounds and rounds[-1]["knowledge_sufficient"]),
        }

    def _step3_context_summarization(self, state: PreFlowAgentTestState) -> None:
        input_data = state._input_data
        knowledge_context = state.knowledge_raw_inputs.get("search_result", "")

        task_prompt = CONTEXT_SUMMARIZING_PROMPT.format(
            macro_plan=input_data["macro_plan"],
            knowledge_context=self._trim_text(
                knowledge_context or "无（未检索到额外 knowledge 片段）",
                self.KNOWLEDGE_CONTEXT_CHAR_LIMIT,
            ),
            workstation_descriptions=input_data["workstation_descriptions"],
        )
        state.context_summary_prompt = f"System Prompt:\n{SYSTEM_PROMPT}\n\nTask Prompt:\n{task_prompt}"

        raw_response = self._invoke_with_retry_direct([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task_prompt},
        ])
        self._parse_context_summary_output(state, raw_response)
        state.knowledge_raw_inputs["final_summary_output"] = raw_response

    def _step4_query_memory(self, state: PreFlowAgentTestState) -> None:
        memory_query = state.memory_raw_inputs.get("query", "")
        memory_search_result = self._research_query.get_related_records(query=memory_query)
        state.memory_raw_inputs.update({
            "query": memory_query,
            "related_records": memory_search_result,
        })
        state.related_workflows_unformatted = memory_search_result

    def _step5_synthesize_context(self, state: PreFlowAgentTestState) -> None:
        input_data = state._input_data
        memory_search_result = state.memory_raw_inputs.get("related_records", "")
        knowledge_search_result = state.knowledge_raw_inputs.get("search_result", "")
        knowledge_queries = state.knowledge_raw_inputs.get("queries") or []
        knowledge_takeaways = state.knowledge_raw_inputs.get("knowledge_takeaways", "")

        task_prompt = FORWARD_TRANSLATING_PROMPT.format(
            macro_plan=input_data["macro_plan"],
            macro_plan_summary=state.macro_plan_summary,
            observation_requirements=self._format_observation_requirements(state.observation_requirements),
            memory_query=state.memory_raw_inputs.get("query", ""),
            knowledge_queries=json.dumps(knowledge_queries, ensure_ascii=False, indent=2) if knowledge_queries else "[]",
            memory_search_result=self._trim_text(memory_search_result, self.RELATED_WORKFLOW_CHAR_LIMIT),
            knowledge_search_result=self._trim_text(knowledge_search_result, self.CONTEXT_CHAR_LIMIT),
            knowledge_takeaways=self._trim_text(knowledge_takeaways, 900),
            reference_format=self._trim_text(input_data["reference_format"], self.REFERENCE_PREVIEW_CHAR_LIMIT),
        )
        state.forward_translating_prompt = f"System Prompt:\n{SYSTEM_PROMPT}\n\nTask Prompt:\n{task_prompt}"

        state.related_workflows_txt = self._build_related_workflows_txt(memory_search_result)
        state.knowledge = self._build_knowledge_context(state, knowledge_search_result)
        state.goal_in_this_iteration = self._build_goal_in_this_iteration(state)

    def _step6_update_log(self, state: PreFlowAgentTestState) -> None:
        if self._log_manager is None:
            return
        iteration_id = state.iteration_id
        self._log_manager.update_iteration(iteration_id, "macro_plan_summary", state.macro_plan_summary)
        self._log_manager.update_iteration(iteration_id, "observation_requirements", state.observation_requirements)
        self._log_manager.update_iteration(iteration_id, "knowledge_raw_inputs", state.knowledge_raw_inputs)
        self._log_manager.update_iteration(iteration_id, "memory_raw_inputs", state.memory_raw_inputs)
        self._log_manager.update_iteration(iteration_id, "knowledge", state.knowledge)
        self._log_manager.update_iteration(iteration_id, "related_workflows_unformatted", state.related_workflows_unformatted)
        self._log_manager.update_iteration(iteration_id, "goal_in_this_iteration", state.goal_in_this_iteration)
        self._log_manager.update_iteration(iteration_id, "related_workflows_txt", state.related_workflows_txt)

    def _parse_knowledge_loop_output(self, output: str) -> Dict[str, Any]:
        assessment = self._extract_section(
            output,
            "知识充分性判断",
            ("知识是否充分", "仍然缺失的知识", "下一次知识检索query"),
        ).strip()
        sufficient_text = self._extract_section(
            output,
            "知识是否充分",
            ("仍然缺失的知识", "下一次知识检索query"),
        ).strip()
        gap = self._extract_section(
            output,
            "仍然缺失的知识",
            ("下一次知识检索query",),
        ).strip()
        query = self._extract_section(output, "下一次知识检索query", ()).strip()

        knowledge_sufficient = self._normalize_yes_no(sufficient_text)
        if not assessment:
            assessment = "模型未明确给出知识充分性说明。"
        if not gap:
            gap = "无" if knowledge_sufficient else "未明确说明"
        if knowledge_sufficient:
            query = ""
        elif query in {"无", "none", "None"}:
            query = ""

        return {
            "knowledge_assessment": assessment,
            "knowledge_sufficient": knowledge_sufficient,
            "knowledge_gap": gap,
            "knowledge_query": query,
        }

    def _parse_context_summary_output(self, state: PreFlowAgentTestState, output: str) -> None:
        state.macro_plan_summary = self._extract_section(
            output,
            "macro_plan摘要",
            ("观察要求", "memory_query", "knowledge_takeaways"),
        ).strip()
        observation_text = self._extract_section(
            output,
            "观察要求",
            ("memory_query", "knowledge_takeaways"),
        ).strip()
        memory_query = self._extract_section(
            output,
            "memory_query",
            ("knowledge_takeaways",),
        ).strip()
        knowledge_takeaways = self._extract_section(output, "knowledge_takeaways", ()).strip()

        if not state.macro_plan_summary:
            state.macro_plan_summary = self._trim_text(state.macro_plan, 300)
        if not observation_text:
            observation_text = self._infer_observation_text(state.macro_plan)
        if not memory_query:
            memory_query = state.macro_plan_summary
        if not knowledge_takeaways:
            knowledge_takeaways = self._trim_text(
                state.knowledge_raw_inputs.get("search_result", "") or "无额外 knowledge 要点。",
                900,
            )

        state.observation_requirements = self._parse_observation_requirements(observation_text)
        state.memory_raw_inputs = {"query": memory_query}
        state.knowledge_raw_inputs["knowledge_takeaways"] = knowledge_takeaways

    def _build_goal_in_this_iteration(self, state: PreFlowAgentTestState) -> str:
        summary = self._trim_text(state.macro_plan_summary or state.macro_plan, 600)
        lines = [summary]
        if state.observation_requirements.get("needs_observation"):
            locations = state.observation_requirements.get("suggested_locations") or []
            if locations:
                location_text = "、".join(locations)
            else:
                location_text = "按 macro_plan 中提到的关键步骤进行观察"
            lines.append(f"执行中需要观察，建议观察位置：{location_text}。")
        else:
            lines.append("执行中无额外 observation 强制要求，除非下游工作站描述文件另有明确约束。")
        return "\n".join(lines)

    def _build_related_workflows_txt(self, memory_search_result: str) -> str:
        normalized = self._normalize_text(memory_search_result)
        if not normalized:
            return "无可直接复用的历史实验记录。"
        return self._trim_text(normalized, self.RELATED_WORKFLOW_CHAR_LIMIT)

    def _build_knowledge_context(self, state: PreFlowAgentTestState, knowledge_search_result: str) -> str:
        sections = []

        summary = self._trim_text(state.macro_plan_summary or state.macro_plan, 500)
        if summary:
            sections.append(f"### macro_plan摘要\n{summary}")

        observation_text = self._format_observation_requirements(state.observation_requirements)
        sections.append(f"### 观察要求\n{self._trim_text(observation_text, 500)}")

        knowledge_takeaways = self._normalize_text(state.knowledge_raw_inputs.get("knowledge_takeaways", ""))
        if knowledge_takeaways:
            sections.append(f"### knowledge要点\n{self._trim_text(knowledge_takeaways, 1000)}")

        normalized_knowledge = self._normalize_text(knowledge_search_result)
        if normalized_knowledge:
            sections.append(
                f"### 临时knowledge检索结果\n{self._trim_text(normalized_knowledge, self.CONTEXT_CHAR_LIMIT)}"
            )

        source_catalog = self._render_source_catalog(state.knowledge_raw_inputs.get("sources", {}))
        if source_catalog:
            sections.append(f"### 当前可用临时knowledge源\n{source_catalog}")

        return "\n\n".join(section for section in sections if section).strip()

    def _render_source_catalog(self, sources: Dict[str, Any]) -> str:
        if not sources:
            return ""
        lines = []
        for source_name, source_text in sources.items():
            normalized = self._normalize_text(str(source_text or ""))
            if not normalized:
                continue
            preview = self._trim_text(normalized, 180)
            lines.append(f"- {source_name}: {preview}")
        return "\n".join(lines)

    def _merge_knowledge_context(self, existing: str, incoming: str) -> str:
        seen = set()
        merged_blocks = []
        for text in (existing, incoming):
            if not text:
                continue
            for block in re.split(r"\n{2,}(?=###\s)", text.strip()):
                normalized = block.strip()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                merged_blocks.append(normalized)
        merged = "\n\n".join(merged_blocks)
        return self._trim_text(merged, self.KNOWLEDGE_CONTEXT_CHAR_LIMIT)

    def _normalize_yes_no(self, text: str) -> bool:
        lowered = str(text or "").strip().lower()
        return lowered in {"yes", "true", "是", "充分", "已充分", "足够", "足够了"}

    def _extract_section(self, text: str, heading: str, next_headings: Tuple[str, ...]) -> str:
        start_pattern = rf"###\s*{re.escape(heading)}\s*"
        start_match = re.search(start_pattern, text)
        if not start_match:
            return ""
        start = start_match.end()
        end = len(text)
        for next_heading in next_headings:
            next_pattern = rf"###\s*{re.escape(next_heading)}\s*"
            next_match = re.search(next_pattern, text[start:])
            if next_match:
                end = start + next_match.start()
                break
        return text[start:end].strip()

    def _parse_observation_requirements(self, text: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {"raw_text": text.strip()}
        for line in text.splitlines():
            stripped = line.strip().lstrip("-").strip()
            if not stripped:
                continue
            if "：" in stripped:
                key, value = stripped.split("：", 1)
            elif ":" in stripped:
                key, value = stripped.split(":", 1)
            else:
                continue
            result[key.strip()] = value.strip()

        need_text = str(result.get("是否需要观察", "否")).strip().lower()
        result["needs_observation"] = need_text in {"是", "需要", "yes", "true"}

        locations_raw = str(result.get("观察位置建议", ""))
        normalized_locations = []
        for item in re.split(r"[，,；;、/]+", locations_raw):
            stripped = item.strip()
            if not stripped or stripped in {"无", "未明确给出"}:
                continue
            normalized = self._normalize_observation_location(stripped)
            if normalized and normalized not in normalized_locations:
                normalized_locations.append(normalized)
        if result["needs_observation"] and not normalized_locations:
            normalized_locations = [self.OBSERVATION_PRIMARY_WORKSTATION]
        result["suggested_locations"] = normalized_locations
        result["allowed_observation_workstations"] = [self.OBSERVATION_PRIMARY_WORKSTATION]
        result["observation_constraint_note"] = (
            "观察点只能落在 observation-capable 工作站上。"
            f"当前真源目录中实际存在且可确认的 observation-capable 工作站是 {self.OBSERVATION_PRIMARY_WORKSTATION}；"
            "若未来真源中加入 XRD/XDR 工作站，也只能在这些工作站中选点。"
        )
        return result

    def _normalize_observation_location(self, location: str) -> str:
        lowered = location.lower()
        if "电化学" in location or "electrochemical" in lowered:
            return self.OBSERVATION_PRIMARY_WORKSTATION
        if "xrd" in lowered or "xdr" in lowered or "衍射" in location:
            return self.OBSERVATION_FUTURE_XRD_WORKSTATION
        return ""

    def _infer_observation_text(self, macro_plan: str) -> str:
        observe_keywords = ["观察", "表征", "检测", "颜色", "沉淀", "电化学"]
        if any(keyword in macro_plan for keyword in observe_keywords):
            return "\n".join([
                "- 是否需要观察：是",
                f"- 观察位置建议：{self.OBSERVATION_PRIMARY_WORKSTATION}",
                "- 观察内容：记录颜色、沉淀、液位或其他显著现象",
                "- 观察目的：为下游 workflow 提供显式观察要求；观察点只能落在 observation-capable 工作站上",
            ])
        return "\n".join([
            "- 是否需要观察：否",
            "- 观察位置建议：无",
            "- 观察内容：无",
            "- 观察目的：无",
        ])

    def _format_observation_requirements(self, observation_requirements: Dict[str, Any]) -> str:
        if not observation_requirements:
            return "无"
        return json.dumps(observation_requirements, ensure_ascii=False, indent=2)

    def _normalize_text(self, text: str) -> str:
        normalized = re.sub(r"\n{3,}", "\n\n", str(text or "").strip())
        return normalized

    def _trim_text(self, text: str, max_chars: int) -> str:
        normalized = self._normalize_text(text)
        if len(normalized) <= max_chars:
            return normalized
        return normalized[:max_chars].rstrip() + "\n...[truncated]"

    def _coerce_text_content(self, content) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
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
