"""
工作站配置加载器
===============
"""

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Set

try:
    from ..skill_contract_audit import parse_operation_schemas
except ImportError:
    from skill_contract_audit import parse_operation_schemas

try:
    from .paths import workstation_dir
except ImportError:  # script-style imports with device_agent on sys.path
    from utils.paths import workstation_dir


class WorkstationLoader:
    """工作站配置加载器。"""

    WORKSTATION_DIR_OLD = str(workstation_dir(use_new_format=False))
    WORKSTATION_DIR_NEW = str(workstation_dir(use_new_format=True))
    PROMPT_USAGE_CHAR_LIMIT = 500
    PROMPT_AUDIT_CHAR_LIMIT = 1200
    LAB_DESIGN_MODULE_DIRS = (
        "references-Synthesis-Module",
        "references-Reaction-and-Testing-Module",
        "references-Characterization-Module",
    )
    KEYWORD_MAP = {
        "General_Material_Station_V1": ["物料", "容器", "进样瓶", "取样瓶", "拿取"],
        "Heat_Resistant_Material_Station": ["耐热瓶", "50ml耐热瓶"],
        "Container_storaging_Station_V1": ["静置", "暂存", "存储", "置放"],
        "Plate_storaging_Station_V1": ["孔板", "96位", "孔板暂存"],
        "Single_Channel_Solid_Weighing_Workstation_V1": ["固体", "粉末", "粉体", "称量", "称取", "固体进样"],
        "Multi_Channel_Solid_Weighing_Workstation_V1": ["多通道", "固体", "粉末", "称量", "平行样"],
        "Multi_Channel_Solid_Weighing_Workstation_V2": ["多通道", "固体", "粉末", "称量", "平行样"],
        "Solid_Sample_Transfer_Workstation_V1": [
            "固体转移", "料斗", "转移固体", "干粉", "粉末", "称量", "称取",
            "定量取", "分装", "取粉",
        ],
        "Liquid_Handling_Station_1ml_V1": [
            "移液", "加液", "滴加", "原液", "开盖", "关盖", "1ml",
            "去离子水", "异丙醇", "nafion", "墨水",
        ],
        "Liquid_Handling_Station_1ml_V2": [
            "移液", "加液", "滴加", "原液", "开盖", "关盖", "1ml",
            "去离子水", "异丙醇", "nafion", "墨水",
        ],
        "Liquid_Handling_Station_5ml_V1": ["移液", "加液", "溶液", "5ml", "大体积"],
        "Liquid_Handling_Station_5ml_V2": ["移液", "加液", "转移", "5ml", "大体积"],
        "Liquid_Handling_Station_5ml_V3": ["移液", "加液", "耐压反应管", "10ml"],
        "Liquid_Handling_Station_4Channel_V1": ["四通道", "孔板加液", "批量加液"],
        "Cleaning_and_Dispensing_Workstation_V1": ["批量加液", "清洗", "分液"],
        "Liquid_Pouring_Workstation_V1": ["倾倒", "倒上清", "去上清", "离心后"],
        "Room_Temperture_Magnetic_Stirrer_Workstation_V1": ["常温搅拌", "搅拌", "熟化", "混合", "络合"],
        "Heating_Magnetic_Stirring_Workstation_V1": ["加热搅拌", "搅拌", "加热", "熟化", "反应"],
        "Spectroscopy_Magnetic_Stirrer_Workstation_V1": ["谱学搅拌", "xrd前", "表征前搅拌"],
        "Centrifuge_V1": ["离心", "固液分离", "沉淀收集"],
        "Purification_Workstation_V1": ["纯化", "洗涤", "离心", "留固", "沉淀"],
        "Drying_Oven_V1": ["烘干", "干燥", "老化", "恒温"],
        "Cooling_Workstation_V1": ["冷却", "降温"],
        "Muffle_Furnace_V1": [
            "马弗炉", "煅烧", "焙烧", "高温烧结", "热处理", "升温速率",
            "保温", "空气气氛", "300", "400", "500",
        ],
        "Ultrasonic_Disperser_V1": ["超声", "分散", "清洗", "墨水"],
        "Ultrasonic_Disperser_V2": ["超声", "分散", "清洗", "墨水"],
        "Ultrasonic_Liquid_Handling_Workstation_V1": ["超声加液", "孔板超声", "超声移液"],
        "Dual_Station_Electrochemical_Workstation_V2": [
            "电化学", "电化学测试", "CV", "EIS", "LSV", "GCD", "恒电位",
            "RHE", "KOH", "碳纸", "OER", "活化",
        ],
        "High_Temperature_High_Pressure_Microreaction_Platform_V1": [
            "高温", "高压", "微反应", "气液固", "催化反应",
        ],
        "Photocatalysis_Workstation_V1": ["光催化", "光照反应", "催化测试"],
        "Photocatalysis_Workstation_V2": ["光催化", "光照反应", "催化测试"],
        "Post_Reaction_Processing_Platform_V1": ["反应后处理", "后处理"],
        "XRD_V1": ["XRD", "PXRD", "衍射", "晶相", "物相"],
        "Infrared_Spectrometer_V1": ["红外", "IR", "FTIR", "官能团"],
        "UV_Vis_Spectrometer_V1": ["UV", "Vis", "UV-Vis", "紫外", "可见", "吸收光谱"],
        "Fluorescence_Spectrometer_V1": ["荧光", "PL", "发射光谱"],
        "Microplate_Reader_V1": ["酶标仪", "微孔板", "吸光度"],
        "Gas_Chromatograph_V1": ["气相色谱", "GC", "气体分析"],
        "Liquid_Chromatograph_V1": ["液相色谱", "LC", "HPLC", "液体分析"],
        "Gas_Liquid_Mass_Transfer_High_Speed_Camera_V1": ["高速摄像", "气泡", "气液传质"],
        "Interfacial_Wettability_and_Mass_Transfer_Characterization_Workstation": [
            "润湿", "接触角", "界面传质",
        ],
        "Darkbox_Imaging_Workstation_V1": ["暗箱", "成像", "拍照"],
        "LED_Illumination_and_Membrane_Clamping_Workstation_V1": ["LED", "膜夹持", "光照"],
        "Spectroscopy_Container_Transfer_Station_V1": ["谱学容器", "表征中转"],
        "Intelligent_Photocatalysis_Container_Transfer_Station_V1": ["光催容器", "光催中转"],
    }
    # Keep alternative liquid ranges visible together.  A query may say
    # “滴加 10 mL” without using the exact “5ml 平台” keyword; hiding the
    # larger-range station makes a valid batch mapping look infeasible.
    LIQUID_HANDLING_STATIONS = {
        "Liquid_Handling_Station_1ml_V1",
        "Liquid_Handling_Station_1ml_V2",
        "Liquid_Handling_Station_5ml_V1",
        "Liquid_Handling_Station_5ml_V2",
        "Liquid_Handling_Station_5ml_V3",
        "Liquid_Handling_Station_4Channel_V1",
        "Cleaning_and_Dispensing_Workstation_V1",
        "Ultrasonic_Liquid_Handling_Workstation_V1",
    }
    STIRRING_STATIONS = {
        "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
        "Heating_Magnetic_Stirring_Workstation_V1",
        "Spectroscopy_Magnetic_Stirrer_Workstation_V1",
    }
    STATION_ALIAS_MAP = {
        "物料站": "General_Material_Station_V1",
        "常规物料站": "General_Material_Station_V1",
        "固体进样工作站": "Single_Channel_Solid_Weighing_Workstation_V1",
        "固体称量工作站": "Single_Channel_Solid_Weighing_Workstation_V1",
        "液体进样站": "Liquid_Handling_Station_1ml_V2",
        "移液平台": "Liquid_Handling_Station_1ml_V2",
        "磁力搅拌工作站": "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
        "常温磁力搅拌工作站": "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
        "加热磁力搅拌工作站": "Heating_Magnetic_Stirring_Workstation_V1",
        "纯化工作站": "Purification_Workstation_V1",
        "离心机": "Centrifuge_V1",
        "超声清洗": "Ultrasonic_Disperser_V2",
        "超声清洗工作站": "Ultrasonic_Disperser_V2",
        "超声分散仪": "Ultrasonic_Disperser_V2",
        "烘干机": "Drying_Oven_V1",
        "双工位电化学工作站": "Dual_Station_Electrochemical_Workstation_V2",
        "电化学工作站": "Dual_Station_Electrochemical_Workstation_V2",
        "电化学存储工作站": "Container_storaging_Station_V1",
        "X射线衍射仪": "XRD_V1",
        "XRD": "XRD_V1",
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
        self._station_alias_map = dict(self.STATION_ALIAS_MAP)
        self._old_workstation_dir = str(workstation_dir(use_new_format=False))
        self._new_workstation_dir = str(workstation_dir(use_new_format=True))
        self._status_overlay = self._load_status_overlay()

        self.refresh_truth_snapshot()

    def truth_source_root(self) -> Path:
        return Path(
            self._normalize_new_workstation_root(self._new_workstation_dir)
            if self._use_new_format else self._old_workstation_dir
        )

    def _disk_truth_manifest(self) -> Dict:
        """Read only source files, including roster additions and status bytes."""
        root = self.truth_source_root()
        paths = {root / name for name in ("SKILL.md", "工作站名称中英文对照.md", "0410数据转换.txt")}
        if self._use_new_format:
            for pattern in ("SKILL.md", "USAGE.md", "AUDIT-RULES.md"):
                paths.update(root.rglob(pattern))
            audit_dir = root / "references_audit"
            if audit_dir.is_dir():
                paths.update(audit_dir.glob("*.md"))
        else:
            paths.update(root.glob("*.json"))
        status_path = os.getenv("CHEM_DEVICE_STATUS_JSON", "").strip()
        return {
            "sources": {str(path): self._read_text(str(path)) for path in sorted(paths)},
            "status_path": status_path,
            "status_content": self._read_text(status_path) if status_path else "",
        }

    def _disk_truth_digest(self) -> str:
        return hashlib.sha256(json.dumps(
            self._disk_truth_manifest(), ensure_ascii=False, sort_keys=True
        ).encode("utf-8")).hexdigest()

    def refresh_truth_snapshot(self) -> None:
        """Reload all cached facts under a before/after source-stability check."""
        self._old_workstation_dir = str(workstation_dir(use_new_format=False))
        self._new_workstation_dir = str(workstation_dir(use_new_format=True))
        before = self._disk_truth_digest()
        self._workstations = {}
        self._new_workstations = {}
        self._station_alias_map = dict(self.STATION_ALIAS_MAP)
        self._status_overlay = self._load_status_overlay()
        if self._use_new_format:
            self._load_all_new()
        else:
            self._load_all()
        after = self._disk_truth_digest()
        if before != after:
            raise RuntimeError("Workstation truth changed while loading its snapshot; retry with stable sources")
        self._snapshot_disk_digest = after

    def assert_snapshot_current(self) -> None:
        if self._disk_truth_digest() != self._snapshot_disk_digest:
            raise RuntimeError("Workstation truth changed during this Device run; reload before continuing")

    UNAVAILABLE_STATUS_VALUES = {
        "offline",
        "unavailable",
        "down",
        "maintenance",
        "fault",
        "disabled",
        "停机",
        "维修",
        "故障",
        "不可用",
        "禁用",
    }
    BUSY_STATUS_VALUES = {"busy", "occupied", "占用", "忙碌"}

    def _load_status_overlay(self) -> Dict[str, str]:
        """Live station availability from $CHEM_DEVICE_STATUS_JSON (optional)."""
        path_text = os.getenv("CHEM_DEVICE_STATUS_JSON", "").strip()
        if not path_text:
            return {}
        try:
            with open(path_text, "r", encoding="utf-8") as handle:
                parsed = json.load(handle)
        except Exception as exc:
            print(f"Warning: failed to load device status file {path_text}: {exc}")
            return {}
        if isinstance(parsed, dict) and isinstance(parsed.get("stations"), dict):
            parsed = parsed["stations"]
        if not isinstance(parsed, dict):
            return {}
        return {
            str(name).strip().lower(): str(status).strip()
            for name, status in parsed.items()
            if str(name).strip()
        }

    def _station_status(self, station_code: str, station_data: Dict) -> str:
        if not self._status_overlay:
            return ""
        identity = station_data.get("station_identity")
        identity_name = identity.get("name", "") if isinstance(identity, dict) else ""
        for candidate in (
            station_code,
            station_data.get("display_name", ""),
            identity_name,
        ):
            key = str(candidate or "").strip().lower()
            if key and key in self._status_overlay:
                return self._status_overlay[key]
        return ""

    def _availability_banner(self, station_code: str, station_data: Dict) -> str:
        status = self._station_status(station_code, station_data)
        if not status:
            return ""
        lowered = status.lower()
        if lowered in self.UNAVAILABLE_STATUS_VALUES:
            return (
                f"⛔ 当前不可用（status={status}）：不得在 workflow 中选择该工作站；"
                "若必要化学动作只有该站能实现，必须返回 device_feasibility_error "
                "并在 blocking_constraints 中说明该工作站当前不可用。"
            )
        if lowered in self.BUSY_STATUS_VALUES:
            return f"⚠️ 当前占用（status={status}）：可以选择，但在 notes 中注明可能需要等待。"
        return f"当前状态：{status}"

    def _load_all(self):
        if not os.path.exists(self._old_workstation_dir):
            raise FileNotFoundError(f"Workstation directory not found: {self._old_workstation_dir}")

        for filename in os.listdir(self._old_workstation_dir):
            if not filename.endswith('.json'):
                continue
            filepath = os.path.join(self._old_workstation_dir, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    ws_data = json.load(f)
                code = ws_data.get("station_identity", {}).get("code", filename[:-5])
                self._workstations[code] = ws_data
            except Exception as exc:
                print(f"Warning: Failed to load {filename}: {exc}")

    def _load_all_new(self):
        root = self._normalize_new_workstation_root(self._new_workstation_dir)
        if not os.path.exists(root):
            raise FileNotFoundError(f"New workstation directory not found: {root}")

        if self._is_lab_design_root(root):
            self._load_lab_design_root(root)
            return

        for station_dir in os.listdir(root):
            station_path = os.path.join(root, station_dir)
            if not os.path.isdir(station_path):
                continue

            station_data = {
                "station_name": station_dir,
                "display_name": station_dir,
                "module_name": "",
                "station_path": station_path,
                "usage_content": "",
                "usage_label": "USAGE",
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

    def _normalize_new_workstation_root(self, root: str) -> str:
        """Accept workstations_new, lab-design-all, or a nested workstation skill root."""
        if self._is_lab_design_root(root):
            return root

        nested = os.path.join(
            root,
            "skills",
            "chemistry-experiment-workstation",
        )
        if self._is_lab_design_root(nested):
            return nested

        return root

    def _is_lab_design_root(self, root: str) -> bool:
        return os.path.isdir(root) and any(
            os.path.isdir(os.path.join(root, module_dir))
            for module_dir in self.LAB_DESIGN_MODULE_DIRS
        )

    def _load_lab_design_root(self, root: str) -> None:
        name_map = self._load_lab_design_name_map(root)
        audit_dir = os.path.join(root, "references_audit")

        for module_dir in self.LAB_DESIGN_MODULE_DIRS:
            module_path = os.path.join(root, module_dir)
            if not os.path.isdir(module_path):
                continue
            module_name = self._module_display_name(module_dir)
            for station_dir in sorted(os.listdir(module_path)):
                station_path = os.path.join(module_path, station_dir)
                if not os.path.isdir(station_path):
                    continue

                skill_path = os.path.join(station_path, "SKILL.md")
                skill_content = self._read_text(skill_path)
                audit_content = self._read_text(
                    self._audit_path_for_station(audit_dir, station_dir)
                )
                display_name = name_map.get(station_dir, station_dir)
                station_data = {
                    "station_name": station_dir,
                    "display_name": display_name,
                    "module_name": module_name,
                    "station_path": station_path,
                    "usage_content": skill_content,
                    "usage_label": "SKILL",
                    "audit_rules_content": audit_content,
                    "skill_content": skill_content,
                }
                self._new_workstations[station_dir] = station_data
                self._station_alias_map[station_dir] = station_dir
                self._station_alias_map[display_name] = station_dir

    def _load_lab_design_name_map(self, root: str) -> Dict[str, str]:
        mapping_path = os.path.join(root, "工作站名称中英文对照.md")
        mapping: Dict[str, str] = {}
        for line in self._read_text(mapping_path).splitlines():
            stripped = line.strip()
            if not stripped or "\t" not in stripped:
                continue
            english_name, chinese_name = [
                part.strip() for part in stripped.split("\t", 1)
            ]
            if not english_name or english_name in {"英文名", "合成模块Synthesis Module"}:
                continue
            if set(english_name) == {"-"}:
                continue
            mapping[english_name] = chinese_name
        return mapping

    def _audit_path_for_station(self, audit_dir: str, station_name: str) -> str:
        exact_path = os.path.join(audit_dir, f"{station_name}_audit.md")
        if os.path.exists(exact_path):
            return exact_path
        if not os.path.isdir(audit_dir):
            return exact_path
        prefix = f"{station_name}_"
        for filename in os.listdir(audit_dir):
            if filename.startswith(prefix) and filename.endswith(".md"):
                return os.path.join(audit_dir, filename)
        return exact_path

    def _module_display_name(self, module_dir: str) -> str:
        return {
            "references-Synthesis-Module": "Synthesis Module",
            "references-Reaction-and-Testing-Module": "Reaction and Testing Module",
            "references-Characterization-Module": "Characterization Module",
        }.get(module_dir, module_dir)

    def _read_text(self, path: str) -> str:
        if not path or not os.path.exists(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read()
        except Exception as exc:
            print(f"Warning: Failed to load {path}: {exc}")
            return ""

    def get_all(self) -> List[Dict]:
        if self._use_new_format:
            return list(self._new_workstations.values())
        return list(self._workstations.values())

    def get_by_code(self, code: str) -> Optional[Dict]:
        if self._use_new_format:
            return self._new_workstations.get(code)
        return self._workstations.get(code)

    def global_rules_for_prompt(self) -> str:
        """Cross-station rules are always visible, independently of selection."""
        if not self._use_new_format:
            return ""
        root = self._normalize_new_workstation_root(self._new_workstation_dir)
        return self._read_text(os.path.join(root, "SKILL.md")).strip()

    def capability_catalog(self) -> List[Dict]:
        """Complete discovery metadata, deliberately without parameter tables."""
        catalog: List[Dict] = []
        for entry in self.get_all():
            code = str(entry.get("station_name", "")).strip()
            if not code:
                identity = entry.get("station_identity", {})
                code = str(identity.get("code", identity.get("name", "")))
            if not code:
                continue
            content = str(entry.get("skill_content") or entry.get("usage_content") or "")
            description = re.search(r"^description:\s*(.+)$", content, re.MULTILINE)
            operations = list(parse_operation_schemas(content))
            if not operations:
                operations = [
                    str(op.get("name", ""))
                    for op in entry.get("supported_operations", [])
                    if isinstance(op, dict)
                ]
            catalog.append({
                "station_code": code,
                "display_name": entry.get("display_name", code),
                "module": entry.get("module_name", ""),
                "description": description.group(1).strip() if description else entry.get("description", ""),
                "operations": list(dict.fromkeys(operations)),
                "availability": self._station_status(code, entry) or "available",
            })
        return catalog

    def format_capability_catalog(self) -> str:
        return "# 全部工作站能力目录（不是参数合同）\n" + json.dumps(
            self.capability_catalog(), ensure_ascii=False, separators=(",", ":")
        )

    def resolve_station_code(self, name: str) -> Optional[str]:
        """Resolve declared exact aliases, never paths or fuzzy substrings."""
        entries = {item["station_code"] for item in self.capability_catalog()}
        if name in entries:
            return name
        alias = self._station_alias_map.get(name)
        return alias if alias in entries else None

    def load_workstation_skill(self, station_code: str) -> Dict:
        """Read one known station, preserving the entire immutable source."""
        known = {item["station_code"] for item in self.capability_catalog()}
        if station_code not in known:
            raise ValueError(f"Unknown workstation station_code: {station_code}")
        entry = self.get_by_code(station_code)
        if not isinstance(entry, dict):
            raise ValueError(f"Workstation source missing: {station_code}")
        skill = str(entry.get("skill_content") or entry.get("usage_content") or "")
        if not skill and not self._use_new_format:
            skill = json.dumps(entry, ensure_ascii=False)
        if not skill.strip():
            raise ValueError(f"Empty workstation skill: {station_code}")
        audit = str(entry.get("audit_rules_content") or "")
        return {
            "station_code": station_code,
            "display_name": entry.get("display_name", station_code),
            "availability": self._station_status(station_code, entry) or "available",
            "skill_content": skill,
            "audit_rules_content": audit,
            "source_path": str(Path(entry.get("station_path", "")) / "SKILL.md"),
            "source_sha256": hashlib.sha256((skill + "\n" + audit).encode("utf-8")).hexdigest(),
        }

    def truth_source_digest(self) -> str:
        """Hash full truth and availability; prompt projection never participates."""
        payload = {
            "root_sources": self._disk_truth_manifest(),
            "stations": self.get_all(),
            "availability": self._status_overlay,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        return "device_truth_" + hashlib.sha256(encoded).hexdigest()

    def snapshot_id(self) -> str:
        """Stable id for the loaded workstation truth-source set (issue 4).

        Research and device layers reference the same device reality by
        threading this id; it changes only when the station roster or the
        live availability overlay changes.
        """
        import hashlib

        stations = self._new_workstations if self._use_new_format else self._workstations
        roster = "|".join(sorted(stations.keys()))
        overlay = "|".join(
            f"{name}={status}" for name, status in sorted(self._status_overlay.items())
        )
        digest = hashlib.sha1(f"{roster}##{overlay}".encode("utf-8")).hexdigest()[:12]
        return f"ws_{digest}"

    # Basic-infrastructure stations that keyword matching keeps missing (the
    # 2026-07-15/07-26 evaluations both hit "no muffle furnace / no solid
    # transfer / no spectroscopy placing station" false rejections). They are
    # cheap to include and load-bearing for container routing, so they are
    # always part of the relevant-station prompt.
    ALWAYS_INCLUDE_STATIONS = (
        "General_Material_Station_V1",
        "Heat_Resistant_Material_Station",
        "Container_storaging_Station_V1",
        "Muffle_Furnace_V1",
        "Solid_Sample_Transfer_Workstation_V1",
        "Spectroscopy_Container_Transfer_Station_V1",
        "Single_Channel_Solid_Weighing_Workstation_V1",
        "Multi_Channel_Solid_Weighing_Workstation_V1",
    )

    def _select_relevant_station_codes(self, query_text: str) -> List[str]:
        if not self._use_new_format:
            return list(self._workstations.keys())

        lowered = (query_text or "").lower()
        selected: Set[str] = set()
        for station_code in self.ALWAYS_INCLUDE_STATIONS:
            if station_code in self._new_workstations:
                selected.add(station_code)
        for station_code, keywords in self.KEYWORD_MAP.items():
            if station_code in self._new_workstations and any(
                keyword.lower() in lowered for keyword in keywords
            ):
                selected.add(station_code)

        query_tokens = self._tokenize_query(query_text)
        for station_code, station_data in self._new_workstations.items():
            haystack = "\n".join(
                [
                    station_code,
                    str(station_data.get("display_name", "")),
                    str(station_data.get("module_name", "")),
                    str(station_data.get("usage_content", ""))[:3000],
                ]
            ).lower()
            if station_code.lower() in lowered:
                selected.add(station_code)
                continue
            display_name = str(station_data.get("display_name", "")).lower()
            if display_name and display_name in lowered:
                selected.add(station_code)
                continue
            if any(token in haystack for token in query_tokens):
                selected.add(station_code)

        if not selected:
            return list(self._new_workstations.keys())

        query_has_addition = any(
            marker in lowered
            for marker in ("滴加", "滴入", "加液", "加入", "移液", "dispens", "pipet")
        )
        query_has_stirring = any(
            marker in lowered
            for marker in ("搅拌", "磁力", "stir", "mix")
        )
        if query_has_addition:
            selected.update(
                code for code in self.LIQUID_HANDLING_STATIONS if code in self._new_workstations
            )
        if query_has_stirring or query_has_addition:
            selected.update(
                code for code in self.STIRRING_STATIONS if code in self._new_workstations
            )

        if "General_Material_Station_V1" in self._new_workstations and selected:
            selected.add("General_Material_Station_V1")
        if "Container_storaging_Station_V1" in self._new_workstations and selected:
            selected.add("Container_storaging_Station_V1")

        ordered = [code for code in self._new_workstations.keys() if code in selected]
        return ordered or list(self._new_workstations.keys())

    def _tokenize_query(self, query_text: str) -> List[str]:
        tokens = []
        for token in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}", query_text or ""):
            lowered = token.lower().strip()
            if not lowered:
                continue
            if lowered.isdigit() or len(lowered) <= 2:
                continue
            if lowered in {
                "action",
                "adaptation",
                "agent",
                "and",
                "condition",
                "container",
                "current",
                "device",
                "expected",
                "goal",
                "handoff",
                "input",
                "logic",
                "macro",
                "material",
                "module",
                "observation",
                "operation",
                "output",
                "parameters",
                "planning",
                "point",
                "query",
                "research",
                "route",
                "sample",
                "stage",
                "step",
                "task",
                "test",
                "the",
                "variables",
                "workflow",
                "workstation",
                "cm",
                "koh",
                "min",
                "ml",
                "ul",
                "vs",
                "工作站",
                "实验",
                "步骤",
                "步骤序号",
                "操作",
                "样品",
                "反应",
                "测试",
                "溶液",
                "材料",
                "体系",
                "当前",
                "目标",
                "参数",
                "试剂",
                "对象",
                "输入",
                "输出",
                "容器",
                "类型",
                "设置",
                "执行",
                "制备",
                "加入",
                "进样瓶",
                "西林瓶",
                "耐热瓶",
                "留样瓶",
                "测试架",
                "碳纸架",
                "位塑料孔板",
                "位石英孔板",
            }:
                continue
            if re.fullmatch(r"v\d+", lowered):
                continue
            if lowered:
                tokens.append(lowered)
        return list(dict.fromkeys(tokens))

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
        if stripped in self._station_alias_map:
            return self._station_alias_map[stripped]
        for alias, code in self._station_alias_map.items():
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
                display_name = station_data.get("display_name", station_name)
                module_name = station_data.get("module_name", "")
                header = f"## {station_name}"
                if display_name and display_name != station_name:
                    header += f"（{display_name}）"
                if module_name:
                    header += f"\nmodule: {module_name}"
                lines.append(f"{header}\n{self._trim_text(usage, 900)}")
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
            display_name = station_data.get("display_name", station_code)
            lines.append(f"- {station_code}（{display_name}）: {first_block}")
        return "\n".join(lines)

    def format_for_prompt(self) -> str:
        if self._use_new_format:
            chunks = []
            for station_name, station_data in self._new_workstations.items():
                chunks.append(
                    self._format_station_chunk(
                        station_name,
                        station_data,
                        usage_limit=self.PROMPT_USAGE_CHAR_LIMIT,
                        audit_limit=self.PROMPT_AUDIT_CHAR_LIMIT,
                    )
                )
            return "\n\n".join(chunks)

        chunks = []
        for code, ws_data in self._workstations.items():
            identity = ws_data.get("station_identity", {})
            station_name = identity.get("name", code)
            desc = ws_data.get("description", "")
            operations = ws_data.get("supported_operations", [])
            section = [f"## {station_name}（{code}）"]
            banner = self._availability_banner(code, ws_data)
            if banner:
                section.append(banner)
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

    def format_complete_for_workflow_review(self) -> str:
        """Return untruncated full truth for audits and explicit diagnostics.

        Normal LLM discovery uses the complete capability catalog followed by
        selected full contracts. This compatibility helper does not implement
        that progressive disclosure policy.
        """
        if not self._use_new_format:
            return self.format_for_prompt()

        root = self._normalize_new_workstation_root(self._new_workstation_dir)
        chunks: List[str] = []
        root_skill = self._read_text(os.path.join(root, "SKILL.md")).strip()
        if root_skill:
            chunks.append("# 全局工作站规则\n" + root_skill)

        for station_code, station_data in self._new_workstations.items():
            display_name = str(station_data.get("display_name", "") or "").strip()
            module_name = str(station_data.get("module_name", "") or "").strip()
            heading = f"# 工作站 {station_code}"
            if display_name and display_name != station_code:
                heading += f"（{display_name}）"
            section = [heading]
            if module_name:
                section.append(f"module: {module_name}")
            banner = self._availability_banner(station_code, station_data)
            if banner:
                section.append(banner)

            skill_content = str(
                station_data.get("skill_content", "")
                or station_data.get("usage_content", "")
                or ""
            ).strip()
            audit_content = str(station_data.get("audit_rules_content", "") or "").strip()
            if skill_content:
                section.append("## SKILL\n" + skill_content)
            if audit_content:
                section.append("## AUDIT-RULES\n" + audit_content)
            chunks.append("\n\n".join(section))

        return "\n\n".join(chunks)

    def format_relevant_for_prompt(self, query_text: str) -> str:
        if not self._use_new_format:
            return self.format_for_prompt()
        chunks = []
        for station_code in self._select_relevant_station_codes(query_text):
            station_data = self._new_workstations[station_code]
            chunks.append(
                self._format_station_chunk(
                    station_code,
                    station_data,
                    usage_limit=max(self.PROMPT_USAGE_CHAR_LIMIT, 1200),
                    audit_limit=max(self.PROMPT_AUDIT_CHAR_LIMIT, 1500),
                )
            )
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
            chunk = self._station_header(station_code, station_data) + "\n"
            if usage_excerpt:
                usage_label = station_data.get("usage_label", "USAGE")
                chunk += f"\n### {usage_label}\n{usage_excerpt}\n"
            if include_audit and audit_excerpt:
                chunk += f"\n### AUDIT-RULES\n{audit_excerpt}\n"
            chunks.append(chunk.strip())
        return "\n\n".join(chunks)

    def _station_header(self, station_code: str, station_data: Dict) -> str:
        display_name = station_data.get("display_name", station_code)
        module_name = station_data.get("module_name", "")
        header = f"## {station_code}"
        if display_name and display_name != station_code:
            header += f"（{display_name}）"
        if module_name:
            header += f"\nmodule: {module_name}"
        banner = self._availability_banner(station_code, station_data)
        if banner:
            header += f"\n{banner}"
        return header

    def _format_station_chunk(
        self,
        station_code: str,
        station_data: Dict,
        *,
        usage_limit: int,
        audit_limit: int,
    ) -> str:
        usage = self._trim_text(
            station_data.get("usage_content", "").strip(),
            usage_limit,
        )
        audit_rules = self._trim_text(
            station_data.get("audit_rules_content", "").strip(),
            audit_limit,
        )
        usage_label = station_data.get("usage_label", "USAGE")
        chunk = self._station_header(station_code, station_data) + "\n"
        if usage:
            chunk += f"\n### {usage_label}\n{usage}\n"
        if audit_rules:
            chunk += f"\n### AUDIT-RULES\n{audit_rules}\n"
        return chunk.strip()
