"""Materialize workstation recipe files before Skill/dispatch review.

The workstation workflow stores a file path in ``上传文件``.  A path-shaped
string is not an executable artifact, so this module derives the file contents
from the translated step, writes an experiment-scoped XLSX, and rewrites the
step to point at that concrete file before the LLM Skill reviewer sees it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "device_agent" / "output" / "generated_recipes"
BUILDER_SOURCE = (
    Path(__file__).resolve().parent
    / "utils"
    / "build_solid_recipe_artifact.mjs"
)
TARGET_STATIONS = {
    "多通道固体称量工作站_V1",
    "Multi_Channel_Solid_Weighing_Workstation_V1",
}
TARGET_OPERATION = "固体进样-文件传参-机器人"
BUNDLED_DEPENDENCIES_ROOT = (
    Path.home()
    / ".cache"
    / "codex-runtimes"
    / "codex-primary-runtime"
    / "dependencies"
)
BUNDLED_NODE = BUNDLED_DEPENDENCIES_ROOT / "node" / "bin" / "node"
BUNDLED_NODE_MODULES = BUNDLED_DEPENDENCIES_ROOT / "node" / "node_modules"


class RecipeMaterializationError(ValueError):
    """A file-backed workstation step could not be made executable."""


def _safe_segment(value: str, fallback: str = "experiment") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return cleaned[:96] or fallback


def _as_positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise RecipeMaterializationError(f"{field} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RecipeMaterializationError(f"{field} must be an integer") from exc
    if number <= 0:
        raise RecipeMaterializationError(f"{field} must be greater than zero")
    return number


def _mass_values_g(text: str) -> List[float]:
    values: List[float] = []
    # Validation ranges describe the workstation contract, not requested
    # recipe masses.  Strip forms such as ``[0, 50] g`` and ``(0, 200) g``
    # before extracting values.
    text = re.sub(
        r"[\[(]\s*\d+(?:\.\d+)?\s*[,，]\s*\d+(?:\.\d+)?\s*[\])]\s*"
        r"(?:mg|毫克|g|克)",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    pattern = re.compile(
        r"(?<![0-9.])(\d+(?:\.\d+)?)\s*(mg|毫克|g|克)(?![A-Za-z])",
        re.IGNORECASE,
    )
    for raw, unit in pattern.findall(text):
        value = float(raw)
        if unit.lower() in {"mg", "毫克"}:
            value /= 1000.0
        if 0 <= value <= 50 and not any(abs(value - prior) < 1e-12 for prior in values):
            values.append(value)
    return values


def _hopper_number(text: str) -> Optional[int]:
    match = re.search(
        r"料(?:罐|斗)(?:号|编号)?\s*(?:统一\s*)?(?:为|是|=|:|：)?\s*"
        r"[\[(]?\s*(\d+)\s*[\])] ?",
        text,
    )
    if not match:
        match = re.search(
            r"料(?:罐|斗)(?:号|编号)?\s*(?:统一\s*)?(?:为|是|=|:|：)?\s*"
            r"[\[(]?\s*(\d+)",
            text,
        )
    if not match:
        return None
    value = int(match.group(1))
    return value if 1 <= value <= 30 else None


def _explicit_recipe_rows(
    text: str,
    *,
    count: int,
    container_ids: List[Any],
) -> List[Dict[str, Any]]:
    """Parse explicit ``瓶N: 加样量=...; 料罐号=...`` rows.

    Repeated masses are meaningful (several target bottles often receive the
    same aliquot), and each row may select a different hopper.  The legacy
    scalar extractor intentionally de-duplicates repeated textual evidence,
    so structured row evidence must be handled first and losslessly.
    """
    row_pattern = re.compile(
        r"瓶(?:号)?\s*(\d+)\s*[:：]\s*"
        r"(?:(?!瓶(?:号)?\s*\d+\s*[:：]).)*?"
        r"加样量\s*(?:为|是|=|:|：)?\s*(\d+(?:\.\d+)?)\s*"
        r"(mg|毫克|g|克)"
        r"(?:(?!瓶(?:号)?\s*\d+\s*[:：]).)*?"
        r"料(?:罐|斗)(?:号|编号)?\s*(?:为|是|=|:|：)?\s*(\d+)",
        re.IGNORECASE | re.DOTALL,
    )
    matches = row_pattern.findall(text)
    if not matches:
        return []

    by_container: Dict[int, Dict[str, Any]] = {}
    for raw_container, raw_mass, unit, raw_hopper in matches:
        container = int(raw_container)
        if container in by_container:
            raise RecipeMaterializationError(
                f"duplicate explicit recipe row for container {container}"
            )
        mass_g = float(raw_mass)
        if unit.lower() in {"mg", "毫克"}:
            mass_g /= 1000.0
        hopper = int(raw_hopper)
        if not 0 <= mass_g <= 50:
            raise RecipeMaterializationError(
                f"explicit 加样量 for container {container} is outside [0, 50] g"
            )
        if not 1 <= hopper <= 30:
            raise RecipeMaterializationError(
                f"explicit 料罐号 for container {container} is outside [1, 30]"
            )
        by_container[container] = {
            "mass_g": mass_g,
            "hopper_number": hopper,
        }

    try:
        ordered_ids = [int(value) for value in container_ids]
    except (TypeError, ValueError) as exc:
        raise RecipeMaterializationError(
            "explicit recipe rows require integer 容器编号 values"
        ) from exc
    if len(matches) != count or set(by_container) != set(ordered_ids):
        raise RecipeMaterializationError(
            "explicit recipe rows must cover every 容器编号 exactly once"
        )
    return [
        {
            # The workbook column is the consecutive recipe-row ordinal; the
            # actual target ids remain in workflow_json and define row order.
            "bottle_number": index,
            "mass_g": float(by_container[container]["mass_g"]),
            "hopper_number": int(by_container[container]["hopper_number"]),
        }
        for index, container in enumerate(ordered_ids, start=1)
    ]


def extract_v1_recipe_rows(step: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Derive the V1 three-column recipe from one workflow step."""
    parameters = step.get("parameters")
    if not isinstance(parameters, dict):
        raise RecipeMaterializationError("solid weighing step parameters must be an object")
    count = _as_positive_int(parameters.get("容器数量"), field="容器数量")
    container_ids = parameters.get("容器编号")
    if not isinstance(container_ids, list) or len(container_ids) != count:
        raise RecipeMaterializationError("容器编号 count must equal 容器数量")

    evidence = "\n".join(
        str(value)
        for value in (
            step.get("notes", ""),
            parameters.get("配方", ""),
            parameters.get("加样量", ""),
            parameters.get("料罐号", ""),
            parameters.get("料斗号", ""),
        )
        if value not in (None, "", [], {})
    )
    explicit_rows = _explicit_recipe_rows(
        evidence,
        count=count,
        container_ids=container_ids,
    )
    if explicit_rows:
        return explicit_rows
    masses = _mass_values_g(evidence)
    if not masses:
        raise RecipeMaterializationError(
            "cannot derive 加样量(g) from solid weighing step notes/parameters"
        )
    if len(masses) == 1:
        masses = masses * count
    elif len(masses) != count:
        raise RecipeMaterializationError(
            "number of distinct 加样量 values must be one or equal 容器数量"
        )

    hopper = _hopper_number(evidence)
    if hopper is None:
        raise RecipeMaterializationError(
            "cannot derive one 料罐号/料斗号 within [1, 30]"
        )
    return [
        {
            # Workstation truth defines this as the recipe row ordinal, always
            # consecutive from 1; target container ids live in workflow_json.
            "bottle_number": index,
            "mass_g": float(masses[index - 1]),
            "hopper_number": hopper,
        }
        for index in range(1, count + 1)
    ]


