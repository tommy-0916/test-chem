"""Strongly typed Chem Agent V2 interchange contracts.

The models deliberately describe scientific intent separately from machine
binding.  Research owns the former.  Device may expand it into more steps, but
the immutable Research contract digest makes silent scientific edits visible.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any, Dict, List, Literal, Mapping, Optional, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .identity import (
    decode_package_identity,
    json_scalar_identity_key,
)


CONTRACT_VERSION_V2 = "2.0"
RAW_STEP_DIGEST_SCOPE_V1 = "full_raw_step_excluding_digest_fields_v1"
RAW_OBSERVATIONS_DIGEST_SCOPE_V1 = "full_raw_observations_v1"


def canonical_digest(value: Any, *, prefix: str = "sha256") -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()}"


def canonical_raw_step_digest(raw_step: Mapping[str, Any]) -> str:
    """Bind every raw Research step field except the two derived digest keys."""

    payload = dict(raw_step)
    payload.pop("raw_step_digest_scope", None)
    payload.pop("raw_step_sha256", None)
    return canonical_digest(payload)


def canonical_raw_observations_digest(raw_observations: Any) -> str:
    """Bind the complete ordered observation list consumed by Device."""

    if not isinstance(raw_observations, list):
        raise ValueError("raw V2 observations must be an array")
    return canonical_digest(raw_observations)


def evidence_contains_exact_quantity(
    excerpt: str,
    value: Any,
    unit: str,
) -> bool:
    """Check, without deriving data, that evidence states this value/unit pair."""

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not unit.strip()
    ):
        return False
    normalized_excerpt = (
        str(excerpt).replace("−", "-").replace("–", "-").replace("µ", "u")
    )
    normalized_unit = unit.strip().replace("µ", "u")
    unit_pattern = r"\s*".join(
        re.escape(piece) for piece in re.split(r"\s+", normalized_unit)
    )
    pattern = re.compile(
        rf"(?<![\w.])([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
        rf"\s*{unit_pattern}(?![\w/])",
        re.IGNORECASE,
    )
    return any(
        math.isclose(float(match.group(1)), float(value), rel_tol=1e-12, abs_tol=1e-12)
        for match in pattern.finditer(normalized_excerpt)
    )


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ProvenanceV2(StrictModel):
    kind: Literal[
        "user",
        "paper",
        "agent_inferred",
        "manual_revision",
        "runtime",
        "device_skill",
    ]
    reference: str = ""
    rationale: str = ""
    # Optional wire fields used by the Research publication gate to bind a
    # claim to the current immutable input.  The schema deliberately cannot
    # decide whether a path/excerpt is truthful; the gate checks it against the
    # current task/evidence bundle.
    source_path: str = ""
    excerpt: str = ""
    source_digest: str = ""
    revision_id: str = ""
    manifest_digest: str = ""
    automation_claim: Optional[bool] = None

    @model_validator(mode="after")
    def require_inference_rationale(self) -> "ProvenanceV2":
        if self.kind == "agent_inferred" and not self.rationale.strip():
            raise ValueError("agent_inferred provenance requires a rationale")
        if self.kind == "manual_revision":
            if not self.rationale.strip():
                raise ValueError("manual_revision provenance requires a rationale")
            if not self.revision_id.strip() or not self.manifest_digest.startswith(
                "manifest_sha256_"
            ):
                raise ValueError(
                    "manual_revision provenance requires revision_id and manifest digest"
                )
            if self.automation_claim is not False:
                raise ValueError("manual_revision provenance must set automation_claim=false")
            if not (
                self.source_path.strip()
                and self.excerpt.strip()
                and self.source_digest.startswith("sha256_")
            ):
                raise ValueError(
                    "manual_revision provenance requires source_path, excerpt, and source_digest"
                )
        return self


class QuantityV2(StrictModel):
    mode: Literal["exact", "all_available", "runtime_measured"] = "exact"
    # Research quantities are intentions/requirements, never proof of available
    # inventory, executed consumption, produced yield, or a measured result.
    # ``semantic`` is optional only for historical packages; newly declared
    # material contracts require the mode-specific value in their quality gate.
    semantic: Optional[Literal[
        "planned_target",
        "planning_estimate",
        "runtime_measurement_required",
        "whole_batch_unspecified",
    ]] = None
    value: Optional[float] = None
    unit: str = ""

    @model_validator(mode="after")
    def validate_quantity(self) -> "QuantityV2":
        if self.mode == "exact":
            if self.value is None or not self.unit.strip():
                raise ValueError("exact quantity requires value and unit")
            if not math.isfinite(self.value):
                raise ValueError("quantity value must be finite")
            if self.value < 0:
                raise ValueError("quantity value cannot be negative")
            if self.semantic not in {None, "planned_target", "planning_estimate"}:
                raise ValueError(
                    "exact quantity semantic must be planned_target or planning_estimate"
                )
        else:
            if self.value is not None or self.unit.strip():
                raise ValueError(
                    "all_available/runtime_measured cannot carry value or unit"
                )
            expected = (
                "whole_batch_unspecified"
                if self.mode == "all_available"
                else "runtime_measurement_required"
            )
            if self.semantic not in {None, expected}:
                raise ValueError(f"{self.mode} quantity semantic must be {expected}")
        return self


MaterialContractDispositionV2 = Literal[
    "declared", "not_applicable", "unresolved"
]
MaterialEventKindV2 = Literal[
    "none",
    "state_change",
    "process_same_material",
    "split_same_material",
    "replicate_same_material",
]
MaterialRelationQuantityBasisV2 = Literal[
    "whole_batch",
    "conserved_inventory",
    "runtime_measurement_required",
    "planning_yield_lower_bound",
]

MaterialContractFieldV2 = Literal[
    "material_inputs",
    "material_intermediates",
    "material_outputs",
    "logical_containers",
    "material_relations",
]
MaterialEffectV2 = Literal[
    "none",
    "register_existing_input",
    "observe_without_material_change",
    "consume_material",
    "produce_material",
    "transform_material",
    "transfer_material",
    "split_material",
    "merge_material",
    "unknown",
]


class MaterialOperationSegmentV2(StrictModel):
    """Evidence-bound operation segment; never inferred from operation prose."""

    segment_id: str = Field(min_length=1)
    material_effect: MaterialEffectV2
    source_operation_ref: str = Field(min_length=1)
    provenance: ProvenanceV2


class MaterialApplicabilityEvidenceV2(StrictModel):
    """Structured evidence authorising one explicit ``not_applicable`` value."""

    contract_field: MaterialContractFieldV2
    assertion: Literal[
        "no_material_inputs",
        "no_material_intermediates",
        "no_material_outputs",
        "no_logical_containers",
        "no_material_relations",
    ]
    operation_segment_ids: List[str] = Field(min_length=1)
    provenance: ProvenanceV2

    @model_validator(mode="after")
    def validate_assertion(self) -> "MaterialApplicabilityEvidenceV2":
        expected = f"no_{self.contract_field}"
        if self.assertion != expected:
            raise ValueError(
                f"{self.contract_field}=not_applicable requires assertion={expected}"
            )
        if len(set(self.operation_segment_ids)) != len(self.operation_segment_ids):
            raise ValueError("operation_segment_ids must not contain duplicates")
        if self.provenance.kind not in {"user", "paper", "manual_revision"}:
            raise ValueError(
                "not_applicable requires user, paper, or evidence-bound manual revision; "
                "unsupported inference "
                "must remain unresolved"
            )
        return self


class MaterialOutputRefV2(StrictModel):
    """Exact upstream output instance consumed by a later material port."""

    macro_step_id: str = Field(min_length=1)
    material_instance_id: str = Field(min_length=1)


class MaterialPortV2(StrictModel):
    material_id: str = Field(min_length=1)
    # ``material_id`` names the material identity; ``material_instance_id``
    # names one concrete batch/state instance.  The latter is optional only so
    # historical V2 packages remain readable.  A newly declared material
    # contract requires it at MacroStepV2 validation time.
    material_instance_id: Optional[str] = Field(default=None, min_length=1)
    name: str = Field(min_length=1)
    state: str = "unknown"
    quantity: Optional[QuantityV2] = None
    concentration_value: Optional[float] = Field(default=None, allow_inf_nan=False)
    concentration_unit: str = ""
    material_origin: Optional[
        Literal["external_inventory", "upstream_output", "same_step_relation"]
    ] = None
    parent_output_refs: List[MaterialOutputRefV2] = Field(default_factory=list)
    logical_container_id: Optional[str] = Field(default=None, min_length=1)
    provenance: ProvenanceV2

    @model_validator(mode="after")
    def concentration_has_unit(self) -> "MaterialPortV2":
        if self.concentration_value is not None and not self.concentration_unit.strip():
            raise ValueError("concentration value requires concentration_unit")
        if self.material_origin == "upstream_output" and not self.parent_output_refs:
            raise ValueError("upstream_output material requires parent_output_refs")
        if self.material_origin == "external_inventory" and self.parent_output_refs:
            raise ValueError("external_inventory material cannot have parent_output_refs")
        if self.material_origin == "same_step_relation" and self.parent_output_refs:
            raise ValueError("same_step_relation material cannot have parent_output_refs")
        if self.parent_output_refs and self.material_origin != "upstream_output":
            raise ValueError("parent_output_refs require material_origin=upstream_output")
        parent_keys = [
            (ref.macro_step_id, ref.material_instance_id)
            for ref in self.parent_output_refs
        ]
        if len(parent_keys) != len(set(parent_keys)):
            raise ValueError("parent_output_refs must not contain duplicate output references")
        return self


class MaterialAllocationV2(StrictModel):
    material_instance_id: str = Field(min_length=1)
    quantity: QuantityV2

    @model_validator(mode="after")
    def require_planning_quantity(self) -> "MaterialAllocationV2":
        if self.quantity.mode != "exact" or self.quantity.semantic not in {
            "planned_target", "planning_estimate"
        }:
            raise ValueError(
                "Research allocation quantity must be an exact planned target or estimate"
            )
        return self


class MaterialRelationV2(StrictModel):
    """Research-authorized material relation; never inferred from operation names."""

    relation_id: str = Field(min_length=1)
    event_kind: MaterialEventKindV2
    input_material_instance_ids: List[str] = Field(default_factory=list)
    output_material_instance_ids: List[str] = Field(default_factory=list)
    logical_container_ids: List[str] = Field(default_factory=list)
    quantity_basis: Optional[MaterialRelationQuantityBasisV2] = None
    planning_quantity: Optional[QuantityV2] = None
    input_allocations: List[MaterialAllocationV2] = Field(default_factory=list)
    output_allocations: List[MaterialAllocationV2] = Field(default_factory=list)
    source_operation_ref: str = Field(min_length=1)
    provenance: ProvenanceV2

    @model_validator(mode="after")
    def validate_relation_shape(self) -> "MaterialRelationV2":
        for field_name in (
            "input_material_instance_ids",
            "output_material_instance_ids",
            "logical_container_ids",
        ):
            values = getattr(self, field_name)
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"{field_name} must contain nonempty string IDs")
            if len(set(values)) != len(values):
                raise ValueError(f"{field_name} must not contain duplicate IDs")
        if self.event_kind == "none":
            if self.input_material_instance_ids or self.output_material_instance_ids:
                raise ValueError("event_kind=none cannot declare material edges")
            if (
                self.quantity_basis is not None
                or self.planning_quantity is not None
                or self.input_allocations
                or self.output_allocations
            ):
                raise ValueError("event_kind=none cannot declare quantity information")
            return self
        if not self.input_material_instance_ids or not self.output_material_instance_ids:
            raise ValueError("material relation requires input and output instance IDs")
        if self.quantity_basis is None:
            raise ValueError("material relation requires an explicit quantity_basis")
        if self.quantity_basis == "whole_batch" and (
            len(self.input_material_instance_ids) != 1
            or len(self.output_material_instance_ids) != 1
        ):
            raise ValueError("whole_batch relation must be exactly 1-to-1")
        if self.quantity_basis == "planning_yield_lower_bound":
            if (
                self.planning_quantity is None
                or self.planning_quantity.mode != "exact"
                or self.planning_quantity.semantic != "planning_estimate"
            ):
                raise ValueError(
                    "planning_yield_lower_bound requires an exact planning_estimate"
                )
            if not self.provenance.reference.strip():
                raise ValueError(
                    "planning_yield_lower_bound requires a provenance reference"
                )
        elif self.planning_quantity is not None:
            raise ValueError(
                "planning_quantity is only valid for planning_yield_lower_bound"
            )

        for field_name, allocations, expected_ids in (
            ("input_allocations", self.input_allocations, self.input_material_instance_ids),
            ("output_allocations", self.output_allocations, self.output_material_instance_ids),
        ):
            allocation_ids = [item.material_instance_id for item in allocations]
            if len(set(allocation_ids)) != len(allocation_ids):
                raise ValueError(f"{field_name} contains duplicate material instances")
            if allocations and set(allocation_ids) != set(expected_ids):
                raise ValueError(
                    f"{field_name} must cover exactly the relation's material instances"
                )
        branching_or_merge = (
            self.event_kind in {"split_same_material", "replicate_same_material"}
            or len(self.input_material_instance_ids) > 1
            or len(self.output_material_instance_ids) > 1
        )
        if branching_or_merge and self.quantity_basis != "runtime_measurement_required":
            if not self.input_allocations or not self.output_allocations:
                raise ValueError(
                    "split/replicate/multi-instance relation requires explicit input "
                    "and output allocations or runtime_measurement_required"
                )
        if self.quantity_basis == "conserved_inventory":
            if not self.input_allocations or not self.output_allocations:
                raise ValueError(
                    "conserved_inventory requires explicit input and output allocations"
                )
            input_units = {item.quantity.unit for item in self.input_allocations}
            output_units = {item.quantity.unit for item in self.output_allocations}
            if len(input_units) != 1 or input_units != output_units:
                raise ValueError(
                    "conserved_inventory allocations require one matching unit"
                )
            input_total = math.fsum(
                item.quantity.value or 0.0 for item in self.input_allocations
            )
            output_total = math.fsum(
                item.quantity.value or 0.0 for item in self.output_allocations
            )
            if not math.isclose(input_total, output_total, rel_tol=1e-9, abs_tol=1e-12):
                raise ValueError(
                    "conserved_inventory input and output allocation totals must match"
                )
        return self


class MaterialContractStatusV2(StrictModel):
    """Completeness/applicability, separate from the material records themselves."""

    material_inputs: MaterialContractDispositionV2
    material_intermediates: MaterialContractDispositionV2
    material_outputs: MaterialContractDispositionV2
    logical_containers: MaterialContractDispositionV2
    material_relations: MaterialContractDispositionV2


class MaterialContractMigrationV2(StrictModel):
    """Trace of how a Research macro step reached the canonical V2 shape."""

    source_contract_version: Literal["v1", "v2"]
    migration_mode: Literal["legacy_v1_compatibility", "native_v2"]
    source_path: str = Field(min_length=1)
    diagnostics: List[str] = Field(default_factory=list)


class ScientificParameterV2(StrictModel):
    name: str = Field(min_length=1)
    value: Any
    unit: str = ""
    provenance: ProvenanceV2


LogicalLidStateV2 = Literal["open", "closed", "none", "unknown"]
LOGICAL_LID_STATE_DESCRIPTIONS = {
    "open": "有盖结构的容器当前要求开盖/无盖",
    "closed": "有盖结构的容器当前要求关盖/有盖",
    "none": "容器结构不适用盖子概念（例如无盖结构的 XRD 基底片），不是未知盖状态",
    "unknown": "尚未确定盖状态，不表示已开盖",
}
LOGICAL_LID_STATE_PROMPT = (
    "lid_state 只能为 " + ", ".join(get_args(LogicalLidStateV2)) + "；"
    + "；".join(f"{value}：{LOGICAL_LID_STATE_DESCRIPTIONS[value]}"
               for value in get_args(LogicalLidStateV2))
    + "。不得输出 not_applicable、null 或其他别名。none/unknown 都不能替代真实进样瓶等的开盖要求。"
)


class LogicalContainerV2(StrictModel):
    logical_container_id: str = Field(min_length=1)
    container_type: str = "unknown"
    count: int = Field(default=1, ge=1)
    capacity_ml: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    lid_state: LogicalLidStateV2 = Field(
        default="unknown", description=LOGICAL_LID_STATE_PROMPT
    )


class EvidenceItemV2(StrictModel):
    evidence_id: str = Field(min_length=1)
    title: str = ""
    doi: str = ""
    arxiv_id: str = ""
    url: str = ""
    verification_status: str = "unknown"
    full_text_status: str = "unknown"
    excerpt: str = ""


class EvidenceBundleV2(StrictModel):
    bundle_id: str = Field(min_length=1)
    scope: Literal["stage", "macro_action"]
    query: str
    objective: str = ""
    retrieval_status: str = "unknown"
    current_invocation_only: bool = True
    items: List[EvidenceItemV2] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_unique_evidence_ids(self) -> "EvidenceBundleV2":
        evidence_ids = [item.evidence_id for item in self.items]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence_bundle.items evidence_id values must be unique")
        return self


class StageV2(StrictModel):
    stage_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    observation_point: str = Field(min_length=1)
    completion_condition: str = Field(min_length=1)
    capability_requirements: List[str] = Field(default_factory=list)


class ExperimentGroupV2(StrictModel):
    group_id: str = Field(min_length=1)
    role: Literal["experimental", "control", "repeat", "calibration"] = "experimental"
    sample_id: str = Field(min_length=1)
    hypothesis: str = ""
    comparison_to: List[str] = Field(default_factory=list)
    variables: Dict[str, Any] = Field(default_factory=dict)


class MacroActionV2(StrictModel):
    macro_action_id: str = Field(min_length=1)
    stage_id: str = Field(min_length=1)
    # Optional only for reading historical V2 packages.  New adapters always
    # bind this identity so Device does not inherit it from an untrusted raw
    # handoff mirror.
    observation_point_id: str = ""
    experiment_group: ExperimentGroupV2
    objective: str = Field(min_length=1)
    planned_operations: List[str] = Field(min_length=1)
    expected_observation: str = Field(min_length=1)
    completion_condition: str = Field(min_length=1)


class MacroStepV2(StrictModel):
    macro_step_id: str = Field(min_length=1)
    macro_action_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    operation: str = Field(min_length=1)
    # These fields are consumed directly by Device semantic planning.  They
    # must therefore cross the canonical package boundary and be hash-bound;
    # retaining them only in the surrounding raw Research state would permit
    # a semantically different Device input under the same contract hash.
    reagent_or_object: str = ""
    quantity_requirements: List[Dict[str, Any]] = Field(default_factory=list)
    raw_step_digest_scope: Optional[
        Literal["full_raw_step_excluding_digest_fields_v1"]
    ] = None
    raw_step_sha256: str = ""
    sample_id: str = Field(min_length=1)
    material_inputs: List[MaterialPortV2] = Field(default_factory=list)
    material_intermediates: List[MaterialPortV2] = Field(default_factory=list)
    material_outputs: List[MaterialPortV2] = Field(default_factory=list)
    parameters: List[ScientificParameterV2] = Field(default_factory=list)
    logical_containers: List[LogicalContainerV2] = Field(default_factory=list)
    material_relations: List[MaterialRelationV2] = Field(default_factory=list)
    operation_segments: List[MaterialOperationSegmentV2] = Field(default_factory=list)
    material_applicability: List[MaterialApplicabilityEvidenceV2] = Field(
        default_factory=list
    )
    material_contract_status: Optional[MaterialContractStatusV2] = None
    material_contract_migration: Optional[MaterialContractMigrationV2] = None
    expected_return: List[Dict[str, Any]] = Field(default_factory=list)
    provenance: ProvenanceV2

    @model_validator(mode="after")
    def validate_material_contract(self) -> "MacroStepV2":
        if (self.raw_step_digest_scope is None) != (not self.raw_step_sha256):
            raise ValueError(
                "raw_step_digest_scope and raw_step_sha256 must be declared together"
            )
        if self.raw_step_sha256 and re.fullmatch(
            r"sha256_[0-9a-f]{64}", self.raw_step_sha256
        ) is None:
            raise ValueError("raw_step_sha256 must be a canonical sha256 digest")
        # Absence is retained solely for reading historical V2 packages.  New
        # adapters always write an explicit status, and consumers must treat a
        # missing status exactly like ``unresolved``.
        status = self.material_contract_status
        if status is None:
            return self
        segment_ids = [segment.segment_id for segment in self.operation_segments]
        if len(segment_ids) != len(set(segment_ids)):
            raise ValueError("segment_id values must be unique")
        resolved_dispositions = {
            getattr(status, field_name)
            for field_name in (
                "material_inputs",
                "material_intermediates",
                "material_outputs",
                "logical_containers",
                "material_relations",
            )
        }
        if not self.operation_segments and resolved_dispositions != {"unresolved"}:
            raise ValueError(
                "explicit material contract status requires evidence-bound operation_segments"
            )

        applicability_by_field: dict[str, MaterialApplicabilityEvidenceV2] = {}
        for evidence in self.material_applicability:
            if evidence.contract_field in applicability_by_field:
                raise ValueError(
                    f"duplicate not_applicable evidence for {evidence.contract_field}"
                )
            unknown_segments = set(evidence.operation_segment_ids) - set(segment_ids)
            if unknown_segments:
                raise ValueError(
                    "material applicability references unknown operation segments: "
                    + ", ".join(sorted(unknown_segments))
                )
            if set(evidence.operation_segment_ids) != set(segment_ids):
                raise ValueError(
                    f"{evidence.contract_field}=not_applicable evidence must cover "
                    "every operation segment in the macro step"
                )
            applicability_by_field[evidence.contract_field] = evidence

        not_applicable_fields = {
            field_name
            for field_name in (
                "material_inputs",
                "material_intermediates",
                "material_outputs",
                "logical_containers",
                "material_relations",
            )
            if getattr(status, field_name) == "not_applicable"
        }
        if set(applicability_by_field) != not_applicable_fields:
            raise ValueError(
                "material_applicability must contain exactly one evidence record for "
                "each not_applicable contract field"
            )

        segment_effects = {
            segment.material_effect for segment in self.operation_segments
        }
        if "unknown" in segment_effects and not_applicable_fields:
            raise ValueError(
                "unknown material effect cannot authorise any not_applicable dimension"
            )
        if status.material_inputs == "not_applicable" and segment_effects & {
            "register_existing_input",
            "consume_material",
            "transform_material",
            "transfer_material",
            "split_material",
            "merge_material",
        }:
            raise ValueError(
                "material_inputs=not_applicable conflicts with an input-bearing segment"
            )
        if status.material_outputs == "not_applicable" and segment_effects & {
            "produce_material",
            "transform_material",
            "transfer_material",
            "split_material",
            "merge_material",
        }:
            raise ValueError(
                "material_outputs=not_applicable conflicts with an output-bearing segment"
            )

        relation_evidence = applicability_by_field.get("material_relations")
        if relation_evidence is not None:
            segments_by_id = {
                segment.segment_id: segment
                for segment in self.operation_segments
            }
            prohibited = sorted(
                segment_id
                for segment_id in relation_evidence.operation_segment_ids
                if segments_by_id[segment_id].material_effect
                not in {
                    "none",
                    "register_existing_input",
                    "observe_without_material_change",
                }
            )
            if prohibited:
                raise ValueError(
                    "material_relations=not_applicable conflicts with material-changing "
                    "operation segments: " + ", ".join(prohibited)
                )

        if any(
            segment.material_effect == "register_existing_input"
            for segment in self.operation_segments
        ) and status.material_inputs != "declared":
            raise ValueError(
                "a register_existing_input segment must "
                "declare their material input instances; relation inapplicability "
                "does not make the inventory boundary inapplicable"
            )
        collections = {
            "material_inputs": self.material_inputs,
            "material_intermediates": self.material_intermediates,
            "material_outputs": self.material_outputs,
            "logical_containers": self.logical_containers,
            "material_relations": self.material_relations,
        }
        for field_name, values in collections.items():
            disposition = getattr(status, field_name)
            if disposition == "declared" and not values:
                raise ValueError(
                    f"{field_name}=declared requires a nonempty explicit collection"
                )
            if disposition == "not_applicable" and values:
                raise ValueError(
                    f"{field_name}=not_applicable requires an empty collection"
                )

        declared_ports = []
        if status.material_inputs == "declared":
            declared_ports.extend(("input", port) for port in self.material_inputs)
        if status.material_intermediates == "declared":
            declared_ports.extend(
                ("intermediate", port) for port in self.material_intermediates
            )
        if status.material_outputs == "declared":
            declared_ports.extend(("output", port) for port in self.material_outputs)
        authoritative_material_kinds = {"user", "paper", "manual_revision"}
        declared_materials = {
            port.material_id: port.name
            for port in (
                self.material_inputs
                + self.material_intermediates
                + self.material_outputs
            )
        }
        allowed_quantity_kinds = {
            "scientific_input_setpoint",
            "target_dose",
            "whole_batch",
            "runtime_measured_inventory",
            "semantic_classification_required",
        }
        allowed_quantity_sources = {
            "user_query": "user",
            "literature": "paper",
            "process_semantics": None,
            "manual_revision": "manual_revision",
        }
        for requirement_index, requirement in enumerate(self.quantity_requirements):
            kind = str(requirement.get("kind") or "").strip()
            if kind not in allowed_quantity_kinds:
                raise ValueError(
                    f"quantity_requirements[{requirement_index}] has an invalid kind"
                )
            material_id = str(requirement.get("material_id") or "").strip()
            if not material_id or material_id not in declared_materials:
                raise ValueError(
                    f"quantity_requirements[{requirement_index}] must bind one "
                    "declared material_id"
                )
            material_name = str(requirement.get("material") or "").strip()
            if (
                not material_name
                or material_name.casefold()
                != declared_materials[material_id].strip().casefold()
            ):
                raise ValueError(
                    f"quantity_requirements[{requirement_index}] material must "
                    "exactly identify the bound material port"
                )
            raw_provenance = requirement.get("provenance")
            try:
                quantity_provenance = ProvenanceV2.model_validate(
                    raw_provenance, strict=True
                )
            except Exception as exc:
                raise ValueError(
                    f"quantity_requirements[{requirement_index}] requires "
                    "structured provenance"
                ) from exc
            if quantity_provenance.kind not in authoritative_material_kinds:
                raise ValueError(
                    f"quantity_requirements[{requirement_index}] must use user, "
                    "paper, or evidence-bound manual_revision provenance"
                )
            source = str(requirement.get("source") or "").strip()
            expected_kind = allowed_quantity_sources.get(source, "invalid")
            if expected_kind == "invalid":
                raise ValueError(
                    f"quantity_requirements[{requirement_index}] has an "
                    "unsupported Research source"
                )
            if expected_kind is not None and quantity_provenance.kind != expected_kind:
                raise ValueError(
                    f"quantity_requirements[{requirement_index}] source/provenance "
                    "kinds do not agree"
                )
            if source == "process_semantics" and quantity_provenance.kind not in {
                "user",
                "paper",
                "manual_revision",
            }:
                raise ValueError(
                    f"quantity_requirements[{requirement_index}] process semantics "
                    "lack authoritative evidence"
                )
            value = requirement.get("value")
            unit = str(requirement.get("unit") or "").strip()
            if kind in {"whole_batch", "runtime_measured_inventory"}:
                if value is not None or unit:
                    raise ValueError(
                        f"quantity_requirements[{requirement_index}] {kind} cannot "
                        "carry value or unit"
                    )
            elif (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not unit
            ):
                raise ValueError(
                    f"quantity_requirements[{requirement_index}] requires a finite "
                    "value and unit"
                )
        for direction, port in declared_ports:
            if port.provenance.kind not in authoritative_material_kinds:
                raise ValueError(
                    f"declared {direction} material provenance must be user, paper, "
                    "or evidence-bound manual_revision; unsupported inference must "
                    "remain unresolved"
                )
        if status.material_relations == "declared":
            for relation in self.material_relations:
                if relation.provenance.kind not in authoritative_material_kinds:
                    raise ValueError(
                        "declared material relation provenance must be user, paper, "
                        "or evidence-bound manual_revision"
                    )
        if resolved_dispositions != {"unresolved"}:
            for segment in self.operation_segments:
                if segment.provenance.kind not in authoritative_material_kinds:
                    raise ValueError(
                        "operation segment material semantics must be user, paper, "
                        "or evidence-bound manual_revision"
                    )
        for direction, port in declared_ports:
            if not port.material_instance_id:
                raise ValueError(
                    f"declared {direction} material requires material_instance_id"
                )
            if direction == "input" and port.material_origin is None:
                raise ValueError(
                    "declared input material requires explicit material_origin"
                )
            if direction == "intermediate" and port.material_origin != "same_step_relation":
                raise ValueError(
                    "declared intermediate requires material_origin=same_step_relation"
                )
            if port.quantity is None:
                raise ValueError(
                    f"declared {direction} material requires explicit quantity semantics"
                )
            if port.quantity.semantic is None:
                raise ValueError(
                    f"declared {direction} material quantity requires an explicit semantic"
                )

        logical_ids = {
            container.logical_container_id for container in self.logical_containers
        }
        if status.logical_containers == "declared":
            for _, port in declared_ports:
                if not port.logical_container_id:
                    raise ValueError(
                        "declared material port requires logical_container_id when "
                        "logical_containers are declared"
                    )
                if port.logical_container_id not in logical_ids:
                    raise ValueError(
                        "material port references an unknown logical_container_id"
                    )

        input_ids = {
            port.material_instance_id
            for port in self.material_inputs
            if port.material_instance_id
        }
        output_ids = {
            port.material_instance_id
            for port in self.material_outputs
            if port.material_instance_id
        }
        intermediate_ids = {
            port.material_instance_id
            for port in self.material_intermediates
            if port.material_instance_id
        }
        if status.material_inputs == "declared" and len(input_ids) != len(self.material_inputs):
            raise ValueError("declared input material_instance_id values must be unique")
        if status.material_outputs == "declared" and len(output_ids) != len(self.material_outputs):
            raise ValueError("declared output material_instance_id values must be unique")
        if (
            status.material_intermediates == "declared"
            and len(intermediate_ids) != len(self.material_intermediates)
        ):
            raise ValueError("declared intermediate material_instance_id values must be unique")
        if (input_ids & intermediate_ids) or (input_ids & output_ids) or (intermediate_ids & output_ids):
            raise ValueError(
                "input, intermediate, and output material instance IDs must be disjoint"
            )
        if status.material_relations == "declared":
            relation_ids = [relation.relation_id for relation in self.material_relations]
            if len(set(relation_ids)) != len(relation_ids):
                raise ValueError("declared material relation IDs must be unique")
            active_segment_ids = {
                segment.segment_id
                for segment in self.operation_segments
                if segment.material_effect
                not in {
                    "none",
                    "register_existing_input",
                    "observe_without_material_change",
                }
            }
            relation_segment_ids = {
                relation.source_operation_ref
                for relation in self.material_relations
                if relation.event_kind != "none"
            }
            unknown_relation_segments = relation_segment_ids - set(segment_ids)
            if unknown_relation_segments:
                raise ValueError(
                    "material relations reference unknown operation segments: "
                    + ", ".join(sorted(unknown_relation_segments))
                )
            uncovered_segments = active_segment_ids - relation_segment_ids
            if uncovered_segments:
                raise ValueError(
                    "material-changing operation segments require explicit relation edges: "
                    + ", ".join(sorted(uncovered_segments))
                )
            used_inputs: set[str] = set()
            produced_outputs: set[str] = set()
            input_use_counts = {instance_id: 0 for instance_id in input_ids}
            output_production_counts = {instance_id: 0 for instance_id in output_ids}
            input_ports_by_id = {
                port.material_instance_id: port
                for port in [*self.material_inputs, *self.material_intermediates]
                if port.material_instance_id
            }
            output_ports_by_id = {
                port.material_instance_id: port
                for port in [*self.material_intermediates, *self.material_outputs]
                if port.material_instance_id
            }
            for relation in self.material_relations:
                unknown_inputs = set(relation.input_material_instance_ids) - (
                    input_ids | intermediate_ids
                )
                unknown_outputs = set(relation.output_material_instance_ids) - (
                    intermediate_ids | output_ids
                )
                unknown_containers = set(relation.logical_container_ids) - logical_ids
                if unknown_inputs:
                    raise ValueError(
                        "material relation references unknown input instances: "
                        + ", ".join(sorted(unknown_inputs))
                    )
                if unknown_outputs:
                    raise ValueError(
                        "material relation references unknown output instances: "
                        + ", ".join(sorted(unknown_outputs))
                    )
                if unknown_containers:
                    raise ValueError(
                        "material relation references unknown logical containers: "
                        + ", ".join(sorted(unknown_containers))
                    )
                used_inputs.update(
                    set(relation.input_material_instance_ids) & input_ids
                )
                produced_outputs.update(
                    set(relation.output_material_instance_ids) & output_ids
                )
                for instance_id in set(relation.input_material_instance_ids) & input_ids:
                    input_use_counts[instance_id] += 1
                for instance_id in set(relation.output_material_instance_ids) & output_ids:
                    output_production_counts[instance_id] += 1
                if relation.event_kind in {
                    "process_same_material",
                    "split_same_material",
                    "replicate_same_material",
                }:
                    material_ids = {
                        port.material_id
                        for instance_id in relation.input_material_instance_ids
                        for port in [input_ports_by_id[instance_id]]
                    } | {
                        port.material_id
                        for instance_id in relation.output_material_instance_ids
                        for port in [output_ports_by_id[instance_id]]
                    }
                    if len(material_ids) != 1:
                        raise ValueError(
                            f"{relation.event_kind} must preserve one material_id; "
                            "use state_change for a material identity change"
                        )
            if used_inputs != input_ids:
                raise ValueError(
                    "every declared input material must be consumed by a material relation"
                )
            if produced_outputs != output_ids:
                raise ValueError(
                    "every declared output material must be produced by a material relation"
                )
            if any(count != 1 for count in input_use_counts.values()):
                raise ValueError(
                    "each declared input material instance must be consumed exactly once; "
                    "use one explicit split relation for branching"
                )
            if any(count != 1 for count in output_production_counts.values()):
                raise ValueError(
                    "each declared output material instance must be produced exactly once"
                )
            produced_intermediates: dict[str, int] = {}
            consumed_intermediates: dict[str, list[int]] = {}
            for relation_index, relation in enumerate(self.material_relations):
                for instance_id in (
                    set(relation.input_material_instance_ids) & intermediate_ids
                ):
                    consumed_intermediates.setdefault(instance_id, []).append(relation_index)
                newly_produced = (
                    set(relation.output_material_instance_ids) & intermediate_ids
                )
                for instance_id in newly_produced:
                    if instance_id in produced_intermediates:
                        raise ValueError(
                            "each intermediate material instance must have one producing relation"
                        )
                    produced_intermediates[instance_id] = relation_index
            if set(produced_intermediates) != intermediate_ids:
                raise ValueError(
                    "every declared intermediate must be produced by a material relation"
                )
            if set(consumed_intermediates) != intermediate_ids:
                raise ValueError(
                    "every declared intermediate must be consumed by a later material relation"
                )
            for instance_id in intermediate_ids:
                if len(consumed_intermediates[instance_id]) != 1:
                    raise ValueError(
                        "each intermediate material instance must be consumed by one relation; "
                        "use one explicit split relation for branching"
                    )
                if any(
                    consumer_index <= produced_intermediates[instance_id]
                    for consumer_index in consumed_intermediates[instance_id]
                ):
                    raise ValueError(
                        "intermediate material must be consumed after its producing relation"
                    )
        return self


class ResearchActionPackageV2(StrictModel):
    schema_version: Literal["2.0"] = CONTRACT_VERSION_V2
    # Historical V2 packages omitted this field and treated every ID as a
    # literal string.  New adapters set it so numeric JSON-scalar identities
    # can cross the string-only V2 wire contract without losing their type.
    identity_encoding: Optional[Literal["typed-json-scalar-v1"]] = None
    campaign_id: str
    capability_snapshot_id: str = ""
    stage: StageV2
    macro_action: MacroActionV2
    macro_steps: List[MacroStepV2] = Field(min_length=1)
    evidence_bundle: EvidenceBundleV2
    raw_observations_digest_scope: Optional[
        Literal["full_raw_observations_v1"]
    ] = None
    raw_observations_sha256: str = ""
    research_contract_hash: str = ""

    @model_validator(mode="after")
    def validate_links_and_hash(self) -> "ResearchActionPackageV2":
        if (self.raw_observations_digest_scope is None) != (
            not self.raw_observations_sha256
        ):
            raise ValueError(
                "raw_observations_digest_scope and raw_observations_sha256 "
                "must be declared together"
            )
        if self.raw_observations_sha256 and re.fullmatch(
            r"sha256_[0-9a-f]{64}", self.raw_observations_sha256
        ) is None:
            raise ValueError(
                "raw_observations_sha256 must be a canonical sha256 digest"
            )
        action_id = self.macro_action.macro_action_id
        if self.macro_action.stage_id != self.stage.stage_id:
            raise ValueError("macro_action.stage_id must match stage.stage_id")
        if any(step.macro_action_id != action_id for step in self.macro_steps):
            raise ValueError("all macro_steps must reference macro_action_id")
        decoded_action_id = decode_package_identity(
            action_id,
            self.identity_encoding,
            "macro_action.macro_action_id",
        )
        action_key = json_scalar_identity_key(decoded_action_id)
        step_keys = []
        for index, step in enumerate(self.macro_steps):
            decoded_step_action = decode_package_identity(
                step.macro_action_id,
                self.identity_encoding,
                f"macro_steps[{index}].macro_action_id",
            )
            if json_scalar_identity_key(decoded_step_action) != action_key:
                raise ValueError("all macro_steps must reference macro_action_id")
            decoded_step_id = decode_package_identity(
                step.macro_step_id,
                self.identity_encoding,
                f"macro_steps[{index}].macro_step_id",
            )
            step_keys.append(json_scalar_identity_key(decoded_step_id))
        if len(set(step_keys)) != len(step_keys):
            raise ValueError("macro_step_id values must be unique typed identities")
        expected = list(range(1, len(self.macro_steps) + 1))
        actual = [step.sequence for step in self.macro_steps]
        if actual != expected:
            raise ValueError("macro_steps must have contiguous sequence numbers")
        if any(
            step.sample_id != self.macro_action.experiment_group.sample_id
            for step in self.macro_steps
        ):
            raise ValueError("one Research action package may contain only one sample group")

        provenance_records: list[tuple[str, ProvenanceV2]] = []
        for step_index, step in enumerate(self.macro_steps):
            provenance_records.append(
                (f"macro_steps[{step_index}].provenance", step.provenance)
            )
            for parameter_index, parameter in enumerate(step.parameters):
                provenance_records.append(
                    (
                        f"macro_steps[{step_index}].parameters[{parameter_index}].provenance",
                        parameter.provenance,
                    )
                )
            for requirement_index, requirement in enumerate(
                step.quantity_requirements
            ):
                provenance_records.append(
                    (
                        f"macro_steps[{step_index}].quantity_requirements["
                        f"{requirement_index}].provenance",
                        ProvenanceV2.model_validate(
                            requirement.get("provenance"), strict=True
                        ),
                    )
                )
            for collection_name in (
                "material_inputs",
                "material_intermediates",
                "material_outputs",
            ):
                for item_index, item in enumerate(getattr(step, collection_name)):
                    provenance_records.append(
                        (
                            f"macro_steps[{step_index}].{collection_name}[{item_index}].provenance",
                            item.provenance,
                        )
                    )
            for relation_index, relation in enumerate(step.material_relations):
                provenance_records.append(
                    (
                        f"macro_steps[{step_index}].material_relations[{relation_index}].provenance",
                        relation.provenance,
                    )
                )
            for segment_index, segment in enumerate(step.operation_segments):
                provenance_records.append(
                    (
                        f"macro_steps[{step_index}].operation_segments[{segment_index}].provenance",
                        segment.provenance,
                    )
                )
            for evidence_index, evidence in enumerate(step.material_applicability):
                provenance_records.append(
                    (
                        f"macro_steps[{step_index}].material_applicability[{evidence_index}].provenance",
                        evidence.provenance,
                    )
                )

        def package_scalar(source_path: str) -> Any:
            if source_path == "evidence_bundle.query":
                return self.evidence_bundle.query
            match = re.fullmatch(
                r"evidence_bundle\.items\[(\d+)\]\.excerpt", source_path
            )
            if match:
                index = int(match.group(1))
                if index < len(self.evidence_bundle.items):
                    return self.evidence_bundle.items[index].excerpt
                raise ValueError(f"provenance source_path out of range: {source_path}")
            match = re.fullmatch(r"macro_steps\[(\d+)\]\.operation", source_path)
            if match:
                index = int(match.group(1))
                if index < len(self.macro_steps):
                    return self.macro_steps[index].operation
                raise ValueError(f"provenance source_path out of range: {source_path}")
            match = re.fullmatch(
                r"macro_steps\[(\d+)\]\.parameters\[(\d+)\]\.value",
                source_path,
            )
            if match:
                step_index = int(match.group(1))
                parameter_index = int(match.group(2))
                if step_index < len(self.macro_steps) and parameter_index < len(
                    self.macro_steps[step_index].parameters
                ):
                    return self.macro_steps[step_index].parameters[parameter_index].value
                raise ValueError(f"provenance source_path out of range: {source_path}")
            raise ValueError(
                "provenance source_path is not a package-verifiable scalar: "
                + source_path
            )

        paper_items = {
            item.evidence_id: (index, item)
            for index, item in enumerate(self.evidence_bundle.items)
        }
        verified_evidence_statuses = {
            "verified_doi",
            "verified_arxiv",
            "verified_semantic_scholar",
            "local_file",
        }
        for location, provenance in provenance_records:
            if provenance.kind == "paper":
                binding = paper_items.get(provenance.reference)
                if binding is None:
                    raise ValueError(
                        f"{location} paper reference must exactly match one current "
                        "evidence_id"
                    )
                item_index, item = binding
                if not self.evidence_bundle.current_invocation_only:
                    raise ValueError(
                        f"{location} paper provenance requires a current-invocation bundle"
                    )
                if item.verification_status not in verified_evidence_statuses or (
                    item.full_text_status not in {"parsed", "local_parsed"}
                ):
                    raise ValueError(
                        f"{location} paper provenance requires a verified, parsed "
                        "current evidence record"
                    )
                expected_source_path = (
                    f"evidence_bundle.items[{item_index}].excerpt"
                )
                if provenance.source_path != expected_source_path:
                    raise ValueError(
                        f"{location} paper provenance must use "
                        f"source_path={expected_source_path}"
                    )
            if provenance.kind not in {"user", "paper", "manual_revision"}:
                continue
            if provenance.kind == "user" and provenance.source_path != "evidence_bundle.query":
                raise ValueError(
                    f"{location} user provenance must use source_path=evidence_bundle.query"
                )
            source_value = package_scalar(provenance.source_path)
            source_text = str(source_value)
            if not provenance.excerpt or provenance.excerpt not in source_text:
                raise ValueError(f"{location} excerpt is not present in its source field")
            if provenance.source_digest != canonical_digest(source_value):
                raise ValueError(f"{location} source_digest does not match its source field")

        for step_index, step in enumerate(self.macro_steps):
            for requirement_index, requirement in enumerate(
                step.quantity_requirements
            ):
                location = (
                    f"macro_steps[{step_index}].quantity_requirements["
                    f"{requirement_index}]"
                )
                provenance = ProvenanceV2.model_validate(
                    requirement.get("provenance"), strict=True
                )
                material_name = str(requirement.get("material") or "").strip()
                if material_name.casefold() not in provenance.excerpt.casefold():
                    raise ValueError(
                        f"{location} evidence excerpt does not identify its bound material"
                    )
                kind = str(requirement.get("kind") or "").strip()
                if kind not in {"whole_batch", "runtime_measured_inventory"} and not (
                    evidence_contains_exact_quantity(
                        provenance.excerpt,
                        requirement.get("value"),
                        str(requirement.get("unit") or ""),
                    )
                ):
                    raise ValueError(
                        f"{location} evidence excerpt does not contain the exact value/unit pair"
                    )

        step_sequence = {
            step.macro_step_id: step.sequence for step in self.macro_steps
        }
        outputs_by_ref = {
            (step.macro_step_id, port.material_instance_id): port
            for step in self.macro_steps
            for port in step.material_outputs
            if port.material_instance_id
        }
        relation_ids = [
            relation.relation_id
            for step in self.macro_steps
            for relation in step.material_relations
        ]
        if len(relation_ids) != len(set(relation_ids)):
            raise ValueError(
                "material relation IDs must be globally unique within a Research package"
            )
        parent_consumers: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for step in self.macro_steps:
            status = step.material_contract_status
            if status is None or status.material_inputs != "declared":
                continue
            for port in step.material_inputs:
                if port.material_origin != "upstream_output":
                    continue
                for parent_ref in port.parent_output_refs:
                    parent_key = (
                        parent_ref.macro_step_id,
                        parent_ref.material_instance_id,
                    )
                    parent = outputs_by_ref.get(
                        parent_key
                    )
                    if parent is None:
                        raise ValueError(
                            "declared upstream input references an unknown output instance"
                        )
                    if step_sequence[parent_ref.macro_step_id] >= step.sequence:
                        raise ValueError(
                            "declared upstream input must reference an earlier macro step"
                        )
                    if parent.material_id != port.material_id:
                        raise ValueError(
                            "upstream input material_id must match its parent output"
                        )
                    parent_consumers.setdefault(parent_key, []).append(
                        (step.macro_step_id, port.material_instance_id or "")
                    )
        if any(len(consumers) != 1 for consumers in parent_consumers.values()):
            raise ValueError(
                "one upstream output instance cannot feed multiple downstream inputs; "
                "declare one explicit split relation with distinct output instances"
            )
        payload = self.model_dump(mode="json", exclude={"research_contract_hash"})
        if "observation_point_id" not in self.macro_action.model_fields_set:
            payload["macro_action"].pop("observation_point_id", None)
        for field_name in (
            "raw_observations_digest_scope",
            "raw_observations_sha256",
        ):
            if field_name not in self.model_fields_set:
                payload.pop(field_name, None)
        # New material fields are omitted from the digest only when they were
        # absent in a historical package.  Newly adapted packages set them
        # explicitly, so their completeness/migration facts are hash-bound.
        for index, step in enumerate(self.macro_steps):
            step_payload = payload["macro_steps"][index]
            for field_name in (
                "reagent_or_object",
                "quantity_requirements",
                "raw_step_digest_scope",
                "raw_step_sha256",
                "material_intermediates",
                "material_relations",
                "operation_segments",
                "material_applicability",
                "material_contract_status",
                "material_contract_migration",
            ):
                if field_name not in step.model_fields_set:
                    step_payload.pop(field_name, None)
            for collection_name in (
                "material_inputs", "material_intermediates", "material_outputs"
            ):
                for port_index, port in enumerate(getattr(step, collection_name)):
                    port_payload = step_payload[collection_name][port_index]
                    for field_name in (
                        "material_instance_id",
                        "material_origin",
                        "parent_output_refs",
                        "logical_container_id",
                    ):
                        if field_name not in port.model_fields_set:
                            port_payload.pop(field_name, None)
                    if (
                        port.quantity is not None
                        and "semantic" not in port.quantity.model_fields_set
                    ):
                        port_payload["quantity"].pop("semantic", None)

        def strip_unset_provenance_extensions(
            model_value: Any, payload_value: Any
        ) -> None:
            if isinstance(model_value, ProvenanceV2) and isinstance(payload_value, dict):
                for field_name in (
                    "source_path",
                    "excerpt",
                    "source_digest",
                    "revision_id",
                    "manifest_digest",
                    "automation_claim",
                ):
                    if field_name not in model_value.model_fields_set:
                        payload_value.pop(field_name, None)
                return
            if isinstance(model_value, BaseModel) and isinstance(payload_value, dict):
                for field_name in model_value.model_fields:
                    if field_name in payload_value:
                        strip_unset_provenance_extensions(
                            getattr(model_value, field_name), payload_value[field_name]
                        )
                return
            if isinstance(model_value, list) and isinstance(payload_value, list):
                for child_model, child_payload in zip(model_value, payload_value):
                    strip_unset_provenance_extensions(child_model, child_payload)

        strip_unset_provenance_extensions(self, payload)
        # Preserve hashes of historical V2 packages created before the optional
        # identity codec marker existed.  Marked packages include the marker in
        # their digest so decoding semantics are immutable and auditable.
        if self.identity_encoding is None:
            payload.pop("identity_encoding", None)
        digest = canonical_digest(payload, prefix="research_v2")
        if self.research_contract_hash and self.research_contract_hash != digest:
            raise ValueError("research_contract_hash does not match package content")
        self.research_contract_hash = digest
        return self


def validate_raw_steps_against_canonical(
    raw_steps: Any,
    package: ResearchActionPackageV2 | Mapping[str, Any],
) -> None:
    """Fail closed when Device's raw Research view diverges from canonical V2.

    Device still consumes a few raw step fields for prompts and compatibility.
    This validator binds the complete raw objects, their typed identities, and
    their order to the canonical package before any model or workstation use.
    """

    canonical = (
        package
        if isinstance(package, ResearchActionPackageV2)
        else ResearchActionPackageV2.model_validate(package)
    )
    if (
        not isinstance(raw_steps, list)
        or not raw_steps
        or any(not isinstance(step, dict) for step in raw_steps)
    ):
        raise ValueError("raw V2 macro steps must be a nonempty object array")
    if len(raw_steps) != len(canonical.macro_steps):
        raise ValueError("raw V2 macro step count does not match canonical package")

    for index, (raw_step, canonical_step) in enumerate(
        zip(raw_steps, canonical.macro_steps)
    ):
        raw_id = raw_step.get("macro_step_id")
        if raw_id is None:
            raw_id = raw_step.get("logical_step_id")
        if raw_id is None:
            raise ValueError(f"raw V2 macro step {index} lacks an explicit typed ID")
        if raw_step.get("macro_step_id") is not None and raw_step.get(
            "logical_step_id"
        ) is not None:
            if json_scalar_identity_key(
                raw_step["macro_step_id"],
                f"raw_steps[{index}].macro_step_id",
            ) != json_scalar_identity_key(
                raw_step["logical_step_id"],
                f"raw_steps[{index}].logical_step_id",
            ):
                raise ValueError(
                    f"raw V2 macro step {index} has conflicting typed IDs"
                )
        raw_key = json_scalar_identity_key(
            raw_id, f"raw_steps[{index}].macro_step_id"
        )
        canonical_key = json_scalar_identity_key(
            decode_package_identity(
                canonical_step.macro_step_id,
                canonical.identity_encoding,
                f"macro_steps[{index}].macro_step_id",
            )
        )
        if raw_key != canonical_key or canonical_step.sequence != index + 1:
            raise ValueError(
                f"raw/canonical macro step typed ID or order mismatch at index {index}"
            )
        raw_sequence = raw_step.get("步骤序号", raw_step.get("sequence"))
        if raw_sequence is not None and (
            isinstance(raw_sequence, bool)
            or not isinstance(raw_sequence, int)
            or raw_sequence != index + 1
        ):
            raise ValueError(
                f"raw V2 macro step sequence mismatch at index {index}"
            )
        if canonical_step.raw_step_digest_scope != RAW_STEP_DIGEST_SCOPE_V1:
            raise ValueError(
                f"canonical macro step {index} lacks the required raw digest scope"
            )
        if canonical_step.raw_step_sha256 != canonical_raw_step_digest(raw_step):
            raise ValueError(
                f"raw/canonical macro step digest mismatch at index {index}"
            )

        raw_reagent = str(
            raw_step.get("试剂/对象") or raw_step.get("reagent_or_object") or ""
        )
        if canonical_step.reagent_or_object != raw_reagent:
            raise ValueError(
                f"raw/canonical reagent_or_object mismatch at index {index}"
            )
        raw_requirements = raw_step.get("quantity_requirements", [])
        if (
            not isinstance(raw_requirements, list)
            or any(not isinstance(item, dict) for item in raw_requirements)
            or canonical_step.quantity_requirements != raw_requirements
        ):
            raise ValueError(
                f"raw/canonical quantity_requirements mismatch at index {index}"
            )

        raw_action_id = raw_step.get("macro_action_id")
        if raw_action_id is None or json_scalar_identity_key(
            raw_action_id,
            f"raw_steps[{index}].macro_action_id",
        ) != json_scalar_identity_key(
            decode_package_identity(
                canonical.macro_action.macro_action_id,
                canonical.identity_encoding,
                "macro_action.macro_action_id",
            )
        ):
            raise ValueError(
                f"raw/canonical macro_action_id mismatch at index {index}"
            )
        raw_observation_point_id = str(
            raw_step.get("observation_point_id") or ""
        ).strip()
        if (
            not raw_observation_point_id
            or raw_observation_point_id
            != canonical.macro_action.observation_point_id
        ):
            raise ValueError(
                f"raw/canonical observation_point_id mismatch at index {index}"
            )


def _v2_device_agent_contract() -> Dict[str, Any]:
    """Return the program-owned Device boundary; never trust an input copy."""

    return {
        "research_macro_action_level": (
            "macro_action_steps are immutable chemical-semantic Research actions; "
            "Device may bind only supported execution details without changing "
            "their scientific meaning"
        ),
        "device_agent_responsibilities": [
            "Select supported physical containers and workstations from the current truth source.",
            "Expand only execution details that preserve the canonical Research contract.",
            "Return a blocker when a mandatory scientific condition cannot be implemented.",
        ],
        "do_not_return_to_research_for": [
            "physical container identifiers",
            "workstation selection",
            "reagent slot allocation",
            "lid and balancing expansion",
        ],
        "must_return_to_research_for": [
            "scientific route, material identity/state, inventory boundary, scale, order, or endpoint changes",
        ],
    }


def canonicalize_v2_device_handoff(
    raw_handoff: Mapping[str, Any],
    package: ResearchActionPackageV2 | Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Validate a V2 handoff and rebuild the only Device-authoritative view.

    Raw compatibility mirrors are never passed through wholesale.  Canonical
    task/action/evidence fields are projected from the signed Research package;
    complete raw macro steps and observations survive only after digest checks.
    Narrative ``research_context`` and caller-supplied ``device_agent_contract``
    are deliberately discarded.
    """

    if not isinstance(raw_handoff, Mapping):
        raise ValueError("V2 Device handoff must be an object")
    raw_package: Any = package
    if raw_package is None:
        raw_package = raw_handoff.get("research_action_package_v2")
    if raw_package is None:
        raise ValueError("V2 Device handoff requires a canonical Research package")
    canonical = (
        raw_package
        if isinstance(raw_package, ResearchActionPackageV2)
        else ResearchActionPackageV2.model_validate(raw_package)
    )
    if not canonical.macro_action.observation_point_id.strip():
        raise ValueError(
            "canonical V2 package lacks a hash-bound observation_point_id"
        )
    if (
        canonical.raw_observations_digest_scope
        != RAW_OBSERVATIONS_DIGEST_SCOPE_V1
        or not canonical.raw_observations_sha256
    ):
        raise ValueError(
            "canonical V2 package lacks the required raw observations digest"
        )

    raw_steps = raw_handoff.get("macro_action_steps")
    validate_raw_steps_against_canonical(raw_steps, canonical)
    raw_observations = raw_handoff.get("observations", [])
    if canonical.raw_observations_sha256 != canonical_raw_observations_digest(
        raw_observations
    ):
        raise ValueError("raw/canonical observations digest mismatch")

    def _same(left: Any, right: Any) -> bool:
        return json.dumps(
            left, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ) == json.dumps(
            right, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )

    def _reject_conflict(
        container: Any,
        field_name: str,
        expected: Any,
        path: str,
    ) -> None:
        if not isinstance(container, Mapping) or field_name not in container:
            return
        actual = container[field_name]
        if actual in (None, "", [], {}):
            return
        if not _same(actual, expected):
            raise ValueError(f"V2 raw/canonical conflict at {path}.{field_name}")

    supplied_version = str(raw_handoff.get("contract_version") or "").strip()
    if supplied_version and supplied_version != "v2":
        raise ValueError("V2 Device handoff cannot be relabelled as another contract")
    supplied_campaign = raw_handoff.get("campaign_id")
    if supplied_campaign not in (None, "", canonical.campaign_id):
        raise ValueError("V2 raw/canonical campaign_id conflict")

    raw_task = raw_handoff.get("task")
    _reject_conflict(raw_task, "query", canonical.evidence_bundle.query, "task")
    _reject_conflict(raw_task, "current_stage", canonical.stage.name, "task")
    _reject_conflict(
        raw_task, "current_stage_plan", canonical.stage.objective, "task"
    )
    if isinstance(raw_task, Mapping) and raw_task.get("stage_route") not in (
        None,
        "",
        [],
    ):
        raw_route = raw_task.get("stage_route")
        if not isinstance(raw_route, list) or canonical.stage.name not in raw_route:
            raise ValueError("V2 raw/canonical conflict at task.stage_route")

    raw_action = raw_handoff.get("macro_action")
    canonical_action = canonical.macro_action.model_dump(
        mode="json", exclude_none=True
    )
    if isinstance(raw_action, Mapping) and raw_action.get("macro_action_id") not in (
        None,
        "",
    ):
        if json_scalar_identity_key(
            raw_action.get("macro_action_id"),
            "macro_action.macro_action_id",
        ) != json_scalar_identity_key(
            decode_package_identity(
                canonical.macro_action.macro_action_id,
                canonical.identity_encoding,
                "macro_action.macro_action_id",
            )
        ):
            raise ValueError(
                "V2 raw/canonical conflict at macro_action.macro_action_id"
            )
    for field_name in (
        "observation_point_id",
        "objective",
        "expected_observation",
        "completion_condition",
        "planned_operations",
        "experiment_group",
    ):
        _reject_conflict(
            raw_action,
            field_name,
            canonical_action.get(field_name),
            "macro_action",
        )
    _reject_conflict(
        raw_action, "observation_point", canonical.stage.observation_point, "macro_action"
    )
    _reject_conflict(raw_action, "stage", canonical.stage.name, "macro_action")

    raw_evidence = raw_handoff.get("current_evidence_bundle")
    if isinstance(raw_evidence, Mapping) and raw_evidence:
        _reject_conflict(
            raw_evidence, "bundle_id", canonical.evidence_bundle.bundle_id, "evidence"
        )
        _reject_conflict(
            raw_evidence, "query", canonical.evidence_bundle.query, "evidence"
        )

    evidence_items: List[Dict[str, Any]] = []
    for item in canonical.evidence_bundle.items:
        projected = item.model_dump(mode="json", exclude_none=True)
        projected["evidence_excerpt"] = item.excerpt
        evidence_items.append(projected)
    evidence_view = {
        "bundle_id": canonical.evidence_bundle.bundle_id,
        "scope": canonical.evidence_bundle.scope,
        "query": canonical.evidence_bundle.query,
        "objective": canonical.evidence_bundle.objective,
        "retrieval_status": canonical.evidence_bundle.retrieval_status,
        "current_invocation_only": canonical.evidence_bundle.current_invocation_only,
        "results": evidence_items,
        "errors": list(canonical.evidence_bundle.errors),
    }
    macro_action_view = canonical_action
    macro_action_view.update(
        {
            "observation_point": canonical.stage.observation_point,
            "stage": canonical.stage.name,
        }
    )
    return {
        "contract_version": "v2",
        "contract_resolution": {
            "requested": "v2",
            "source_input": "v2",
            "effective": "v2",
            "requested_matches_effective": True,
            "source_matches_effective": True,
        },
        "canonical_research_view_version": "v2-package-projection-v1",
        "campaign_id": canonical.campaign_id,
        "handoff_type": "research_to_device_adaptation",
        "task": {
            "query": canonical.evidence_bundle.query,
            "stage_route": [canonical.stage.name],
            "current_stage": canonical.stage.name,
            "current_stage_plan": canonical.stage.objective,
            "observation_point_id": canonical.macro_action.observation_point_id,
            "observation_point": canonical.stage.observation_point,
            "completion_condition": canonical.stage.completion_condition,
        },
        "macro_action_steps": copy.deepcopy(raw_steps),
        "macro_action": macro_action_view,
        "current_evidence_bundle": evidence_view,
        "observations": copy.deepcopy(raw_observations),
        "device_agent_contract": _v2_device_agent_contract(),
        "research_action_package_v2": canonical.model_dump(
            mode="json", exclude_none=True
        ),
    }


