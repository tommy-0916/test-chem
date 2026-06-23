"""Enrich reversed workflow JSON with parameters stored in referenced files."""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import unquote

import pandas as pd


PATH_RE = re.compile(r"/files/[^\s\"']+")
PRINTABLE_RE = re.compile(rb"[\x20-\x7E]{6,}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument(
        "--smart-root",
        type=Path,
        default=Path("/Users/yutinghuang/Documents/agent code/chem-data/智慧楼/files"),
    )
    parser.add_argument(
        "--lab303-root",
        type=Path,
        default=Path("/Users/yutinghuang/Documents/agent code/chem-data/303/files"),
    )
    args = parser.parse_args()

    output_path = args.output or args.input.with_name(
        args.input.stem + "_补全参数.json"
    )
    roots = {
        "智慧楼实验数据": args.smart_root,
        "303实验数据": args.lab303_root,
    }

    data = json.loads(args.input.read_text(encoding="utf-8"))
    stats = {
        "records": 0,
        "original_steps": 0,
        "generated_steps": 0,
        "external_references": 0,
        "resolved_files": 0,
        "unresolved_files": 0,
        "parse_errors": 0,
    }

    enriched: Dict[str, Any] = {}
    for sheet_name, rows in data.items():
        root = roots.get(sheet_name)
        enriched_rows = []
        for row in rows:
            stats["records"] += 1
            new_row = deepcopy(row)
            workflow = new_row.get("workflow_json") or {}
            original_steps = workflow.get("steps") or []
            enriched_steps = []

            for step in original_steps:
                stats["original_steps"] += 1
                base_step = deepcopy(step)
                external_infos, generated = enrich_step(
                    step=step,
                    root=root,
                    stats=stats,
                )
                if external_infos:
                    base_step.setdefault("parameters", {})["_external_files"] = external_infos
                enriched_steps.append(base_step)
                enriched_steps.extend(generated)

            renumber_steps(enriched_steps)
            workflow["steps"] = enriched_steps
            workflow.setdefault("unknown_steps", None)
            workflow["enrichment_summary"] = {
                "original_step_count": len(original_steps),
                "enriched_step_count": len(enriched_steps),
                "generated_step_count": len(enriched_steps) - len(original_steps),
            }
            new_row["workflow_json"] = workflow
            new_row["workflow_text"] = format_workflow_text(enriched_steps)
            enriched_rows.append(new_row)
        enriched[sheet_name] = enriched_rows

    output_path.write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(output_path))
    print(json.dumps(stats, ensure_ascii=False, indent=2))