def _sidecar_path(workbook_path: Path) -> Path:
    return workbook_path.with_name(workbook_path.name + ".recipe.json")


def _sidecar_matches(workbook_path: Path, rows: List[Dict[str, Any]]) -> bool:
    sidecar = _sidecar_path(workbook_path)
    if not workbook_path.is_file() or not sidecar.is_file():
        return False
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return payload.get("rows") == rows


def _write_sidecar(workbook_path: Path, rows: List[Dict[str, Any]]) -> None:
    _sidecar_path(workbook_path).write_text(
        json.dumps(
            {
                "workstation": "Multi_Channel_Solid_Weighing_Workstation_V1",
                "operation": TARGET_OPERATION,
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _artifact_builder(
    output_path: Path,
    rows: List[Dict[str, Any]],
    *,
    node_binary: Optional[str] = None,
    node_modules: Optional[str] = None,
) -> None:
    node_path, modules_path = _resolve_spreadsheet_runtime(
        node_binary=node_binary,
        node_modules=node_modules,
    )
    if not BUILDER_SOURCE.is_file():
        raise RecipeMaterializationError(f"missing workbook builder: {BUILDER_SOURCE}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="chem-solid-recipe-") as temp_text:
        temp_dir = Path(temp_text)
        (temp_dir / "node_modules").symlink_to(modules_path, target_is_directory=True)
        builder = temp_dir / BUILDER_SOURCE.name
        shutil.copy2(BUILDER_SOURCE, builder)
        rows_path = temp_dir / "rows.json"
        rows_path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        completed = subprocess.run(
            [str(node_path), str(builder), str(output_path), str(rows_path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if completed.returncode != 0 or not output_path.is_file():
            detail = (completed.stderr or completed.stdout)[-1200:]
            raise RecipeMaterializationError(
                f"solid recipe workbook generation failed: {detail.strip()}"
            )
    _write_sidecar(output_path, rows)


def _resolve_spreadsheet_runtime(
    *,
    node_binary: Optional[str] = None,
    node_modules: Optional[str] = None,
) -> tuple[Path, Path]:
    """Resolve explicit runtime settings or the bundled Codex dependency tree."""
    node = node_binary or os.getenv("CHEM_SPREADSHEET_NODE", "").strip()
    modules = node_modules or os.getenv("CHEM_SPREADSHEET_NODE_MODULES", "").strip()
    if not node and not modules and BUNDLED_NODE.is_file() and BUNDLED_NODE_MODULES.is_dir():
        node = str(BUNDLED_NODE)
        modules = str(BUNDLED_NODE_MODULES)
    if not node or not modules:
        raise RecipeMaterializationError(
            "spreadsheet runtime is not configured; set CHEM_SPREADSHEET_NODE and "
            "CHEM_SPREADSHEET_NODE_MODULES, or install the bundled Codex runtime"
        )
    node_path = Path(node).expanduser().resolve()
    modules_path = Path(modules).expanduser().resolve()
    if not node_path.is_file() or not modules_path.is_dir():
        raise RecipeMaterializationError("configured spreadsheet runtime path is invalid")
    return node_path, modules_path


def materialize_workflow_recipe_files(
    workflow_json: Dict[str, Any],
    *,
    exp_id: str,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    builder: Optional[Callable[[Path, List[Dict[str, Any]]], None]] = None,
    known_artifacts: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Create task-scoped recipe files and rewrite their workflow paths in place."""
    steps = workflow_json.get("steps") if isinstance(workflow_json, dict) else None
    if not isinstance(steps, list):
        return []
    records: List[Dict[str, Any]] = []
    safe_exp = _safe_segment(exp_id)
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        if str(step.get("workstation", "")).strip() not in TARGET_STATIONS:
            continue
        if str(step.get("operation", "")).strip() != TARGET_OPERATION:
            continue
        parameters = step.get("parameters")
        if not isinstance(parameters, dict):
            raise RecipeMaterializationError(
                f"step {index}: solid weighing parameters must be an object"
            )
        try:
            rows = extract_v1_recipe_rows(step)
        except RecipeMaterializationError:
            rows = []
            for artifact in known_artifacts or []:
                if not isinstance(artifact, dict):
                    continue
                same_step = artifact.get("step_number") == int(
                    step.get("step_number") or index
                )
                same_macro = (
                    step.get("source_macro_step") is not None
                    and artifact.get("source_macro_step")
                    == step.get("source_macro_step")
                )
                if not (same_step or same_macro):
                    continue
                candidate_rows = artifact.get("rows")
                if isinstance(candidate_rows, list) and candidate_rows:
                    rows = candidate_rows
                    break
            if not rows:
                raise
        output_path = (
            output_root
            / safe_exp
            / f"step_{index:03d}_Multi_Channel_Solid_Weighing_Workstation_V1.xlsx"
        ).resolve()
        requested_text = str(parameters.get("上传文件", "")).strip()
        requested_path = Path(requested_text).expanduser() if requested_text else None
        if requested_path is not None and not requested_path.is_absolute():
            requested_path = (REPO_ROOT / requested_path).resolve()

        if _sidecar_matches(output_path, rows):
            source = "cached_task_scoped"
        elif requested_path is not None and _sidecar_matches(requested_path, rows):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(requested_path, output_path)
            shutil.copy2(_sidecar_path(requested_path), _sidecar_path(output_path))
            source = "copied_verified_recipe"
        else:
            (builder or _artifact_builder)(output_path, rows)
            if not _sidecar_matches(output_path, rows):
                _write_sidecar(output_path, rows)
            source = "generated"

        try:
            relative_path = output_path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            relative_path = str(output_path)
        parameters["上传文件"] = relative_path
        records.append(
            {
                "step_number": int(step.get("step_number") or index),
                "workstation": "Multi_Channel_Solid_Weighing_Workstation_V1",
                "operation": TARGET_OPERATION,
                "source_macro_step": step.get("source_macro_step"),
                "file_path": relative_path,
                "rows": rows,
                "source": source,
            }
        )
    return records
