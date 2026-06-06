"""
工作站配置加载器
===============
"""

import json
import os
import re
from typing import Dict, List, Optional, Set

from utils.paths import workstation_dir


class WorkstationLoader:
    """工作站配置加载器。"""

    WORKSTATION_DIR_OLD = str(workstation_dir(use_new_format=False))
    WORKSTATION_DIR_NEW = str(workstation_dir(use_new_format=True))
    PROMPT_USAGE_CHAR_LIMIT = 500
    PROMPT_AUDIT_CHAR_LIMIT = 1200
    KEYWORD_MAP = {
        "material-workstation": ["物料", "容器", "进样瓶", "取样瓶"],
        "solid-sample-workstation": ["固体", "粉末", "加样", "称取"],
        "liquid-dispensing-workstation": ["液体", "加液", "滴加", "原液", "开盖", "关盖"],
        "magnetic-stirring-workstation": ["搅拌", "络合", "反应"],
        "pure-workstation": ["离心", "纯化", "洗涤", "留固"],
        "ultrasonic-cleaning-workstation": ["超声"],
        "dryer-workstation": ["烘干", "干燥"],
        "dual-station-electrochemical-workstation": ["电化学测试", "CV", "EIS", "LSV", "GCD", "表征"],
        "elec-chem-storage-workstation": ["静置", "暂存", "存储"],
    }
    STATION_ALIAS_MAP = {
        "物料站": "material-workstation",
        "固体进样工作站": "solid-sample-workstation",
        "液体进样站": "liquid-dispensing-workstation",
        "磁力搅拌工作站": "magnetic-stirring-workstation",
        "纯化工作站": "pure-workstation",
        "超声清洗": "ultrasonic-cleaning-workstation",
        "超声清洗工作站": "ultrasonic-cleaning-workstation",
        "烘干机": "dryer-workstation",
        "双工位电化学工作站": "dual-station-electrochemical-workstation",
        "电化学存储工作站": "elec-chem-storage-workstation",
    }
    OPERATION_ALIAS_MAP = {
        "获取容器": ["物料拿取", "获取容器"],
        "物料拿取": ["物料拿取"],
        "固体进样": ["固体加样", "固体进样"],
        "固体加样": ["固体加样"],
        "移液": ["加液", "移液"],
        "加液": ["加液"],
        "开盖": ["开盖"],
        "关盖": ["关盖"],
        "搅拌": ["磁力搅拌", "搅拌"],
        "磁力搅拌": ["磁力搅拌"],
        "离心": ["离心"],
        "超声清洗": ["超声清洗"],
        "静置烘干": ["静置烘干", "烘干"],
    }

    def __init__(self, use_new_format: bool = True):
        self._use_new_format = use_new_format
        self._workstations: Dict[str, Dict] = {}
        self._new_workstations: Dict[str, Dict] = {}

        if use_new_format:
            self._load_all_new()
        else:
            self._load_all()

    def _load_all(self):
        if not os.path.exists(self.WORKSTATION_DIR_OLD):
            raise FileNotFoundError(f"Workstation directory not found: {self.WORKSTATION_DIR_OLD}")

        for filename in os.listdir(self.WORKSTATION_DIR_OLD):
            if not filename.endswith('.json'):
                continue
            filepath = os.path.join(self.WORKSTATION_DIR_OLD, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    ws_data = json.load(f)
                code = ws_data.get("station_identity", {}).get("code", filename[:-5])
                self._workstations[code] = ws_data
            except Exception as exc:
                print(f"Warning: Failed to load {filename}: {exc}")

    def _load_all_new(self):
        if not os.path.exists(self.WORKSTATION_DIR_NEW):
            raise FileNotFoundError(f"New workstation directory not found: {self.WORKSTATION_DIR_NEW}")

        for station_dir in os.listdir(self.WORKSTATION_DIR_NEW):
            station_path = os.path.join(self.WORKSTATION_DIR_NEW, station_dir)
            if not os.path.isdir(station_path):
                continue

            station_data = {
                "station_name": station_dir,
                "station_path": station_path,
                "usage_content": "",
                "audit_rules_content": "",
                "skill_content": "",
            }
            for key, filename in (
                ("usage_content", "USAGE.md"),
                ("audit_rules_content", "AUDIT-RULES.md"),
                ("skill_content", "SKILL.md"),
            ):
                file_path = os.path.join(station_path, filename)
                if not os.path.exists(file_path):
                    continue
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        station_data[key] = f.read()
                except Exception as exc:
                    print(f"Warning: Failed to load {filename} in {station_dir}: {exc}")

            self._new_workstations[station_dir] = station_data

    def get_all(self) -> List[Dict]:
        if self._use_new_format:
            return list(self._new_workstations.values())
        return list(self._workstations.values())

    def get_by_code(self, code: str) -> Optional[Dict]:
        if self._use_new_format:
            return self._new_workstations.get(code)
        return self._workstations.get(code)

    def _select_relevant_station_codes(self, query_text: str) -> List[str]:
        if not self._use_new_format:
            return list(self._workstations.keys())

        lowered = (query_text or "").lower()
        selected: Set[str] = set()
        for station_code, keywords in self.KEYWORD_MAP.items():
            if any(keyword.lower() in lowered for keyword in keywords):
                selected.add(station_code)

        if not selected:
            return list(self._new_workstations.keys())

        if selected - {"dual-station-electrochemical-workstation"}:
            selected.add("material-workstation")
        if "liquid-dispensing-workstation" in selected or "pure-workstation" in selected:
            selected.add("material-workstation")

        ordered = [code for code in self._new_workstations.keys() if code in selected]
        return ordered or list(self._new_workstations.keys())

    def _trim_text(self, text: str, max_chars: int) -> str:
        normalized = (text or "").strip()
        if len(normalized) <= max_chars:
            return normalized
        return normalized[:max_chars].rstrip() + "\n...[truncated]"

    def _map_station_name_to_code(self, station_name: str) -> Optional[str]:
        stripped = (station_name or "").strip()
        if not stripped:
            return None
        if stripped in self._new_workstations:
            return stripped
        if stripped in self.STATION_ALIAS_MAP:
            return self.STATION_ALIAS_MAP[stripped]
        for alias, code in self.STATION_ALIAS_MAP.items():
            if alias in stripped or stripped in alias:
                return code
        return None

    def _expand_operation_keywords(self, operation_name: str) -> List[str]:
        stripped = (operation_name or "").strip()
        if not stripped:
            return []
        keywords = list(self.OPERATION_ALIAS_MAP.get(stripped, []))
        keywords.append(stripped)
        return list(dict.fromkeys(keywords))

    def _parse_workflow_station_operations(self, workflow_txt: str) -> Dict[str, Set[str]]:
        mapping: Dict[str, Set[str]] = {}
        current_station = None
        for line in (workflow_txt or "").splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            station_match = re.search(r"---工作站[:：]\s*(.+)", stripped)
            if not station_match:
                station_match = re.match(r"\d+\.\s*第\d+步\s+(.+?)[:：]", stripped)
            if station_match:
                current_station = self._map_station_name_to_code(station_match.group(1))
                if current_station:
                    mapping.setdefault(current_station, set())
                continue

            operation_match = re.search(r"操作[:：]\s*(.+)", stripped)
            if operation_match and current_station:
                mapping.setdefault(current_station, set()).update(
                    self._expand_operation_keywords(operation_match.group(1))
                )
        return mapping

    def _extract_relevant_excerpt(self, text: str, keywords: List[str], max_chars: int, fallback_chars: int) -> str:
        normalized = (text or "").strip()
        if not normalized:
            return ""
        if not keywords:
            return self._trim_text(normalized, fallback_chars)

        excerpts = []
        for keyword in keywords:
            idx = normalized.find(keyword)
            if idx < 0:
                continue
            start = normalized.rfind("###", 0, idx)
            if start < 0:
                start = max(0, idx - 160)
            end = normalized.find("###", idx + len(keyword))
            if end < 0 or end - start > max_chars:
                end = min(len(normalized), start + max_chars)
            excerpt = normalized[start:end].strip()
            if excerpt and excerpt not in excerpts:
                excerpts.append(excerpt)

        if not excerpts:
            return self._trim_text(normalized, fallback_chars)
        return self._trim_text("\n\n".join(excerpts), max_chars)

    def get_operation_summary(self) -> str:
        if self._use_new_format:
            lines = []
            for station_name, station_data in self._new_workstations.items():
                usage = station_data.get("usage_content", "").strip()
                lines.append(f"## {station_name}\n{usage}")
            return "\n\n".join(lines)

        lines = []
        for code, ws_data in self._workstations.items():
            identity = ws_data.get("station_identity", {})
            station_name = identity.get("name", code)
            operations = ws_data.get("supported_operations", [])
            op_names = [op.get("name", "") for op in operations if op.get("name")]
            lines.append(f"- {station_name}（{code}）：支持操作 {', '.join(op_names)}")
        return "\n".join(lines)

    def get_relevant_operation_summary(self, query_text: str) -> str:
        if not self._use_new_format:
            return self.get_operation_summary()
        selected_codes = self._select_relevant_station_codes(query_text)
        lines = []
        for station_code in selected_codes:
            station_data = self._new_workstations[station_code]
            usage = station_data.get("usage_content", "").strip()
            first_block = usage.split("\n\n", 1)[0].strip() if usage else ""
            lines.append(f"- {station_code}: {first_block}")
        return "\n".join(lines)

    def format_for_prompt(self) -> str:
        if self._use_new_format:
            chunks = []
            for station_name, station_data in self._new_workstations.items():
                usage = station_data.get("usage_content", "").strip()
                audit_rules = station_data.get("audit_rules_content", "").strip()
                chunk = f"## {station_name}\n"
                if usage:
                    chunk += f"\n### USAGE\n{usage}\n"
                if audit_rules:
                    chunk += f"\n### AUDIT-RULES\n{audit_rules}\n"
                chunks.append(chunk.strip())
            return "\n\n".join(chunks)

        chunks = []
        for code, ws_data in self._workstations.items():
            identity = ws_data.get("station_identity", {})
            station_name = identity.get("name", code)
            desc = ws_data.get("description", "")
            operations = ws_data.get("supported_operations", [])
            section = [f"## {station_name}（{code}）"]
            if desc:
                section.append(desc)
            for op in operations:
                op_name = op.get("name", "未命名操作")
                params = op.get("parameters", [])
                section.append(f"- 操作：{op_name}")
                for param in params:
                    section.append(
                        f"  - 参数：{param.get('name', '')} | 类型：{param.get('type', '')} | 必填：{param.get('required', False)}"
                    )
            chunks.append("\n".join(section))
        return "\n\n".join(chunks)

    def format_relevant_for_prompt(self, query_text: str) -> str:
        if not self._use_new_format:
            return self.format_for_prompt()
        chunks = []
        for station_code in self._select_relevant_station_codes(query_text):
            station_data = self._new_workstations[station_code]
            usage = self._trim_text(station_data.get("usage_content", "").strip(), self.PROMPT_USAGE_CHAR_LIMIT)
            audit_rules = self._trim_text(station_data.get("audit_rules_content", "").strip(), self.PROMPT_AUDIT_CHAR_LIMIT)
            chunk = f"## {station_code}\n"
            if usage:
                chunk += f"\n### USAGE\n{usage}\n"
            if audit_rules:
                chunk += f"\n### AUDIT-RULES\n{audit_rules}\n"
            chunks.append(chunk.strip())
        return "\n\n".join(chunks)

    def format_workflow_specific_for_prompt(self, workflow_txt: str, include_audit: bool = True) -> str:
        if not self._use_new_format:
            return self.format_for_prompt()

        station_operations = self._parse_workflow_station_operations(workflow_txt)
        if not station_operations:
            return self.format_relevant_for_prompt(workflow_txt)

        chunks = []
        for station_code in self._new_workstations.keys():
            if station_code not in station_operations:
                continue
            station_data = self._new_workstations[station_code]
            operation_keywords = sorted(station_operations[station_code])
            usage_excerpt = self._extract_relevant_excerpt(
                station_data.get("usage_content", ""),
                operation_keywords,
                max_chars=900,
                fallback_chars=400,
            )
            audit_excerpt = self._extract_relevant_excerpt(
                station_data.get("audit_rules_content", ""),
                operation_keywords,
                max_chars=1100,
                fallback_chars=500,
            )
            chunk = f"## {station_code}\n"
            if usage_excerpt:
                chunk += f"\n### USAGE\n{usage_excerpt}\n"
            if include_audit and audit_excerpt:
                chunk += f"\n### AUDIT-RULES\n{audit_excerpt}\n"
            chunks.append(chunk.strip())
        return "\n\n".join(chunks)
