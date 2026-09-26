"""Deterministic binding-ledger construction (binding-ledger/v1).

Rebuilds the batch/transition layer of a Device candidate from evidence that
is already frozen inside the candidate itself (steps, containers, material
identity references) plus the frozen Research registry.  Nothing is guessed:
any ambiguity produces an issue and the affected item is left unbuilt.

The same constructor serves history repair and normal generation.
``ensure_binding_ledger`` fills only missing pieces, so a second run over the
same candidate adds nothing (idempotent).

Rule versions: ``frozen-sample-ref/v1`` (sample_id layering),
``binding-ledger/v1`` (batch/transition construction).
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

try:
    from .macro_identity import (
        MacroIdentityError,
        MacroKey,
        extract_source_macro_ids,
        macro_id_key,
        normalize_macro_id,
    )
except ImportError:  # Direct script compatibility.
    from macro_identity import (
        MacroIdentityError,
        MacroKey,
        extract_source_macro_ids,
        macro_id_key,
        normalize_macro_id,
    )

FROZEN_SAMPLE_REF_RULE = "frozen-sample-ref/v1"
BINDING_LEDGER_RULE = "binding-ledger/v1"

RESOURCE_ROLES = {"reagent", "solvent", "resource"}
STATE_CATEGORIES = ("reaction", "purification", "drying", "calcination")
SAMPLE_ID_KEYS = {"sample_id", "control_id", "样品编号", "对照编号"}
GROUP_ID_KEYS = {"group_id"}
MATERIAL_ID_FIELDS = (
    "material_identity_id",
    "research_material_identity_id",
)
MATERIAL_ID_LIST_FIELDS = (
    "material_identity_ids",
    "source_material_identity_ids",
    "co_material_identity_ids",
)


def _slug(text: Any) -> str:
    return re.sub(r"[^0-9a-zA-Z]+", "_", str(text or "")).strip("_").lower()


def _roles_of(entry: Dict[str, Any]) -> set:
    """Frozen registry roles; ``roles`` is the list form, ``role`` the legacy scalar."""
    roles = {str(r).strip() for r in entry.get("roles") or [] if str(r).strip()}
    scalar = str(entry.get("role") or "").strip()
    if scalar:
        roles.add(scalar)
    return roles


class LedgerIssue(dict):
    """One blocked item; never silently skipped."""

    def __init__(self, code: str, message: str, **context: Any) -> None:
        super().__init__(code=code, message=message, context=context)


def _typed_id_token(value: Any, path: str) -> str:
    """Return a readable, collision-resistant token for one typed scalar ID."""

    kind, normalized = macro_id_key(value, path)
    readable = re.sub(r"[^0-9a-zA-Z_-]+", "_", str(normalized)).strip("_")
    readable = readable[:24] or "id"
    encoded = json.dumps(
        [kind, normalized], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:10]
    return f"{kind}_{readable}_{digest}"


def _typed_id_label(value: Any) -> str:
    """Stable log label that keeps number/string identities visibly distinct."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _plan_identity_index(
    candidate: Dict[str, Any],
) -> Tuple[
    List[Dict[str, Any]],
    Dict[int, MacroKey],
    Dict[MacroKey, Any],
    Dict[int, int],
    Dict[MacroKey, Dict[str, Any]],
    List[LedgerIssue],
]:
    """Index ``device_plan`` by opaque typed IDs and reject ambiguity.

    List position supplies execution order only.  It is never used as an ID,
    and IDs are never parsed, stringified, or compared by truthiness.
    """

    issues: List[LedgerIssue] = []
    raw_plan = candidate.get("device_plan")
    if raw_plan is None:
        raw_plan = []
    if not isinstance(raw_plan, list):
        return [], {}, {}, {}, {}, [LedgerIssue(
            "invalid_device_plan",
            "device_plan 必须是数组",
            path="candidate.device_plan",
        )]

    plan: List[Dict[str, Any]] = []
    key_by_object: Dict[int, MacroKey] = {}
    value_by_key: Dict[MacroKey, Any] = {}
    position_by_object: Dict[int, int] = {}
    step_by_key: Dict[MacroKey, Dict[str, Any]] = {}
    for position, raw_step in enumerate(raw_plan):
        path = f"candidate.device_plan[{position}].plan_step"
        if not isinstance(raw_step, dict):
            issues.append(LedgerIssue(
                "invalid_plan_step",
                "device_plan 条目必须是对象",
                path=f"candidate.device_plan[{position}]",
            ))
            continue
        plan.append(raw_step)
        position_by_object[id(raw_step)] = position
        if "plan_step" not in raw_step:
            issues.append(LedgerIssue(
                "missing_plan_step_id",
                "device plan step 缺少稳定 plan_step ID",
                path=path,
            ))
            continue
        raw_id = raw_step["plan_step"]
        try:
            normalized = normalize_macro_id(raw_id, path)
            step_key = macro_id_key(normalized, path)
        except MacroIdentityError as exc:
            issues.append(LedgerIssue(
                "invalid_plan_step_id",
                str(exc),
                path=exc.path,
                plan_step=raw_id,
            ))
            continue
        if step_key in step_by_key:
            issues.append(LedgerIssue(
                "duplicate_plan_step_id",
                "device_plan 中存在重复的 typed plan_step ID",
                path=path,
                plan_step=normalized,
            ))
            continue
        key_by_object[id(raw_step)] = step_key
        value_by_key[step_key] = copy.deepcopy(normalized)
        step_by_key[step_key] = raw_step

    return (
        plan,
        key_by_object,
        value_by_key,
        position_by_object,
        step_by_key,
        issues,
    )


def _validate_plan_references(
    candidate: Dict[str, Any], known_plan_keys: set[MacroKey]
) -> List[LedgerIssue]:
    """Validate explicit plan references without positional or lexical fallback."""

    issues: List[LedgerIssue] = []
    scalar_fields = {"source_plan_step"}
    list_fields = {"source_plan_steps", "processing_step_refs"}

    def check_one(raw: Any, path: str) -> Optional[MacroKey]:
        try:
            normalized = normalize_macro_id(raw, path)
            key = macro_id_key(normalized, path)
        except MacroIdentityError as exc:
            issues.append(LedgerIssue(
                "invalid_plan_step_reference",
                str(exc),
                path=exc.path,
                source_plan_step=raw,
            ))
            return None
        if key not in known_plan_keys:
            issues.append(LedgerIssue(
                "unknown_plan_step_reference",
                "plan-step reference 不存在于当前 device_plan",
                path=path,
                source_plan_step=normalized,
            ))
            # Keep the typed key for duplicate detection; unknown and duplicate
            # are independent contract violations and both must remain visible.
            return key
        return key

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for field, child in value.items():
                child_path = f"{path}.{field}"
                if field in scalar_fields:
                    check_one(child, child_path)
                    continue
                if field in list_fields:
                    if not isinstance(child, list):
                        issues.append(LedgerIssue(
                            "invalid_plan_step_reference",
                            f"{field} 必须是 typed scalar ID 数组",
                            path=child_path,
                        ))
                        continue
                    keys: List[MacroKey] = []
                    for index, item in enumerate(child):
                        key = check_one(item, f"{child_path}[{index}]")
                        if key is not None:
                            keys.append(key)
                    if len(keys) != len(set(keys)):
                        issues.append(LedgerIssue(
                            "duplicate_plan_step_reference",
                            f"{field} 含重复 typed plan-step reference",
                            path=child_path,
                        ))
                    continue
                walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(candidate, "candidate")
    return issues


