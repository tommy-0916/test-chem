"""Apply evidence-bound material contract revisions to frozen Research states.

This module deliberately does *not* infer material semantics from macro-step
numbers, operation categories, or a generated Device candidate.  A caller must
provide an explicit revision manifest whose evidence quotes resolve verbatim in
the immutable source Research state.  The source file is never modified; the
CLI writes a new state with an embedded revision record by atomic replacement.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, MutableMapping, Sequence, Tuple

from pydantic import ValidationError

from .adapters import research_state_to_v2
from .identity import json_scalar_identity_key
from .v2 import (
    MaterialApplicabilityEvidenceV2,
    MaterialOperationSegmentV2,
    MaterialRelationV2,
    ProvenanceV2,
)


MANIFEST_SCHEMA_VERSION = "material-contract-revision-manifest-v2"
RECORD_SCHEMA_VERSION = "material-contract-revision-record-v2"

_CONTRACT_COLLECTIONS = (
    "material_inputs",
    "material_outputs",
    "material_intermediates",
    "logical_containers",
    "material_relations",
)
_PORT_COLLECTIONS = (
    "material_inputs",
    "material_outputs",
    "material_intermediates",
)
_STRUCTURAL_COLLECTIONS = (
    "operation_segments",
    "material_applicability",
)
_DISPOSITIONS = {"declared", "not_applicable", "unresolved"}
_REVISION_MODES = {"manual_evidence_bound", "automated_evidence_bound"}
_RELATION_QUANTITY_BASES = {
    "whole_batch",
    "conserved_inventory",
    "runtime_measurement_required",
    "planning_yield_lower_bound",
}
_PATH_TOKEN_RE = re.compile(r"(?:^|\.)([^.\[\]]+)|\[(\d+)\]")


class MaterialContractRevisionError(ValueError):
    """A deterministic manifest/source contract violation."""

    def __init__(self, code: str, path: str, message: str) -> None:
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"{code} at {path}: {message}")


def _fail(code: str, path: str, message: str) -> None:
    raise MaterialContractRevisionError(code, path, message)


def canonical_digest(value: Any, *, prefix: str = "sha256") -> str:
    """Return a stable digest without relying on mutable model defaults."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()}"


def _path_tokens(path: str) -> List[Any]:
    if not isinstance(path, str) or not path:
        _fail("INVALID_SOURCE_PATH", "source_path", "expected a nonempty JSON path")
    tokens: List[Any] = []
    cursor = 0
    for match in _PATH_TOKEN_RE.finditer(path):
        if match.start() != cursor:
            _fail("INVALID_SOURCE_PATH", path, "unsupported path syntax")
        key, index = match.groups()
        tokens.append(key if key is not None else int(index))
        cursor = match.end()
    if cursor != len(path) or not tokens:
        _fail("INVALID_SOURCE_PATH", path, "unsupported path syntax")
    return tokens


def resolve_json_path(document: Any, path: str) -> Any:
    """Resolve a restricted dotted/indexed path such as ``macro_plan[0].参数``."""

    value = document
    traversed: List[str] = []
    for token in _path_tokens(path):
        if isinstance(token, int):
            if not isinstance(value, list) or token >= len(value):
                _fail("SOURCE_PATH_NOT_FOUND", path, f"index {token} does not resolve")
            value = value[token]
            traversed.append(f"[{token}]")
        else:
            if not isinstance(value, Mapping) or token not in value:
                _fail("SOURCE_PATH_NOT_FOUND", path, f"key {token!r} does not resolve")
            value = value[token]
            traversed.append(token)
    return value