def enrich_step(
    step: Mapping[str, Any],
    root: Optional[Path],
    stats: Dict[str, int],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    params = step.get("parameters") or {}
    refs = collect_external_refs(params)
    external_infos = []
    generated_steps = []

    for ref in refs:
        stats["external_references"] += 1
        decoded = unquote(ref["url"])
        local_path = resolve_local_path(decoded, root) if root else None
        info = {
            "param_code": ref["param_code"],
            "json_path": ref["json_path"],
            "url": decoded,
            "local_path": str(local_path) if local_path else "",
            "resolved": bool(local_path),
        }
        if not local_path:
            stats["unresolved_files"] += 1
            external_infos.append(info)
            continue

        stats["resolved_files"] += 1
        try:
            parsed = parse_external_file(local_path)
        except Exception as exc:  # Keep going; one file should not block the dataset.
            stats["parse_errors"] += 1
            parsed = {
                "kind": "解析失败",
                "file_type": local_path.suffix.lower(),
                "error": f"{type(exc).__name__}: {exc}",
            }
        info.update(file_summary(parsed))
        external_infos.append(info)
        new_steps = build_generated_steps(step, ref, decoded, local_path, parsed)
        generated_steps.extend(new_steps)
        stats["generated_steps"] += len(new_steps)

    return external_infos, generated_steps


def collect_external_refs(value: Any, param_code: str = "", path: str = "parameters") -> List[Dict[str, str]]:
    refs: List[Dict[str, str]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            refs.extend(collect_external_refs(item, str(key), f"{path}/{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            refs.extend(collect_external_refs(item, param_code, f"{path}/{index}"))
    elif isinstance(value, str):
        if "url" in param_code.lower() and value.strip():
            refs.append({"param_code": param_code, "json_path": path, "url": value})
        else:
            for match in PATH_RE.findall(value):
                refs.append({"param_code": param_code, "json_path": path, "url": match})
    return refs


def resolve_local_path(decoded_url: str, root: Optional[Path]) -> Optional[Path]:
    if not root:
        return None
    rel = decoded_url.strip().lstrip("/")
    rel_no_files = rel[len("files/") :] if rel.startswith("files/") else rel
    for candidate in (root / rel, root / rel_no_files):
        if candidate.is_file():
            return candidate
    prefix = root / rel_no_files
    matches = [path for path in prefix.parent.glob(prefix.name + "*") if path.is_file()]
    if len(matches) == 1:
        return matches[0]
    return None


def parse_external_file(path: Path) -> Dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix in {".xlsx"}:
        return parse_excel(path)
    if suffix == ".csv":
        return parse_csv(path)
    if suffix == ".json":
        return parse_json_file(path)
    if suffix == ".zip":
        return parse_zip(path)
    if suffix in {".xyz", ".cif", ".vasp", ".txt", ".htm"}:
        return parse_text(path)
    if suffix in {".imd2", ".xls"}:
        return parse_binary_summary(path)
    return parse_binary_summary(path)


def parse_excel(path: Path) -> Dict[str, Any]:
    xl = pd.ExcelFile(path)
    sheets = []
    for sheet_name in xl.sheet_names:
        df = pd.read_excel(path, sheet_name=sheet_name, dtype=object, keep_default_na=False)
        df = drop_empty(df)
        sheets.append(
            {
                "sheet_name": sheet_name,
                "row_count": int(len(df)),
                "columns": [str(c) for c in df.columns],
                "rows": clean_records(df.to_dict("records")),
            }
        )
    return {
        "kind": classify_file(path),
        "file_type": ".xlsx",
        "sheets": sheets,
    }


def parse_csv(path: Path) -> Dict[str, Any]:
    last_error = None
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            df = pd.read_csv(path, encoding=encoding)
            df = drop_empty(df)
            return {
                "kind": classify_file(path),
                "file_type": ".csv",
                "encoding": encoding,
                "row_count": int(len(df)),
                "columns": [str(c) for c in df.columns],
                "rows": clean_records(df.to_dict("records")),
            }
        except Exception as exc:
            last_error = exc
    raise last_error


def parse_json_file(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "kind": classify_file(path),
        "file_type": ".json",
        "content": clean_value(data),
    }


def parse_zip(path: Path) -> Dict[str, Any]:
    entries = []
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            entries.append(
                {
                    "name": info.filename,
                    "file_size": info.file_size,
                    "compressed_size": info.compress_size,
                }
            )
    return {
        "kind": classify_file(path),
        "file_type": ".zip",
        "entry_count": len(entries),
        "entries": entries,
    }


def parse_text(path: Path) -> Dict[str, Any]:
    text = path.read_text(errors="replace")
    result: Dict[str, Any] = {
        "kind": classify_file(path),
        "file_type": path.suffix.lower(),
        "text": text,
    }
    if path.suffix.lower() == ".xyz":
        result["atom_count"] = parse_first_int_line(text)
    return result


def parse_binary_summary(path: Path) -> Dict[str, Any]:
    data = path.read_bytes()
    strings = [
        item.decode("utf-8", errors="ignore")
        for item in PRINTABLE_RE.findall(data)
    ]
    return {
        "kind": classify_file(path),
        "file_type": path.suffix.lower(),
        "byte_size": len(data),
        "readable_strings_sample": strings[:120],
    }


def build_generated_steps(
    source_step: Mapping[str, Any],
    ref: Mapping[str, str],
    decoded_url: str,
    local_path: Path,
    parsed: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    kind = str(parsed.get("kind") or classify_file(local_path))
    base = {
        "id": source_step.get("id"),
        "source_step_number": source_step.get("step_number"),
        "source_param": ref["param_code"],
        "source_url": decoded_url,
        "source_file": str(local_path),
        "file_kind": kind,
    }
    workstation = generated_workstation_name(source_step, kind)
    steps: List[Dict[str, Any]] = []

    if parsed.get("file_type") == ".xlsx":
        for sheet in parsed.get("sheets", []):
            parameters = {**base, **sheet}
            steps.append(
                make_step(workstation, sheet_operation(kind, sheet["sheet_name"]), parameters)
            )
    elif parsed.get("file_type") == ".csv":
        steps.append(make_step(workstation, f"{kind}参数表", {**base, **parsed}))
    elif parsed.get("file_type") == ".json":
        steps.append(make_step(workstation, f"{kind}参数", {**base, **parsed}))
    elif parsed.get("file_type") == ".zip":
        steps.append(make_step(workstation, f"{kind}文件清单", {**base, **parsed}))
    else:
        steps.append(make_step(workstation, f"{kind}文件内容", {**base, **parsed}))
    return steps


def make_step(workstation: str, operation: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "step_number": 0,
        "workstation": workstation,
        "operation": operation,
        "id": parameters.get("id"),
        "parameters": clean_value(parameters),
        "generated_from_external_file": True,
    }


def classify_file(path: Path) -> str:
    name = path.name
    lower = name.lower()
    if "hiwo任务" in lower:
        return "HIWO任务"
    if "hiwo物料" in lower:
        return "HIWO物料"
    if "sowo任务" in lower:
        return "SOWO任务"
    if "sowo物料" in lower:
        return "SOWO物料"
    if "materials" in lower:
        return "堆栈布局"
    if "多通道固体" in name or "多通道加样" in name:
        return "固体加样"
    if "液体加液" in name or "加液文件" in name:
        return "液体加液"
    if "method" in lower:
        return "仪器方法"
    if "batch" in lower:
        return "进样批次"
    if "样本浓度" in name:
        return "样本浓度"
    if path.suffix.lower() == ".imd2":
        return "ICP-MS方法"
    if path.suffix.lower() in {".xyz", ".cif", ".vasp"}:
        return "结构文件"
    if path.suffix.lower() == ".zip":
        return "压缩包"
    if path.suffix.lower() == ".xls":
        return "Excel旧格式"
    return "外部参数"


def generated_workstation_name(source_step: Mapping[str, Any], kind: str) -> str:
    source = str(source_step.get("workstation") or "外部参数")
    if source == "合成平台":
        return f"合成平台-{kind}"
    if kind in {"固体加样", "液体加液"}:
        return kind
    return f"{source}-{kind}"


def sheet_operation(kind: str, sheet_name: str) -> str:
    if "|||" in sheet_name:
        return sheet_name.split("|||", 1)[0]
    if kind == "固体加样":
        return "固体加样参数"
    return sheet_name


def file_summary(parsed: Mapping[str, Any]) -> Dict[str, Any]:
    summary = {
        "file_kind": parsed.get("kind", ""),
        "file_type": parsed.get("file_type", ""),
    }
    if "sheets" in parsed:
        summary["sheet_count"] = len(parsed.get("sheets", []))
        summary["sheets"] = [
            {
                "sheet_name": sheet.get("sheet_name"),
                "row_count": sheet.get("row_count"),
                "columns": sheet.get("columns", [])[:20],
            }
            for sheet in parsed.get("sheets", [])
        ]
    if "row_count" in parsed:
        summary["row_count"] = parsed.get("row_count")
        summary["columns"] = parsed.get("columns", [])[:20]
    if "entry_count" in parsed:
        summary["entry_count"] = parsed.get("entry_count")
    if "byte_size" in parsed:
        summary["byte_size"] = parsed.get("byte_size")
    if "error" in parsed:
        summary["error"] = parsed.get("error")
    return summary


def renumber_steps(steps: List[Dict[str, Any]]) -> None:
    for index, step in enumerate(steps, start=1):
        step["step_number"] = index


def format_workflow_text(steps: Iterable[Mapping[str, Any]]) -> str:
    blocks = []
    for step in steps:
        lines = [
            f"{step.get('step_number')}. 第{step.get('step_number')}步 {step.get('workstation')}：",
            f"   - 操作：{step.get('operation')}",
        ]
        params = step.get("parameters") or {}
        for key, value in params.items():
            if key in {"rows", "content", "sheets", "text", "readable_strings_sample"}:
                lines.append(f"   - {key}：<{summary_value(value)}>")
            else:
                lines.append(f"   - {key}：{summary_value(value)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def summary_value(value: Any) -> str:
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if isinstance(value, dict):
        return f"dict[{len(value)}]"
    return str(value)


def drop_empty(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df = df.dropna(how="all")
    df = df.loc[:, ~df.apply(lambda col: all(str(v).strip() == "" for v in col))]
    return df


def clean_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [clean_value(record) for record in records]


def clean_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_value(v) for v in value]
    if pd.isna(value) if not isinstance(value, (list, dict, str)) else False:
        return None
    return value


def parse_first_int_line(text: str) -> Optional[int]:
    for line in text.splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


if __name__ == "__main__":
    main()
