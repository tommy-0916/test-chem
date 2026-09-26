"""Versioned, evidence-scoped candidates for chemical route decisions.

These models record proposed routes and their evidence.  They do not decide
whether a route is scientifically complete or executable on a workstation.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from .v2 import (
    EvidenceItemV2,
    MacroStepV2,
    ProvenanceV2,
    StateTransitionV2,
    StrictModel,
    normalize_material_state,
)


class _RouteStrictModel(StrictModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, strict=True)


def _unique_nonempty(values: list[str], field_name: str) -> list[str]:
    if any(not value.strip() for value in values):
        raise ValueError(f"{field_name} must contain nonempty strings")
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} values must be unique")
    return values


class RouteTargetV1(_RouteStrictModel):
    material: str = Field(min_length=1)
    desired_state: str = Field(min_length=1)
    objective: str = Field(min_length=1)

    @field_validator("desired_state")
    @classmethod
    def normalize_desired_state(cls, value: str) -> str:
        return normalize_material_state(value)


class ExperimentalGroupScopeV1(_RouteStrictModel):
    paper_id: str = Field(min_length=1)
    experimental_group_id: str = Field(min_length=1)
    section: str = ""
    locator: str = ""
    source_digest: str = ""


class RouteSignatureV1(_RouteStrictModel):
    route_family: str = Field(min_length=1)
    target_transformation: str = Field(min_length=1)
    precursor_roles: list[str] = Field(default_factory=list)
    reagent_roles: list[str] = Field(default_factory=list)
    operations: list[str] = Field(default_factory=list)
    control_modes: list[str] = Field(default_factory=list)
    phase_transitions: list[StateTransitionV2] = Field(default_factory=list)
    endpoint_state: str = Field(min_length=1)

    @field_validator("endpoint_state")
    @classmethod
    def normalize_endpoint_state(cls, value: str) -> str:
        return normalize_material_state(value)


class RouteFieldEvidenceV1(_RouteStrictModel):
    field_path: str = Field(min_length=1)
    value: Any = None
    unit: str = ""
    required: bool = True
    status: Literal["supported", "runtime_pending", "unsupported", "unknown"]
    provenance: ProvenanceV2 | None = None
    evidence_id: str = ""
    source_scope: ExperimentalGroupScopeV1 | None = None
    resolution_path: str = ""
    derived_or_scaled: bool = False


class RouteCandidateV1(_RouteStrictModel):
    route_id: str = Field(min_length=1)
    target: RouteTargetV1
    source_scope: ExperimentalGroupScopeV1 | None = None
    route_signature: RouteSignatureV1
    evidence_bundle: list[EvidenceItemV2] = Field(default_factory=list)
    evidence_matrix: list[RouteFieldEvidenceV1] = Field(default_factory=list)
    material_graph: list[MacroStepV2] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    origin: Literal["paper_experimental_group", "hypothesis"]
    scientific_status: Literal["unassessed"] = "unassessed"
    device_status: Literal["unassessed"] = "unassessed"

    @model_validator(mode="after")
    def validate_candidate(self) -> RouteCandidateV1:
        if self.origin == "paper_experimental_group" and self.source_scope is None:
            raise ValueError("paper experimental-group candidate requires source_scope")
        _unique_nonempty(
            [item.evidence_id for item in self.evidence_bundle],
            "evidence_bundle.evidence_id",
        )
        _unique_nonempty(
            [item.field_path for item in self.evidence_matrix],
            "evidence_matrix.field_path",
        )
        _unique_nonempty(self.required_capabilities, "required_capabilities")
        return self


class RouteGoalV1(_RouteStrictModel):
    goal_id: str = Field(min_length=1)
    target: RouteTargetV1
    constraint: Literal["open", "locked_family", "locked_experimental_group"]
    locked_family: str = ""
    locked_scope: ExperimentalGroupScopeV1 | None = None
    required_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_constraint(self) -> RouteGoalV1:
        if self.constraint == "locked_family" and not self.locked_family.strip():
            raise ValueError("locked_family constraint requires locked_family")
        if self.constraint == "locked_experimental_group" and self.locked_scope is None:
            raise ValueError("locked_experimental_group constraint requires locked_scope")
        _unique_nonempty(self.required_fields, "required_fields")
        return self
