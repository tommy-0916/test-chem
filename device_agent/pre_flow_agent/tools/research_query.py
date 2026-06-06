"""
Memory/Research Query 工具
=========================

当前阶段尚未接入正式 memory service。
这里使用 exp_logs 目录中的历史实验日志作为临时 memory 代理，
对 query 做简单匹配后返回相关实验摘要。
"""

import json
import os
import re
from typing import Dict, List, Tuple


class ResearchQuery:
    BASE_PATH = "/workspace/chem_resources/exp_logs"

    def __init__(self, use_research_agent: bool = False):
        self._use_research_agent = use_research_agent

    def get_related_records(self, query: str = "", max_experiments: int = 3, max_chars: int = 5000) -> str:
        if self._use_research_agent:
            raise NotImplementedError("Research Agent not implemented yet")
        if not os.path.exists(self.BASE_PATH):
            return ""

        tokens = self._tokenize(query)
        ranked_experiments: List[Tuple[int, str, str]] = []

        exp_dirs = sorted(
            [d for d in os.listdir(self.BASE_PATH) if os.path.isdir(os.path.join(self.BASE_PATH, d))],
            reverse=True,
        )
        for exp_id in exp_dirs:
            exp_dir = os.path.join(self.BASE_PATH, exp_id)
            summary, search_text, accepted_bonus = self._build_experiment_summary(exp_id, exp_dir)
            if not summary:
                continue
            score = self._score_text(search_text, tokens) + accepted_bonus
            ranked_experiments.append((score, exp_id, summary))

        ranked_experiments.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if not ranked_experiments:
            return ""

        chunks = []
        total = 0
        for score, _, summary in ranked_experiments[:max_experiments]:
            block = summary if not tokens else f"{summary}\n- 匹配得分: {score}"
            if total + len(block) > max_chars and chunks:
                break
            chunks.append(block)
            total += len(block)

        return "\n\n".join(chunks)[:max_chars]

    def get_all(self) -> Dict[str, str]:
        return {"related_records": self.get_related_records()}

    def _build_experiment_summary(self, exp_id: str, exp_dir: str) -> Tuple[str, str, int]:
        exp_log_path = os.path.join(exp_dir, "exp_log.json")
        if not os.path.exists(exp_log_path):
            return "", "", 0

        try:
            with open(exp_log_path, 'r', encoding='utf-8') as f:
                exp_log = json.load(f)
        except Exception:
            return "", "", 0

        macro_plan = str(exp_log.get('macro_plan') or exp_log.get('final_goal') or '').strip()
        parts = [f"### 实验 {exp_id}"]
        if macro_plan:
            parts.append(f"- macro_plan 摘要: {self._trim_text(macro_plan, 260)}")
        search_text_parts = [macro_plan]
        accepted_bonus = 0

        iteration_files = sorted(
            [name for name in os.listdir(exp_dir) if name.startswith('iteration') and name.endswith('.json')],
            reverse=True,
        )
        for iter_name in iteration_files[:2]:
            iter_path = os.path.join(exp_dir, iter_name)
            try:
                with open(iter_path, 'r', encoding='utf-8') as f:
                    iter_data = json.load(f)
            except Exception:
                continue

            macro_plan_summary = str(iter_data.get('macro_plan_summary', '') or '').strip()
            if macro_plan_summary:
                parts.append(f"- 本轮摘要: {self._trim_text(macro_plan_summary, 220)}")
                search_text_parts.append(macro_plan_summary)

            workflows = iter_data.get('workflows', [])
            selected_workflow = self._select_reference_workflow(workflows)
            if selected_workflow:
                workflow_txt = str(selected_workflow.get('workflow_txt', '') or '')
                verification_suggestion = str(selected_workflow.get('verification_suggestion', '') or '')
                verification_result = selected_workflow.get('verification_result', '')
                verification_category = selected_workflow.get('verification_category', '')
                workflow_id = selected_workflow.get('workflow_id', '')
                parts.append(
                    f"- 推荐参考 workflow {workflow_id}: result={verification_result}, category={verification_category}"
                )
                if verification_result == "accepted":
                    accepted_bonus += 15
                if workflow_txt:
                    excerpt = self._trim_text(workflow_txt, 1800)
                    parts.append("```text")
                    parts.append(excerpt)
                    parts.append("```")
                    search_text_parts.append(workflow_txt)
                if verification_result != "accepted" and verification_suggestion:
                    parts.append(f"- 返修建议摘要: {verification_suggestion[:500]}")
                    search_text_parts.append(verification_suggestion)

        return "\n".join(parts), "\n".join(search_text_parts), accepted_bonus

    def _select_reference_workflow(self, workflows: List[Dict]) -> Dict:
        accepted = [
            workflow for workflow in workflows
            if str(workflow.get('verification_result', '')).strip().lower() == 'accepted'
            and len(str(workflow.get('workflow_txt', '') or '').strip()) >= 500
        ]
        if accepted:
            return accepted[-1]

        recent_with_text = [
            workflow for workflow in workflows
            if str(workflow.get('workflow_txt', '') or '').strip()
        ]
        if recent_with_text:
            return max(
                recent_with_text,
                key=lambda workflow: len(str(workflow.get('workflow_txt', '') or '').strip()),
            )
        return {}

    def _trim_text(self, text: str, max_chars: int) -> str:
        normalized = str(text or '').strip()
        if len(normalized) <= max_chars:
            return normalized
        return normalized[:max_chars].rstrip() + "\n...[truncated]"

    def _tokenize(self, text: str) -> List[str]:
        raw_tokens = re.findall(r"[A-Za-z0-9_+\-.]+|[\u4e00-\u9fff]{1,8}", text or "")
        tokens = []
        for token in raw_tokens:
            lowered = token.lower().strip()
            if not lowered:
                continue
            if re.fullmatch(r"[\u4e00-\u9fff]{1}", lowered):
                continue
            tokens.append(lowered)
        return list(dict.fromkeys(tokens))

    def _score_text(self, text: str, tokens: List[str]) -> int:
        if not tokens:
            return 0
        lowered = (text or "").lower()
        score = 0
        for token in tokens:
            if token in lowered:
                score += 3 if len(token) > 2 else 1
        return score