def resolve_frozen_sample_binding(
    research_handoff: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str], List[LedgerIssue]]:
    """Resolve the current action's typed sample/group without value heuristics.

    The current ``macro_action.experiment_group`` is authoritative.  The V2
    mirror and current ``macro_steps`` are consistency witnesses.  Historical
    observations and prior actions are deliberately ignored.  Older payloads
    may fall back to a top-level frozen sample matrix, but sample/control and
    group fields remain separate and must each be unique.
    """

    issues: List[LedgerIssue] = []
    sources: List[Tuple[str, Optional[str], Optional[str]]] = []
    structured_seen = False

    def add_group(path: str, value: Any) -> None:
        nonlocal structured_seen
        if not isinstance(value, dict):
            return
        structured_seen = True
        sample_values = _explicit_values(value, SAMPLE_ID_KEYS)
        group_values = _explicit_values(value, GROUP_ID_KEYS)
        if len(sample_values) > 1 or len(group_values) > 1:
            issues.append(LedgerIssue(
                "sample_binding_conflict",
                f"{path} 内 sample/control 或 group 标识不唯一",
                path=path,
                sample_ids=sorted(sample_values),
                group_ids=sorted(group_values),
            ))
            return
        sample_id = next(iter(sample_values), None)
        group_id = next(iter(group_values), None)
        if sample_id is not None or group_id is not None:
            sources.append((path, sample_id, group_id))

    macro_action = research_handoff.get("macro_action")
    if isinstance(macro_action, dict):
        add_group("macro_action.experiment_group", macro_action.get("experiment_group"))
    package_v2 = research_handoff.get("research_action_package_v2")
    if isinstance(package_v2, dict):
        v2_action = package_v2.get("macro_action")
        if isinstance(v2_action, dict):
            add_group(
                "research_action_package_v2.macro_action.experiment_group",
                v2_action.get("experiment_group"),
            )

    macro_steps: List[Any] = []
    if isinstance(research_handoff.get("macro_steps"), list):
        macro_steps.extend(research_handoff["macro_steps"])
    if isinstance(package_v2, dict) and isinstance(package_v2.get("macro_steps"), list):
        macro_steps.extend(package_v2["macro_steps"])
    if isinstance(package_v2, dict):
        v2_action = package_v2.get("macro_action")
        if isinstance(v2_action, dict) and isinstance(v2_action.get("macro_steps"), list):
            macro_steps.extend(v2_action["macro_steps"])
    for index, step in enumerate(macro_steps):
        if not isinstance(step, dict):
            continue
        add_group(f"macro_steps[{index}]", step)
        add_group(f"macro_steps[{index}].experiment_group", step.get("experiment_group"))

    structured_present = structured_seen or bool(issues)
    if structured_present:
        sample_ids = {sample for _, sample, _ in sources if sample}
        group_ids = {group for _, _, group in sources if group}
        if len(sample_ids) != 1 or len(group_ids) > 1:
            issues.append(LedgerIssue(
                "sample_binding_conflict",
                "当前 action 的结构化 sample/control/group 绑定互相冲突",
                sources=[
                    {"path": path, "sample_id": sample, "group_id": group}
                    for path, sample, group in sources
                ],
            ))
            return None, None, issues
        if issues:
            return None, None, issues
        return next(iter(sample_ids)), next(iter(group_ids), None), []

    # Legacy fallback is deliberately confined to one top-level frozen matrix.
    matrix = None
    for field in ("sample_control_matrix", "sample_matrix"):
        if isinstance(research_handoff.get(field), (list, dict)):
            matrix = research_handoff[field]
            break
    sample_ids: set[str] = set()
    group_ids: set[str] = set()

    def collect_matrix(value: Any) -> None:
        if isinstance(value, dict):
            sample_ids.update(_explicit_values(value, SAMPLE_ID_KEYS))
            group_ids.update(_explicit_values(value, GROUP_ID_KEYS))
            for child in value.values():
                if isinstance(child, (dict, list)):
                    collect_matrix(child)
        elif isinstance(value, list):
            for child in value:
                collect_matrix(child)

    if matrix is not None:
        collect_matrix(matrix)
    if len(sample_ids) != 1 or len(group_ids) > 1:
        return None, None, [LedgerIssue(
            "sample_binding_unresolved",
            "冻结 sample matrix 未提供唯一 sample/control 与可选 group 绑定",
            sample_ids=sorted(sample_ids),
            group_ids=sorted(group_ids),
        )]
    return next(iter(sample_ids)), next(iter(group_ids), None), []


# ---------------------------------------------------------------------------
# registry / container mapping
# ---------------------------------------------------------------------------


def _strict_material_identity(value: Any) -> Optional[str]:
    """Normalize one material ID without changing its JSON type.

    Unlike macro/plan IDs, the frozen material-identity contract is
    deliberately string-only.  Accepting ``1`` as ``"1"`` here would merge
    distinct JSON values and can silently select the wrong registry entry.
    """

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _registry(
    semantic_analysis: Dict[str, Any],
) -> Tuple[Dict[str, Dict[str, Any]], List[LedgerIssue]]:
    """Build the frozen material registry without string coercion/overwrite."""

    out: Dict[str, Dict[str, Any]] = {}
    issues: List[LedgerIssue] = []
    raw_registry = semantic_analysis.get("material_identity_registry")
    if raw_registry is None:
        return out, issues
    if not isinstance(raw_registry, list):
        return out, [LedgerIssue(
            "invalid_material_identity_registry",
            "material_identity_registry 必须是对象数组",
            path="semantic_analysis.material_identity_registry",
        )]

    first_path_by_id: Dict[str, str] = {}
    for index, entry in enumerate(raw_registry):
        path = f"semantic_analysis.material_identity_registry[{index}]"
        if not isinstance(entry, dict):
            issues.append(LedgerIssue(
                "invalid_material_identity_registry_entry",
                "material identity registry 条目必须是对象",
                path=path,
            ))
            continue
        identity_id = _strict_material_identity(entry.get("identity_id"))
        if identity_id is None:
            issues.append(LedgerIssue(
                "invalid_material_identity_id",
                "material identity_id 必须是非空字符串",
                path=f"{path}.identity_id",
                actual=entry.get("identity_id"),
            ))
            continue
        if identity_id in out:
            issues.append(LedgerIssue(
                "duplicate_material_identity_id",
                "material_identity_registry 中存在重复 identity_id",
                path=f"{path}.identity_id",
                material_identity_id=identity_id,
                first_path=first_path_by_id[identity_id],
            ))
            continue
        out[identity_id] = entry
        first_path_by_id[identity_id] = f"{path}.identity_id"
    return out, issues