def _require_mapping(value: Any, path: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail("INVALID_OBJECT", path, "expected an object")
    return dict(value)


def _require_list(value: Any, path: str) -> List[Any]:
    if not isinstance(value, list):
        _fail("INVALID_ARRAY", path, "expected an array")
    return value


def _require_nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("INVALID_STRING", path, "expected a nonempty string")
    return value


def _require_utc_timestamp(value: Any, path: str) -> str:
    """Normalize an authored UTC timestamp so revision replay is deterministic."""

    raw = _require_nonempty_string(value, path)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        _fail("INVALID_TIMESTAMP", path, "expected an ISO-8601 UTC timestamp")
        raise AssertionError("unreachable") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(None):
        _fail("INVALID_TIMESTAMP", path, "timestamp must include the UTC offset")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _revision_metadata(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    mode = _require_nonempty_string(
        manifest.get("revision_mode"), "manifest.revision_mode"
    )
    if mode not in _REVISION_MODES:
        _fail(
            "INVALID_REVISION_MODE",
            "manifest.revision_mode",
            f"expected one of {sorted(_REVISION_MODES)!r}",
        )
    if mode == "automated_evidence_bound":
        _fail(
            "AUTOMATED_REVISION_RESOLVER_UNAVAILABLE",
            "manifest.revision_mode",
            "automated evidence-bound revision is disabled until a trusted "
            "machine evidence resolver verifies every injected claim",
        )
    purpose = _require_nonempty_string(manifest.get("purpose"), "manifest.purpose")
    if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", purpose) is None:
        _fail(
            "INVALID_REVISION_PURPOSE",
            "manifest.purpose",
            "expected a stable lower_snake_case purpose identifier",
        )
    automation_claim = manifest.get("automation_claim")
    if not isinstance(automation_claim, bool):
        _fail(
            "INVALID_AUTOMATION_CLAIM",
            "manifest.automation_claim",
            "expected an explicit boolean",
        )
    expected_claim = mode == "automated_evidence_bound"
    if automation_claim is not expected_claim:
        _fail(
            "REVISION_AUTOMATION_CONTRADICTION",
            "manifest.automation_claim",
            f"revision_mode={mode!r} requires automation_claim={expected_claim!r}",
        )
    raw_policy = manifest.get("authoring_policy", {})
    if not isinstance(raw_policy, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, bool)
        for key, value in raw_policy.items()
    ):
        _fail(
            "INVALID_AUTHORING_POLICY",
            "manifest.authoring_policy",
            "expected an object of boolean policy declarations",
        )
    return {
        "revision_mode": mode,
        "purpose": purpose,
        "automation_claim": automation_claim,
        "authoring_policy": copy.deepcopy(dict(raw_policy)),
    }


def _identity_key(value: Any, path: str) -> Tuple[str, Any]:
    try:
        return json_scalar_identity_key(value, path)
    except Exception as exc:
        _fail("INVALID_IDENTITY", path, str(exc))
    raise AssertionError("unreachable")


def _canonical_package_source_path(source_path: str) -> str:
    """Map a frozen Research-state scalar to its canonical package mirror."""

    match = re.fullmatch(r"macro_plan\[(\d+)\]\.(操作|operation)", source_path)
    if match:
        return f"macro_steps[{match.group(1)}].operation"
    match = re.fullmatch(r"macro_plan\[(\d+)\]\.(参数|parameters)", source_path)
    if match:
        return f"macro_steps[{match.group(1)}].parameters[0].value"
    return ""


def _validate_source_refs(
    original: Mapping[str, Any],
    step_manifest: Mapping[str, Any],
    step_path: str,
    manifest_path: str,
) -> Dict[str, Dict[str, str]]:
    raw_refs = _require_list(step_manifest.get("source_refs"), f"{manifest_path}.source_refs")
    if not raw_refs:
        _fail(
            "MISSING_SOURCE_REFERENCE",
            f"{manifest_path}.source_refs",
            "every revised step needs at least one verbatim source reference",
        )
    refs: Dict[str, Dict[str, str]] = {}
    for index, raw_ref in enumerate(raw_refs):
        ref_path = f"{manifest_path}.source_refs[{index}]"
        ref = _require_mapping(raw_ref, ref_path)
        ref_id = _require_nonempty_string(ref.get("source_ref_id"), f"{ref_path}.source_ref_id")
        if ref_id in refs:
            _fail("DUPLICATE_SOURCE_REFERENCE", f"{ref_path}.source_ref_id", ref_id)
        source_path = _require_nonempty_string(ref.get("source_path"), f"{ref_path}.source_path")
        source_quote = _require_nonempty_string(ref.get("source_quote"), f"{ref_path}.source_quote")
        source_value = resolve_json_path(original, source_path)
        if not isinstance(source_value, str):
            _fail(
                "SOURCE_REFERENCE_NOT_TEXT",
                f"{ref_path}.source_path",
                "verbatim quotes may only bind to a frozen string field",
            )
        if source_quote not in source_value:
            _fail(
                "SOURCE_QUOTE_MISMATCH",
                f"{ref_path}.source_quote",
                "quote is not an exact substring of the frozen source field",
            )
        refs[ref_id] = {
            "source_ref_id": ref_id,
            "source_path": source_path,
            "source_quote": source_quote,
            "canonical_source_path": _canonical_package_source_path(source_path),
            "source_digest": canonical_digest(source_value),
        }
    return refs


def _validate_ref_ids(
    value: Any,
    refs: Mapping[str, Any],
    path: str,
) -> List[str]:
    ids = _require_list(value, path)
    if not ids:
        _fail("MISSING_EVIDENCE_BINDING", path, "at least one source_ref_id is required")
    result: List[str] = []
    for index, ref_id in enumerate(ids):
        ref_id = _require_nonempty_string(ref_id, f"{path}[{index}]")
        if ref_id not in refs:
            _fail("UNKNOWN_SOURCE_REFERENCE", f"{path}[{index}]", ref_id)
        if ref_id in result:
            _fail("DUPLICATE_SOURCE_REFERENCE", f"{path}[{index}]", ref_id)
        result.append(ref_id)
    return result


def _strip_evidence_metadata(
    raw_items: Any,
    refs: Mapping[str, Dict[str, str]],
    path: str,
    *,
    port_collection: bool = False,
    revision_id: str,
    manifest_digest: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    items = _require_list(raw_items, path)
    contracts: List[Dict[str, Any]] = []
    evidence_bindings: List[Dict[str, Any]] = []
    for index, raw_item in enumerate(items):
        item_path = f"{path}[{index}]"
        item = _require_mapping(raw_item, item_path)
        source_ref_ids = _validate_ref_ids(
            item.pop("source_ref_ids", None), refs, f"{item_path}.source_ref_ids"
        )
        quantity_semantic = None
        if port_collection:
            quantity = item.get("quantity")
            if quantity is not None:
                quantity = _require_mapping(quantity, f"{item_path}.quantity")
                mode = quantity.get("mode", "exact")
                quantity_semantic = quantity.get("semantic")
                allowed_semantics = {
                    "exact": {"planned_target", "planning_estimate"},
                    "all_available": {"whole_batch_unspecified"},
                    # In a Research contract this mode is a future measurement
                    # requirement.  It is never evidence that a measurement
                    # has already happened.
                    "runtime_measured": {"runtime_measurement_required"},
                }.get(mode)
                if allowed_semantics is None:
                    _fail("INVALID_QUANTITY_MODE", f"{item_path}.quantity.mode", str(mode))
                if quantity_semantic not in allowed_semantics:
                    _fail(
                        "UNKNOWN_QUANTITY_PROMOTED_TO_MEASURED",
                        f"{item_path}.quantity.semantic",
                        f"{mode} requires one of {sorted(allowed_semantics)!r}; "
                        "a Research revision may only request a future runtime measurement",
                    )
        provenance = item.get("provenance")
        if not isinstance(provenance, Mapping):
            _fail("MISSING_PROVENANCE", f"{item_path}.provenance", "structured provenance is required")
        if provenance.get("kind") != "manual_revision":
            _fail(
                "REVISION_PROVENANCE_MUST_BE_MANUAL",
                f"{item_path}.provenance.kind",
                "claims injected or replaced by the revision CLI must use "
                "kind=manual_revision; user/paper provenance cannot be asserted "
                "by a manual manifest",
            )
        reference = _require_nonempty_string(
            provenance.get("reference"), f"{item_path}.provenance.reference"
        )
        allowed_paths = {refs[ref_id]["source_path"] for ref_id in source_ref_ids}
        if reference not in allowed_paths:
            _fail(
                "UNBOUND_PROVENANCE_REFERENCE",
                f"{item_path}.provenance.reference",
                "reference must equal the source_path of a bound source_ref_id",
            )
        bound_ref = next(
            refs[ref_id]
            for ref_id in source_ref_ids
            if refs[ref_id]["source_path"] == reference
        )
        canonical_source_path = bound_ref.get("canonical_source_path", "")
        if not canonical_source_path:
            _fail(
                "UNVERIFIABLE_MANUAL_REVISION_SOURCE",
                f"{item_path}.provenance.reference",
                "manual revision evidence must map to a scalar retained in the "
                "canonical Research package",
            )
        provenance = dict(provenance)
        provenance.update(
            {
                "source_path": canonical_source_path,
                "excerpt": bound_ref["source_quote"],
                "source_digest": bound_ref["source_digest"],
                "revision_id": revision_id,
                "manifest_digest": manifest_digest,
                "automation_claim": False,
            }
        )
        item["provenance"] = provenance
        try:
            ProvenanceV2.model_validate(item["provenance"], strict=True)
        except ValidationError as exc:
            _fail(
                "INVALID_PROVENANCE",
                f"{item_path}.provenance",
                "; ".join(error["msg"] for error in exc.errors(include_url=False)),
            )
        contracts.append(item)
        binding: Dict[str, Any] = {
            "target_index": index,
            "source_ref_ids": source_ref_ids,
        }
        if quantity_semantic is not None:
            binding["quantity_semantic"] = quantity_semantic
        evidence_bindings.append(binding)
    return contracts, evidence_bindings


def _validate_status(
    step_manifest: Mapping[str, Any],
    refs: Mapping[str, Dict[str, str]],
    manifest_path: str,
) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    raw_status = _require_mapping(
        step_manifest.get("material_contract_status"),
        f"{manifest_path}.material_contract_status",
    )
    missing = [field for field in _CONTRACT_COLLECTIONS if field not in raw_status]
    extra = sorted(set(raw_status) - set(_CONTRACT_COLLECTIONS))
    if missing or extra:
        _fail(
            "INVALID_MATERIAL_CONTRACT_STATUS",
            f"{manifest_path}.material_contract_status",
            f"missing={missing}, extra={extra}",
        )
    status: Dict[str, str] = {}
    for field in _CONTRACT_COLLECTIONS:
        value = raw_status[field]
        if value not in _DISPOSITIONS:
            _fail(
                "INVALID_MATERIAL_CONTRACT_STATUS",
                f"{manifest_path}.material_contract_status.{field}",
                str(value),
            )
        status[field] = value

    raw_status_refs = _require_mapping(
        step_manifest.get("status_source_ref_ids"),
        f"{manifest_path}.status_source_ref_ids",
    )
    if set(raw_status_refs) != set(_CONTRACT_COLLECTIONS):
        _fail(
            "INVALID_STATUS_EVIDENCE",
            f"{manifest_path}.status_source_ref_ids",
            "must bind every material contract disposition",
        )
    status_refs = {
        field: _validate_ref_ids(
            raw_status_refs[field], refs, f"{manifest_path}.status_source_ref_ids.{field}"
        )
        for field in _CONTRACT_COLLECTIONS
    }
    return status, status_refs


def _validate_relation_shape(relation: Mapping[str, Any], path: str) -> None:
    basis = relation.get("quantity_basis")
    event_kind = relation.get("event_kind")
    input_ids = relation.get("input_material_instance_ids")
    output_ids = relation.get("output_material_instance_ids")
    if not isinstance(input_ids, list) or not isinstance(output_ids, list):
        _fail("INVALID_MATERIAL_RELATION", path, "input/output IDs must be arrays")
    if event_kind == "none":
        if input_ids or output_ids or basis is not None:
            _fail("INVALID_MATERIAL_RELATION", path, "event_kind=none cannot carry edges or quantity basis")
        return
    if not input_ids or not output_ids:
        _fail("INVALID_MATERIAL_RELATION", path, "a material event requires input and output IDs")
    if basis not in _RELATION_QUANTITY_BASES:
        _fail("INVALID_RELATION_QUANTITY_BASIS", f"{path}.quantity_basis", str(basis))
    if basis == "whole_batch" and (len(input_ids) != 1 or len(output_ids) != 1):
        _fail("INVALID_WHOLE_BATCH_RELATION", path, "whole_batch must be exactly 1-to-1")
    if basis == "planning_yield_lower_bound":
        planning_quantity = relation.get("planning_quantity")
        if not isinstance(planning_quantity, Mapping):
            _fail(
                "MISSING_PLANNING_QUANTITY",
                f"{path}.planning_quantity",
                "planning_yield_lower_bound needs an explicit exact planning quantity",
            )
        if (
            planning_quantity.get("mode") != "exact"
            or planning_quantity.get("semantic") != "planning_estimate"
            or planning_quantity.get("value") is None
            or not str(planning_quantity.get("unit") or "").strip()
        ):
            _fail(
                "INVALID_PLANNING_QUANTITY",
                f"{path}.planning_quantity",
                "expected {mode: exact, semantic: planning_estimate, value, unit}",
            )


def _validate_step_graph(
    step_contract: Mapping[str, Any],
    status: Mapping[str, str],
    path: str,
) -> None:
    collections = {
        field: _require_list(step_contract[field], f"{path}.{field}")
        for field in _CONTRACT_COLLECTIONS
    }
    for field, values in collections.items():
        disposition = status[field]
        if disposition == "declared" and not values:
            _fail("EMPTY_DECLARED_COLLECTION", f"{path}.{field}", "declared requires records")
        if disposition == "not_applicable" and values:
            _fail(
                "NONEMPTY_NOT_APPLICABLE_COLLECTION",
                f"{path}.{field}",
                "not_applicable requires an empty array",
            )

    ids_by_collection: Dict[str, List[str]] = {}
    ports_by_collection: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for field in _PORT_COLLECTIONS:
        ids: List[str] = []
        ports: Dict[str, Dict[str, Any]] = {}
        for index, item in enumerate(collections[field]):
            instance_id = _require_nonempty_string(
                item.get("material_instance_id"), f"{path}.{field}[{index}].material_instance_id"
            )
            ids.append(instance_id)
            ports[instance_id] = item
            origin = item.get("material_origin")
            if field == "material_inputs" and origin not in {
                "external_inventory", "upstream_output"
            }:
                _fail(
                    "INVALID_MATERIAL_ORIGIN",
                    f"{path}.{field}[{index}].material_origin",
                    "input origin must be external_inventory or upstream_output",
                )
            if field == "material_intermediates" and origin != "same_step_relation":
                _fail(
                    "INVALID_MATERIAL_ORIGIN",
                    f"{path}.{field}[{index}].material_origin",
                    "intermediate origin must be same_step_relation",
                )
        if len(ids) != len(set(ids)):
            _fail("DUPLICATE_MATERIAL_INSTANCE", f"{path}.{field}", "instance IDs must be unique")
        ids_by_collection[field] = ids
        ports_by_collection[field] = ports

    all_port_ids = [value for values in ids_by_collection.values() for value in values]
    if len(all_port_ids) != len(set(all_port_ids)):
        _fail("DUPLICATE_MATERIAL_INSTANCE", path, "an instance ID may be defined by only one port")

    if status["material_relations"] != "declared":
        return
    if status["material_inputs"] == "unresolved" or status["material_outputs"] == "unresolved":
        _fail(
            "RELATION_OVER_UNRESOLVED_PORTS",
            f"{path}.material_relations",
            "declared relations require resolved input and output collections",
        )

    input_ids = set(ids_by_collection["material_inputs"])
    output_ids = set(ids_by_collection["material_outputs"])
    intermediate_ids = set(ids_by_collection["material_intermediates"])
    relation_ids: List[str] = []
    produced_by: Dict[str, int] = {}
    consumed_at: Dict[str, List[int]] = {value: [] for value in intermediate_ids}
    used_inputs: set[str] = set()
    produced_outputs: set[str] = set()
    input_use_counts = {instance_id: 0 for instance_id in input_ids}
    output_production_counts = {instance_id: 0 for instance_id in output_ids}
    for index, relation in enumerate(collections["material_relations"]):
        relation_path = f"{path}.material_relations[{index}]"
        relation_id = _require_nonempty_string(relation.get("relation_id"), f"{relation_path}.relation_id")
        relation_ids.append(relation_id)
        _validate_relation_shape(relation, relation_path)
        try:
            parsed_relation = MaterialRelationV2.model_validate(relation, strict=True)
        except ValidationError as exc:
            _fail(
                "INVALID_MATERIAL_RELATION",
                relation_path,
                "; ".join(error["msg"] for error in exc.errors(include_url=False)),
            )
        relation_inputs = relation.get("input_material_instance_ids", [])
        relation_outputs = relation.get("output_material_instance_ids", [])
        unknown_inputs = set(relation_inputs) - input_ids - intermediate_ids
        unknown_outputs = set(relation_outputs) - output_ids - intermediate_ids
        if unknown_inputs or unknown_outputs:
            _fail(
                "UNBOUND_RELATION_ENDPOINT",
                relation_path,
                f"unknown inputs={sorted(unknown_inputs)}, outputs={sorted(unknown_outputs)}",
            )
        used_inputs.update(set(relation_inputs) & input_ids)
        produced_outputs.update(set(relation_outputs) & output_ids)
        for instance_id in set(relation_inputs) & input_ids:
            input_use_counts[instance_id] += 1
        for instance_id in set(relation_outputs) & output_ids:
            output_production_counts[instance_id] += 1
        if parsed_relation.event_kind in {
            "process_same_material",
            "split_same_material",
            "replicate_same_material",
        } and not unknown_inputs and not unknown_outputs:
            endpoint_ports = [
                ports_by_collection["material_inputs"].get(instance_id)
                or ports_by_collection["material_intermediates"].get(instance_id)
                for instance_id in relation_inputs
            ] + [
                ports_by_collection["material_intermediates"].get(instance_id)
                or ports_by_collection["material_outputs"].get(instance_id)
                for instance_id in relation_outputs
            ]
            material_ids = {
                port.get("material_id")
                for port in endpoint_ports
                if isinstance(port, Mapping)
                and isinstance(port.get("material_id"), str)
                and port.get("material_id").strip()
            }
            if all(
                isinstance(port, Mapping)
                and isinstance(port.get("material_id"), str)
                and bool(port.get("material_id").strip())
                for port in endpoint_ports
            ) and len(material_ids) != 1:
                _fail(
                    "SAME_MATERIAL_IDENTITY_CHANGED",
                    relation_path,
                    f"{parsed_relation.event_kind} must preserve one material_id; "
                    "use state_change for identity changes",
                )
        for instance_id in set(relation_outputs) & intermediate_ids:
            if instance_id in produced_by:
                _fail(
                    "MULTIPLE_INTERMEDIATE_PRODUCERS",
                    relation_path,
                    f"{instance_id} was already produced",
                )
            produced_by[instance_id] = index
        for instance_id in set(relation_inputs) & intermediate_ids:
            consumed_at[instance_id].append(index)
    if len(relation_ids) != len(set(relation_ids)):
        _fail("DUPLICATE_RELATION_ID", f"{path}.material_relations", "relation IDs must be unique")
    if used_inputs != input_ids:
        _fail(
            "ORPHAN_MATERIAL_INPUT",
            f"{path}.material_relations",
            f"unconsumed input instances={sorted(input_ids - used_inputs)}",
        )
    if produced_outputs != output_ids:
        _fail(
            "ORPHAN_MATERIAL_OUTPUT",
            f"{path}.material_relations",
            f"unproduced output instances={sorted(output_ids - produced_outputs)}",
        )
    multiply_consumed = sorted(
        instance_id for instance_id, count in input_use_counts.items() if count > 1
    )
    multiply_produced = sorted(
        instance_id
        for instance_id, count in output_production_counts.items()
        if count > 1
    )
    if multiply_consumed:
        _fail(
            "DUPLICATE_MATERIAL_CONSUMPTION",
            f"{path}.material_relations",
            f"inputs consumed more than once={multiply_consumed}; use one split relation",
        )
    if multiply_produced:
        _fail(
            "DUPLICATE_MATERIAL_PRODUCTION",
            f"{path}.material_relations",
            f"outputs produced more than once={multiply_produced}",
        )
    for instance_id in sorted(intermediate_ids):
        producer = produced_by.get(instance_id)
        consumers = consumed_at.get(instance_id) or []
        if producer is None or not consumers:
            _fail(
                "OPEN_INTERMEDIATE_CHAIN",
                f"{path}.material_intermediates",
                f"{instance_id} must have one producer and a later consumer",
            )
        if any(consumer <= producer for consumer in consumers):
            _fail(
                "NONCAUSAL_INTERMEDIATE_CHAIN",
                f"{path}.material_relations",
                f"{instance_id} is consumed before it is produced",
            )


def _validate_structural_material_contract(
    step_contract: Mapping[str, Any],
    status: Mapping[str, str],
    path: str,
) -> None:
    raw_segments = _require_list(
        step_contract.get("operation_segments"), f"{path}.operation_segments"
    )
    if not raw_segments:
        _fail(
            "MISSING_OPERATION_SEGMENTS",
            f"{path}.operation_segments",
            "an evidence-bound revision requires at least one structured operation segment",
        )
    segments: Dict[str, MaterialOperationSegmentV2] = {}
    for index, raw_segment in enumerate(raw_segments):
        try:
            segment = MaterialOperationSegmentV2.model_validate(raw_segment, strict=True)
        except ValidationError as exc:
            _fail(
                "INVALID_OPERATION_SEGMENT",
                f"{path}.operation_segments[{index}]",
                "; ".join(error["msg"] for error in exc.errors(include_url=False)),
            )
        if segment.segment_id in segments:
            _fail(
                "DUPLICATE_OPERATION_SEGMENT",
                f"{path}.operation_segments[{index}].segment_id",
                segment.segment_id,
            )
        segments[segment.segment_id] = segment

    raw_applicability = _require_list(
        step_contract.get("material_applicability"),
        f"{path}.material_applicability",
    )
    applicability: Dict[str, MaterialApplicabilityEvidenceV2] = {}
    for index, raw_evidence in enumerate(raw_applicability):
        try:
            evidence = MaterialApplicabilityEvidenceV2.model_validate(
                raw_evidence, strict=True
            )
        except ValidationError as exc:
            _fail(
                "INVALID_MATERIAL_APPLICABILITY",
                f"{path}.material_applicability[{index}]",
                "; ".join(error["msg"] for error in exc.errors(include_url=False)),
            )
        if evidence.contract_field in applicability:
            _fail(
                "DUPLICATE_MATERIAL_APPLICABILITY",
                f"{path}.material_applicability[{index}].contract_field",
                evidence.contract_field,
            )
        unknown = set(evidence.operation_segment_ids) - set(segments)
        if unknown:
            _fail(
                "UNKNOWN_OPERATION_SEGMENT",
                f"{path}.material_applicability[{index}].operation_segment_ids",
                repr(sorted(unknown)),
            )
        if set(evidence.operation_segment_ids) != set(segments):
            _fail(
                "PARTIAL_MATERIAL_APPLICABILITY",
                f"{path}.material_applicability[{index}].operation_segment_ids",
                "not_applicable evidence must cover every operation segment in the macro step",
            )
        applicability[evidence.contract_field] = evidence

    not_applicable_fields = {
        field for field in _CONTRACT_COLLECTIONS if status[field] == "not_applicable"
    }
    if set(applicability) != not_applicable_fields:
        _fail(
            "INCOMPLETE_MATERIAL_APPLICABILITY",
            f"{path}.material_applicability",
            "must contain exactly one evidence record for every not_applicable field",
        )
    segment_effects = {segment.material_effect for segment in segments.values()}
    if "unknown" in segment_effects and not_applicable_fields:
        _fail(
            "UNKNOWN_EFFECT_NA_BYPASS",
            f"{path}.material_applicability",
            "unknown material effect cannot authorise any not_applicable dimension",
        )
    if status["material_inputs"] == "not_applicable" and segment_effects & {
        "register_existing_input",
        "consume_material",
        "transform_material",
        "transfer_material",
        "split_material",
        "merge_material",
    }:
        _fail(
            "INPUT_APPLICABILITY_CONFLICT",
            f"{path}.material_contract_status.material_inputs",
            "input-bearing segments require an explicit material input boundary",
        )
    if status["material_outputs"] == "not_applicable" and segment_effects & {
        "produce_material",
        "transform_material",
        "transfer_material",
        "split_material",
        "merge_material",
    }:
        _fail(
            "OUTPUT_APPLICABILITY_CONFLICT",
            f"{path}.material_contract_status.material_outputs",
            "output-bearing segments require an explicit material output boundary",
        )
    relation_evidence = applicability.get("material_relations")
    if relation_evidence is not None:
        conflicting = sorted(
            segment_id
            for segment_id in relation_evidence.operation_segment_ids
            if segments[segment_id].material_effect
            not in {
                "none",
                "register_existing_input",
                "observe_without_material_change",
            }
        )
        if conflicting:
            _fail(
                "MATERIAL_RELATION_APPLICABILITY_BYPASS",
                f"{path}.material_applicability",
                "material-changing segments cannot authorise material_relations=not_applicable: "
                + repr(conflicting),
            )
    if any(
        segment.material_effect == "register_existing_input"
        for segment in segments.values()
    ) and status["material_inputs"] != "declared":
        _fail(
            "UNDECLARED_REGISTERED_INPUT",
            f"{path}.material_contract_status.material_inputs",
            "register_existing_input requires an explicit material input boundary",
        )

    active_segments = {
        segment_id
        for segment_id, segment in segments.items()
        if segment.material_effect
        not in {
            "none",
            "register_existing_input",
            "observe_without_material_change",
        }
    }
    covered_segments: set[str] = set()
    for index, relation in enumerate(step_contract["material_relations"]):
        source_ref = str(relation.get("source_operation_ref") or "")
        if source_ref not in segments:
            _fail(
                "UNKNOWN_RELATION_OPERATION_SEGMENT",
                f"{path}.material_relations[{index}].source_operation_ref",
                source_ref,
            )
        if relation.get("event_kind") != "none":
            covered_segments.add(source_ref)
    uncovered = sorted(active_segments - covered_segments)
    if uncovered:
        _fail(
            "UNCOVERED_MATERIAL_EFFECT",
            f"{path}.operation_segments",
            "material-changing segments require explicit relation coverage: "
            + repr(uncovered),
        )


def _step_identity(source_step: Mapping[str, Any], manifest_id: Any, path: str) -> Any:
    explicit_candidates = [
        source_step.get("macro_step_id"),
        source_step.get("logical_step_id"),
    ]
    existing = [candidate for candidate in explicit_candidates if candidate is not None]
    # A presentation sequence is only a compatibility identity when the source
    # has no stable macro/logical ID.  It must not conflict with a real string ID.
    if not existing and source_step.get("步骤序号") is not None:
        existing = [source_step["步骤序号"]]
    manifest_key = _identity_key(manifest_id, f"{path}.macro_step_id")
    if existing and any(
        _identity_key(candidate, f"{path}.source_identity") != manifest_key
        for candidate in existing
    ):
        _fail(
            "MACRO_STEP_ID_MISMATCH",
            f"{path}.macro_step_id",
            f"manifest ID {manifest_id!r} does not match frozen source identities {existing!r}",
        )
    return manifest_id


def apply_material_contract_revision(
    original_state: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    source_state_path: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Validate and apply one manifest, returning a new state and audit record."""

    original = copy.deepcopy(_require_mapping(original_state, "research_state"))
    manifest_copy = copy.deepcopy(_require_mapping(manifest, "manifest"))
    if manifest_copy.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        _fail(
            "UNSUPPORTED_MANIFEST_SCHEMA",
            "manifest.schema_version",
            f"expected {MANIFEST_SCHEMA_VERSION!r}",
        )
    revision_id = _require_nonempty_string(manifest_copy.get("revision_id"), "manifest.revision_id")
    created_at = _require_utc_timestamp(
        manifest_copy.get("created_at"), "manifest.created_at"
    )
    revision_metadata = _revision_metadata(manifest_copy)
    manifest_digest = canonical_digest(manifest_copy, prefix="manifest_sha256")
    source_digest = canonical_digest(original)
    expected_source_digest = _require_nonempty_string(
        manifest_copy.get("source_state_digest"), "manifest.source_state_digest"
    )
    if expected_source_digest != source_digest:
        _fail(
            "SOURCE_STATE_DIGEST_MISMATCH",
            "manifest.source_state_digest",
            f"expected {expected_source_digest}, got {source_digest}",
        )
    prior_revisions = original.get("material_contract_revisions") or []
    if not isinstance(prior_revisions, list):
        _fail("INVALID_REVISION_HISTORY", "research_state.material_contract_revisions", "expected an array")
    if any(
        isinstance(record, Mapping) and record.get("revision_id") == revision_id
        for record in prior_revisions
    ):
        _fail("DUPLICATE_REVISION_ID", "manifest.revision_id", revision_id)

    raw_steps = _require_list(manifest_copy.get("steps"), "manifest.steps")
    if not raw_steps:
        _fail("EMPTY_REVISION", "manifest.steps", "at least one explicit step revision is required")

    revised = copy.deepcopy(original)
    prepared_steps: List[Dict[str, Any]] = []
    prepared_meta: List[Dict[str, Any]] = []
    manifest_paths: Dict[Tuple[str, Any], str] = {}
    seen_source_paths: set[str] = set()
    for index, raw_step_manifest in enumerate(raw_steps):
        manifest_path = f"manifest.steps[{index}]"
        step_manifest = _require_mapping(raw_step_manifest, manifest_path)
        source_step_path = _require_nonempty_string(
            step_manifest.get("source_step_path"), f"{manifest_path}.source_step_path"
        )
        if source_step_path in seen_source_paths:
            _fail("DUPLICATE_STEP_REVISION", f"{manifest_path}.source_step_path", source_step_path)
        seen_source_paths.add(source_step_path)
        source_step = _require_mapping(resolve_json_path(original, source_step_path), source_step_path)
        revised_step = resolve_json_path(revised, source_step_path)
        if not isinstance(revised_step, MutableMapping):
            _fail("INVALID_SOURCE_STEP", source_step_path, "step must be a mutable object")
        macro_step_id = _step_identity(
            source_step, step_manifest.get("macro_step_id"), manifest_path
        )
        macro_key = _identity_key(macro_step_id, f"{manifest_path}.macro_step_id")
        if macro_key in manifest_paths:
            _fail("DUPLICATE_MACRO_STEP_ID", f"{manifest_path}.macro_step_id", repr(macro_step_id))
        manifest_paths[macro_key] = manifest_path
        refs = _validate_source_refs(original, step_manifest, source_step_path, manifest_path)
        status, status_ref_ids = _validate_status(step_manifest, refs, manifest_path)

        step_contract: Dict[str, Any] = {"macro_step_id": macro_step_id}
        evidence_bindings: Dict[str, Any] = {
            "status_source_ref_ids": status_ref_ids,
            "collections": {},
        }
        for field in _CONTRACT_COLLECTIONS:
            contracts, bindings = _strip_evidence_metadata(
                step_manifest.get(field),
                refs,
                f"{manifest_path}.{field}",
                port_collection=field in _PORT_COLLECTIONS,
                revision_id=revision_id,
                manifest_digest=manifest_digest,
            )
            step_contract[field] = contracts
            evidence_bindings["collections"][field] = bindings
        for field in _STRUCTURAL_COLLECTIONS:
            contracts, bindings = _strip_evidence_metadata(
                step_manifest.get(field),
                refs,
                f"{manifest_path}.{field}",
                revision_id=revision_id,
                manifest_digest=manifest_digest,
            )
            step_contract[field] = contracts
            evidence_bindings["collections"][field] = bindings
        quantity_requirements_revised = "quantity_requirements" in step_manifest
        if quantity_requirements_revised:
            quantity_contracts, quantity_bindings = _strip_evidence_metadata(
                step_manifest.get("quantity_requirements"),
                refs,
                f"{manifest_path}.quantity_requirements",
                revision_id=revision_id,
                manifest_digest=manifest_digest,
            )
            declared_material_ids = {
                str(item.get("material_id") or "").strip()
                for field in _PORT_COLLECTIONS
                for item in step_contract[field]
                if str(item.get("material_id") or "").strip()
            }
            for requirement_index, requirement in enumerate(quantity_contracts):
                requirement_path = (
                    f"{manifest_path}.quantity_requirements[{requirement_index}]"
                )
                if requirement.get("source") != "manual_revision":
                    _fail(
                        "INVALID_MANUAL_QUANTITY_SOURCE",
                        f"{requirement_path}.source",
                        "a quantity introduced by a manual manifest must use "
                        "source=manual_revision",
                    )
                material_id = _require_nonempty_string(
                    requirement.get("material_id"),
                    f"{requirement_path}.material_id",
                )
                if material_id not in declared_material_ids:
                    _fail(
                        "UNBOUND_MANUAL_QUANTITY_MATERIAL",
                        f"{requirement_path}.material_id",
                        "manual quantity must bind a material_id declared by the same step",
                    )
            step_contract["quantity_requirements"] = quantity_contracts
            evidence_bindings["collections"]["quantity_requirements"] = (
                quantity_bindings
            )
        else:
            raw_source_requirements = source_step.get("quantity_requirements", [])
            step_contract["quantity_requirements"] = copy.deepcopy(
                raw_source_requirements
                if isinstance(raw_source_requirements, list)
                else raw_source_requirements
            )
        raw_step_provenance = _require_mapping(
            step_manifest.get("step_provenance"),
            f"{manifest_path}.step_provenance",
        )
        step_provenance_ref_ids = raw_step_provenance.pop("source_ref_ids", None)
        step_provenance_contracts, step_provenance_bindings = _strip_evidence_metadata(
            [
                {
                    "provenance": raw_step_provenance,
                    "source_ref_ids": step_provenance_ref_ids,
                }
            ],
            refs,
            f"{manifest_path}.step_provenance",
            revision_id=revision_id,
            manifest_digest=manifest_digest,
        )
        step_contract["provenance"] = step_provenance_contracts[0]["provenance"]
        evidence_bindings["step_provenance"] = step_provenance_bindings[0]
        step_contract["material_contract_status"] = status
        _validate_step_graph(step_contract, status, manifest_path)
        _validate_structural_material_contract(step_contract, status, manifest_path)
        prepared_steps.append(step_contract)
        prepared_meta.append(
            {
                "manifest_path": manifest_path,
                "source_step_path": source_step_path,
                "source_refs": list(refs.values()),
                "evidence_bindings": evidence_bindings,
                "source_step": source_step,
                "revised_step": revised_step,
                "quantity_requirements_revised": quantity_requirements_revised,
            }
        )

    # Cross-step references are validated after the patch against the complete
    # canonical Research package.  Validating only ``prepared_steps`` would
    # reject a legitimate partial revision whose parent remains unchanged, and
    # manifest order is not the scientific step order.

    field_diffs: List[Dict[str, Any]] = []
    for step_contract, meta in zip(prepared_steps, prepared_meta):
        revised_step = meta["revised_step"]
        patch_values = {
            "macro_step_id": step_contract["macro_step_id"],
            "material_inputs": step_contract["material_inputs"],
            "material_outputs": step_contract["material_outputs"],
            "material_intermediates": step_contract["material_intermediates"],
            "container_requirements": step_contract["logical_containers"],
            "material_relations": step_contract["material_relations"],
            "operation_segments": step_contract["operation_segments"],
            "material_applicability": step_contract["material_applicability"],
            "material_contract_status": step_contract["material_contract_status"],
            "provenance": step_contract["provenance"],
        }
        if meta["quantity_requirements_revised"]:
            patch_values["quantity_requirements"] = step_contract[
                "quantity_requirements"
            ]
        changed: Dict[str, Any] = {}
        for field, after in patch_values.items():
            before = copy.deepcopy(revised_step.get(field)) if field in revised_step else None
            if before != after:
                changed[field] = {
                    "before_present": field in revised_step,
                    "before": before,
                    "after": copy.deepcopy(after),
                }
                revised_step[field] = copy.deepcopy(after)
        field_diffs.append(
            {
                "source_step_path": meta["source_step_path"],
                "macro_step_id": step_contract["macro_step_id"],
                "changed_fields": changed,
                "source_refs": meta["source_refs"],
                "evidence_bindings": meta["evidence_bindings"],
            }
        )

    synchronized_views: List[Dict[str, Any]] = []
    touches_top_plan = any(path.startswith("macro_plan[") for path in seen_source_paths)
    touches_handoff_plan = any(
        path.startswith("device_adaptation_handoff.待执行 macro plan[")
        for path in seen_source_paths
    )
    original_handoff = original.get("device_adaptation_handoff")
    revised_handoff = revised.get("device_adaptation_handoff")
    original_top_plan = original.get("macro_plan")
    original_handoff_plan = (
        original_handoff.get("待执行 macro plan")
        if isinstance(original_handoff, Mapping)
        else None
    )
    if (
        (touches_top_plan or touches_handoff_plan)
        and isinstance(original_top_plan, list)
        and isinstance(original_handoff_plan, list)
    ):
        if original_top_plan != original_handoff_plan:
            _fail(
                "DIVERGENT_RESEARCH_PLAN_VIEWS",
                "research_state.device_adaptation_handoff.待执行 macro plan",
                "top-level and handoff macro plans differed before revision; refusing to "
                "silently choose one",
            )
        if not isinstance(revised_handoff, MutableMapping):
            _fail(
                "INVALID_DEVICE_HANDOFF",
                "research_state.device_adaptation_handoff",
                "expected an object while synchronizing mirrored macro plans",
            )
        revised_top_plan = revised.get("macro_plan")
        revised_handoff_plan = revised_handoff.get("待执行 macro plan")
        if touches_top_plan and touches_handoff_plan:
            if revised_top_plan != revised_handoff_plan:
                _fail(
                    "DIVERGENT_REVISED_PLAN_VIEWS",
                    "research_state.device_adaptation_handoff.待执行 macro plan",
                    "one revision may not produce conflicting mirrored macro plans",
                )
        elif touches_top_plan:
            revised_handoff["待执行 macro plan"] = copy.deepcopy(revised_top_plan)
            synchronized_views.append(
                {
                    "source": "macro_plan",
                    "target": "device_adaptation_handoff.待执行 macro plan",
                    "before_digest": canonical_digest(original_handoff_plan),
                    "after_digest": canonical_digest(revised_top_plan),
                }
            )
        else:
            revised["macro_plan"] = copy.deepcopy(revised_handoff_plan)
            synchronized_views.append(
                {
                    "source": "device_adaptation_handoff.待执行 macro plan",
                    "target": "macro_plan",
                    "before_digest": canonical_digest(original_top_plan),
                    "after_digest": canonical_digest(revised_handoff_plan),
                }
            )

    # ``ResearchAgentState.debug_snapshot`` persists the same plan through
    # four public views.  A revision must update every view atomically; Device
    # intentionally rejects a state where its generation plan and canonical
    # audit authority could come from different mirrors.  These legacy views
    # are data mirrors only -- synchronizing them does not infer any material
    # fact or broaden the manifest's authorized field edits.
    revised_top_plan = revised.get("macro_plan")
    auxiliary_views = (
        (
            "persistent_outputs",
            "persistent_outputs.待执行 macro plan",
        ),
        (
            "A. research layer 内部持久化输出",
            "A. research layer 内部持久化输出.待执行 macro plan",
        ),
        (
            "B. 发给下游 device adaptation layer agent 的外部交接输出",
            "B. 发给下游 device adaptation layer agent 的外部交接输出.待执行 macro plan",
        ),
    )
    if isinstance(revised_top_plan, list) and revised_top_plan:
        for view_key, plan_path in auxiliary_views:
            original_view = original.get(view_key)
            revised_view = revised.get(view_key)
            if original_view is None and revised_view is None:
                continue
            if not isinstance(original_view, Mapping) or not isinstance(
                revised_view, Mapping
            ):
                _fail(
                    "INVALID_RESEARCH_PLAN_MIRROR",
                    view_key,
                    "persisted Research plan mirror must be an object",
                )
            original_aux_plan = original_view.get("待执行 macro plan")
            if isinstance(original_aux_plan, list) and isinstance(
                original_top_plan, list
            ) and original_aux_plan != original_top_plan:
                _fail(
                    "DIVERGENT_RESEARCH_PLAN_VIEWS",
                    plan_path,
                    "persisted Research plan mirrors differed before revision; "
                    "refusing to silently choose one",
                )
            updated_view = copy.deepcopy(dict(revised_view))
            before_plan = updated_view.get("待执行 macro plan")
            updated_view["待执行 macro plan"] = copy.deepcopy(revised_top_plan)
            updated_view["contract_version"] = "v2"
            revised[view_key] = updated_view
            if before_plan != revised_top_plan:
                synchronized_views.append(
                    {
                        "source": "macro_plan",
                        "target": plan_path,
                        "before_digest": canonical_digest(before_plan),
                        "after_digest": canonical_digest(revised_top_plan),
                    }
                )

    stale_contract_digests: Dict[str, str] = {}
    if "research_action_package_v2" in revised:
        stale_contract_digests["research_action_package_v2"] = canonical_digest(
            revised["research_action_package_v2"]
        )
        revised.pop("research_action_package_v2")
    raw_handoff = revised.get("device_adaptation_handoff")
    if raw_handoff is not None and not isinstance(raw_handoff, Mapping):
        _fail(
            "INVALID_DEVICE_HANDOFF",
            "research_state.device_adaptation_handoff",
            "expected an object before synchronizing the revised V2 package",
        )
    handoff = copy.deepcopy(dict(raw_handoff or {}))
    if "research_action_package_v2" in handoff:
        stale_contract_digests[
            "device_adaptation_handoff.research_action_package_v2"
        ] = canonical_digest(handoff["research_action_package_v2"])
        handoff.pop("research_action_package_v2")
    for view_key, _ in auxiliary_views:
        raw_view = revised.get(view_key)
        if not isinstance(raw_view, Mapping):
            continue
        updated_view = copy.deepcopy(dict(raw_view))
        if "research_action_package_v2" in updated_view:
            stale_contract_digests[
                f"{view_key}.research_action_package_v2"
            ] = canonical_digest(updated_view["research_action_package_v2"])
            updated_view.pop("research_action_package_v2")
        revised[view_key] = updated_view
    # The revised document is a native V2 Research contract.  Set the version
    # before canonicalization so a later adapter rebuild produces the exact
    # same migration metadata and contract hash.
    revised["contract_version"] = "v2"
    canonical_package = research_state_to_v2(revised)
    canonical_payload = canonical_package.model_dump(
        mode="json", exclude_none=True
    )
    revised["research_action_package_v2"] = canonical_payload
    handoff["contract_version"] = "v2"
    handoff["research_action_package_v2"] = copy.deepcopy(canonical_payload)
    revised["device_adaptation_handoff"] = handoff
    for view_key, _ in auxiliary_views:
        raw_view = revised.get(view_key)
        if not isinstance(raw_view, Mapping):
            continue
        updated_view = copy.deepcopy(dict(raw_view))
        updated_view["contract_version"] = "v2"
        updated_view["research_action_package_v2"] = copy.deepcopy(
            canonical_payload
        )
        revised[view_key] = updated_view
    after_payload_digest = canonical_digest(revised)
    record: Dict[str, Any] = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "revision_id": revision_id,
        "created_at": created_at,
        **revision_metadata,
        "source_state_path": str(source_state_path),
        "source_state_digest": source_digest,
        "manifest_digest": manifest_digest,
        "before_state_digest": source_digest,
        "after_payload_digest": after_payload_digest,
        "research_contract_hash": canonical_package.research_contract_hash,
        "stale_embedded_contract_digests": stale_contract_digests,
        "policy": {
            "inferred_from_macro_number": False,
            "inferred_from_operation_category": False,
            "inferred_from_device_candidate": False,
            "runtime_measurement_claimed": False,
        },
        "field_diffs": field_diffs,
        "synchronized_views": synchronized_views,
    }
    revised.setdefault("material_contract_revisions", []).append(copy.deepcopy(record))
    record["output_state_digest"] = canonical_digest(revised)
    # Final readback uses the same adapter and model as the Device entry.  This
    # catches ordering bugs where a state mutation after canonicalization would
    # leave an internally valid-looking but stale research_contract_hash.
    rebuilt_final = research_state_to_v2(revised)
    if rebuilt_final.research_contract_hash != canonical_package.research_contract_hash:
        _fail(
            "STALE_RESEARCH_CONTRACT_HASH",
            "research_state.research_action_package_v2",
            "final persisted Research state no longer matches the canonical V2 package",
        )
    for package_path, embedded in (
        (
            "research_action_package_v2",
            revised.get("research_action_package_v2"),
        ),
        (
            "device_adaptation_handoff.research_action_package_v2",
            revised.get("device_adaptation_handoff", {}).get(
                "research_action_package_v2"
            ),
        ),
        *(
            (
                f"{view_key}.research_action_package_v2",
                revised.get(view_key, {}).get("research_action_package_v2"),
            )
            for view_key, _ in auxiliary_views
            if isinstance(revised.get(view_key), Mapping)
        ),
    ):
        try:
            parsed_embedded = type(canonical_package).model_validate(embedded)
        except (ValidationError, TypeError) as exc:
            _fail(
                "INVALID_EMBEDDED_RESEARCH_CONTRACT",
                package_path,
                str(exc),
            )
        if (
            parsed_embedded.research_contract_hash
            != canonical_package.research_contract_hash
        ):
            _fail(
                "DIVERGENT_RESEARCH_CONTRACT_VIEWS",
                package_path,
                "embedded canonical V2 package differs from the revised top-level package",
            )
    return revised, record


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise MaterialContractRevisionError("JSON_READ_FAILED", str(path), str(exc)) from exc
    return _require_mapping(value, str(path))


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _resolved_distinct(*paths: Path) -> None:
    resolved = [path.resolve() for path in paths]
    if len(resolved) != len(set(resolved)):
        _fail("OUTPUT_OVERLAPS_INPUT", "cli.paths", "input, manifest, and outputs must be distinct")


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply an evidence-bound material contract revision without mutating its source."
    )
    parser.add_argument("--input", required=True, type=Path, help="Frozen Research state JSON")
    parser.add_argument("--manifest", required=True, type=Path, help="Explicit revision manifest JSON")
    parser.add_argument("--output", type=Path, help="New Research state JSON")
    parser.add_argument("--revision-record", type=Path, help="Optional audit-record sidecar JSON")
    parser.add_argument("--dry-run", action="store_true", help="Validate and report without writing files")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_cli_parser().parse_args(argv)
    if not args.dry_run and args.output is None:
        raise SystemExit("--output is required unless --dry-run is used")
    original = _load_json(args.input)
    manifest = _load_json(args.manifest)
    revised, record = apply_material_contract_revision(
        original,
        manifest,
        source_state_path=str(args.input.resolve()),
    )
    if args.dry_run:
        print(
            json.dumps(
                {
                    "status": "valid",
                    "revision_id": record["revision_id"],
                    "revision_mode": record["revision_mode"],
                    "purpose": record["purpose"],
                    "automation_claim": record["automation_claim"],
                    "source_state_digest": record["source_state_digest"],
                    "manifest_digest": record["manifest_digest"],
                    "output_state_digest": record["output_state_digest"],
                    "research_contract_hash": record["research_contract_hash"],
                    "changed_steps": len(record["field_diffs"]),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    output_path = args.output
    assert output_path is not None
    paths = [args.input, args.manifest, output_path]
    if args.revision_record is not None:
        paths.append(args.revision_record)
    _resolved_distinct(*paths)
    _atomic_write_json(output_path, revised)
    if args.revision_record is not None:
        _atomic_write_json(args.revision_record, record)
    print(
        json.dumps(
            {
                "status": "written",
                "output": str(output_path.resolve()),
                "revision_mode": record["revision_mode"],
                "purpose": record["purpose"],
                "automation_claim": record["automation_claim"],
                "revision_record": (
                    str(args.revision_record.resolve())
                    if args.revision_record is not None
                    else "embedded_only"
                ),
                "output_state_digest": record["output_state_digest"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
