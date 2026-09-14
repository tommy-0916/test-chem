"""Read-only, progressively disclosed workstation contracts for Device LLMs."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

# The existing Device command-line entry points import modules script-style.
# Make the one repository-local native conversation helper importable there.
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from agent_skills.native_tools import invoke_with_tools

try:
    from .skill_contract_audit import parse_operation_schemas
except ImportError:
    from skill_contract_audit import parse_operation_schemas


class LoadWorkstationSkillInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    station_code: str = Field(
        min_length=1,
        max_length=160,
        description="Exact station_code from the complete workstation capability catalog; not a file path.",
    )


class WorkstationTruthChangedError(RuntimeError):
    """The run's frozen contracts no longer match the source files."""


class WorkstationSkillSession:
    """A run-local cache; discovery and full-source validation remain separate."""

    def __init__(self, loader: Any, dispatch_catalog: Any, workflow_validator: Any = None) -> None:
        self.loader = loader
        self.dispatch_catalog = dispatch_catalog
        self.workflow_validator = workflow_validator
        self.catalog = loader.capability_catalog()
        self.codes = {item["station_code"] for item in self.catalog}
        platform_codes: Dict[str, List[str]] = {}
        for code in sorted(self.codes):
            platform = dispatch_catalog.resolve_station(code)
            if platform:
                platform_codes.setdefault(platform, []).append(code)
        self.platform_aliases = {
            name: codes[0] for name, codes in platform_codes.items() if len(codes) == 1
        }
        self.loaded: Dict[str, Dict[str, Any]] = {}
        self.events: List[Dict[str, Any]] = []
        self.loader.assert_snapshot_current()
        self._snapshot_digest = self.current_truth_digest()
        self.loader.assert_snapshot_current()

    def discovery_context(self) -> str:
        return (
            "# 全局工作站规则\n" + self.loader.global_rules_for_prompt()
            + "\n\n" + self.loader.format_capability_catalog()
            + "\n\n先根据完整目录选择候选工作站，再调用 load_workstation_skill(station_code) "
            "读取其完整 SKILL、审核规则和实际下发合同。目录没有参数表不代表能力不存在。"
            "必须检查容器、相态、前置操作和跨站辅助操作；不能只选择主反应设备。"
            "只能引用本轮已读完整合同的工作站。真实能力缺口结论需要完整候选核验。"
        )

    def resolve(self, name: Any) -> Optional[str]:
        text = str(name or "").strip()
        # Machine output uses platform names; bridge only an exact, unambiguous
        # known platform alias, never the formatter's containment fallback.
        return self.loader.resolve_station_code(text) or self.platform_aliases.get(text)

    def load(self, station_code: str, *, origin: str = "tool") -> Dict[str, Any]:
        self.assert_current()
        if station_code not in self.codes:
            raise ValueError(f"Unknown workstation station_code: {station_code}")
        cached = station_code in self.loaded
        if not cached:
            source = self.loader.load_workstation_skill(station_code)
            source["operation_contracts"] = {
                name: asdict(operation)
                for name, operation in parse_operation_schemas(source["skill_content"]).items()
            }
            platform_name = self.dispatch_catalog.resolve_station(station_code)
            source["dispatch_contract"] = {
                "platform_station": platform_name,
                "operations": copy.deepcopy(self.dispatch_catalog.stations.get(platform_name, {})),
                "precedence": "Actual platform schema is authoritative for wire fields; unsupported fields cannot be silently dropped.",
            }
            self.loaded[station_code] = source
        self.events.append({
            "station_code": station_code,
            "source_sha256": self.loaded[station_code]["source_sha256"],
            "origin": origin,
            "cached": cached,
        })
        return copy.deepcopy(self.loaded[station_code])

    def tool(self) -> StructuredTool:
        return StructuredTool.from_function(
            func=lambda station_code: self.load(station_code),
            name="load_workstation_skill",
            description=(
                "Read one selected workstation's complete capabilities, operations, input/output, "
                "container rules, audit rules, and exact parameter/wire contracts. Read-only: "
                "does not run or dispatch equipment. Choose station_code from the complete catalog."
            ),
            args_schema=LoadWorkstationSkillInput,
            metadata={"station_codes": sorted(self.codes)},
        )

    def referenced_codes(self, payload: Any) -> List[str]:
        selected: List[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"workstation", "工作站", "station_code"} and isinstance(item, str):
                        code = self.resolve(item)
                        if code and code not in selected:
                            selected.append(code)
                    elif isinstance(item, (dict, list)):
                        walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(payload)
        return selected

    def format_contracts(self, codes: Iterable[str], *, origin: str = "context") -> str:
        return "\n\n".join(
            json.dumps(self.load(code, origin=origin), ensure_ascii=False)
            for code in dict.fromkeys(codes)
        )

    def manifest(self) -> List[Dict[str, str]]:
        return [
            {"station_code": code, "source_sha256": source["source_sha256"], "source_path": source["source_path"]}
            for code, source in sorted(self.loaded.items())
        ]

    def truth_digest(self) -> str:
        return self._snapshot_digest

    def assert_current(self) -> None:
        try:
            self.loader.assert_snapshot_current()
        except RuntimeError as exc:
            raise WorkstationTruthChangedError(str(exc)) from exc
        if self.current_truth_digest() != self._snapshot_digest:
            raise WorkstationTruthChangedError(
                "Workstation contracts, availability or wire schema changed during this Device run"
            )

    def current_truth_digest(self) -> str:
        # Include effective wire facts and IDs as well as the complete source
        # digest; neither selected station names nor prompt text are inputs.
        payload = {
            "workstation_truth": self.loader.truth_source_digest(),
            "dispatch_stations": self.dispatch_catalog.stations,
            "dispatch_ids": self.dispatch_catalog.station_ids,
            "auxiliary_sources": self.auxiliary_source_manifest(self.dispatch_catalog, self.workflow_validator),
        }
        return "device_truth_" + hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def auxiliary_source_manifest(dispatch_catalog: Any, workflow_validator: Any = None) -> Dict[str, str]:
        """Wire and validator references must share the same run snapshot too."""
        try:
            from .utils.paths import format_reference_path, workstation_dir
        except ImportError:
            from utils.paths import format_reference_path, workstation_dir
        paths = {Path(path) for path in dispatch_catalog._conversion_paths()}
        paths.update({format_reference_path("json"), format_reference_path("txt")})
        validator_sources = getattr(workflow_validator, "source_paths", None)
        if callable(validator_sources):
            paths.update(Path(path) for path in validator_sources())
        else:
            old_root = workstation_dir(use_new_format=False)
            if old_root.is_dir():
                paths.update(old_root.glob("*.json"))
        return {
            str(path): path.read_text(encoding="utf-8") if path.is_file() else ""
            for path in sorted(paths)
        }