class WorkstationRequirementV2(StrictModel):
    workstation_task_id: str = Field(min_length=1)
    macro_step_id: str = Field(min_length=1)
    functional_role: str = Field(min_length=1)
    required_capabilities: List[str] = Field(default_factory=list)
    input_state: str = "unknown"
    output_state: str = "unknown"
    candidate_station_codes: List[str] = Field(default_factory=list)


class DeviceStepV2(StrictModel):
    device_step_id: str = Field(min_length=1)
    source_macro_step_id: str = Field(min_length=1)
    # Historical V2 packages exposed only the primary source.  New packages
    # preserve the complete, ordered Research coverage when one physical step
    # implements more than one macro step.  Omission remains backward
    # compatible; when present, the primary must be the first entry.
    source_macro_step_ids: List[str] = Field(default_factory=list)
    workstation_task_id: str = ""
    station_code: str = Field(min_length=1)
    station_version: str = Field(min_length=1)
    platform_name: str = Field(min_length=1)
    station_id: Optional[int] = None
    operation: str = Field(min_length=1)
    parameters: Dict[str, Any] = Field(default_factory=dict)
    container_bindings: List[Dict[str, Any]] = Field(default_factory=list)
    material_bindings: List[Dict[str, Any]] = Field(default_factory=list)
    skill_sha256: str = ""


