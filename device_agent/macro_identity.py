"""Pure helpers for opaque, type-preserving Research macro identifiers.

Macro identifiers are JSON scalars, not numbers encoded as text.  The string
``"001"`` therefore remains a string, ``1`` and ``"1"`` are distinct, and a
string containing punctuation such as ``"1,2"`` is one identifier.  Only an
actual JSON array in a source field expands to multiple identifiers.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from chem_agent_contracts.identity import (
    IdentityContractError,
    IdentityKey,
    JsonScalarIdentity,
    json_scalar_identity_key,
    normalize_json_scalar_identity,
)


MacroId = JsonScalarIdentity
MacroKey = IdentityKey

EXPLICIT_MACRO_STEP_ID_FIELDS = ("macro_step_id", "logical_step_id")
LEGACY_MACRO_STEP_ID_FIELDS = ("步骤序号", "step")


class MacroIdentityError(ValueError):
    def __init__(self, code: str, path: str, message: str) -> None:
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}（{code.lower()}）。")


def normalize_macro_id(value: Any, path: str = "macro_id") -> MacroId:
    """Validate one JSON scalar identifier without parsing or type coercion."""

    try:
        return normalize_json_scalar_identity(value, path)
    except IdentityContractError as exc:
        if isinstance(value, bool) or value is None:
            raise MacroIdentityError(
                "INVALID_MACRO_ID",
                path,
                "expected a non-boolean JSON scalar identifier",
            ) from exc
        if isinstance(value, str):
            raise MacroIdentityError(
                "INVALID_MACRO_ID",
                path,
                "expected a nonempty string identifier",
            ) from exc
        raise MacroIdentityError(
            "INVALID_MACRO_ID",
            path,
            "expected a string or finite JSON number; arrays/objects are not scalar IDs",
        ) from exc


def macro_id_key(value: Any, path: str = "macro_id") -> MacroKey:
    """Return a hash key that keeps JSON number/string identities distinct."""

    normalized = normalize_macro_id(value, path)
    return json_scalar_identity_key(normalized, path)


def macro_ids_equal(left: Any, right: Any) -> bool:
    try:
        return macro_id_key(left) == macro_id_key(right)
    except MacroIdentityError:
        return False


def unique_macro_ids(values: Iterable[Any], path: str = "macro_ids") -> List[MacroId]:
    output: List[MacroId] = []
    seen: set[MacroKey] = set()
    for index, value in enumerate(values):
        normalized = normalize_macro_id(value, f"{path}[{index}]")
        key = macro_id_key(normalized)
        if key not in seen:
            seen.add(key)
            output.append(normalized)
    return output


def expand_macro_id_value(
    value: Any,
    path: str,
    *,
    allow_list: bool,
) -> List[MacroId]:
    """Expand only a real JSON list; string syntax is always opaque."""

    if isinstance(value, list):
        if not allow_list:
            raise MacroIdentityError(
                "INVALID_MACRO_ID", path, "expected one scalar macro identifier"
            )
        return [
            normalize_macro_id(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    return [normalize_macro_id(value, path)]


def extract_source_macro_ids(
    payload: Dict[str, Any],
    path: str = "payload",
    *,
    required: bool = False,
) -> List[MacroId]:
    """Read one typed primary source plus its optional complete coverage list.

    All three spellings are mirrors of the same binding.  Scalar mirrors must
    agree with the first item of a declared list, complete list mirrors must
    agree in typed order, and duplicate coverage is invalid.  This prevents a
    higher-priority spelling from hiding contradictory provenance.
    """

    if not isinstance(payload, dict):
        raise MacroIdentityError("INVALID_MACRO_SOURCE", path, "expected an object")
    declared_primaries: List[tuple[MacroId, str]] = []
    full_list_mirrors: List[tuple[List[MacroId], str]] = []

    if "source_macro_step_id" in payload:
        field_path = f"{path}.source_macro_step_id"
        raw = payload["source_macro_step_id"]
        if isinstance(raw, list):
            raise MacroIdentityError(
                "INVALID_MACRO_SOURCE",
                field_path,
                "expected one scalar macro identifier",
            )
        declared_primaries.append((normalize_macro_id(raw, field_path), field_path))

    if "source_macro_step" in payload:
        field_path = f"{path}.source_macro_step"
        raw = payload["source_macro_step"]
        values = expand_macro_id_value(raw, field_path, allow_list=True)
        if not values:
            raise MacroIdentityError(
                "INVALID_MACRO_SOURCE",
                field_path,
                "source list must contain a primary macro identifier",
            )
        declared_primaries.append((values[0], field_path))
        if isinstance(raw, list):
            full_list_mirrors.append((values, field_path))

    if "source_macro_steps" in payload:
        field_path = f"{path}.source_macro_steps"
        raw = payload["source_macro_steps"]
        if not isinstance(raw, list):
            raise MacroIdentityError(
                "INVALID_MACRO_SOURCE",
                field_path,
                "expected an array of scalar macro identifiers",
            )
        values = expand_macro_id_value(raw, field_path, allow_list=True)
        if not values:
            raise MacroIdentityError(
                "INVALID_MACRO_SOURCE",
                field_path,
                "source list must contain a primary macro identifier",
            )
        declared_primaries.append((values[0], field_path))
        full_list_mirrors.append((values, field_path))

    if not declared_primaries:
        if not required:
            return []
        raise MacroIdentityError(
            "MISSING_MACRO_SOURCE",
            path,
            "missing source_macro_step_id/source_macro_step/source_macro_steps",
        )

    primary, _ = declared_primaries[0]
    primary_key = macro_id_key(primary)
    if any(macro_id_key(candidate) != primary_key for candidate, _ in declared_primaries[1:]):
        raise MacroIdentityError(
            "CONFLICTING_MACRO_SOURCE",
            path,
            "source mirrors must declare the same typed primary macro identifier",
        )

    if len(full_list_mirrors) > 1:
        reference = [macro_id_key(value) for value in full_list_mirrors[0][0]]
        for values, field_path in full_list_mirrors[1:]:
            if [macro_id_key(value) for value in values] != reference:
                raise MacroIdentityError(
                    "CONFLICTING_MACRO_SOURCE_COVERAGE",
                    field_path,
                    "complete source-list mirrors must match in typed order",
                )

    coverage = full_list_mirrors[0][0] if full_list_mirrors else [primary]
    coverage_keys = [macro_id_key(value) for value in coverage]
    if len(set(coverage_keys)) != len(coverage_keys):
        raise MacroIdentityError(
            "DUPLICATE_MACRO_SOURCE_COVERAGE",
            full_list_mirrors[0][1] if full_list_mirrors else path,
            "source coverage must not repeat a typed macro identifier",
        )
    return list(coverage)


def _resolve_typed_alias_group(
    record: Dict[str, Any],
    fields: tuple[str, ...],
    path: str,
) -> Optional[MacroId]:
    declared: List[tuple[MacroId, str]] = []
    for field in fields:
        if field not in record:
            continue
        field_path = f"{path}.{field}"
        declared.append((normalize_macro_id(record[field], field_path), field_path))
    if not declared:
        return None
    resolved, _ = declared[0]
    resolved_key = macro_id_key(resolved)
    if any(macro_id_key(candidate) != resolved_key for candidate, _ in declared[1:]):
        raise MacroIdentityError(
            "CONFLICTING_MACRO_ID_MIRRORS",
            path,
            "declared macro identifier mirrors must agree by typed identity",
        )
    return resolved


def semantic_macro_id(
    record: Dict[str, Any],
    path: str = "macro_step",
    *,
    required: bool = True,
) -> Optional[MacroId]:
    """Resolve one macro *step* identity without crossing identity domains.

    Explicit step-ID mirrors own resolution and must agree.  Legacy ordinal
    mirrors are considered only when both explicit spellings are absent.
    ``macro_action_id`` is deliberately not a step-ID fallback: one action may
    contain several steps, so such a fallback collapses distinct identities.
    """

    if not isinstance(record, dict):
        raise MacroIdentityError("INVALID_MACRO_RECORD", path, "expected an object")
    resolved = _resolve_typed_alias_group(
        record, EXPLICIT_MACRO_STEP_ID_FIELDS, path
    )
    if resolved is not None:
        return resolved
    resolved = _resolve_typed_alias_group(record, LEGACY_MACRO_STEP_ID_FIELDS, path)
    if resolved is not None:
        return resolved
    if required:
        raise MacroIdentityError(
            "MISSING_MACRO_ID",
            path,
            "missing macro_step_id/logical_step_id/步骤序号/step",
        )
    return None


def semantic_macro_action_id(
    record: Dict[str, Any],
    path: str = "macro_action",
    *,
    required: bool = True,
) -> Optional[MacroId]:
    """Resolve the distinct macro-action identity domain explicitly."""

    if not isinstance(record, dict):
        raise MacroIdentityError("INVALID_MACRO_RECORD", path, "expected an object")
    if "macro_action_id" in record:
        return normalize_macro_id(record["macro_action_id"], f"{path}.macro_action_id")
    if required:
        raise MacroIdentityError(
            "MISSING_MACRO_ACTION_ID", path, "missing macro_action_id"
        )
    return None
