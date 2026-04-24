"""Workflow implementation for the partially implemented research agent."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Sequence

from .core import BaseAgent
from .prompts import (
    BOOTSTRAP_SYSTEM_PROMPT,
    MACRO_PLAN_DESIGN_PROMPT,
    SIMILAR_EXP_SEARCH_PROMPT,
    STAGE_DESIGN_PROMPT,
    SURVEY_EXPANSION_PROMPT,
    SURVEY_QUERY_GENERATE_PROMPT,
    SURVEY_REPORT_GENERATE_PROMPT,
)
from .state import ResearchAgentState, ResearchEvent, SearchHit
from .tools import KnowledgeQuery, MemoryQuery
from .utils import LLMFactory

logger = logging.getLogger(__name__)


class ResearchAgent(BaseAgent):
    """Research agent runtime with B0 waiting and B1 bootstrap implemented."""

    def __init__(
        self,
        model: Any = None,
        use_llm: bool | None = None,
        corpus_dir: str | None = None,
        knowledge_base_dir: str | None = None,
        memory_dir: str | None = None,
        max_survey_rounds: int = 2,
        knowledge_top_k: int = 5,
        memory_top_k: int = 3,
    ) -> None:
        if model is None:
            model = LLMFactory.create_or_none()

        super().__init__(model=model)
        self._use_llm = bool(model) if use_llm is None else bool(model) and use_llm
        self._max_survey_rounds = max_survey_rounds
        resolved_knowledge_dir = (
            knowledge_base_dir
            or os.getenv("RESEARCH_KNOWLEDGE_BASE_DIR")
            or corpus_dir
        )
        resolved_memory_dir = (
            memory_dir
            or os.getenv("RESEARCH_MEMORY_DIR")
            or resolved_knowledge_dir
            or corpus_dir
        )
        self._knowledge_query = KnowledgeQuery(
            corpus_dir=resolved_knowledge_dir,
            top_k=knowledge_top_k,
        )
        self._memory_query = MemoryQuery(
            corpus_dir=resolved_memory_dir,
            top_k=memory_top_k,
        )

    def run(
        self,
        event_type: str,
        query: str = "",
        constraints: Dict[str, Any] | None = None,
        payload: Dict[str, Any] | None = None,
    ) -> ResearchAgentState:
        event = ResearchEvent(
            event_type=event_type,
            query=query,
            constraints=constraints or {},
            payload=payload or {},
        )
        return self.run_event(event)

    def run_event(self, event: ResearchEvent) -> ResearchAgentState:
        state = ResearchAgentState(event=event)
        state.status = "running"
        state.add_log(
            f"ResearchAgent started with event={event.event_type}, llm_enabled={self._use_llm}"
        )
        return self._run_b0(state)

    def _run_b0(self, state: ResearchAgentState) -> ResearchAgentState:
        state.current_branch = "B0"
        if not state.branch_history or state.branch_history[-1] != "B0":
            state.branch_history.append("B0")
        state.add_log(f"B0 waiting received event: {state.event.event_type}")

        if state.event.event_type == "bootstrap":
            state.next_branch = "B1"
            state.route_message = "bootstrap event received; routing to B1"
            state.add_log(state.route_message)
            return self._run_b1(state)

        state.status = "not_implemented"
        state.next_branch = None
        state.route_message = (
            f"当前阶段仅实现 B0/B1；事件 {state.event.event_type} 暂未实现。"
        )
        state.add_log(state.route_message)
        return state

    def _run_b1(self, state: ResearchAgentState) -> ResearchAgentState:
        state.current_branch = "B1"
        if state.branch_history[-1] != "B1":
            state.branch_history.append("B1")
        state.add_log("Entered B1 bootstrap")

        try:
            state.survey_queries = self._step_survey_query_generate(state)
            state.add_log(f"survey query generate completed with {len(state.survey_queries)} queries")

            accumulated_hits: List[SearchHit] = []
            query_queue = list(state.survey_queries)
            seen_queries = set()

            for round_index in range(1, self._max_survey_rounds + 1):
                query_queue = [query for query in query_queue if query not in seen_queries]
                if not query_queue:
                    break

                seen_queries.update(query_queue)
                round_hits = self._knowledge_query.search(query_queue)
                if not round_hits and round_index == 1:
                    fallback_hits = self._knowledge_query.search(
                        ["普鲁士蓝 类似物 合成", "Prussian Blue analogue synthesis"]
                    )
                    round_hits = fallback_hits

                accumulated_hits = self._merge_hits(accumulated_hits, round_hits)
                state.survey_rounds.append(
                    {
                        "round": round_index,
                        "queries": list(query_queue),
                        "hit_titles": [hit.title for hit in round_hits],
                    }
                )
                state.add_log(
                    f"Survey round {round_index} produced {len(round_hits)} hits; "
                    f"{len(accumulated_hits)} unique hits accumulated"
                )

                expansion = self._step_survey_expansion(state, accumulated_hits)
                if not expansion["continue_research"]:
                    break
                query_queue = expansion["new_queries"]

            state.knowledge_hits = accumulated_hits
            state.memory_queries = self._step_similar_exp_search(state)
            state.memory_hits = self._memory_query.search(state.memory_queries)
            state.add_log(
                f"similar exp search completed with {len(state.memory_queries)} queries and "
                f"{len(state.memory_hits)} memory hits"
            )

            state.survey_report = self._step_survey_report_generate(state)
            stage_design = self._step_stage_design(state)
            state.stage_route = stage_design["stage_route"]
            state.current_stage = stage_design["current_stage"]
            state.stage_route_reason = stage_design["stage_route_reason"]
            state.current_stage_reason = stage_design["current_stage_reason"]

            macro_design = self._step_macro_plan_design(state)
            state.current_stage_plan = macro_design["current_stage_plan"]
            state.macro_plan = macro_design["macro_plan"]

            state.persistent_outputs = state.research_layer_internal_outputs()
            state.device_adaptation_handoff = state.device_adaptation_external_handoff()

            state.last_completed_branch = "B1"
            state.current_branch = "B0"
            state.next_branch = "B0"
            if state.branch_history[-1] != "B0":
                state.branch_history.append("B0")
            state.status = "completed"
            state.route_message = "B1 bootstrap completed; stage route and first macro plan are ready"
            state.add_log(state.route_message)
            return state

        except Exception as exc:
            logger.exception("B1 bootstrap failed")
            state.add_error(f"B1 bootstrap failed: {exc}")
            state.status = "manual_required"
            state.current_branch = "B1"
            state.next_branch = "B8"
            state.route_message = "bootstrap unresolved; manual intervention required"
            state.add_log(state.route_message)
            return state

    def _step_survey_query_generate(self, state: ResearchAgentState) -> List[str]:
        constraints_json = json.dumps(state.event.constraints, ensure_ascii=False, indent=2)
        if self._use_llm:
            try:
                result = self.invoke_json(
                    BOOTSTRAP_SYSTEM_PROMPT,
                    SURVEY_QUERY_GENERATE_PROMPT.format(
                        query=state.event.query,
                        constraints_json=constraints_json,
                    ),
                )
                state.raw_llm_outputs["survey_query_generate"] = result
                queries = self._clean_queries(result.get("queries", []))
                if queries:
                    return queries
            except Exception as exc:  # pragma: no cover - depends on remote model
                logger.warning("survey query generate failed, falling back to heuristics: %s", exc)
                state.add_log(f"survey query generate LLM failed, fallback used: {exc}")

        return self._heuristic_survey_queries(state.event.query, state.event.constraints)

    def _step_survey_expansion(
        self, state: ResearchAgentState, accumulated_hits: Sequence[SearchHit]
    ) -> Dict[str, Any]:
        knowledge_context = self._knowledge_query.format_context(accumulated_hits)
        if self._use_llm:
            try:
                result = self.invoke_json(
                    BOOTSTRAP_SYSTEM_PROMPT,
                    SURVEY_EXPANSION_PROMPT.format(
                        query=state.event.query,
                        knowledge_context=knowledge_context or "当前没有命中结果",
                    ),
                )
                state.raw_llm_outputs.setdefault("survey_expansion", []).append(result)
                cleaned_queries = self._clean_queries(result.get("new_queries", []))
                return {
                    "continue_research": bool(result.get("continue_research")),
                    "new_queries": cleaned_queries,
                    "reason": result.get("reason", ""),
                }
            except Exception as exc:  # pragma: no cover - depends on remote model
                logger.warning("survey expansion failed, falling back to heuristics: %s", exc)
                state.add_log(f"survey expansion LLM failed, fallback used: {exc}")

        return self._heuristic_survey_expansion(state.event.query, accumulated_hits, state.survey_rounds)

    def _step_similar_exp_search(self, state: ResearchAgentState) -> List[str]:
        knowledge_context = self._knowledge_query.format_context(state.knowledge_hits[:3])
        if self._use_llm:
            try:
                result = self.invoke_json(
                    BOOTSTRAP_SYSTEM_PROMPT,
                    SIMILAR_EXP_SEARCH_PROMPT.format(
                        query=state.event.query,
                        knowledge_context=knowledge_context or "当前没有命中结果",
                    ),
                )
                state.raw_llm_outputs["similar_exp_search"] = result
                queries = self._clean_queries(result.get("queries", []))
                if queries:
                    return queries
            except Exception as exc:  # pragma: no cover - depends on remote model
                logger.warning("similar exp search failed, falling back to heuristics: %s", exc)
                state.add_log(f"similar exp search LLM failed, fallback used: {exc}")

        return self._heuristic_memory_queries(state.event.query, state.knowledge_hits)

    def _step_survey_report_generate(self, state: ResearchAgentState) -> Dict[str, Any]:
        knowledge_context = self._knowledge_query.format_context(state.knowledge_hits[:5])
        memory_context = self._memory_query.format_context(state.memory_hits[:3])
        if self._use_llm:
            try:
                result = self.invoke_json(
                    BOOTSTRAP_SYSTEM_PROMPT,
                    SURVEY_REPORT_GENERATE_PROMPT.format(
                        query=state.event.query,
                        knowledge_context=knowledge_context or "当前没有知识命中",
                        memory_context=memory_context or "当前没有历史案例命中",
                    ),
                )
                state.raw_llm_outputs["survey_report_generate"] = result
                if result.get("summary"):
                    return result
            except Exception as exc:  # pragma: no cover - depends on remote model
                logger.warning("survey report generate failed, using heuristic report: %s", exc)
                state.add_log(f"survey report generate LLM failed, fallback used: {exc}")

        return self._heuristic_survey_report(state.event.query, state.knowledge_hits, state.memory_hits)

    def _step_stage_design(self, state: ResearchAgentState) -> Dict[str, Any]:
        if self._use_llm:
            try:
                result = self.invoke_json(
                    BOOTSTRAP_SYSTEM_PROMPT,
                    STAGE_DESIGN_PROMPT.format(
                        query=state.event.query,
                        survey_report_json=json.dumps(state.survey_report, ensure_ascii=False, indent=2),
                    ),
                )
                state.raw_llm_outputs["stage_design"] = result
                route = result.get("stage_route", [])
                current_stage = result.get("current_stage", "")
                if route and current_stage in route:
                    return self._normalize_stage_design(
                        query=state.event.query,
                        survey_report=state.survey_report,
                        stage_design={
                            "stage_route": route,
                            "current_stage": current_stage,
                            "stage_route_reason": result.get("stage_route_reason", ""),
                            "current_stage_reason": result.get("current_stage_reason", ""),
                        },
                    )
            except Exception as exc:  # pragma: no cover - depends on remote model
                logger.warning("stage design failed, falling back to heuristics: %s", exc)
                state.add_log(f"stage design LLM failed, fallback used: {exc}")

        return self._normalize_stage_design(
            query=state.event.query,
            survey_report=state.survey_report,
            stage_design=self._heuristic_stage_design(
                state.event.query,
                state.knowledge_hits,
                state.survey_report,
            ),
        )

    def _step_macro_plan_design(self, state: ResearchAgentState) -> Dict[str, Any]:
        reference_context = self._knowledge_query.format_context(state.knowledge_hits[:2])
        if self._use_llm:
            try:
                result = self.invoke_json(
                    BOOTSTRAP_SYSTEM_PROMPT,
                    MACRO_PLAN_DESIGN_PROMPT.format(
                        query=state.event.query,
                        survey_report_json=json.dumps(state.survey_report, ensure_ascii=False, indent=2),
                        stage_route_json=json.dumps(state.stage_route, ensure_ascii=False, indent=2),
                        current_stage=state.current_stage,
                        stage_route_reason=state.stage_route_reason,
                        current_stage_reason=state.current_stage_reason,
                        reference_context=reference_context or "当前没有可用参考案例",
                    ),
                )
                state.raw_llm_outputs["macro_plan_design"] = result
                macro_plan = self._normalize_macro_plan(result.get("macro_plan", []))
                if macro_plan:
                    current_stage_plan = self._ensure_stage_plan_mentions_observation(
                        state,
                        result.get("current_stage_plan", "").strip(),
                    )
                    macro_plan = self._ensure_macro_plan_reaches_observation(state, macro_plan)
                    return {
                        "current_stage_plan": current_stage_plan,
                        "macro_plan": macro_plan,
                    }
            except Exception as exc:  # pragma: no cover - depends on remote model
                logger.warning("macro plan design failed, falling back to heuristics: %s", exc)
                state.add_log(f"macro plan design LLM failed, fallback used: {exc}")

        heuristic_design = self._heuristic_macro_plan_design(state)
        heuristic_design["current_stage_plan"] = self._ensure_stage_plan_mentions_observation(
            state,
            heuristic_design.get("current_stage_plan", "").strip(),
        )
        heuristic_design["macro_plan"] = self._ensure_macro_plan_reaches_observation(
            state,
            heuristic_design.get("macro_plan", []),
        )
        return heuristic_design

    def _merge_hits(self, existing: Sequence[SearchHit], new_hits: Sequence[SearchHit]) -> List[SearchHit]:
        merged: Dict[str, SearchHit] = {hit.file_path: hit for hit in existing}
        for hit in new_hits:
            if hit.file_path not in merged or hit.score > merged[hit.file_path].score:
                merged[hit.file_path] = hit
        return sorted(merged.values(), key=lambda hit: (-hit.score, hit.title))

    def _clean_queries(self, queries: Iterable[Any]) -> List[str]:
        cleaned: List[str] = []
        seen = set()
        for item in queries:
            query = str(item).strip()
            if query and query not in seen:
                cleaned.append(query)
                seen.add(query)
        return cleaned

    def _infer_observation_points(
        self,
        query: str,
        survey_report: Dict[str, Any] | None = None,
    ) -> List[str]:
        context_blob = " ".join(
            [
                query or "",
                json.dumps(survey_report or {}, ensure_ascii=False),
            ]
        ).lower()
        observation_catalog = [
            ("XRD", ["xrd", "x-ray diffraction", "衍射"]),
            ("XPS", ["xps", "photoelectron"]),
            ("Raman", ["raman"]),
            ("FTIR", ["ftir", "infrared", "红外"]),
            ("SEM", ["sem"]),
            ("TEM", ["tem"]),
            ("BET", ["bet"]),
            ("UV-Vis", ["uv-vis", "uv vis", "紫外"]),
            ("电化学测试", ["lsv", "cv", "eis", "电化学", "oer", "her"]),
        ]

        observations: List[str] = []
        for label, keywords in observation_catalog:
            if any(keyword in context_blob for keyword in keywords):
                observations.append(label)

        if not observations and any(token in context_blob for token in ["观察", "表征", "测试", "分析"]):
            observations.append("首次结果观察")

        return observations

    def _infer_target_material(self, query: str) -> str:
        query = query.strip()
        patterns = [
            r"合成(.+?)(?:并|并且|后|，|,|。|$)",
            r"制备(.+?)(?:并|并且|后|，|,|。|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, query)
            if match:
                candidate = match.group(1).strip(" 的结果工作站测试表征观察")
                if candidate:
                    return candidate
        return "目标样品"

    def _build_observation_stage_route(self, query: str, observations: Sequence[str]) -> List[str]:
        material = self._infer_target_material(query)
        if not observations:
            return []
        route: List[str] = []
        for index, observation in enumerate(observations):
            if index == 0:
                route.append(f"合成{material}并完成 {observation} 观察")
            else:
                route.append(f"基于前序结果继续推进并完成 {observation} 观察")
        return route

    def _normalize_stage_design(
        self,
        query: str,
        survey_report: Dict[str, Any],
        stage_design: Dict[str, Any],
    ) -> Dict[str, Any]:
        observations = self._infer_observation_points(query, survey_report)
        if not observations:
            return stage_design

        route = self._build_observation_stage_route(query, observations)
        current_stage = route[0]
        return {
            "stage_route": route,
            "current_stage": current_stage,
            "stage_route_reason": (
                "stage 应以 observation point 为边界来划分，而不是按工艺动作切段。"
                f"当前 query 识别到的 observation point 为：{'、'.join(observations)}，"
                "因此 stage_route 按这些观察点组织。"
            ),
            "current_stage_reason": (
                f"当前还没有得到第一个 observation point（{observations[0]}）的结果，"
                "所以当前 stage 应覆盖从起点到该观察点之前的完整实验流程。"
            ),
        }

    def _ensure_stage_plan_mentions_observation(
        self,
        state: ResearchAgentState,
        current_stage_plan: str,
    ) -> str:
        observations = self._infer_observation_points(state.event.query, state.survey_report)
        if not observations:
            return current_stage_plan

        primary_observation = observations[0]
        if primary_observation.lower() in current_stage_plan.lower():
            return current_stage_plan

        suffix = f"该 stage 的终点 observation point 为 {primary_observation} 结果。"
        return f"{current_stage_plan} {suffix}".strip()

    def _ensure_macro_plan_reaches_observation(
        self,
        state: ResearchAgentState,
        macro_plan: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        observations = self._infer_observation_points(state.event.query, state.survey_report)
        if not observations:
            return macro_plan

        primary_observation = observations[0]
        plan_blob = " ".join(
            str(step.get("操作", "")) + " " + str(step.get("试剂/对象", "")) + " " + str(step.get("参数", ""))
            for step in macro_plan
        ).lower()

        if primary_observation == "XRD" and "xrd" not in plan_blob:
            if "洗涤" not in plan_blob and "干燥" not in plan_blob:
                macro_plan.append(
                    {
                        "步骤序号": len(macro_plan) + 1,
                        "操作": "洗涤并干燥样品",
                        "试剂/对象": "反应得到的普鲁士蓝沉淀",
                        "参数": "离心收集后用去离子水洗涤至上清液基本澄清，再进行低温干燥以获得可测粉末",
                    }
                )
            macro_plan.extend(
                [
                    {
                        "步骤序号": len(macro_plan) + 1,
                        "操作": "制备 XRD 测试样品",
                        "试剂/对象": "干燥后的普鲁士蓝粉末",
                        "参数": "将样品研磨并均匀铺展在样品台上，保证表面平整以满足粉末衍射测试要求",
                    },
                    {
                        "步骤序号": len(macro_plan) + 2,
                        "操作": "执行 XRD 观察",
                        "试剂/对象": "XRD 工作站、普鲁士蓝样品",
                        "参数": "采集样品的粉末 XRD 图谱，并用于后续物相与峰位分析",
                    },
                ]
            )

        return self._normalize_macro_plan(macro_plan)

    def _heuristic_survey_queries(
        self, query: str, constraints: Dict[str, Any] | None = None
    ) -> List[str]:
        base_queries = [
            query,
            f"{query} synthesis",
            f"{query} key parameters",
            f"{query} structure characterization",
            f"{query} performance",
        ]
        if constraints:
            joined_constraints = " ".join(f"{key} {value}" for key, value in constraints.items())
            if joined_constraints.strip():
                base_queries.append(f"{query} {joined_constraints}")

        if "普鲁士蓝" not in query and "PBA" not in query.upper():
            base_queries.append("普鲁士蓝 类似物 合成")

        return self._clean_queries(base_queries)[:6]

    def _heuristic_survey_expansion(
        self,
        query: str,
        accumulated_hits: Sequence[SearchHit],
        survey_rounds: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if len(accumulated_hits) >= 3 or len(survey_rounds) >= self._max_survey_rounds:
            return {
                "continue_research": False,
                "new_queries": [],
                "reason": "已有知识已足够支撑初始 stage 设计。",
            }

        candidate_queries = self._clean_queries(
            [
                f"{query} mechanism",
                f"{query} optimization route",
                f"{query} activation process",
            ]
        )
        return {
            "continue_research": bool(candidate_queries),
            "new_queries": candidate_queries,
            "reason": "当前知识命中数量偏少，补充机理与优化方向检索。",
        }

    def _heuristic_memory_queries(
        self, query: str, knowledge_hits: Sequence[SearchHit]
    ) -> List[str]:
        queries = [query, f"{query} similar experiment"]
        for hit in knowledge_hits[:2]:
            queries.append(hit.title)
        return self._clean_queries(queries)[:4]

    def _heuristic_survey_report(
        self,
        query: str,
        knowledge_hits: Sequence[SearchHit],
        memory_hits: Sequence[SearchHit],
    ) -> Dict[str, Any]:
        top_titles = [hit.title for hit in knowledge_hits[:3]]
        common_operations = self._collect_common_operations(knowledge_hits[:3])
        summary = (
            f"围绕“{query}”的本地知识检索共命中 {len(knowledge_hits)} 个相关案例。"
            f"最相关的参考包括：{'；'.join(top_titles) if top_titles else '暂无高置信案例'}。"
            f"这些案例中反复出现的核心实验动作包括：{'、'.join(common_operations) if common_operations else '前驱体配置、反应和后处理'}。"
            "它们表明，bootstrap 阶段通常应先从可复用的合成路线入手，再视是否涉及电极构筑、活化或性能验证决定后续 stage。"
        )

        return {
            "summary": summary,
            "key_findings": [
                f"最相近的本地案例数：{len(knowledge_hits)}",
                f"高频实验动作：{'、'.join(common_operations[:5]) if common_operations else '未识别'}",
                f"历史可参考案例数：{len(memory_hits)}",
            ],
            "candidate_precedents": top_titles,
            "route_implications": [
                "优先沿用已有案例中的成熟合成步骤作为首个 macro plan。",
                "若案例包含电极构筑或活化步骤，可在后续 stage 中显式拆出。",
            ],
            "open_questions": [
                "需要在哪个观察点对当前路线做第一次结果判断。",
                "哪些性能目标应放到后续 stage，而不是首个 bootstrap macro plan。",
            ],
        }

    def _heuristic_stage_design(
        self,
        query: str,
        knowledge_hits: Sequence[SearchHit],
        survey_report: Dict[str, Any],
    ) -> Dict[str, Any]:
        observations = self._infer_observation_points(query, survey_report)
        if observations:
            route = self._build_observation_stage_route(query, observations)
            return {
                "stage_route": route,
                "current_stage": route[0],
                "stage_route_reason": (
                    "根据 query 中识别到的 observation point 来划分 stage。"
                    f"当前 observation point 为：{'、'.join(observations)}。"
                ),
                "current_stage_reason": (
                    f"当前需要先推进到第一个 observation point（{observations[0]}），"
                    "因此当前 stage 应覆盖到该观察点之前的完整实验段。"
                ),
            }

        operations_blob = " ".join(
            str(step.get("操作", "")) + " " + str(step.get("试剂/对象", ""))
            for hit in knowledge_hits[:3]
            for step in hit.steps
        )

        route = ["前驱体/目标材料合成"]
        if any(keyword in operations_blob for keyword in ["电极", "涂覆", "浆料", "FTO", "NF", "nickel foam"]):
            route.append("电极构筑与样品制备")
        else:
            route.append("结构与组成确认")

        if any(keyword in operations_blob for keyword in ["活化", "测试", "表征", "OER", "HER", "XRD"]):
            route.append("活化、观察与性能验证")
        else:
            route.append("性能验证与后续优化")

        return {
            "stage_route": route,
            "current_stage": route[0],
            "stage_route_reason": (
                "根据本地案例的重复流程，bootstrap 阶段通常先完成材料或前驱体合成，"
                "再进入样品确认/电极构筑，最后再做活化、观察或性能验证。"
            ),
            "current_stage_reason": (
                "当前尚无本轮实验的真实 observation，因此最合理的起点是先建立首个可执行的合成 stage。"
            ),
        }

    def _heuristic_macro_plan_design(self, state: ResearchAgentState) -> Dict[str, Any]:
        reference_hit = state.knowledge_hits[0] if state.knowledge_hits else None
        if reference_hit is None:
            fallback_plan = self._synthesize_macro_plan_from_context(state, None)
            return {
                "current_stage_plan": "当前没有命中本地案例，无法自动生成高置信度的完整 stage 计划。",
                "macro_plan": fallback_plan,
            }

        stage_steps = self._select_stage_steps(reference_hit.steps, state.current_stage)
        macro_plan = self._normalize_macro_plan(stage_steps)
        if not macro_plan:
            macro_plan = self._synthesize_macro_plan_from_context(state, reference_hit)

        operation_preview = " -> ".join(
            str(step.get("操作", "")) for step in macro_plan[:5] if step.get("操作")
        )
        current_stage_plan = (
            f"当前 stage 以参考案例《{reference_hit.title}》为主线，"
            f"优先完成以下实验语义步骤：{operation_preview or '步骤待补充'}。"
            f"阶段目标是在不进入后续性能验证前先得到可用于下一观察点的样品或中间体。"
        )

        return {
            "current_stage_plan": current_stage_plan,
            "macro_plan": macro_plan,
        }

    def _synthesize_macro_plan_from_context(
        self,
        state: ResearchAgentState,
        reference_hit: SearchHit | None,
    ) -> List[Dict[str, Any]]:
        context_blob = " ".join(
            [
                state.event.query,
                state.current_stage,
                reference_hit.title if reference_hit else "",
                reference_hit.problem if reference_hit else "",
                reference_hit.synthesis_summary if reference_hit else "",
            ]
        ).lower()

        if "普鲁士蓝" in context_blob or "prussian blue" in context_blob:
            if "xrd" in context_blob and any(
                token in state.current_stage for token in ["XRD", "结构", "表征", "鉴定"]
            ):
                return [
                    {
                        "步骤序号": 1,
                        "操作": "配制铁源前驱体溶液",
                        "试剂/对象": "FeCl3、去离子水",
                        "参数": "称取适量 FeCl3 溶于去离子水，配制成浓度约为 0.05-0.1 mol/L 的澄清溶液",
                    },
                    {
                        "步骤序号": 2,
                        "操作": "配制六氰合铁酸盐溶液",
                        "试剂/对象": "K4[Fe(CN)6]、去离子水",
                        "参数": "称取等摩尔比的 K4[Fe(CN)6] 溶于去离子水，配制成浓度约为 0.05-0.1 mol/L 的反应底液",
                    },
                    {
                        "步骤序号": 3,
                        "操作": "进行液相共沉淀反应",
                        "试剂/对象": "FeCl3 溶液、K4[Fe(CN)6] 溶液",
                        "参数": "在室温搅拌条件下将 FeCl3 溶液缓慢滴加至 K4[Fe(CN)6] 溶液中，观察深蓝色沉淀形成",
                    },
                    {
                        "步骤序号": 4,
                        "操作": "陈化并促进晶体生长",
                        "试剂/对象": "反应后的普鲁士蓝悬浊液",
                        "参数": "滴加结束后继续搅拌并静置 2-4 小时，以提高结晶度",
                    },
                    {
                        "步骤序号": 5,
                        "操作": "洗涤并干燥样品",
                        "试剂/对象": "普鲁士蓝沉淀样品",
                        "参数": "离心收集产物后，用去离子水洗涤至上清液接近无色，再进行低温干燥处理",
                    },
                    {
                        "步骤序号": 6,
                        "操作": "制备并执行 XRD 观察",
                        "试剂/对象": "干燥后的普鲁士蓝样品、XRD 工作站",
                        "参数": "将样品研磨并铺展在样品台上，采集粉末 XRD 图谱用于目标物相判定",
                    },
                ]

            if any(token in state.current_stage for token in ["洗涤", "干燥", "纯化"]):
                return [
                    {
                        "步骤序号": 1,
                        "操作": "离心分离产物",
                        "试剂/对象": "反应后的普鲁士蓝悬浊液",
                        "参数": "采用离心方式分离固液相，保留深蓝色沉淀",
                    },
                    {
                        "步骤序号": 2,
                        "操作": "多轮洗涤",
                        "试剂/对象": "去离子水、乙醇、普鲁士蓝沉淀",
                        "参数": "交替使用去离子水与乙醇洗涤 2-3 次，以去除未反应前驱体和可溶性杂质",
                    },
                    {
                        "步骤序号": 3,
                        "操作": "低温干燥",
                        "试剂/对象": "洗涤后的普鲁士蓝湿样",
                        "参数": "在真空或鼓风条件下于 50-60℃ 干燥至获得稳定蓝色粉末",
                    },
                ]

            return [
                {
                    "步骤序号": 1,
                    "操作": "配制铁源前驱体溶液",
                    "试剂/对象": "FeCl3、去离子水",
                    "参数": "称取适量 FeCl3 溶于去离子水，配制成浓度约为 0.05-0.1 mol/L 的澄清溶液",
                },
                {
                    "步骤序号": 2,
                    "操作": "配制六氰合铁酸盐溶液",
                    "试剂/对象": "K4[Fe(CN)6]、去离子水",
                    "参数": "称取等摩尔比的 K4[Fe(CN)6] 溶于去离子水，配制成浓度约为 0.05-0.1 mol/L 的反应底液",
                },
                {
                    "步骤序号": 3,
                    "操作": "进行液相共沉淀反应",
                    "试剂/对象": "FeCl3 溶液、K4[Fe(CN)6] 溶液",
                    "参数": "在室温搅拌条件下将 FeCl3 溶液缓慢滴加至 K4[Fe(CN)6] 溶液中，观察深蓝色沉淀形成",
                },
                {
                    "步骤序号": 4,
                    "操作": "陈化促进晶体生长",
                    "试剂/对象": "反应后的普鲁士蓝悬浊液",
                    "参数": "滴加结束后继续搅拌并静置 2-4 小时，以提高结晶度并为后续表征做准备",
                },
            ]

        return [
            {
                "步骤序号": 1,
                "操作": "围绕 query 进行首轮探索性配方准备",
                "试剂/对象": state.event.query,
                "参数": "当前缺少可直接映射为结构化步骤的知识库记录，请结合 query 和命中文献进一步细化",
            }
        ]

    def _select_stage_steps(
        self, steps: Sequence[Dict[str, Any]], current_stage: str
    ) -> List[Dict[str, Any]]:
        if not steps:
            return []

        observation_keywords = ["XRD", "XPS", "Raman", "SEM", "TEM", "表征", "观察", "测试"]
        if "合成" in current_stage and any(keyword in current_stage for keyword in observation_keywords):
            return list(steps)

        synth_break_keywords = ["电极", "涂覆", "浆料", "活化", "测试", "表征"]
        electrode_keywords = ["电极", "涂覆", "浆料", "FTO", "NF", "nickel foam"]
        validation_keywords = ["活化", "测试", "表征", "XRD", "OER", "HER", "电化学"]

        if "合成" in current_stage or "前驱体" in current_stage:
            collected: List[Dict[str, Any]] = []
            for step in steps:
                operation = str(step.get("操作", ""))
                if any(keyword in operation for keyword in synth_break_keywords):
                    break
                collected.append(step)
            return collected or list(steps[: min(4, len(steps))])

        if "电极" in current_stage or "样品" in current_stage:
            filtered = [
                step
                for step in steps
                if any(
                    keyword in (
                        str(step.get("操作", ""))
                        + " "
                        + str(step.get("试剂/对象", ""))
                        + " "
                        + str(step.get("参数", ""))
                    )
                    for keyword in electrode_keywords
                )
            ]
            return filtered or list(steps[-2:])

        filtered = [
            step
            for step in steps
            if any(
                keyword in (
                    str(step.get("操作", ""))
                    + " "
                    + str(step.get("试剂/对象", ""))
                    + " "
                    + str(step.get("参数", ""))
                )
                for keyword in validation_keywords
            )
        ]
        return filtered or list(steps[-2:])

    def _normalize_macro_plan(self, steps: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for index, step in enumerate(steps, start=1):
            normalized.append(
                {
                    "步骤序号": step.get("步骤序号", index) or index,
                    "操作": str(step.get("操作", "")).strip(),
                    "试剂/对象": str(step.get("试剂/对象", "")).strip(),
                    "参数": str(step.get("参数", "")).strip(),
                }
            )

        # Renumber to keep the local macro plan self-contained.
        for index, step in enumerate(normalized, start=1):
            step["步骤序号"] = index
        return normalized

    def _collect_common_operations(self, hits: Sequence[SearchHit]) -> List[str]:
        counts: Dict[str, int] = {}
        for hit in hits:
            for step in hit.steps:
                operation = str(step.get("操作", "")).strip()
                if operation:
                    counts[operation] = counts.get(operation, 0) + 1
        return [name for name, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]