class WorkstationMappingV2(StrictModel):
    requirements: List[WorkstationRequirementV2] = Field(default_factory=list)
    device_steps: List[DeviceStepV2] = Field(default_factory=list)


class ValidationIssueV2(StrictModel):
    issue_id: str = Field(min_length=1)
    validator: str = Field(min_length=1)
    severity: Literal["error", "warning"] = "error"
    macro_step_id: str = ""
    device_step_id: str = ""
    field_path: str = ""
    actual: Any = None
    expected: Any = None
    rule: str = ""
    source_ref: str = ""
    repair_scope: Literal["device_step", "macro_step", "terminal"] = "device_step"


class ValidationReportV2(StrictModel):
    status: Literal["passed", "failed"]
    rounds_used: int = Field(default=0, ge=0, le=10)
    max_rounds: Literal[10] = 10
    issues: List[ValidationIssueV2] = Field(default_factory=list)
    locked_step_hashes: Dict[str, str] = Field(default_factory=dict)


class DeviceWorkflowPackageV2(StrictModel):
    schema_version: Literal["2.0"] = CONTRACT_VERSION_V2
    identity_encoding: Optional[Literal["typed-json-scalar-v1"]] = None
    status: Literal[
        "ready_for_dispatch",
        "terminal_unmappable",
        "human_review_required",
        "device_internal_error",
    ]
    campaign_id: str = ""
    device_package_id: str
    capability_snapshot_id: str = ""
    research_contract_hash: str
    workstation_mapping: WorkstationMappingV2
    workflow: Dict[str, Any] = Field(default_factory=dict)
    dispatch_payload: Dict[str, Any] = Field(default_factory=dict)
    loaded_workstation_skills: List[Dict[str, Any]] = Field(default_factory=list)
    validation: ValidationReportV2
    terminal_macro_step_ids: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_identity_links_and_dispatch_gate(self) -> "DeviceWorkflowPackageV2":
        requirement_keys: Dict[tuple[str, Any], str] = {}
        for index, requirement in enumerate(self.workstation_mapping.requirements):
            decoded = decode_package_identity(
                requirement.macro_step_id,
                self.identity_encoding,
                f"workstation_mapping.requirements[{index}].macro_step_id",
            )
            key = json_scalar_identity_key(decoded)
            if key in requirement_keys:
                raise ValueError(
                    "workstation requirements must have unique typed macro_step_id values"
                )
            requirement_keys[key] = requirement.macro_step_id

        device_step_ids = set()
        for index, step in enumerate(self.workstation_mapping.device_steps):
            if not step.device_step_id.strip():
                raise ValueError("device_step_id must be a nonempty string")
            if step.device_step_id in device_step_ids:
                raise ValueError("device_step_id values must be unique")
            device_step_ids.add(step.device_step_id)
            primary = decode_package_identity(
                step.source_macro_step_id,
                self.identity_encoding,
                f"workstation_mapping.device_steps[{index}].source_macro_step_id",
            )
            primary_key = json_scalar_identity_key(primary)
            if primary_key not in requirement_keys:
                raise ValueError(
                    "every device source_macro_step_id must reference a workstation "
                    "requirement macro_step_id"
                )

            coverage = step.source_macro_step_ids
            if coverage:
                if coverage[0] != step.source_macro_step_id:
                    raise ValueError(
                        "source_macro_step_id must equal the first source_macro_step_ids entry"
                    )
                coverage_keys = []
                for coverage_index, wire_id in enumerate(coverage):
                    decoded_coverage = decode_package_identity(
                        wire_id,
                        self.identity_encoding,
                        (
                            "workstation_mapping.device_steps"
                            f"[{index}].source_macro_step_ids[{coverage_index}]"
                        ),
                    )
                    coverage_key = json_scalar_identity_key(decoded_coverage)
                    if coverage_key not in requirement_keys:
                        raise ValueError(
                            "every source_macro_step_ids entry must reference a "
                            "workstation requirement macro_step_id"
                        )
                    coverage_keys.append(coverage_key)
                if len(set(coverage_keys)) != len(coverage_keys):
                    raise ValueError(
                        "source_macro_step_ids must contain unique typed identities"
                    )

        terminal_keys = []
        for index, wire_id in enumerate(self.terminal_macro_step_ids):
            decoded = decode_package_identity(
                wire_id,
                self.identity_encoding,
                f"terminal_macro_step_ids[{index}]",
            )
            key = json_scalar_identity_key(decoded)
            if key not in requirement_keys:
                raise ValueError(
                    "terminal_macro_step_ids must reference workstation requirement "
                    "macro_step_id values"
                )
            terminal_keys.append(key)
        if len(set(terminal_keys)) != len(terminal_keys):
            raise ValueError("terminal_macro_step_ids must contain unique typed identities")
        if self.status == "terminal_unmappable" and not self.terminal_macro_step_ids:
            raise ValueError("terminal_unmappable requires terminal_macro_step_ids")

        for index, issue in enumerate(self.validation.issues):
            if issue.macro_step_id:
                decode_package_identity(
                    issue.macro_step_id,
                    self.identity_encoding,
                    f"validation.issues[{index}].macro_step_id",
                )
        if self.status == "ready_for_dispatch" and self.validation.status != "passed":
            raise ValueError(
                "ready_for_dispatch requires validation.status=passed"
            )
        if (
            self.status == "ready_for_dispatch"
            and not self.workstation_mapping.device_steps
        ):
            raise ValueError("ready_for_dispatch requires at least one device step")
        return self


