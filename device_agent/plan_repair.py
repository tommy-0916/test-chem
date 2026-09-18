"""Persistent, permission-constrained local repair for Device plan candidates.

This module implements the four repair responsibilities around the existing
Stage-1 repair flow:

- ``diagnose_candidate``    run the validator and normalize field-level errors
- ``build_repair_context``  assemble the per-round view from the RepairCase
                            (never "append the whole chat history")
- ``propose_repair``        deterministic JSON-Patch proposal inside the
                            authorized scope, or a controlled non-patch verdict
- ``validate_repair``       verify versions/permissions, apply on an isolated
                            copy, re-run the audit, promote the draft only on
                            full acceptance

A RepairCase is persisted program-side JSON (contracts, baseline, draft,
append-only log).  The model never writes it, a saved baseline is not an
approval, and a promoted draft is still not dispatchable until the normal
acceptance gates pass.

Offline usage (no model, no network, no devices):

    python device_agent/plan_repair.py \
        --device-state path/to/device_state.json --progress-index 3 \
        --case-dir result/device_repair_cases/demo --output report.json
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

if __package__ in (None, ""):
    _here = Path(__file__).resolve().parent
    _root = _here.parent
    for _path in (str(_here), str(_root)):
        if _path not in sys.path:
            sys.path.insert(0, _path)
    import single_agent as _single_agent
    from single_agent import (
        RECIPE_FIELD_CONTRACT,
        recipe_step_diagnostics,
        recipe_step_is_file_dosing,
    )
else:
    from . import single_agent as _single_agent
    from .single_agent import (
        RECIPE_FIELD_CONTRACT,
        recipe_step_diagnostics,
        recipe_step_is_file_dosing,
    )

SCHEMA_VERSION = 1
PATCH_FORMAT = "chem-plan-patch/1"
DEFAULT_PATCH_LIMIT = 3
DEFAULT_MAX_ROUNDS = 6

# Findings whose failed checks can be repaired by a deterministic, authorized
# value-type patch.  Anything else routes to the controlled non-patch verdicts.
PATCHABLE_RULES = {"json_number", "json_integer"}

RECIPE_FINDING_MESSAGE = (
    "文件传参固体称量缺少可物化的逐瓶配方。必须给出每行瓶号、"
    "确定加样量(g)和料罐号；不能使用按实测质量/按比例/适量等运行时未知值。"
)

FROZEN_TOP_LEVEL_KEYS = ("sample_control_matrix",)

_AUDITOR = Callable[[Dict[str, Any]], List[Dict[str, Any]]]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def recipe_contract_version() -> str:
    """Read the contract version at call time so tests can simulate drift."""
    return _single_agent.RECIPE_FIELD_CONTRACT_VERSION


# ---------------------------------------------------------------------------
# RepairCase persistence
# ---------------------------------------------------------------------------


def create_repair_case(
    candidate: Dict[str, Any],
    diagnosis: List[Dict[str, Any]],
    *,
    case_id: Optional[str] = None,
    patch_limit: int = DEFAULT_PATCH_LIMIT,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> Dict[str, Any]:
    """Open a repair case around a candidate that failed validation.

    The baseline is preserved verbatim — including its illegality — for
    comparison, rollback and audit.  Saving a baseline never approves it.
    """
    baseline = copy.deepcopy(candidate)
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id or uuid.uuid4().hex,
        "created_at": _utc_now(),
        "contracts": {
            "recipe_contract_version": recipe_contract_version(),
            "recipe_contract": {
                key: (
                    sorted(value)
                    if isinstance(value, (set, frozenset))
                    else copy.deepcopy(value)
                )
                for key, value in RECIPE_FIELD_CONTRACT.items()
            },
        },
        "baseline": {
            "candidate_digest": _digest(baseline),
            "candidate": baseline,
            "diagnosis": copy.deepcopy(diagnosis),
            "captured_at": _utc_now(),
        },
        "draft": {
            "version": 1,
            "parent_version": None,
            "candidate": copy.deepcopy(candidate),
            "status": "working",
            "diagnosis": copy.deepcopy(diagnosis),
            "updated_at": _utc_now(),
        },
        "progress": {"verified": [], "open_issues": []},
        "history_constraints": [],
        "log": [],
        "budgets": {
            "patches_used": 0,
            "patch_limit": patch_limit,
            "max_rounds": max_rounds,
        },
        "stop_reason": "",
    }


def save_repair_case(case: Dict[str, Any], path: Path) -> None:
    _atomic_write_json(path, case)


def load_repair_case(path: Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _log_round(case: Dict[str, Any], entry: Dict[str, Any]) -> None:
    entry = dict(entry)
    entry.setdefault("at", _utc_now())
    entry["round"] = len(case["log"]) + 1
    case["log"].append(entry)


# ---------------------------------------------------------------------------
# diagnose_candidate
# ---------------------------------------------------------------------------


def diagnose_candidate(
    auditor: _AUDITOR, candidate: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Run the validator and normalize findings to field-level diagnostics."""
    findings = auditor(candidate) or []
    normalized: List[Dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        item = copy.deepcopy(finding)
        item.setdefault("type", "plan_level_finding")
        item.setdefault("message", str(finding))
        normalized.append(item)
    return normalized


def recipe_auditor(candidate: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Offline deterministic auditor for the file-dosing recipe contract."""
    findings: List[Dict[str, Any]] = []
    for step in candidate.get("device_plan") or []:
        if not isinstance(step, dict):
            continue
        if not recipe_step_is_file_dosing(step):
            continue
        details = recipe_step_diagnostics(step)
        if details is None:
            continue
        findings.append(
            {
                "type": "missing_concrete_recipe_evidence",
                "plan_step": step.get("plan_step"),
                "message": (
                    f"device_plan[{step.get('plan_step', '?')}] {RECIPE_FINDING_MESSAGE}"
                ),
                "details": details,
            }
        )
    return findings


def failed_checks(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten every failed check across findings (never short-circuited)."""
    checks: List[Dict[str, Any]] = []
    for finding in findings:
        details = finding.get("details") or {}
        for check in details.get("checks") or []:
            if isinstance(check, dict) and check.get("status") == "failed":
                item = dict(check)
                item.setdefault("plan_step", details.get("plan_step"))
                item.setdefault("contract_version", details.get("contract_version"))
                checks.append(item)
    return checks


def _findings_signature(findings: List[Dict[str, Any]]) -> Tuple[Tuple[Any, ...], ...]:
    signature = []
    for check in failed_checks(findings):
        signature.append(
            (
                check.get("plan_step"),
                check.get("field"),
                check.get("rule"),
                type(check.get("actual")).__name__,
                str(check.get("actual"))[:80],
            )
        )
    for finding in findings:
        if not failed_checks([finding]):
            signature.append((finding.get("type"), str(finding.get("message"))[:120]))
    return tuple(sorted(signature))


# ---------------------------------------------------------------------------
# build_repair_context
# ---------------------------------------------------------------------------


def _failing_step_excerpts(
    candidate: Dict[str, Any], findings: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Dependency-selected excerpts: only steps named by the diagnosis."""
    plan_steps = candidate.get("device_plan") or []
    step_ids = []
    for finding in findings:
        details = finding.get("details") or {}
        if details.get("plan_step") is not None:
            step_ids.append(details["plan_step"])
    excerpts: List[Dict[str, Any]] = []
    seen = set()
    for step in plan_steps:
        if not isinstance(step, dict):
            continue
        if step.get("plan_step") in step_ids and step.get("plan_step") not in seen:
            seen.add(step.get("plan_step"))
            excerpts.append(
                {
                    "step_ref": f"device_plan[step={step.get('plan_step')}]",
                    "plan_step": step.get("plan_step"),
                    "workstation": step.get("workstation"),
                    "operation_intent": step.get("operation_intent"),
                    "key_values": copy.deepcopy(step.get("key_values", {})),
                    "containers": copy.deepcopy(step.get("containers", {})),
                    "sample_lineage": copy.deepcopy(step.get("sample_lineage", {})),
                }
            )
    return excerpts


def build_repair_context(case: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble this round's model-facing view from the repair case.

    Facts and draft data are explicitly separated; the excerpt contains only
    the steps the current diagnosis depends on — never the whole plan, the
    chat history, or environment secrets.
    """
    draft = case["draft"]
    findings = draft.get("diagnosis") or []
    contract_version = case["contracts"]["recipe_contract_version"]
    history = [
        entry["statement"]
        for entry in case.get("history_constraints", [])
        if entry.get("contract_version") == contract_version
    ]
    errors = []
    for check in failed_checks(findings):
        errors.append(
            {
                "step_ref": f"device_plan[step={check.get('plan_step')}]",
                "path": check.get("path"),
                "field": check.get("field"),
                "rule": check.get("rule"),
                "expected": check.get("expected"),
                "actual": check.get("actual"),
                "actual_type": check.get("actual_type"),
            }
        )
    context = {
        "data_notice": (
            "当前草稿为待验证的模型输出，不是已批准的执行事实；"
            "合同与权限来自程序控制来源。"
        ),
        "task": (
            "修复当前配方行的 JSON 值类型。不重新生成完整设备计划，"
            "不重新审查工作站，不改变实验方案。"
        ),
        "target": {
            "step_refs": sorted({error["step_ref"] for error in errors}),
            "draft_version": draft.get("version"),
        },
        "permissions": {
            "allowed": [
                "仅修改诊断指出的字段值类型（数字字符串 → JSON number）",
                "保持数值大小与对象映射不变",
            ],
            "forbidden": [
                "Research 方案、冻结样品矩阵、物料身份",
                "瓶位映射与其他计划步骤",
                "校验器、技能合同与别名表",
            ],
        },
        "contract": {
            "name": "file_dosing_recipe",
            "version": contract_version,
            "fields": {
                "bottle": sorted(RECIPE_FIELD_CONTRACT["bottle_keys"]),
                "mass": sorted(RECIPE_FIELD_CONTRACT["mass_keys"]),
                "hopper": sorted(RECIPE_FIELD_CONTRACT["hopper_keys"]),
                "wrappers": sorted(RECIPE_FIELD_CONTRACT["wrapper_keys"]),
            },
            "value_rules": copy.deepcopy(RECIPE_FIELD_CONTRACT["value_rules"]),
        },
        "draft_excerpt": _failing_step_excerpts(
            draft.get("candidate", {}), findings
        ),
        "field_errors": errors,
        "verified_progress": list(case.get("progress", {}).get("verified", [])),
        "open_issues": list(case.get("progress", {}).get("open_issues", [])),
        "history_constraints": history,
    }
    return context


def context_manifest(context: Dict[str, Any]) -> Dict[str, str]:
    """Content hashes of each context section for the append-only log."""
    return {
        key: _digest(value)
        for key, value in sorted(context.items())
        if isinstance(value, (dict, list, str))
    }


# ---------------------------------------------------------------------------
# path grammar: device_plan[step=N].key_values.配方行[0].瓶号
# ---------------------------------------------------------------------------


def _parse_path(path: str) -> List[Tuple[str, Any]]:
    segments: List[Tuple[str, Any]] = []
    for part in path.split("."):
        while "[" in part:
            head, rest = part.split("[", 1)
            index_text, part = rest.split("]", 1)
            if head:
                segments.append(("key", head))
            if index_text.startswith("step="):
                segments.append(("step", index_text[len("step=") :]))
            else:
                segments.append(("index", int(index_text)))
        if part:
            segments.append(("key", part))
    return segments


def resolve_path(
    candidate: Dict[str, Any], path: str
) -> Tuple[Any, Any, List[Tuple[str, Any]]]:
    """Resolve a path to (container, final_key, resolved_segments)."""
    segments = _parse_path(path)
    node: Any = candidate
    resolved: List[Tuple[str, Any]] = []
    for position, (kind, value) in enumerate(segments):
        last = position == len(segments) - 1
        if kind == "step":
            steps = node.get("device_plan") if isinstance(node, dict) else node
            if not isinstance(steps, list):
                raise KeyError(f"path step {value!r} has no device_plan list")
            match = None
            for index, step in enumerate(steps):
                if isinstance(step, dict) and str(step.get("plan_step")) == str(value):
                    match = (index, step)
                    break
            if match is None:
                raise KeyError(f"path step {value!r} not found in device_plan")
            node = match[1]
            resolved.append(("step_index", match[0]))
            continue
        if kind == "key":
            if not isinstance(node, dict) or value not in node:
                raise KeyError(f"path key {value!r} not found")
            if last:
                return node, value, resolved
            node = node[value]
            resolved.append(("key", value))
            continue
        # list index
        if not isinstance(node, list) or not (0 <= int(value) < len(node)):
            raise KeyError(f"path index {value!r} out of range")
        if last:
            return node, int(value), resolved
        node = node[int(value)]
        resolved.append(("index", int(value)))
    raise KeyError(f"path {path!r} does not select a value")


def canonical_path(candidate: Dict[str, Any], path: str) -> str:
    """Resolve step selectors to stable index-based paths for diffing.

    The output format matches ``_diff_paths`` exactly so permission checks
    compare like with like.
    """
    segments = _parse_path(path)
    node: Any = candidate
    chunks: List[str] = []
    for kind, value in segments:
        if kind == "step":
            steps = node.get("device_plan") if isinstance(node, dict) else node
            if not isinstance(steps, list):
                raise KeyError(f"path step {value!r} has no device_plan list")
            for index, step in enumerate(steps):
                if isinstance(step, dict) and str(step.get("plan_step")) == str(value):
                    chunk = f"device_plan[{index}]"
                    if chunks and chunks[-1] == "device_plan":
                        chunks[-1] = chunk
                    else:
                        chunks.append(chunk)
                    node = step
                    break
            else:
                raise KeyError(f"path step {value!r} not found in device_plan")
            continue
        if kind == "key":
            chunks.append(str(value))
            node = node[value] if isinstance(node, dict) else None
            continue
        if not chunks:
            raise KeyError(f"path index {value!r} without container")
        chunks[-1] = f"{chunks[-1]}[{value}]"
        node = node[int(value)] if isinstance(node, list) else None
    return ".".join(chunks)


def _diff_paths(before: Any, after: Any, prefix: str = "") -> List[str]:
    paths: List[str] = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in before or key not in after:
                paths.append(child)
            else:
                paths.extend(_diff_paths(before[key], after[key], child))
        return paths
    if isinstance(before, list) and isinstance(after, list):
        for index in range(max(len(before), len(after))):
            child = f"{prefix}[{index}]"
            if index >= len(before) or index >= len(after):
                paths.append(child)
            else:
                paths.extend(_diff_paths(before[index], after[index], child))
        return paths
    if before != after:
        paths.append(prefix)
    return paths


# ---------------------------------------------------------------------------
# propose_repair
# ---------------------------------------------------------------------------


_NUMERIC_STRING = re.compile(r"^(?:\d+(?:\.\d+)?|\.\d+)$")
_INTEGER_STRING = re.compile(r"^\d+$")


def _coerce_numeric_string(value: Any, rule: str) -> Optional[Any]:
    """Lossless numeric-string -> JSON number coercion, or None if unsafe."""
    if isinstance(value, bool) or not isinstance(value, str):
        return None
    text = value.strip()
    pattern = _INTEGER_STRING if rule == "json_integer" else _NUMERIC_STRING
    if not pattern.match(text):
        return None
    try:
        number: Any = int(text) if rule == "json_integer" else float(text)
    except ValueError:  # pragma: no cover - regex already guarded
        return None
    import math

    if isinstance(number, float) and not math.isfinite(number):
        return None
    if number <= 0:
        return None
    if rule == "json_integer" and isinstance(number, float):
        return None
    return number


def propose_repair(
    context: Dict[str, Any], draft_candidate: Dict[str, Any]
) -> Dict[str, Any]:
    """Propose a deterministic local patch, or a controlled non-patch verdict.

    A patch is proposed only when every field error is an authorized value
    type error (contract-correct field name, actual value a losslessly
    parseable numeric string, single-value replace inside the diagnosed
    path).  Anything else returns ``requires_llm`` or ``manual`` — never a
    best-effort rewrite.
    """
    errors = context.get("field_errors") or []
    if not errors:
        return {"status": "accepted", "patch": None, "reason": "no open errors"}
    operations: List[Dict[str, Any]] = []
    unpatchable: List[Dict[str, Any]] = []
    for error in errors:
        rule = error.get("rule")
        if rule not in PATCHABLE_RULES:
            unpatchable.append({**error, "reason": f"rule {rule!r} not patchable"})
            continue
        coerced = _coerce_numeric_string(error.get("actual"), str(rule))
        if coerced is None:
            unpatchable.append(
                {**error, "reason": "actual value is not a lossless numeric string"}
            )
            continue
        path = f"{error['step_ref']}.{error['path']}"
        try:
            container, key, _ = resolve_path(draft_candidate, path)
        except (KeyError, AttributeError, TypeError) as exc:
            unpatchable.append({**error, "reason": f"path unresolvable: {exc}"})
            continue
        if container[key] != error.get("actual"):
            unpatchable.append(
                {**error, "reason": "draft value no longer matches diagnosis"}
            )
            continue
        operations.append(
            {"op": "test", "path": path, "value": container[key]}
        )
        operations.append(
            {
                "op": "replace",
                "path": path,
                "value": coerced,
                "original_text": error.get("actual"),
            }
        )
    if unpatchable:
        if operations:
            reason = "partial"
        else:
            reason = "manual" if not errors else "requires_llm"
        return {
            "status": reason,
            "patch": None,
            "unpatchable": unpatchable,
            "patchable_count": len(operations) // 2,
        }
    return {
        "status": "patch_ready",
        "patch": {
            "format": PATCH_FORMAT,
            "base_draft_version": context.get("target", {}).get("draft_version"),
            "contract_version": context.get("contract", {}).get("version"),
            "ops": operations,
        },
    }


# ---------------------------------------------------------------------------
# validate_repair (commit entry)
# ---------------------------------------------------------------------------


def _refresh_progress(
    case: Dict[str, Any], findings: List[Dict[str, Any]], *, mark_resolved: bool = False
) -> None:
    """Validator-owned progress table: models never self-report progress."""
    verified: List[str] = []
    open_issues: List[str] = []
    for finding in findings:
        details = finding.get("details") or {}
        step_ref = f"device_plan[step={details.get('plan_step')}]"
        for check in details.get("checks") or []:
            if not isinstance(check, dict):
                continue
            if check.get("status") == "passed":
                text = f"{step_ref} 字段 {check.get('field')} 符合合同（{check.get('rule')}）"
                if text not in verified:
                    verified.append(text)
            elif check.get("status") == "failed" and mark_resolved:
                text = (
                    f"{step_ref} 字段 {check.get('field')} 的 {check.get('rule')} 错误"
                    "已由授权补丁修复并复检通过"
                )
                if text not in verified:
                    verified.append(text)
            elif check.get("status") == "failed":
                text = (
                    f"{step_ref} 字段 {check.get('field')}：期望 {check.get('expected')}，"
                    f"实际 {check.get('actual_type')} {check.get('actual')!r}"
                )
                if text not in open_issues:
                    open_issues.append(text)
    case["progress"] = {"verified": verified, "open_issues": open_issues}


def _record_history_constraints(
    case: Dict[str, Any], findings: List[Dict[str, Any]]
) -> None:
    contract_version = case["contracts"]["recipe_contract_version"]
    seen = {
        (entry.get("contract_version"), entry.get("statement"))
        for entry in case["history_constraints"]
    }
    for check in failed_checks(findings):
        statement = (
            f"字段 {check.get('field')} 以 {check.get('actual_type')} 提供，"
            f"未通过 {check.get('rule')}（已排除）"
        )
        key = (contract_version, statement)
        if key not in seen:
            seen.add(key)
            case["history_constraints"].append(
                {"contract_version": contract_version, "statement": statement}
            )


def validate_repair(
    case: Dict[str, Any], patch: Dict[str, Any], auditor: _AUDITOR
) -> Dict[str, Any]:
    """Validate and commit a patch.  Any failure leaves formal state intact."""
    draft = case["draft"]
    candidate = draft.get("candidate", {})
    pre_patch_findings = copy.deepcopy(draft.get("diagnosis") or [])

    if patch.get("format") != PATCH_FORMAT:
        return {"status": "rejected", "reason": "unsupported_patch_format"}
    if patch.get("contract_version") != case["contracts"]["recipe_contract_version"]:
        return {"status": "rejected", "reason": "contract_mismatch"}
    if recipe_contract_version() != case["contracts"]["recipe_contract_version"]:
        return {"status": "rejected", "reason": "contract_mismatch_current"}
    if patch.get("base_draft_version") != draft.get("version"):
        return {"status": "rejected", "reason": "draft_version_mismatch"}

    # Resolve every op path up front: frozen keys reject before any mutation,
    # and permissions are checked per path during application.
    resolved_ops: List[Tuple[Dict[str, Any], str]] = []
    for operation in patch.get("ops") or []:
        if not isinstance(operation, dict) or operation.get("op") not in {
            "test",
            "replace",
        }:
            return {"status": "rejected", "reason": "unsupported_operation"}
        try:
            path = canonical_path(candidate, str(operation.get("path", "")))
        except (KeyError, AttributeError, TypeError) as exc:
            return {"status": "rejected", "reason": f"path_error: {exc}"}
        for frozen in FROZEN_TOP_LEVEL_KEYS:
            if path == frozen or path.startswith(frozen + ".") or path.startswith(
                frozen + "["
            ):
                return {"status": "rejected", "reason": f"frozen_key_modified: {frozen}"}
        resolved_ops.append((operation, path))

    # Permission scope: only paths named by the current diagnosis may change.
    allowed_paths = set()
    for check in failed_checks(draft.get("diagnosis") or []):
        allowed_paths.add(
            canonical_path(candidate, f"device_plan[step={check.get('plan_step')}].{check.get('path')}")
        )

    patched = copy.deepcopy(candidate)
    for operation, path in resolved_ops:
        try:
            container, key, _ = resolve_path(patched, str(operation.get("path", "")))
        except (KeyError, AttributeError, TypeError) as exc:
            return {"status": "rejected", "reason": f"path_error: {exc}"}
        if operation["op"] == "test":
            if container[key] != operation.get("value"):
                return {"status": "rejected", "reason": f"test_failed: {path}"}
            continue
        if path not in allowed_paths:
            return {"status": "rejected", "reason": f"path_not_authorized: {path}"}
        container[key] = operation.get("value")

    # Frozen invariants: baseline comparisons on an isolated copy only.
    for key in FROZEN_TOP_LEVEL_KEYS:
        if patched.get(key) != case["baseline"]["candidate"].get(key):
            return {"status": "rejected", "reason": f"frozen_key_modified: {key}"}
    if len(patched.get("device_plan") or []) != len(
        case["baseline"]["candidate"].get("device_plan") or []
    ):
        return {"status": "rejected", "reason": "device_plan_length_changed"}

    unauthorized = [
        path
        for path in _diff_paths(candidate, patched)
        if path not in allowed_paths
    ]
    if unauthorized:
        return {
            "status": "rejected",
            "reason": "unauthorized_changes",
            "paths": unauthorized[:10],
        }

    findings = diagnose_candidate(auditor, patched)
    _log_round(
        case,
        {
            "kind": "validate",
            "patch_digest": _digest(patch),
            "patch_ops": len(patch.get("ops") or []),
            "result_findings": len(findings),
        },
    )
    if findings:
        _record_history_constraints(case, findings)
        _refresh_progress(case, findings)
        return {
            "status": "rejected",
            "reason": "audit_still_failing",
            "findings": findings,
        }

    draft["parent_version"] = draft["version"]
    draft["version"] = int(draft["version"]) + 1
    draft["candidate"] = patched
    draft["diagnosis"] = []
    draft["status"] = "working"
    draft["updated_at"] = _utc_now()
    _refresh_progress(case, pre_patch_findings, mark_resolved=True)
    return {"status": "promoted", "draft_version": draft["version"]}


# ---------------------------------------------------------------------------
# repair loop
# ---------------------------------------------------------------------------


def run_repair_loop(
    candidate: Dict[str, Any],
    auditor: _AUDITOR,
    *,
    case_dir: Optional[Path] = None,
    patch_limit: int = DEFAULT_PATCH_LIMIT,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> Dict[str, Any]:
    """Run the controlled local-repair loop for one failed candidate."""
    diagnosis = diagnose_candidate(auditor, candidate)
    case = create_repair_case(
        candidate,
        diagnosis,
        patch_limit=patch_limit,
        max_rounds=max_rounds,
    )
    _log_round(
        case,
        {
            "kind": "open",
            "baseline_digest": case["baseline"]["candidate_digest"],
            "initial_findings": len(diagnosis),
        },
    )

    final_status = "stopped"
    stop_reason = ""
    previous_signature: Optional[Tuple[Tuple[Any, ...], ...]] = None

    while True:
        if int(case["budgets"]["patches_used"]) >= int(case["budgets"]["patch_limit"]):
            final_status = "stopped"
            stop_reason = "patch_budget_exhausted"
            _log_round(case, {"kind": "stop", "reason": stop_reason})
            break
        if len(case["log"]) >= int(case["budgets"]["max_rounds"]):
            final_status = "stopped"
            stop_reason = "round_budget_exhausted"
            _log_round(case, {"kind": "stop", "reason": stop_reason})
            break
        findings = diagnose_candidate(auditor, case["draft"]["candidate"])
        case["draft"]["diagnosis"] = findings
        if not findings:
            case["draft"]["status"] = "accepted"
            final_status = "accepted"
            stop_reason = "accepted"
            _log_round(case, {"kind": "accept", "reason": "no findings remain"})
            break
        signature = _findings_signature(findings)
        if signature == previous_signature:
            final_status = "stopped"
            stop_reason = "no_progress"
            _log_round(
                case,
                {
                    "kind": "stop",
                    "reason": "no_progress",
                    "detail": "same error signature as previous round",
                },
            )
            break
        previous_signature = signature

        context = build_repair_context(case)
        proposal = propose_repair(context, case["draft"]["candidate"])
        if proposal["status"] == "patch_ready":
            case["budgets"]["patches_used"] = int(case["budgets"]["patches_used"]) + 1
        _log_round(
            case,
            {
                "kind": "propose",
                "status": proposal["status"],
                "context_manifest": context_manifest(context),
                "patch_ops": len((proposal.get("patch") or {}).get("ops") or []),
            },
        )
        if proposal["status"] != "patch_ready":
            final_status = "stopped"
            stop_reason = proposal["status"]
            _log_round(
                case,
                {
                    "kind": "stop",
                    "reason": stop_reason,
                    "unpatchable": proposal.get("unpatchable", []),
                },
            )
            break
        result = validate_repair(case, proposal["patch"], auditor)
        if result["status"] == "promoted":
            _log_round(
                case,
                {"kind": "promote", "draft_version": result["draft_version"]},
            )
            continue
        _log_round(case, {"kind": "reject", "reason": result["reason"]})
        if result["reason"] == "audit_still_failing":
            # Keep D1 and give the loop one more chance; an unchanged error
            # signature stops the loop as no_progress above.
            continue
        final_status = "stopped"
        stop_reason = f"patch_rejected:{result['reason']}"
        break

    case["stop_reason"] = stop_reason
    if case_dir is not None:
        save_repair_case(case, Path(case_dir) / "repair_case.json")
    return {
        "status": final_status,
        "stop_reason": stop_reason,
        "case": case,
        "final_candidate": case["draft"]["candidate"],
        "draft_status": case["draft"]["status"],
    }


# ---------------------------------------------------------------------------
# CLI: offline replay against a saved device state
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-state", required=True, help="Saved device state JSON")
    parser.add_argument(
        "--progress-index",
        type=int,
        required=True,
        help="feasibility_progress index whose candidate should be repaired",
    )
    parser.add_argument("--auditor", choices=["recipe"], default="recipe")
    parser.add_argument("--case-dir", help="Directory for repair_case.json")
    parser.add_argument("--output", help="Optional JSON report path")
    args = parser.parse_args(argv)

    state = json.loads(Path(args.device_state).read_text(encoding="utf-8"))
    entries = state.get("feasibility_progress") or []
    if not (0 <= args.progress_index < len(entries)):
        raise SystemExit(
            f"progress index {args.progress_index} out of range ({len(entries)} entries)"
        )
    candidate = entries[args.progress_index].get("candidate")
    if not isinstance(candidate, dict) or not candidate.get("device_plan"):
        raise SystemExit("selected progress entry carries no candidate device_plan")

    auditor = recipe_auditor
    result = run_repair_loop(
        candidate,
        auditor,
        case_dir=Path(args.case_dir) if args.case_dir else None,
    )
    report = {
        "device_state": str(args.device_state),
        "progress_index": args.progress_index,
        "status": result["status"],
        "stop_reason": result["stop_reason"],
        "draft_status": result["draft_status"],
        "patches_used": result["case"]["budgets"]["patches_used"],
        "rounds": len(result["case"]["log"]),
        "progress": result["case"]["progress"],
        "final_candidate_digest": _digest(result["final_candidate"]),
        "baseline_digest": result["case"]["baseline"]["candidate_digest"],
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).expanduser().write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if result["status"] == "accepted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
