"""
实验日志管理器
=============
"""

import json
import os
from datetime import datetime
from typing import Any, Dict

from utils.paths import exp_logs_dir


class LogManager:
    BASE_PATH = str(exp_logs_dir())

    def __init__(self, exp_id: str = None):
        if exp_id is None:
            exp_id = self._generate_exp_id()

        self.exp_id = exp_id
        self.exp_dir = os.path.join(self.BASE_PATH, exp_id)
        self._ensure_exp_dir()

    def _generate_exp_id(self) -> str:
        os.makedirs(self.BASE_PATH, exist_ok=True)
        today = datetime.now().strftime("%Y%m%d")
        existing = [
            d for d in os.listdir(self.BASE_PATH)
            if os.path.isdir(os.path.join(self.BASE_PATH, d)) and d.startswith(f"exp_{today}_")
        ]
        return f"exp_{today}_{len(existing):03d}"

    def _ensure_exp_dir(self) -> None:
        os.makedirs(self.exp_dir, exist_ok=True)
        exp_log_path = os.path.join(self.exp_dir, "exp_log.json")
        if not os.path.exists(exp_log_path):
            self._write_json(exp_log_path, {
                "final_goal": "",
                "macro_plan": "",
                "iterations": []
            })

    def _read_json(self, path: str) -> Dict[str, Any]:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _write_json(self, path: str, data: Dict[str, Any]) -> None:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def set_final_goal(self, goal: str) -> None:
        exp_log_path = os.path.join(self.exp_dir, "exp_log.json")
        data = self._read_json(exp_log_path)
        data["final_goal"] = goal
        self._write_json(exp_log_path, data)

    def set_macro_plan(self, macro_plan: str) -> None:
        exp_log_path = os.path.join(self.exp_dir, "exp_log.json")
        data = self._read_json(exp_log_path)
        data["macro_plan"] = macro_plan
        if not data.get("final_goal"):
            data["final_goal"] = macro_plan
        self._write_json(exp_log_path, data)

    def _get_default_iteration_data(self, iteration_id: int) -> Dict[str, Any]:
        return {
            "iteration_id": iteration_id,
            "macro_plan_summary": "",
            "observation_requirements": {},
            "knowledge_raw_inputs": {},
            "memory_raw_inputs": {},
            "knowledge": "",
            "related_workflows_unformatted": "",
            "related_workflows_txt": "",
            "goal_in_this_iteration": "",
            "workflows": []
        }

    def update_iteration(self, iteration_id: int, field: str, value: Any) -> None:
        iter_path = os.path.join(self.exp_dir, f"iteration{iteration_id}.json")
        if os.path.exists(iter_path):
            data = self._read_json(iter_path)
        else:
            data = self._get_default_iteration_data(iteration_id)
            self._add_iteration_index(iteration_id)
        data[field] = value
        self._write_json(iter_path, data)

    def ensure_iteration(self, iteration_id: int) -> Dict[str, Any]:
        iter_path = os.path.join(self.exp_dir, f"iteration{iteration_id}.json")
        if not os.path.exists(iter_path):
            data = self._get_default_iteration_data(iteration_id)
            self._add_iteration_index(iteration_id)
            self._write_json(iter_path, data)
            return data
        return self._read_json(iter_path)

    def add_workflow(self, iteration_id: int, workflow_data: Dict[str, Any]) -> int:
        iter_path = os.path.join(self.exp_dir, f"iteration{iteration_id}.json")
        data = self.ensure_iteration(iteration_id)
        workflow_id = len(data.get("workflows", []))
        workflow_record = {
            "workflow_id": workflow_id,
            "workflow_skeleton_txt": "",
            "workflow_txt": "",
            "verification_result": "",
            "verification_category": "",
            "blocking_constraints": [],
            "verification_suggestion": "",
            "workflow_json": None,
        }
        workflow_record.update(workflow_data)
        data.setdefault("workflows", []).append(workflow_record)
        self._write_json(iter_path, data)
        return workflow_id

    def update_workflow(self, iteration_id: int, workflow_id: int, fields: Dict[str, Any]) -> None:
        iter_path = os.path.join(self.exp_dir, f"iteration{iteration_id}.json")
        data = self.ensure_iteration(iteration_id)
        workflows = data.setdefault("workflows", [])
        if workflow_id >= len(workflows):
            raise IndexError(f"Workflow {workflow_id} not found in iteration {iteration_id}")
        workflows[workflow_id].update(fields)
        self._write_json(iter_path, data)

    def _add_iteration_index(self, iteration_id: int) -> None:
        exp_log_path = os.path.join(self.exp_dir, "exp_log.json")
        data = self._read_json(exp_log_path)
        iter_entry = {
            "iteration_id": iteration_id,
            "iteration_file": os.path.join(self.exp_dir, f"iteration{iteration_id}.json")
        }
        existing_ids = [it["iteration_id"] for it in data.get("iterations", [])]
        if iteration_id not in existing_ids:
            data.setdefault("iterations", []).append(iter_entry)
            self._write_json(exp_log_path, data)

    def get_iteration(self, iteration_id: int) -> Dict[str, Any]:
        iter_path = os.path.join(self.exp_dir, f"iteration{iteration_id}.json")
        if not os.path.exists(iter_path):
            raise FileNotFoundError(f"Iteration {iteration_id} not found")
        return self._read_json(iter_path)

    def get_exp_log(self) -> Dict[str, Any]:
        exp_log_path = os.path.join(self.exp_dir, "exp_log.json")
        return self._read_json(exp_log_path)