class ParameterTraceV2(StrictModel):
    macro_step_id: str = Field(min_length=1)
    device_step_id: str = ""
    name: str = Field(min_length=1)
    planned_value: Any = None
    device_setpoint: Any = None
    actual_value: Any = None
    unit: str = ""
    deviation: Any = None
    actual_status: Literal["reported", "unavailable", "not_applicable"] = "unavailable"


class ObservationEventV2(StrictModel):
    schema_version: Literal["2.0"] = CONTRACT_VERSION_V2
    identity_encoding: Optional[Literal["typed-json-scalar-v1"]] = None
    campaign_id: str = ""
    macro_action_id: str = Field(min_length=1)
    device_package_id: str = Field(min_length=1)
    execution_status: str
    summary: str = ""
    macro_parameter_summary: List[Dict[str, Any]] = Field(default_factory=list)
    device_parameter_trace: List[ParameterTraceV2] = Field(default_factory=list)
    measurements: Dict[str, Any] = Field(default_factory=dict)
    artifacts: List[Dict[str, Any]] = Field(default_factory=list)
    material_consumption: List[Dict[str, Any]] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_wire_identities(self) -> "ObservationEventV2":
        decode_package_identity(
            self.macro_action_id,
            self.identity_encoding,
            "macro_action_id",
        )
        for index, item in enumerate(self.macro_parameter_summary):
            if "macro_step_id" in item and item["macro_step_id"] is not None:
                decode_package_identity(
                    item["macro_step_id"],
                    self.identity_encoding,
                    f"macro_parameter_summary[{index}].macro_step_id",
                )
        for index, item in enumerate(self.device_parameter_trace):
            decode_package_identity(
                item.macro_step_id,
                self.identity_encoding,
                f"device_parameter_trace[{index}].macro_step_id",
            )
        return self