def _validate_material_identity_fields(
    value: Any, path: str = "candidate"
) -> List[LedgerIssue]:
    """Validate every explicitly named Device material-ID field.

    This runs before any registry lookup or ledger construction so malformed
    values cannot be ignored, iterated as characters, or converted with
    ``str()`` by a downstream compatibility path.
    """

    issues: List[LedgerIssue] = []
    if isinstance(value, dict):
        for field, child in value.items():
            child_path = f"{path}.{field}"
            if field in MATERIAL_ID_FIELDS:
                if _strict_material_identity(child) is None:
                    issues.append(LedgerIssue(
                        "invalid_material_identity_id",
                        f"{field} 必须是非空字符串",
                        path=child_path,
                        actual=child,
                    ))
                continue
            if field in MATERIAL_ID_LIST_FIELDS:
                if not isinstance(child, list):
                    code = (
                        "invalid_source_material_identity_ids"
                        if field == "source_material_identity_ids"
                        else "invalid_material_identity_ids"
                    )
                    issues.append(LedgerIssue(
                        code,
                        f"{field} 必须是非空字符串 ID 数组",
                        path=child_path,
                    ))
                    continue
                invalid_indices = [
                    index
                    for index, item in enumerate(child)
                    if _strict_material_identity(item) is None
                ]
                if invalid_indices:
                    code = (
                        "invalid_source_material_identity_id"
                        if field == "source_material_identity_ids"
                        else "invalid_material_identity_id"
                    )
                    issues.append(LedgerIssue(
                        code,
                        f"{field} 的每一项都必须是非空字符串",
                        path=child_path,
                        invalid_indices=invalid_indices,
                    ))
                continue
            issues.extend(_validate_material_identity_fields(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            issues.extend(
                _validate_material_identity_fields(child, f"{path}[{index}]")
            )
    return issues


def _validate_assessment_material_identities(
    semantic_analysis: Dict[str, Any],
) -> List[LedgerIssue]:
    """Validate identity members used to bind registry roots to assessments."""

    issues: List[LedgerIssue] = []
    if "macro_step_assessments" not in semantic_analysis:
        return issues
    assessments = semantic_analysis.get("macro_step_assessments")
    if not isinstance(assessments, list):
        return [LedgerIssue(
            "invalid_macro_step_assessments",
            "macro_step_assessments 必须是对象数组",
            path="semantic_analysis.macro_step_assessments",
        )]
    for assessment_index, assessment in enumerate(assessments):
        assessment_path = (
            f"semantic_analysis.macro_step_assessments[{assessment_index}]"
        )
        if not isinstance(assessment, dict):
            issues.append(LedgerIssue(
                "invalid_macro_step_assessment",
                "macro_step_assessments 条目必须是对象",
                path=assessment_path,
            ))
            continue
        if "material_identities" not in assessment:
            materials = []
        else:
            materials = assessment.get("material_identities")
        if not isinstance(materials, list):
            issues.append(LedgerIssue(
                "invalid_material_identities",
                "macro assessment material_identities 必须是对象数组",
                path=f"{assessment_path}.material_identities",
            ))
            continue
        for identity_index, material in enumerate(materials):
            path = (
                "semantic_analysis.macro_step_assessments"
                f"[{assessment_index}].material_identities[{identity_index}]"
            )
            if not isinstance(material, dict):
                issues.append(LedgerIssue(
                    "invalid_material_identity",
                    "macro assessment material identity 必须是对象",
                    path=path,
                ))
                continue
            if _strict_material_identity(material.get("identity_id")) is None:
                issues.append(LedgerIssue(
                    "invalid_material_identity_id",
                    "macro assessment identity_id 必须是非空字符串",
                    path=f"{path}.identity_id",
                    actual=material.get("identity_id"),
                ))
    return issues


def _explicit_values(record: Dict[str, Any], fields: Iterable[str]) -> set[str]:
    """Return non-empty strings from explicitly named scalar/list fields."""

    values: set[str] = set()
    for field in fields:
        raw = record.get(field)
        items = raw if isinstance(raw, (list, tuple, set)) else [raw]
        for item in items:
            if isinstance(item, str) and item.strip():
                values.add(item.strip())
    return values


def _valid_container_id(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _validate_material_transition_id_lists(
    plan: Iterable[Dict[str, Any]],
) -> List[LedgerIssue]:
    """Reject malformed transition-reference fields before any ledger apply.

    A string is an iterable in Python, but it is never an array in the JSON
    contract. Validating here prevents ``list(value)`` from silently turning
    one transition ID into a list of characters.
    """

    issues: List[LedgerIssue] = []
    for position, step in enumerate(plan):
        if "material_transition_ids" not in step:
            continue
        raw = step.get("material_transition_ids")
        path = f"candidate.device_plan[{position}].material_transition_ids"
        if raw is None:
            continue
        if not isinstance(raw, list):
            issues.append(LedgerIssue(
                "invalid_material_transition_ids",
                "material_transition_ids 必须是字符串 ID 数组",
                path=path,
            ))
            continue
        invalid = [
            index
            for index, value in enumerate(raw)
            if not isinstance(value, str) or not value.strip()
        ]
        if invalid:
            issues.append(LedgerIssue(
                "invalid_material_transition_id",
                "material_transition_ids 的每一项都必须是非空字符串",
                path=path,
                invalid_indices=invalid,
            ))
    return issues


def _step_container_ids(
    step: Dict[str, Any], path: str
) -> Tuple[List[Any], List[LedgerIssue]]:
    """Read and reconcile every explicit container-id alias on one step.

    Alias order is not precedence.  If two aliases are present they must name
    the same container set; otherwise selecting the first alias would make the
    result depend on spelling rather than structured evidence.
    """

    containers = step.get("containers")
    if not isinstance(containers, dict):
        return [], []

    aliases: List[Tuple[str, List[Any]]] = []
    issues: List[LedgerIssue] = []
    for field in ("容器编号", "container_ids", "container_id"):
        if field not in containers:
            continue
        raw = containers[field]
        values = raw if isinstance(raw, list) else [raw]
        invalid_indices = [
            index for index, value in enumerate(values)
            if not _valid_container_id(value)
        ]
        if invalid_indices:
            issues.append(LedgerIssue(
                "invalid_step_container_id",
                f"{field} 必须只包含有效容器 ID",
                path=f"{path}.containers.{field}",
                invalid_indices=invalid_indices,
            ))
            continue
        normalized = [
            value.strip() if isinstance(value, str) else value
            for value in values
        ]
        aliases.append((field, normalized))

    if issues or not aliases:
        return [], issues

    first_field, first_values = aliases[0]
    first_set = set(first_values)
    conflicts = [
        field for field, values in aliases[1:]
        if set(values) != first_set
    ]
    if conflicts:
        return [], [LedgerIssue(
            "conflicting_container_id_aliases",
            "同一步骤的容器 ID 别名必须声明同一容器集合",
            path=f"{path}.containers",
            aliases={field: values for field, values in aliases},
            first_alias=first_field,
            conflicting_aliases=conflicts,
        )]

    # Preserve the first declaration's order while suppressing duplicate IDs.
    ordered: List[Any] = []
    for value in first_values:
        if value not in ordered:
            ordered.append(value)
    return ordered, []


def _structured_container_evidence(
    candidate: Dict[str, Any], registry: Dict[str, Dict[str, Any]]
) -> tuple[
    dict[Any, set[str]],
    dict[Any, set[str]],
    dict[Any, set[str]],
    List[LedgerIssue],
]:
    """Collect explicit material/sample/group bindings from Device steps."""

    material: dict[Any, set[str]] = {}
    samples: dict[Any, set[str]] = {}
    groups: dict[Any, set[str]] = {}
    issues: List[LedgerIssue] = []
    for step_index, step in enumerate(candidate.get("device_plan") or []):
        if not isinstance(step, dict):
            continue
        containers, container_issues = _step_container_ids(
            step, f"candidate.device_plan[{step_index}]"
        )
        issues.extend(container_issues)
        if not containers:
            continue
        identities = {
            identity_id
            for identity_id in _explicit_values(step, MATERIAL_ID_LIST_FIELDS)
            if identity_id in registry
            and not (_roles_of(registry[identity_id]) & RESOURCE_ROLES)
        }
        step_samples = _explicit_values(step, SAMPLE_ID_KEYS)
        step_groups = _explicit_values(step, GROUP_ID_KEYS)
        for container in containers:
            material.setdefault(container, set()).update(identities)
            samples.setdefault(container, set()).update(step_samples)
            groups.setdefault(container, set()).update(step_groups)
    return material, samples, groups, issues


def _container_instance_ref(
    record: Dict[str, Any], path: str
) -> Tuple[Any, Optional[MacroKey], List[LedgerIssue]]:
    """Resolve instance aliases as one typed scalar identity."""

    aliases: List[Tuple[str, Any, MacroKey]] = []
    issues: List[LedgerIssue] = []
    for field in ("instance_ref", "replicate_id"):
        if field not in record:
            continue
        raw = record.get(field)
        if raw is None:
            continue
        try:
            normalized = normalize_macro_id(raw, f"{path}.{field}")
            key = macro_id_key(normalized, f"{path}.{field}")
        except MacroIdentityError as exc:
            issues.append(LedgerIssue(
                "invalid_container_instance_ref",
                str(exc),
                path=exc.path,
                actual=raw,
            ))
            continue
        aliases.append((field, normalized, key))

    if issues or not aliases:
        return None, None, issues
    first_field, first_value, first_key = aliases[0]
    conflicts = [field for field, _, key in aliases[1:] if key != first_key]
    if conflicts:
        return None, None, [LedgerIssue(
            "conflicting_container_instance_aliases",
            "同一容器记录的 instance_ref 与 replicate_id 必须一致",
            path=path,
            first_alias=first_field,
            conflicting_aliases=conflicts,
        )]
    return first_value, first_key, []


def build_instance_registry(
    candidate: Dict[str, Any],
    registry: Dict[str, Dict[str, Any]],
    *,
    frozen_sample_ids: Iterable[str] = (),
    frozen_group_ids: Iterable[str] = (),
) -> Tuple[List[Dict[str, Any]], List[LedgerIssue]]:
    """Derive container bindings from explicit structured evidence only.

    Idempotence: containers already described by the batch layer
    (``container_ref``/``instance_ref``/co-identities) reuse that record —
    their display labels are never reinterpreted as identity evidence.
    """
    identity_issues = _validate_material_identity_fields(candidate)
    if identity_issues:
        return [], identity_issues

    allowed_samples = {
        value.strip()
        for value in frozen_sample_ids
        if isinstance(value, str) and value.strip()
    }
    allowed_groups = {
        value.strip()
        for value in frozen_group_ids
        if isinstance(value, str) and value.strip()
    }
    (
        step_material,
        step_samples,
        step_groups,
        evidence_issues,
    ) = _structured_container_evidence(candidate, registry)
    if evidence_issues:
        return [], evidence_issues
    entries: List[Dict[str, Any]] = []
    issues: List[LedgerIssue] = []
    existing_by_container: Dict[Any, Dict[str, Any]] = {}
    for batch_index, batch in enumerate(candidate.get("batch_plan") or []):
        if not isinstance(batch, dict):
            continue
        container = batch.get("container_ref")
        if container is None:
            continue
        identity_values: List[str] = []
        primary_identity: Optional[str] = None
        if "material_identity_id" in batch:
            material_identity_id = _strict_material_identity(
                batch.get("material_identity_id")
            )
            if material_identity_id is not None:
                identity_values.append(material_identity_id)
                primary_identity = material_identity_id
        for value in batch.get("co_material_identity_ids") or []:
            co_identity_id = _strict_material_identity(value)
            if co_identity_id is not None:
                identity_values.append(co_identity_id)
        identities = set(identity_values)
        if not identities:
            continue
        unknown_identities = identities - set(registry)
        if unknown_identities:
            issues.append(
                LedgerIssue(
                    "unknown_batch_material_identity",
                    f"容器 {container} 的既有 batch 身份不在冻结注册表中",
                    container=container,
                    material_identity_ids=sorted(unknown_identities),
                )
            )
            continue
        instance, _instance_key, instance_issues = _container_instance_ref(
            batch, f"candidate.batch_plan[{batch_index}]"
        )
        if instance_issues:
            issues.extend(instance_issues)
            continue
        record = existing_by_container.setdefault(container, {
            "identities": [],
            "primary_identities": set(),
            "instance": instance,
            "samples": set(),
            "groups": set(),
        })
        if primary_identity is not None:
            record["primary_identities"].add(primary_identity)
        for value in sorted(identities):
            if value and value not in record["identities"]:
                record["identities"].append(value)
        record["samples"].update(_explicit_values(batch, SAMPLE_ID_KEYS | {"sample_ids"}))
        record["groups"].update(_explicit_values(batch, GROUP_ID_KEYS | {"group_ids"}))

    seen_container_bindings: Dict[Any, Dict[str, Any]] = {}
    for item_index, item in enumerate(candidate.get("container_plan") or []):
        if not isinstance(item, dict):
            continue
        item_path = f"candidate.container_plan[{item_index}]"
        container = item.get("容器编号")
        if not _valid_container_id(container):
            has_structured_binding = bool(
                _explicit_values(
                    item,
                    MATERIAL_ID_FIELDS
                    + MATERIAL_ID_LIST_FIELDS
                    + tuple(SAMPLE_ID_KEYS)
                    + tuple(GROUP_ID_KEYS),
                )
            )
            if has_structured_binding:
                issues.append(
                    LedgerIssue(
                        "container_id_missing",
                        "具有结构化身份绑定的容器记录缺少有效容器 ID",
                    )
                )
            continue
        explicit_material = _explicit_values(
            item, MATERIAL_ID_FIELDS + MATERIAL_ID_LIST_FIELDS
        )
        explicit_primary = _explicit_values(item, MATERIAL_ID_FIELDS)
        samples = _explicit_values(item, SAMPLE_ID_KEYS)
        groups = _explicit_values(item, GROUP_ID_KEYS)
        replicate, replicate_key, instance_issues = _container_instance_ref(
            item, item_path
        )
        if instance_issues:
            issues.extend(instance_issues)
            continue
        unknown_material = explicit_material - set(registry)
        if unknown_material:
            issues.append(LedgerIssue(
                "unknown_container_material_identity",
                f"容器 {container} 的显式物料身份不在冻结注册表中",
                container=container,
                material_identity_ids=sorted(unknown_material),
            ))
            continue
        binding = {
            "material": set(explicit_material),
            "primary": set(explicit_primary),
            "samples": set(samples),
            "groups": set(groups),
            "instance": replicate,
            "instance_key": replicate_key,
        }
        if container in seen_container_bindings:
            previous = seen_container_bindings[container]
            previous_material = previous["material"]
            if explicit_material != previous_material:
                issues.append(LedgerIssue(
                    "duplicate_container_identity_conflict",
                    f"容器 {container} 在 container_plan 中有冲突的物料身份声明",
                    container=container,
                    first_material_identity_ids=sorted(previous_material),
                    duplicate_material_identity_ids=sorted(explicit_material),
                ))
            elif any(
                binding[field] != previous[field]
                for field in ("primary", "samples", "groups", "instance_key")
            ):
                issues.append(LedgerIssue(
                    "duplicate_container_binding_conflict",
                    f"容器 {container} 在 container_plan 中有冲突的结构化绑定",
                    container=container,
                    first_binding={
                        "primary_material_identity_ids": sorted(previous["primary"]),
                        "sample_ids": sorted(previous["samples"]),
                        "group_ids": sorted(previous["groups"]),
                        "instance_ref": previous["instance"],
                    },
                    duplicate_binding={
                        "primary_material_identity_ids": sorted(binding["primary"]),
                        "sample_ids": sorted(binding["samples"]),
                        "group_ids": sorted(binding["groups"]),
                        "instance_ref": binding["instance"],
                    },
                ))
            continue
        seen_container_bindings[container] = binding
        known = existing_by_container.get(container)
        identities: set[str] = set()
        container_identities: set[str] = set()
        primary_identities = set(explicit_primary)
        if known:
            container_identities.update(known["identities"])
            primary_identities.update(known["primary_identities"])
            samples.update(known["samples"])
            groups.update(known["groups"])
            if replicate is None:
                replicate = known.get("instance")
        container_identities.update(explicit_material)
        step_identities = set(step_material.get(container, set()))
        identities.update(container_identities)
        identities.update(step_identities)
        if not identities:
            # Ignore unrelated empty storage/container declarations.  A
            # container carrying sample/group lineage is in scope and must not
            # be guessed from its type, purpose, or display label.
            if not known and not samples and not groups:
                continue
            issues.append(
                LedgerIssue(
                    "container_identity_unresolved",
                    f"容器 {container} 缺少结构化物料身份绑定",
                    container=container,
                )
            )
            continue
        samples.update(step_samples.get(container, set()))
        groups.update(step_groups.get(container, set()))
        if len(allowed_samples) == 1:
            # A single frozen sample is an unambiguous structural namespace;
            # legacy per-container labels are normalized to it later.
            sample_id = next(iter(allowed_samples))
        else:
            trusted_samples = samples & allowed_samples
            if len(trusted_samples) != 1:
                issues.append(LedgerIssue(
                    "container_sample_lineage_unresolved",
                    f"容器 {container} 缺少唯一冻结 sample/control 关联",
                    container=container,
                    candidate_sample_ids=sorted(samples),
                    frozen_sample_ids=sorted(allowed_samples),
                ))
                continue
            sample_id = next(iter(trusted_samples))
        group_id: Optional[str] = None
        if len(allowed_groups) == 1:
            group_id = next(iter(allowed_groups))
        elif allowed_groups:
            trusted_groups = groups & allowed_groups
            if len(trusted_groups) != 1:
                issues.append(LedgerIssue(
                    "container_group_lineage_unresolved",
                    f"容器 {container} 缺少唯一冻结 group 关联",
                    container=container,
                    candidate_group_ids=sorted(groups),
                    frozen_group_ids=sorted(allowed_groups),
                ))
                continue
            group_id = next(iter(trusted_groups))
        entries.append(
            {
                "container": container,
                "identities": sorted(identities),
                "container_identities": sorted(container_identities),
                "primary_identities": sorted(primary_identities),
                "step_identities": sorted(step_identities),
                "instance": replicate,
                "sample_id": sample_id,
                "group_id": group_id,
            }
        )
    if issues:
        return [], issues
    return entries, []


# ---------------------------------------------------------------------------
# research source refs for root batches (verbatim binding only)
# ---------------------------------------------------------------------------


def _research_source_ref(
    semantic_analysis: Dict[str, Any], identity_id: str
) -> Tuple[Optional[Dict[str, Any]], Optional[LedgerIssue]]:
    """Bind a root identity to explicit semantic-assessment membership."""

    hits: List[Dict[str, Any]] = []
    for assessment_index, assessment in enumerate(
        semantic_analysis.get("macro_step_assessments") or []
    ):
        if not isinstance(assessment, dict):
            continue
        macro_id = assessment.get("source_macro_step")
        try:
            macro_id_key(
                macro_id,
                f"semantic_analysis.macro_step_assessments[{assessment_index}]"
                ".source_macro_step",
            )
        except MacroIdentityError as exc:
            return None, LedgerIssue(
                exc.code.lower(),
                str(exc),
                path=exc.path,
                assessment_index=assessment_index,
            )
        for identity_index, material in enumerate(
            assessment.get("material_identities") or []
        ):
            if not isinstance(material, dict):
                continue
            material_id = _strict_material_identity(material.get("identity_id"))
            if material_id != identity_id:
                continue
            evidence_refs = [
                str(value).strip()
                for value in assessment.get("evidence_refs") or []
                if isinstance(value, str) and value.strip()
            ]
            hits.append({
                "source_path": (
                    evidence_refs[0]
                    if evidence_refs
                    else (
                        "semantic_analysis.macro_step_assessments"
                        f"[{assessment_index}].material_identities[{identity_index}]"
                    )
                ),
                "source_macro_step": macro_id,
                "source_field": "material_identities",
                "source_context": copy.deepcopy(material),
                "material_identity_id": identity_id,
            })
            break
    if hits:
        # Assessment order is frozen input order; do not coerce typed macro IDs
        # to integers or sort identifiers lexically.
        return hits[0], None
    return None, LedgerIssue(
        "root_source_not_found",
        f"资源身份 {identity_id} 在结构化 macro assessment 中无成员关系",
        identity_id=identity_id,
    )


# ---------------------------------------------------------------------------
# main constructor
# ---------------------------------------------------------------------------


def construct_binding_ledger(
    candidate: Dict[str, Any],
    research_handoff: Dict[str, Any],
    semantic_analysis: Dict[str, Any],
    *,
    category_of_workstation: Callable[[str], Optional[str]],
    frozen_sample_id: str,
    frozen_group_id: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[LedgerIssue]]:
    """Build the binding layer for one candidate.

    Returns ``(changes, issues)``.  ``changes`` carries new ``batch_plan`` /
    ``material_transitions`` / per-step ``material_transition_ids`` bindings,
    ``sample_id`` rewrites and handoff identity additions.  Existing entries
    are re-emitted verbatim so application is a no-op on a second run
    (idempotent).
    """
    issues: List[LedgerIssue] = []
    notices: List[Dict[str, Any]] = []
    registry, registry_issues = _registry(semantic_analysis)
    issues.extend(registry_issues)
    issues.extend(_validate_assessment_material_identities(semantic_analysis))
    issues.extend(_validate_material_identity_fields(candidate))
    if issues:
        return {}, issues
    if not registry:
        return {}, [LedgerIssue("registry_missing", "语义分析缺 material_identity_registry")]

    (
        plan,
        plan_key_by_object,
        plan_values_by_key,
        plan_position_by_object,
        _plan_step_by_key,
        plan_identity_issues,
    ) = _plan_identity_index(candidate)
    issues.extend(plan_identity_issues)
    if not plan_identity_issues:
        issues.extend(_validate_material_transition_id_lists(plan))
        issues.extend(
            _validate_plan_references(candidate, set(plan_values_by_key))
        )
    if issues:
        return {}, issues
    instance_registry, registry_issues = build_instance_registry(
        candidate,
        registry,
        frozen_sample_ids=(frozen_sample_id,),
        frozen_group_ids=(frozen_group_id,) if frozen_group_id else (),
    )
    issues.extend(registry_issues)
    if registry_issues:
        return {}, issues
    container_line: Dict[Any, Dict[str, Any]] = {
        entry["container"]: entry for entry in instance_registry
    }

    # existing ledger artefacts (idempotent re-emission)
    existing_transitions: Dict[str, Dict[str, Any]] = {
        str(t.get("transition_id")): t
        for t in candidate.get("material_transitions") or []
        if isinstance(t, dict) and str(t.get("transition_id") or "").strip()
    }
    existing_batches_by_id: Dict[str, Dict[str, Any]] = {
        str(b.get("batch_id")): b
        for b in candidate.get("batch_plan") or []
        if isinstance(b, dict) and str(b.get("batch_id") or "").strip()
    }

    # ---- required state categories per macro (semantic contract) ----------
    required: Dict[MacroKey, set] = {}
    macro_values: Dict[MacroKey, Any] = {}
    macro_order: List[MacroKey] = []
    for assessment_index, assessment in enumerate(
        semantic_analysis.get("macro_step_assessments") or []
    ):
        if not isinstance(assessment, dict):
            continue
        macro_value = assessment.get("source_macro_step")
        try:
            macro_id = macro_id_key(
                macro_value,
                f"semantic_analysis.macro_step_assessments[{assessment_index}]"
                ".source_macro_step",
            )
        except MacroIdentityError as exc:
            issues.append(LedgerIssue(
                exc.code.lower(),
                str(exc),
                path=exc.path,
                assessment_index=assessment_index,
            ))
            continue
        if macro_id not in macro_values:
            macro_values[macro_id] = macro_value
            macro_order.append(macro_id)
        caps = {
            str(c.get("category") or "").strip()
            for c in assessment.get("required_capabilities") or []
            if isinstance(c, dict)
        }
        state_caps = caps & set(STATE_CATEGORIES)
        if state_caps:
            required.setdefault(macro_id, set()).update(state_caps)

    # ---- root resource batches ---------------------------------------------
    used_identities = {
        value.strip()
        for step in plan
        for value in step.get("source_material_identity_ids") or []
        if isinstance(value, str) and value.strip()
    }
    resource_ids = sorted(
        identity_id
        for identity_id, entry in registry.items()
        if _roles_of(registry[identity_id]) & RESOURCE_ROLES
        and identity_id in used_identities
    )
    batches: List[Dict[str, Any]] = []
    root_batch_by_identity: Dict[str, str] = {}
    for identity_id in resource_ids:
        batch_id = f"RB_{_slug(identity_id)}"
        ref, issue = _research_source_ref(semantic_analysis, identity_id)
        if issue is not None:
            issues.append(issue)
            continue
        root_batch_by_identity[identity_id] = batch_id
        batches.append(
            {
                "batch_id": batch_id,
                "quantity_mode": "whole_batch",
                "material_identity_id": identity_id,
                "research_material_identity_id": identity_id,
                "material_id": str(
                    (registry[identity_id].get("canonical_name_variants") or [identity_id])[0]
                ),
                "is_root_batch": True,
                "source_kind": "research",
                "source_refs": [ref["source_path"]],
                "research_source_refs": [ref],
                "source_macro_steps": [ref["source_macro_step"]],
                "sample_id": frozen_sample_id,
                "state_label": "frozen_input_resource",
                "construction_rule": BINDING_LEDGER_RULE,
            }
        )

    # ---- per-macro state-change chains --------------------------------------
    transitions: List[Dict[str, Any]] = []
    step_bindings: Dict[MacroKey, List[str]] = {}
    current_batch: Dict[Any, str] = {}
    batch_by_id: Dict[str, Dict[str, Any]] = {b["batch_id"]: b for b in batches}

    def feed_parents(
        macro_steps: List[Dict[str, Any]], before_position: int
    ) -> List[str]:
        roots: List[str] = []
        for step in macro_steps:
            if plan_position_by_object[id(step)] >= before_position:
                break
            if str(step.get("material_event_kind") or "") == "state_change":
                continue
            for identity_id in step.get("source_material_identity_ids") or []:
                root = root_batch_by_identity.get(identity_id.strip())
                if root and root not in roots:
                    roots.append(root)
        return roots

    def prior_binding(step: Dict[str, Any], container: Any):
        """Existing transition+child batch for this step×container, if the
        ledger layer already carries them (idempotent second run)."""
        for mt_id in step.get("material_transition_ids") or []:
            transition = existing_transitions.get(str(mt_id))
            if not transition:
                continue
            for child in transition.get("child_batch_ids") or []:
                batch = existing_batches_by_id.get(str(child))
                if batch and batch.get("container_ref") == container:
                    return transition, batch
        return None

    plan_macro_keys: Dict[int, set[MacroKey]] = {}
    for step_index, step in enumerate(plan):
        try:
            source_values = extract_source_macro_ids(
                step, f"candidate.device_plan[{step_index}]"
            )
        except MacroIdentityError as exc:
            issues.append(LedgerIssue(
                exc.code.lower(),
                str(exc),
                path=exc.path,
                plan_step=step.get("plan_step"),
            ))
            source_values = []
        plan_macro_keys[id(step)] = {
            macro_id_key(value) for value in source_values
        }

    for macro_id in [key for key in macro_order if key in required]:
        macro_value = macro_values[macro_id]
        macro_steps = [
            s
            for s in plan
            if macro_id in plan_macro_keys.get(id(s), set())
        ]
        macro_steps.sort(key=lambda s: plan_position_by_object[id(s)])
        state_steps = [
            s
            for s in macro_steps
            if str(s.get("material_event_kind") or "") == "state_change"
        ]
        for step in state_steps:
            step_key = plan_key_by_object[id(step)]
            step_id = plan_values_by_key[step_key]
            step_position = plan_position_by_object[id(step)]
            step_label = _typed_id_label(step_id)
            category = category_of_workstation(str(step.get("workstation") or ""))
            if category not in STATE_CATEGORIES:
                # binding-ledger/v1.1: a step that itself declares
                # material_event_kind=state_change on a dispersion station is
                # a real material-state hop on the same per-container path and
                # is bound with the same evidence rules.
                if not (
                    category == "ultrasonic_dispersion"
                    and str(step.get("material_event_kind") or "") == "state_change"
                ):
                    continue
            containers, container_issues = _step_container_ids(
                step, f"candidate.device_plan[{step_position}]"
            )
            if container_issues:
                issues.extend(container_issues)
                continue
            if not containers:
                issues.append(
                    LedgerIssue(
                        "step_without_containers",
                        f"step {step_label}（{category}）无容器绑定，无法建逐实例对应",
                        plan_step=step_id,
                    )
                )
                continue
            step_non_resource = [
                v.strip()
                for v in step.get("source_material_identity_ids") or []
                if v.strip() in registry
                and not (_roles_of(registry[v.strip()]) & RESOURCE_ROLES)
            ]
            for container in containers:
                prior = prior_binding(step, container)
                if prior is not None:
                    transition, batch = prior
                    transitions.append(copy.deepcopy(transition))
                    batches.append(copy.deepcopy(batch))
                    batch_by_id[batch["batch_id"]] = batch
                    step_bindings.setdefault(step_key, []).append(
                        transition["transition_id"]
                    )
                    current_batch[container] = batch["batch_id"]
                    continue
                line = container_line.get(container)
                if line is None:
                    issues.append(
                        LedgerIssue(
                            "container_not_registered",
                            f"容器 {container} 无实例注册（step {step_label}）",
                            plan_step=step_id,
                            container=container,
                        )
                    )
                    continue
                identity = None
                co_identities: List[str] = []
                container_identities = set(
                    line.get("container_identities") or []
                )
                if len(step_non_resource) == 1:
                    proposed = step_non_resource[0]
                    if container_identities and proposed not in container_identities:
                        issues.append(
                            LedgerIssue(
                                "container_step_identity_conflict",
                                f"step {step_label} 容器 {container} 的结构化身份绑定冲突",
                                plan_step=step_id,
                                container=container,
                                step_material_identity_ids=[proposed],
                                container_material_identity_ids=sorted(
                                    container_identities
                                ),
                            )
                        )
                        continue
                    identity = proposed
                elif len(step_non_resource) > 1:
                    # The step authorization and container registry are both
                    # structured identity sets.  Their intersection is the
                    # only valid discriminator; labels and purpose prose never
                    # participate in identity selection.
                    hits = sorted(set(step_non_resource) & container_identities)
                    if len(hits) == 1:
                        identity = hits[0]
                    elif len(hits) >= 2:
                        # A co-bound batch needs an explicit scalar primary.
                        # Set ordering is never authority: absent, conflicting,
                        # or out-of-intersection primary declarations block.
                        primary_candidates = (
                            _explicit_values(step, MATERIAL_ID_FIELDS)
                            | set(line.get("primary_identities") or [])
                        )
                        if (
                            len(primary_candidates) == 1
                            and primary_candidates <= set(hits)
                        ):
                            identity = next(iter(primary_candidates))
                            co_identities = sorted(set(hits) - {identity})
                if identity is None:
                    issues.append(
                        LedgerIssue(
                            "batch_identity_ambiguous",
                            f"step {step_label} 容器 {container} 身份无法在 "
                            f"{step_non_resource} 中唯一确定",
                            plan_step=step_id,
                            container=container,
                            matching_material_identity_ids=(
                                hits if len(step_non_resource) > 1 else []
                            ),
                            structured_primary_identity_ids=(
                                sorted(
                                    _explicit_values(step, MATERIAL_ID_FIELDS)
                                    | set(line.get("primary_identities") or [])
                                )
                                if len(step_non_resource) > 1
                                else []
                            ),
                        )
                    )
                    continue
                if co_identities:
                    notices.append(
                        {
                            "code": "dual_identity_coregistered",
                            "message": (
                                f"step {step_label} 容器 {container} 为一批两用过程批："
                                f"主身份 {identity}，共存身份 {co_identities}；"
                                "按合同 L2 不虚构 split，仅登记。"
                            ),
                            "plan_step": step_id,
                            "container": container,
                            "identities": sorted([identity] + list(co_identities)),
                            "basis": "structured_container_and_step_identity_intersection",
                        }
                    )
                parents: List[str] = []
                if container in current_batch:
                    parents.append(current_batch[container])
                else:
                    parents.extend(feed_parents(macro_steps, step_position))
                if not parents:
                    issues.append(
                        LedgerIssue(
                            "no_parent_evidence",
                            f"step {step_label} 容器 {container} 无上游批次或进料资源证据",
                            plan_step=step_id,
                            container=container,
                        )
                    )
                    continue
                instance = line.get("instance")
                if instance is None:
                    instance = "rx"
                child_id = (
                    f"B_{_slug(identity)}_{str(instance).lower()}"
                    f"_{category}_c{container}"
                )
                suffix = 2
                base_child = child_id
                while child_id in batch_by_id:
                    child_id = f"{base_child}_{suffix}"
                    suffix += 1
                transition_id = (
                    "mt_m"
                    f"{_typed_id_token(macro_value, 'semantic macro ID')}"
                    f"_{category}_c{container}"
                )
                suffix = 2
                base_mt = transition_id
                existing_mt = {t["transition_id"] for t in transitions}
                while transition_id in existing_mt:
                    transition_id = f"{base_mt}_{suffix}"
                    suffix += 1
                before = (
                    batch_by_id[parents[0]]["state_label"]
                    if parents and parents[0] in batch_by_id
                    else "external_inputs"
                )
                batch = {
                    "batch_id": child_id,
                    "quantity_mode": "whole_batch",
                    "material_identity_id": identity,
                    "material_id": str(
                        (registry[identity].get("canonical_name_variants") or [identity])[0]
                    ),
                    "is_root_batch": False,
                    "source_kind": "derived",
                    "source_refs": [],
                    "transition_kind": "state_change",
                    "parent_batch_ids": list(parents),
                    "sample_id": line["sample_id"],
                    "source_plan_steps": [copy.deepcopy(step_id)],
                    "source_macro_steps": [macro_value],
                    "container_ref": container,
                    "instance_ref": instance,
                    "state_label": (
                        "after_step_"
                        f"{_typed_id_token(step_id, 'device plan_step')}"
                        f"_{category}"
                    ),
                    "construction_rule": BINDING_LEDGER_RULE,
                }
                if co_identities:
                    batch["co_material_identity_ids"] = list(co_identities)
                    batch["dual_use_basis"] = (
                        "structured_container_and_step_identity_intersection"
                    )
                if line.get("group_id"):
                    batch["group_id"] = line["group_id"]
                batches.append(batch)
                batch_by_id[child_id] = batch
                transitions.append(
                    {
                        "transition_id": transition_id,
                        "transition_kind": "state_change",
                        "parent_batch_ids": list(parents),
                        "child_batch_ids": [child_id],
                        "source_plan_steps": [copy.deepcopy(step_id)],
                        "source_macro_steps": [macro_value],
                        "quantity_basis": "whole_batch",
                        # whole_batch edges prove 1→1 lineage only; declaring
                        # input/output allocation numbers on them is forbidden
                        # by the quantity gate, so none are emitted.
                        "before_material_state": before,
                        "after_material_state": batch["state_label"],
                        "calculation_or_basis": (
                            "binding-ledger/v1: 台账由候选自身步骤/容器/身份引用构造；"
                            "未新增操作、参数或执行事实。"
                        ),
                        "construction_rule": BINDING_LEDGER_RULE,
                    }
                )
                step_bindings.setdefault(step_key, []).append(transition_id)
                current_batch[container] = child_id

    # ---- material_ledger sidecar (mirror of the batch layer) ----------------
    # The quantity gate treats batch_plan and material_ledger as one lineage
    # contract: every batch needs exactly one ledger entry carrying the same
    # material_id; derived-batch entries must name the plan steps of the
    # transition that produced them.  Entries mirror batch fields only.
    ledger_entries: List[Dict[str, Any]] = []
    for batch in batches:
        entry = {
            "entry_id": f"ml_{batch['batch_id']}",
            "batch_id": batch["batch_id"],
            "material_id": batch["material_id"],
            "material_identity_id": batch["material_identity_id"],
            "quantity_mode": batch["quantity_mode"],
            "sample_id": batch["sample_id"],
            "source_kind": batch["source_kind"],
            "source_refs": list(batch.get("source_refs") or []),
            "calculation": (
                "binding-ledger/v1: whole_batch 身份绑定，整批沿逐容器路径流转；"
                "无数值分配。"
            ),
            "construction_rule": BINDING_LEDGER_RULE,
        }
        if not batch.get("is_root_batch"):
            entry["processing_step_refs"] = list(batch.get("source_plan_steps") or [])
        ledger_entries.append(entry)

    # ---- F1: sample_id layering rewrite -------------------------------------
    rewrites: List[Dict[str, Any]] = []

    def walk(value: Any, trail: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                lowered = str(key).strip().lower()
                if lowered in SAMPLE_ID_KEYS and isinstance(item, str):
                    if item.strip() and item.strip() != frozen_sample_id:
                        rewrites.append(
                            {
                                "path": f"{trail}.{key}",
                                "old": item,
                                "new": frozen_sample_id,
                                "rule": FROZEN_SAMPLE_REF_RULE,
                            }
                        )
                elif lowered in GROUP_ID_KEYS and isinstance(item, str):
                    if frozen_group_id and item.strip() not in ("", frozen_group_id):
                        rewrites.append(
                            {
                                "path": f"{trail}.{key}",
                                "old": item,
                                "new": frozen_group_id,
                                "rule": FROZEN_SAMPLE_REF_RULE,
                            }
                        )
                walk(item, f"{trail}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{trail}[{index}]")

    # The frozen matrix is authority/evidence and must never be a rewrite
    # target.  Only Device-authored operational records are normalized.
    walk(
        {
            "device_plan": candidate.get("device_plan"),
            "container_plan": candidate.get("container_plan"),
        },
        "candidate",
    )

    # ---- input-boundary resource declarations -------------------------------
    # Only resource-role identities from the boundary's explicitly bound macro
    # assessments may be added.  Sample/product retention remains a checker
    # concern, not a patch.
    expected_by_macro: Dict[MacroKey, set] = {}
    for assessment_index, assessment in enumerate(
        semantic_analysis.get("macro_step_assessments") or []
    ):
        if not isinstance(assessment, dict):
            continue
        try:
            macro_key = macro_id_key(
                assessment.get("source_macro_step"),
                f"semantic_analysis.macro_step_assessments[{assessment_index}]"
                ".source_macro_step",
            )
        except MacroIdentityError:
            # The first assessment pass already emitted the blocking issue.
            continue
        expected_by_macro.setdefault(macro_key, set()).update({
            identity_id
            for item in assessment.get("material_identities") or []
            if isinstance(item, dict)
            for identity_id in [_strict_material_identity(item.get("identity_id"))]
            if identity_id is not None
            and identity_id in registry
            and _roles_of(registry[identity_id]) & RESOURCE_ROLES
        })
    handoff_adds: List[Dict[str, Any]] = []
    handoffs = candidate.get("offline_handoffs") or []
    boundaries_by_macro: Dict[MacroKey, List[int]] = {}
    boundary_macro_values: Dict[MacroKey, Any] = {}
    boundary_macro_keys: Dict[int, set[MacroKey]] = {}
    for index, handoff in enumerate(handoffs):
        if not isinstance(handoff, dict):
            continue
        if str(handoff.get("semantic_classification") or "").strip() != "input_boundary":
            continue
        try:
            source_values = extract_source_macro_ids(
                handoff, f"candidate.offline_handoffs[{index}]", required=True
            )
        except MacroIdentityError as exc:
            issues.append(LedgerIssue(
                exc.code.lower(),
                str(exc),
                path=exc.path,
                handoff_index=index,
            ))
            source_values = []
        macro_ids = {macro_id_key(value) for value in source_values}
        boundary_macro_keys[index] = macro_ids
        for macro_id, value in zip(
            (macro_id_key(value) for value in source_values), source_values
        ):
            boundary_macro_values.setdefault(macro_id, value)
        if not macro_ids:
            if source_values:
                issues.append(LedgerIssue(
                    "input_boundary_macro_unresolved",
                    f"input-boundary handoff[{index}] 缺少结构化 source_macro_step(s)",
                    handoff_index=index,
                ))
            continue
        for macro_id in macro_ids:
            boundaries_by_macro.setdefault(macro_id, []).append(index)

    ambiguous_macros = {
        macro_id: indices
        for macro_id, indices in boundaries_by_macro.items()
        if len(indices) > 1
    }
    for macro_id, indices in ambiguous_macros.items():
        issues.append(LedgerIssue(
            "ambiguous_input_boundary_handoffs",
            "同一 typed macro 对应多个 input-boundary handoff，无法唯一补充资源身份",
            source_macro_step=macro_values.get(
                macro_id, boundary_macro_values.get(macro_id)
            ),
            handoff_indices=indices,
        ))

    for index, handoff in enumerate(handoffs):
        if not isinstance(handoff, dict):
            continue
        if str(handoff.get("semantic_classification") or "").strip() != "input_boundary":
            continue
        macro_ids = boundary_macro_keys.get(index, set())
        unambiguous = {
            macro_id
            for macro_id in macro_ids
            if macro_id not in ambiguous_macros
        }
        if not unambiguous:
            continue
        raw_declared = handoff.get("source_material_identity_ids")
        if raw_declared is None:
            declared: set[str] = set()
        elif not isinstance(raw_declared, list):
            issues.append(LedgerIssue(
                "invalid_source_material_identity_ids",
                "input-boundary source_material_identity_ids 必须是字符串 ID 数组",
                path=(
                    f"candidate.offline_handoffs[{index}]"
                    ".source_material_identity_ids"
                ),
                handoff_index=index,
            ))
            continue
        else:
            invalid_declared = [
                item_index
                for item_index, value in enumerate(raw_declared)
                if not isinstance(value, str) or not value.strip()
            ]
            if invalid_declared:
                issues.append(LedgerIssue(
                    "invalid_source_material_identity_id",
                    "input-boundary source_material_identity_ids 的每一项都必须是非空字符串",
                    path=(
                        f"candidate.offline_handoffs[{index}]"
                        ".source_material_identity_ids"
                    ),
                    handoff_index=index,
                    invalid_indices=invalid_declared,
                ))
                continue
            declared = {value.strip() for value in raw_declared}
        expected_resources = set().union(
            *(expected_by_macro.get(macro_id, set()) for macro_id in unambiguous)
        )
        missing_inputs = sorted(
            identity_id
            for identity_id in expected_resources - declared
            if identity_id in root_batch_by_identity
        )
        if missing_inputs:
            handoff_adds.append(
                {"handoff_index": index, "add": missing_inputs}
            )

    changes = {
        "batch_plan": batches,
        "material_transitions": transitions,
        "material_ledger_entries": ledger_entries,
        "step_bindings": step_bindings,
        "sample_id_rewrites": rewrites,
        "handoff_identity_adds": handoff_adds,
        "instance_registry": instance_registry,
        "notices": notices,
    }
    return changes, issues


# ---------------------------------------------------------------------------
# application with expected-old-value guards (JSON-Patch test semantics)
# ---------------------------------------------------------------------------


def apply_ledger_changes(
    candidate: Dict[str, Any], changes: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[str]]:
    """Apply constructor output to a deep copy; verify expected old values.

    Idempotent: entries already present (identical) are skipped; conflicting
    pre-existing content aborts that item and is reported.
    """
    updated = copy.deepcopy(candidate)
    applied: List[str] = []
    (
        _plan,
        _plan_key_by_object,
        plan_values_by_key,
        _plan_position_by_object,
        step_by_key,
        plan_identity_issues,
    ) = _plan_identity_index(updated)
    if plan_identity_issues:
        return updated, [
            f"conflict:{issue['code']}:{issue['context'].get('path', 'device_plan')}"
            for issue in plan_identity_issues
        ]
    unknown_binding_keys = [
        key
        for key in (changes.get("step_bindings") or {})
        if key not in step_by_key
    ]
    if unknown_binding_keys:
        return updated, [
            f"conflict:binding_missing_step:{key!r}"
            for key in unknown_binding_keys
        ]

    existing_batches = {
        str(b.get("batch_id")): b
        for b in updated.get("batch_plan") or []
        if isinstance(b, dict)
    }
    plan_batches = list(updated.get("batch_plan") or [])
    for batch in changes.get("batch_plan") or []:
        existing = existing_batches.get(batch["batch_id"])
        if existing is not None:
            if existing != batch:
                applied.append(f"conflict:batch:{batch['batch_id']}")
            continue
        plan_batches.append(batch)
        applied.append(f"add:batch:{batch['batch_id']}")
    if plan_batches:
        updated["batch_plan"] = plan_batches

    existing_mt = {
        str(t.get("transition_id")): t
        for t in updated.get("material_transitions") or []
        if isinstance(t, dict)
    }
    plan_mt = list(updated.get("material_transitions") or [])
    for transition in changes.get("material_transitions") or []:
        existing = existing_mt.get(transition["transition_id"])
        if existing is not None:
            if existing != transition:
                applied.append(f"conflict:transition:{transition['transition_id']}")
            continue
        plan_mt.append(transition)
        applied.append(f"add:transition:{transition['transition_id']}")
    if plan_mt:
        updated["material_transitions"] = plan_mt

    existing_entries = {
        str(e.get("entry_id")): e
        for e in (updated.get("material_ledger") or {}).get("entries") or []
        if isinstance(e, dict)
    }
    plan_entries = list((updated.get("material_ledger") or {}).get("entries") or [])
    for entry in changes.get("material_ledger_entries") or []:
        existing = existing_entries.get(entry["entry_id"])
        if existing is not None:
            if existing != entry:
                applied.append(f"conflict:ledger:{entry['entry_id']}")
            continue
        plan_entries.append(entry)
        applied.append(f"add:ledger:{entry['entry_id']}")
    if plan_entries:
        ledger = updated.get("material_ledger")
        if not isinstance(ledger, dict):
            ledger = {}
        ledger["entries"] = plan_entries
        updated["material_ledger"] = ledger

    for step_key, mt_ids in (changes.get("step_bindings") or {}).items():
        step = step_by_key[step_key]
        step_id = plan_values_by_key[step_key]
        step_label = _typed_id_label(step_id)
        current = step.get("material_transition_ids")
        if current in (None, []):
            step["material_transition_ids"] = list(mt_ids)
            applied.append(f"bind:step:{step_label}")
        else:
            if not isinstance(current, list):
                applied.append(
                    f"conflict:invalid_material_transition_ids:{step_label}"
                )
                continue
            if any(
                not isinstance(value, str) or not value.strip()
                for value in current
            ):
                applied.append(
                    f"conflict:invalid_material_transition_id:{step_label}"
                )
                continue
            merged = list(current)
            for mt_id in mt_ids:
                if mt_id not in merged:
                    merged.append(mt_id)
            if merged != current:
                step["material_transition_ids"] = merged
                applied.append(f"bind:step:{step_label}:merge")

    def rewrite(value: Any, trail: str, errors: List[str]) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                lowered = str(key).strip().lower()
                if lowered in SAMPLE_ID_KEYS and isinstance(item, str):
                    expected = rewrite_map.get(f"{trail}.{key}")
                    if expected is not None:
                        if item == expected["new"]:
                            pass  # already applied (idempotent re-run)
                        elif item != expected["old"]:
                            errors.append(f"test_failed:{trail}.{key}")
                        else:
                            value[key] = expected["new"]
                            applied.append(f"rewrite:{trail}.{key}")
                elif lowered in GROUP_ID_KEYS and isinstance(item, str):
                    expected = rewrite_map.get(f"{trail}.{key}")
                    if expected is not None:
                        if item == expected["new"]:
                            pass
                        elif item != expected["old"]:
                            errors.append(f"test_failed:{trail}.{key}")
                        else:
                            value[key] = expected["new"]
                            applied.append(f"rewrite:{trail}.{key}")
                rewrite(item, f"{trail}.{key}", errors)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                rewrite(item, f"{trail}[{index}]", errors)

    rewrite_map = {r["path"]: r for r in changes.get("sample_id_rewrites") or []}
    test_errors: List[str] = []
    rewrite(
        {
            "device_plan": updated.get("device_plan"),
            "container_plan": updated.get("container_plan"),
        },
        "candidate",
        test_errors,
    )
    applied.extend(f"error:{e}" for e in test_errors)

    handoffs = updated.get("offline_handoffs") or []
    for add in changes.get("handoff_identity_adds") or []:
        index = add["handoff_index"]
        if not (0 <= index < len(handoffs)):
            applied.append(f"conflict:handoff:{index}")
            continue
        raw_declared = handoffs[index].get("source_material_identity_ids")
        if raw_declared is None:
            declared: List[str] = []
        elif not isinstance(raw_declared, list):
            applied.append(f"conflict:handoff_source_material_ids:{index}")
            continue
        elif any(
            not isinstance(value, str) or not value.strip()
            for value in raw_declared
        ):
            applied.append(f"conflict:handoff_source_material_id:{index}")
            continue
        else:
            declared = list(raw_declared)
        added_here = [i for i in add["add"] if i not in declared]
        if added_here:
            declared.extend(added_here)
            handoffs[index]["source_material_identity_ids"] = declared
            applied.append(f"handoff_add:{index}:{','.join(added_here)}")

    return updated, applied


def ensure_binding_ledger(
    candidate: Dict[str, Any],
    research_handoff: Dict[str, Any],
    semantic_analysis: Dict[str, Any],
    *,
    category_of_workstation: Callable[[str], Optional[str]],
    frozen_sample_id: str,
    frozen_group_id: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[LedgerIssue], List[str]]:
    """One-call atomic helper: construct + guarded apply.

    A constructor issue or an apply conflict returns the original candidate;
    callers never receive a half-built ledger.  A second successful run adds
    nothing.
    """
    changes, issues = construct_binding_ledger(
        candidate,
        research_handoff,
        semantic_analysis,
        category_of_workstation=category_of_workstation,
        frozen_sample_id=frozen_sample_id,
        frozen_group_id=frozen_group_id,
    )
    if issues:
        return copy.deepcopy(candidate), issues, []
    updated, applied = apply_ledger_changes(candidate, changes)
    apply_failures = [
        item
        for item in applied
        if item.startswith("conflict:") or item.startswith("error:")
    ]
    if apply_failures:
        return copy.deepcopy(candidate), [LedgerIssue(
            "binding_ledger_apply_conflict",
            "binding ledger guarded apply detected conflicting state",
            failures=apply_failures,
        )], applied
    return updated, issues, applied
