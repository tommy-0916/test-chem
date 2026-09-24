"""Deterministic, station-scoped reagent-slot identity projection.

Names in a workflow are display values, not material identifiers.  This module
builds an authoritative ``(station_code, slot_number)`` registry from the
device plan and projects the registry's canonical display name into workflow
source-bottle objects.  It deliberately performs no chemistry/name matching.

Both public operations are pure and atomic: their input is deep-copied and a
blocking error returns an unchanged copy.  Callers may therefore keep model
output as immutable evidence and persist the returned value as a derived
artifact together with the structured events.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional

try:
    from .macro_identity import (
        MacroId,
        MacroIdentityError,
        MacroKey,
        macro_id_key,
        normalize_macro_id,
    )
except ImportError:  # Direct script compatibility.
    from macro_identity import (
        MacroId,
        MacroIdentityError,
        MacroKey,
        macro_id_key,
        normalize_macro_id,
    )


RULE_ID = "reagent_slot_identity/v1"

_INSTANCE_BOTTLE_RE = re.compile(r"^(\d+)号原液瓶$")
_PLAN_BOTTLE_KEY_RE = re.compile(r"^(\d+)号原液瓶(?:\([^)]*\))?$")
_PLAN_BOTTLE_TEXT_RE = re.compile(r"(?<!\d)(\d+)号原液瓶")

_STATION_CODE_FIELDS = ("station_code", "workstation_code", "工作站编码")
_STATION_FIELDS = ("workstation", "station", "工作站")
_SLOT_FIELDS = (
    "slot_number",
    "bottle_number",
    "source_bottle_number",
    "原液编号",
    "原液瓶号",
    "瓶号",
)
_IDENTITY_FIELDS = (
    "material_identity_id",
    "物料身份ID",
    "物料身份标识",
)
_CANONICAL_NAME_FIELDS = (
    "canonical_name",
    "规范名称",
    "标准名称",
    "名称",
    "配料名称",
)
_SOURCE_FIELDS = ("source", "来源", "source_ref", "来源引用")
_PLAN_STEP_FIELDS = ("plan_step", "step_id", "step_number", "计划步骤", "步骤编号")
_SOURCE_PLAN_STEP_FIELDS = ("source_plan_step", "来源计划步骤", "源计划步骤")
_SOURCE_ID_FIELDS = (
    "source_material_identity_ids",
    "源物料身份IDs",
    "源物料身份ID列表",
    "来源物料身份ID列表",
)

StationResolver = Callable[[Any], Any]


@dataclass(frozen=True)
class ReagentSlotProjectionResult:
    """Result of :func:`project_reagent_slot_identities`."""

    workflow: Any
    events: tuple[dict[str, Any], ...]
    errors: tuple[dict[str, Any], ...]

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class ReagentSlotPlanEnrichmentResult:
    """Result of :func:`enrich_legacy_reagent_slot_plan`."""

    reagent_slot_plan: Any
    events: tuple[dict[str, Any], ...]
    errors: tuple[dict[str, Any], ...]

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class ReagentSlotBinding:
    """One authoritative binding in a station-local slot namespace."""

    station_code: str
    slot_number: int
    material_identity_id: str
    canonical_name: str
    pointer: str


@dataclass(frozen=True)
class ReagentSlotRegistryResult:
    """Pure registry-build result, suitable for plan validation."""

    bindings: dict[tuple[str, int], ReagentSlotBinding]
    errors: tuple[dict[str, Any], ...]

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class ReagentSlotValidationResult:
    """Read-only validation result for checker integration."""

    errors: tuple[dict[str, Any], ...]

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class ReagentSlotIdentityRootResult:
    """Membership proof for slot-plan IDs against independent material roots."""

    claimed_identity_ids: frozenset[str]
    trusted_identity_ids: frozenset[str]
    untrusted_identity_ids: frozenset[str]
    evidence_sources: tuple[str, ...]

    @property
    def anchored(self) -> bool:
        return bool(self.claimed_identity_ids) and not self.untrusted_identity_ids


@dataclass(frozen=True)
class _Occurrence:
    bottle: dict[str, Any]
    station_code: str
    slot_number: int
    pointer: str
    source_plan_step: Optional[MacroId]


def project_reagent_slot_identities(
    workflow_json: Any,
    reagent_slot_plan: Any,
    *,
    station_resolver: StationResolver,
    plan_steps: Any = None,
) -> ReagentSlotProjectionResult:
    """Project authoritative reagent names into a workflow.

    ``station_resolver`` receives a workstation label/code and must return its
    canonical code (or an object/mapping containing ``code``).  A direct
    ``station_code`` field is still passed through the resolver when possible,
    but remains a valid canonical code if the resolver does not recognize it.

    When ``plan_steps`` is provided, every bottle occurrence must point to an
    existing source plan step whose ``source_material_identity_ids`` includes
    the slot binding's identity.  Any validation/authorization error blocks
    the *entire* projection and returns an unchanged deep copy.
    """

    original = copy.deepcopy(workflow_json)
    projected = copy.deepcopy(workflow_json)
    errors: list[dict[str, Any]] = []

    bindings = _build_bindings(reagent_slot_plan, station_resolver, errors)
    authorizations = None
    if plan_steps is not None:
        authorizations = _build_plan_step_authorizations(plan_steps, errors)

    occurrences = _collect_workflow_occurrences(projected, station_resolver, errors)
    planned_changes: list[tuple[_Occurrence, ReagentSlotBinding, Any]] = []
    for occurrence in occurrences:
        key = (occurrence.station_code, occurrence.slot_number)
        binding = bindings.get(key)
        if binding is None:
            errors.append(
                _error(
                    "missing_slot_binding",
                    occurrence.pointer,
                    "Workflow source bottle has no authoritative station-scoped slot binding",
                    station_code=occurrence.station_code,
                    slot_number=occurrence.slot_number,
                )
            )
            continue

        payload_identity, payload_identity_problem = _optional_unique_text_alias(
            occurrence.bottle, _IDENTITY_FIELDS, "material_identity_id"
        )
        if payload_identity_problem is not None:
            errors.append(
                _error(
                    payload_identity_problem,
                    occurrence.pointer,
                    "Workflow source-bottle identity field is invalid or ambiguous",
                    station_code=occurrence.station_code,
                    slot_number=occurrence.slot_number,
                )
            )
            continue
        if (
            payload_identity is not None
            and payload_identity != binding.material_identity_id
        ):
            errors.append(
                _error(
                    "workflow_slot_identity_conflict",
                    occurrence.pointer,
                    "Workflow source-bottle identity conflicts with the authoritative slot binding",
                    material_identity_id=payload_identity,
                    expected_material_identity_id=binding.material_identity_id,
                    station_code=occurrence.station_code,
                    slot_number=occurrence.slot_number,
                )
            )
            continue

        if authorizations is not None:
            source_step = occurrence.source_plan_step
            if source_step is None:
                errors.append(
                    _error(
                        "missing_source_plan_step",
                        occurrence.pointer,
                        "Bottle identity cannot be authorized without source_plan_step",
                        station_code=occurrence.station_code,
                        slot_number=occurrence.slot_number,
                        material_identity_id=binding.material_identity_id,
                    )
                )
                continue
            source_key = macro_id_key(source_step, "workflow.source_plan_step")
            if source_key not in authorizations:
                errors.append(
                    _error(
                        "unknown_source_plan_step",
                        occurrence.pointer,
                        "source_plan_step is absent from the supplied plan",
                        source_plan_step=source_step,
                        material_identity_id=binding.material_identity_id,
                    )
                )
                continue
            allowed = authorizations[source_key]
            if allowed is None:
                errors.append(
                    _error(
                        "missing_source_material_identity_ids",
                        occurrence.pointer,
                        "Source plan step has no structured material identity authorization",
                        source_plan_step=source_step,
                        material_identity_id=binding.material_identity_id,
                    )
                )
                continue
            if binding.material_identity_id not in allowed:
                errors.append(
                    _error(
                        "unauthorized_slot_identity",
                        occurrence.pointer,
                        "Slot material identity is not authorized by the source plan step",
                        source_plan_step=source_step,
                        material_identity_id=binding.material_identity_id,
                        authorized_material_identity_ids=sorted(allowed),
                        station_code=occurrence.station_code,
                        slot_number=occurrence.slot_number,
                    )
                )
                continue

        old_value = occurrence.bottle.get("配料名称")
        if old_value != binding.canonical_name:
            planned_changes.append((occurrence, binding, old_value))

    if errors:
        return ReagentSlotProjectionResult(original, (), tuple(errors))

    events: list[dict[str, Any]] = []
    for occurrence, binding, old_value in planned_changes:
        occurrence.bottle["配料名称"] = binding.canonical_name
        events.append(
            {
                "rule_id": RULE_ID,
                "event": "canonical_name_projected",
                "json_pointer": f"{occurrence.pointer}/配料名称",
                "old": old_value,
                "new": binding.canonical_name,
                "material_identity_id": binding.material_identity_id,
                "station_code": binding.station_code,
                "slot_number": binding.slot_number,
            }
        )
    return ReagentSlotProjectionResult(projected, tuple(events), ())


def build_reagent_slot_registry(
    reagent_slot_plan: Any, *, station_resolver: StationResolver
) -> ReagentSlotRegistryResult:
    """Validate and build the authoritative station-scoped registry.

    The returned mapping is newly allocated and the input plan is never
    changed.  Duplicate slots, missing IDs/names, unresolved stations, and
    identity/name conflicts are represented as structured errors.
    """

    errors: list[dict[str, Any]] = []
    bindings = _build_bindings(reagent_slot_plan, station_resolver, errors)
    return ReagentSlotRegistryResult(dict(bindings), tuple(errors))


def validate_reagent_slot_identities(
    workflow_json: Any,
    reagent_slot_plan: Any,
    *,
    station_resolver: StationResolver,
    plan_steps: Any = None,
) -> ReagentSlotValidationResult:
    """Read-only checker facade over the same rules used for projection.

    A name that projection would change is emitted as
    ``noncanonical_reagent_name``.  All registry, shape, binding, and optional
    source-plan authorization errors are returned unchanged.  This keeps a
    dispatch checker strict while allowing an earlier normalization stage to
    produce an audited derived workflow.
    """

    projected = project_reagent_slot_identities(
        workflow_json,
        reagent_slot_plan,
        station_resolver=station_resolver,
        plan_steps=plan_steps,
    )
    if projected.errors:
        return ReagentSlotValidationResult(projected.errors)
    findings: list[dict[str, Any]] = []
    for event in projected.events:
        finding = dict(event)
        finding["code"] = "noncanonical_reagent_name"
        finding["message"] = (
            "Workflow reagent display name does not match the authoritative "
            "station-scoped slot binding"
        )
        finding.pop("event", None)
        findings.append(finding)
    return ReagentSlotValidationResult(tuple(findings))


def validate_reagent_slot_identity_roots(
    reagent_slot_plan: Any,
    *,
    batch_plan: Any = None,
    material_ledger: Any = None,
    material_identity_registry: Any = None,
) -> ReagentSlotIdentityRootResult:
    """Prove slot IDs are members of an independent material namespace.

    The slot plan is a claim, not its own authority.  Trusted membership may
    come from an explicitly marked root batch, a structured material identity
    registry, or a material-ledger entry carrying an identity ID.  Device-plan
    authorizations remain a separate per-step check and deliberately cannot
    bootstrap trust by repeating the same model-authored ID.

    This function is pure and performs no name/alias/chemistry inference.
    Invalid or missing slot identity fields remain the responsibility of the
    registry/projection validator; only well-formed, unambiguous IDs are
    included in ``claimed_identity_ids``.
    """

    claimed: set[str] = set()
    if isinstance(reagent_slot_plan, list):
        for record in reagent_slot_plan:
            if not isinstance(record, Mapping):
                continue
            identity_id, problem = _optional_unique_text_alias(
                record, _IDENTITY_FIELDS, "material_identity_id"
            )
            if problem is None and identity_id is not None:
                claimed.add(identity_id)

    trusted: set[str] = set()
    evidence_sources: list[str] = []

    root_ids = _explicit_root_batch_identity_ids(batch_plan)
    if root_ids:
        trusted.update(root_ids)
        evidence_sources.append("batch_plan.root")

    ledger_ids = _structured_identity_ids(
        material_ledger.get("entries")
        if isinstance(material_ledger, Mapping)
        else None
    )
    if ledger_ids:
        trusted.update(ledger_ids)
        evidence_sources.append("material_ledger.entries")

    registry_ids = _structured_identity_ids(material_identity_registry)
    if registry_ids:
        trusted.update(registry_ids)
        evidence_sources.append("material_identity_registry")

    return ReagentSlotIdentityRootResult(
        claimed_identity_ids=frozenset(claimed),
        trusted_identity_ids=frozenset(trusted),
        untrusted_identity_ids=frozenset(claimed - trusted),
        evidence_sources=tuple(evidence_sources),
    )


def collect_material_identity_registry_evidence(package: Any) -> list[Any]:
    """Collect every registry location recognized by package validation.

    The extraction contract is intentionally shallow and explicit: the package
    root plus ``semantic_analysis``, ``research_handoff``, and ``macro_plan``
    containers, including a semantic-analysis object nested in either latter
    container.  Both upstream normalization and the read-only checker consume
    this helper so a material identity cannot be trusted by only one layer.
    """
    if not isinstance(package, Mapping):
        return []
    evidence: list[Any] = []
    if "material_identity_registry" in package:
        evidence.append(package.get("material_identity_registry"))
    for container_key in (
        "semantic_analysis",
        "research_handoff",
        "macro_plan",
    ):
        container = package.get(container_key)
        if not isinstance(container, Mapping):
            continue
        if "material_identity_registry" in container:
            evidence.append(container.get("material_identity_registry"))
        semantic = container.get("semantic_analysis")
        if (
            isinstance(semantic, Mapping)
            and "material_identity_registry" in semantic
        ):
            evidence.append(semantic.get("material_identity_registry"))
    return evidence


def enrich_legacy_reagent_slot_plan(
    reagent_slot_plan: Any,
    *,
    station_resolver: StationResolver,
    batch_plan: Any = None,
    device_plan: Any = None,
    known_identity_ids: Iterable[str] = (),
) -> ReagentSlotPlanEnrichmentResult:
    """Add missing legacy ``material_identity_id`` fields from stable evidence.

    Evidence is deliberately narrow:

    * a trusted root/resource identity ID appears verbatim in the slot's source
      field; or
    * all structured device-plan uses of the same station+slot authorize one
      and only one trusted root/resource identity.

    ``batch_plan`` contributes trusted IDs and canonical display names only
    from records explicitly marked ``is_root_batch=true``; the structured
    ``material_id`` field is the display authority. ``known_identity_ids`` lets
    a caller add identities from another already-validated resource registry,
    but cannot provide display names.  Legacy names are never used as identity
    evidence or canonical-name authority.  Resolution is atomic: one
    unresolved/conflicting record returns the original plan unchanged.
    """

    original = copy.deepcopy(reagent_slot_plan)
    enriched = copy.deepcopy(reagent_slot_plan)
    errors: list[dict[str, Any]] = []
    if not isinstance(enriched, list):
        return ReagentSlotPlanEnrichmentResult(
            original,
            (),
            (
                _error(
                    "invalid_reagent_slot_plan",
                    "/reagent_slot_plan",
                    "reagent_slot_plan must be a list",
                ),
            ),
        )

    trusted = {
        value.strip()
        for value in known_identity_ids
        if isinstance(value, str) and value.strip()
    }
    root_ids, canonical_names = _root_identity_registry(batch_plan, errors)
    trusted.update(root_ids)
    if not trusted:
        return ReagentSlotPlanEnrichmentResult(
            original,
            (),
            (
                _error(
                    "missing_trusted_identity_registry",
                    "/reagent_slot_plan",
                    "Legacy enrichment requires validated root/resource identity IDs",
                ),
            ),
        )

    use_candidates = _device_plan_slot_candidates(
        device_plan,
        station_resolver,
        trusted,
        _all_batch_identity_ids(batch_plan) | trusted,
        errors,
    )
    planned_identities: list[tuple[dict[str, Any], int, str, str, str, int]] = []
    resolved_records: list[tuple[dict[str, Any], int, str, str, int]] = []
    for index, record in enumerate(enriched):
        pointer = f"/reagent_slot_plan/{index}"
        if not isinstance(record, dict):
            errors.append(
                _error("invalid_slot_record", pointer, "Slot record must be an object")
            )
            continue
        station = _resolve_record_station(record, station_resolver)
        slot, slot_problem = _unique_slot_alias(record)
        if station is None:
            errors.append(
                _error(
                    "missing_station_code",
                    pointer,
                    "Legacy slot record has no resolvable canonical station code",
                )
            )
            continue
        if slot is None:
            errors.append(
                _error(
                    slot_problem or "invalid_slot_number",
                    pointer,
                    "Legacy slot record has no positive integer slot number",
                    station_code=station,
                )
            )
            continue

        existing_identity, identity_problem = _optional_unique_text_alias(
            record, _IDENTITY_FIELDS, "material_identity_id"
        )
        if identity_problem is not None:
            errors.append(
                _error(
                    identity_problem,
                    pointer,
                    "Legacy slot record has an invalid or ambiguous material identity ID",
                    station_code=station,
                    slot_number=slot,
                )
            )
            continue
        if existing_identity is not None:
            resolved_records.append((record, index, existing_identity, station, slot))
            continue

        # Only actual source text can be verbatim identity evidence. Coercing
        # an arbitrary mapping/list to its Python repr would let a nested label
        # or unrelated value bootstrap a material identity.
        source_text = " ".join(
            record[field]
            for field in _SOURCE_FIELDS
            if isinstance(record.get(field), str)
        )
        source_matches = _verbatim_identity_matches(source_text, trusted)
        device_matches = use_candidates.get((station, slot), set())
        chosen: Optional[str] = None
        evidence = ""
        if len(source_matches) > 1:
            errors.append(
                _error(
                    "ambiguous_legacy_source_identity",
                    pointer,
                    "Legacy source field contains more than one trusted identity ID",
                    material_identity_ids=sorted(source_matches),
                    station_code=station,
                    slot_number=slot,
                )
            )
            continue
        if len(source_matches) == 1:
            chosen = next(iter(source_matches))
            if len(device_matches) == 1 and chosen not in device_matches:
                errors.append(
                    _error(
                        "legacy_identity_evidence_conflict",
                        pointer,
                        "Verbatim source identity conflicts with unique device-plan authorization",
                        source_material_identity_id=chosen,
                        device_material_identity_id=next(iter(device_matches)),
                        station_code=station,
                        slot_number=slot,
                    )
                )
                continue
            if device_matches and chosen not in device_matches:
                errors.append(
                    _error(
                        "legacy_identity_evidence_conflict",
                        pointer,
                        "Verbatim source identity is not among device-plan authorizations",
                        source_material_identity_id=chosen,
                        device_material_identity_ids=sorted(device_matches),
                        station_code=station,
                        slot_number=slot,
                    )
                )
                continue
            evidence = "verbatim_source_identity"
        elif len(device_matches) == 1 and next(iter(device_matches)) in trusted:
            chosen = next(iter(device_matches))
            evidence = "unique_device_plan_authorization"
        else:
            errors.append(
                _error(
                    "unresolved_legacy_slot_identity",
                    pointer,
                    "No unique stable identity evidence exists for this legacy slot",
                    device_material_identity_ids=sorted(device_matches),
                    station_code=station,
                    slot_number=slot,
                )
            )
            continue
        planned_identities.append(
            (record, index, chosen, evidence, station, slot)
        )
        resolved_records.append((record, index, chosen, station, slot))

    if errors:
        return ReagentSlotPlanEnrichmentResult(original, (), tuple(errors))

    events: list[dict[str, Any]] = []
    for record, index, identity_id, evidence, station, slot in planned_identities:
        record["material_identity_id"] = identity_id
        events.append(
            {
                "rule_id": RULE_ID,
                "event": "legacy_identity_enriched",
                "json_pointer": f"/reagent_slot_plan/{index}/material_identity_id",
                "old": None,
                "new": identity_id,
                "material_identity_id": identity_id,
                "station_code": station,
                "slot_number": slot,
                "evidence": evidence,
            }
        )

    display_aliases = (
        "canonical_name",
        "规范名称",
        "标准名称",
        "名称",
        "配料名称",
    )
    for record, index, identity_id, station, slot in resolved_records:
        canonical_name = canonical_names.get(identity_id)
        if canonical_name is None:
            continue
        fields = ["canonical_name"]
        fields.extend(
            field for field in display_aliases[1:] if field in record
        )
        for field in fields:
            old_value = record.get(field)
            if old_value == canonical_name:
                continue
            record[field] = canonical_name
            events.append(
                {
                    "rule_id": RULE_ID,
                    "event": "legacy_canonical_name_enriched",
                    "json_pointer": (
                        f"/reagent_slot_plan/{index}/{_pointer_token(field)}"
                    ),
                    "old": old_value,
                    "new": canonical_name,
                    "material_identity_id": identity_id,
                    "station_code": station,
                    "slot_number": slot,
                    "evidence": "root_material_registry",
                }
            )
    return ReagentSlotPlanEnrichmentResult(enriched, tuple(events), ())


def _build_bindings(
    plan: Any,
    station_resolver: StationResolver,
    errors: list[dict[str, Any]],
) -> dict[tuple[str, int], ReagentSlotBinding]:
    if not isinstance(plan, list):
        errors.append(
            _error(
                "invalid_reagent_slot_plan",
                "/reagent_slot_plan",
                "reagent_slot_plan must be a list",
            )
        )
        return {}
    bindings: dict[tuple[str, int], ReagentSlotBinding] = {}
    identity_names: dict[str, tuple[str, str]] = {}
    for index, record in enumerate(plan):
        pointer = f"/reagent_slot_plan/{index}"
        if not isinstance(record, Mapping):
            errors.append(
                _error("invalid_slot_record", pointer, "Slot record must be an object")
            )
            continue
        station = _resolve_record_station(record, station_resolver)
        slot, slot_problem = _unique_slot_alias(record)
        identity_id, identity_problem = _unique_text_alias(
            record, _IDENTITY_FIELDS, "material_identity_id"
        )
        canonical_name, canonical_problem = _unique_text_alias(
            record, _CANONICAL_NAME_FIELDS, "canonical_name"
        )
        valid = True
        if station is None:
            valid = False
            errors.append(
                _error(
                    "missing_station_code",
                    pointer,
                    "Slot record has no resolvable canonical station code",
                )
            )
        if slot is None:
            valid = False
            errors.append(
                _error(
                    slot_problem or "invalid_slot_number",
                    pointer,
                    "Slot record must have a positive integer slot number",
                )
            )
        if identity_id is None:
            valid = False
            errors.append(
                _error(
                    identity_problem or "missing_material_identity_id",
                    pointer,
                    "Authoritative slot record requires material_identity_id",
                )
            )
        if canonical_name is None:
            valid = False
            errors.append(
                _error(
                    canonical_problem or "missing_canonical_name",
                    pointer,
                    "Authoritative slot record requires canonical_name",
                )
            )
        if not valid:
            continue

        assert station is not None and slot is not None
        assert identity_id is not None and canonical_name is not None
        key = (station, slot)
        previous = bindings.get(key)
        candidate = ReagentSlotBinding(
            station, slot, identity_id, canonical_name, pointer
        )
        if previous is not None:
            code = (
                "duplicate_slot_binding"
                if (
                    previous.material_identity_id == identity_id
                    and previous.canonical_name == canonical_name
                )
                else "ambiguous_slot_binding"
            )
            errors.append(
                _error(
                    code,
                    pointer,
                    "Station-scoped slot is bound more than once",
                    station_code=station,
                    slot_number=slot,
                    material_identity_id=identity_id,
                    previous_material_identity_id=previous.material_identity_id,
                    previous_pointer=previous.pointer,
                )
            )
            continue
        bindings[key] = candidate

        prior_name = identity_names.get(identity_id)
        if prior_name is not None and prior_name[0] != canonical_name:
            errors.append(
                _error(
                    "identity_canonical_name_conflict",
                    pointer,
                    "One material identity ID has multiple canonical names",
                    material_identity_id=identity_id,
                    canonical_name=canonical_name,
                    previous_canonical_name=prior_name[0],
                    previous_pointer=prior_name[1],
                )
            )
        else:
            identity_names[identity_id] = (canonical_name, pointer)
    return bindings


def _collect_workflow_occurrences(
    workflow: Any,
    station_resolver: StationResolver,
    errors: list[dict[str, Any]],
) -> list[_Occurrence]:
    if not isinstance(workflow, dict):
        errors.append(
            _error("invalid_workflow", "/", "workflow_json must be an object")
        )
        return []
    steps = workflow.get("steps")
    if not isinstance(steps, list):
        errors.append(
            _error("invalid_workflow_steps", "/steps", "workflow steps must be a list")
        )
        return []
    found: list[_Occurrence] = []
    for index, step in enumerate(steps):
        step_pointer = f"/steps/{index}"
        if not isinstance(step, dict):
            errors.append(_error("invalid_workflow_step", step_pointer, "Step must be an object"))
            continue
        parameters = step.get("parameters")
        if not isinstance(parameters, dict):
            continue
        raw_occurrences = list(
            _iter_bottle_payloads(
                parameters, f"{step_pointer}/parameters", errors
            )
        )
        if not raw_occurrences:
            continue
        station = _resolve_record_station(step, station_resolver)
        if station is None:
            errors.append(
                _error(
                    "missing_workflow_station_code",
                    step_pointer,
                    "Workflow step containing source bottles has no canonical station code",
                )
            )
            continue
        source_plan_step, _ = _resolve_typed_id_aliases(
            step,
            _SOURCE_PLAN_STEP_FIELDS,
            step_pointer,
            errors,
            field_kind="source_plan_step",
        )
        for bottle, slot, pointer in raw_occurrences:
            found.append(_Occurrence(bottle, station, slot, pointer, source_plan_step))
    return found


def _iter_bottle_payloads(
    node: Any,
    pointer: str,
    errors: list[dict[str, Any]],
):
    if isinstance(node, dict):
        for key, value in node.items():
            child_pointer = f"{pointer}/{_pointer_token(str(key))}"
            match = _INSTANCE_BOTTLE_RE.fullmatch(str(key))
            if match:
                slot = _parse_slot(match.group(1))
                if not isinstance(value, dict):
                    errors.append(
                        _error(
                            "invalid_bottle_payload",
                            child_pointer,
                            "Instantiated source-bottle value must be an object",
                        )
                    )
                elif slot is not None:
                    internal_slot, internal_problem = _optional_unique_slot_alias(value)
                    if internal_problem is not None:
                        errors.append(
                            _error(
                                internal_problem,
                                child_pointer,
                                "Instantiated source-bottle has invalid or ambiguous internal slot metadata",
                            )
                        )
                    elif internal_slot is not None and internal_slot != slot:
                        errors.append(
                            _error(
                                "bottle_slot_conflict",
                                child_pointer,
                                "Instantiated source-bottle key conflicts with internal slot metadata",
                                slot_number=slot,
                                internal_slot_number=internal_slot,
                            )
                        )
                    else:
                        yield value, slot, child_pointer
                continue
            if key == "N号原液瓶":
                entries: Sequence[Any]
                if isinstance(value, list):
                    entries = value
                elif isinstance(value, dict):
                    entries = [value]
                else:
                    errors.append(
                        _error(
                            "invalid_generic_bottle_payload",
                            child_pointer,
                            "N号原液瓶 must be an object or list of objects",
                        )
                    )
                    continue
                for entry_index, entry in enumerate(entries):
                    entry_pointer = (
                        f"{child_pointer}/{entry_index}" if isinstance(value, list) else child_pointer
                    )
                    if not isinstance(entry, dict):
                        errors.append(
                            _error(
                                "invalid_generic_bottle_entry",
                                entry_pointer,
                                "Generic source-bottle entry must be an object",
                            )
                        )
                        continue
                    slot, slot_problem = _unique_slot_alias(entry)
                    if slot is None:
                        errors.append(
                            _error(
                                slot_problem or "invalid_slot_number",
                                entry_pointer,
                                "Generic source-bottle entry requires a positive bottle number",
                            )
                        )
                        continue
                    yield entry, slot, entry_pointer
                continue
            yield from _iter_bottle_payloads(value, child_pointer, errors)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _iter_bottle_payloads(item, f"{pointer}/{index}", errors)


def _build_plan_step_authorizations(
    plan_steps: Any, errors: list[dict[str, Any]]
) -> dict[MacroKey, Optional[frozenset[str]]]:
    if isinstance(plan_steps, Mapping) and isinstance(plan_steps.get("device_plan"), list):
        plan_steps = plan_steps["device_plan"]
    records: list[tuple[Any, Any]] = []
    if isinstance(plan_steps, Mapping):
        records = list(plan_steps.items())
    elif isinstance(plan_steps, list):
        records = [(None, item) for item in plan_steps]
    else:
        errors.append(
            _error(
                "invalid_plan_steps",
                "/device_plan",
                "plan_steps must be a list or mapping",
            )
        )
        return {}

    result: dict[MacroKey, Optional[frozenset[str]]] = {}
    for index, (mapping_key, record) in enumerate(records):
        pointer = f"/device_plan/{_pointer_token(str(mapping_key if mapping_key is not None else index))}"
        if not isinstance(record, Mapping):
            errors.append(_error("invalid_plan_step", pointer, "Plan step must be an object"))
            continue
        step_id, has_explicit_id = _resolve_typed_id_aliases(
            record,
            _PLAN_STEP_FIELDS,
            pointer,
            errors,
            field_kind="plan_step",
        )
        if step_id is None and not has_explicit_id and mapping_key is not None:
            try:
                step_id = normalize_macro_id(mapping_key, f"{pointer}/<mapping-key>")
            except MacroIdentityError as exc:
                errors.append(
                    _error(
                        "invalid_plan_step_id",
                        pointer,
                        "Plan-step mapping key must be a typed scalar ID",
                        plan_step_id=mapping_key,
                        identity_error=str(exc),
                    )
                )
        if step_id is None:
            if not has_explicit_id and mapping_key is None:
                errors.append(
                    _error("missing_plan_step_id", pointer, "Plan step requires a stable ID")
                )
            continue
        step_key = macro_id_key(step_id, f"{pointer}/plan_step")
        if step_key in result:
            errors.append(
                _error(
                    "duplicate_plan_step_id",
                    pointer,
                    "Plan step ID is duplicated",
                    source_plan_step=step_id,
                )
            )
            continue
        raw_ids, has_source_ids, source_aliases_ok = _resolve_identical_alias_values(
            record,
            _SOURCE_ID_FIELDS,
            pointer,
            errors,
            field_kind="source_material_identity_ids",
        )
        if not source_aliases_ok:
            result[step_key] = None
        elif not has_source_ids:
            result[step_key] = None
        elif not isinstance(raw_ids, list):
            errors.append(
                _error(
                    "invalid_source_material_identity_ids",
                    pointer,
                    "source_material_identity_ids must be a list",
                    source_plan_step=step_id,
                )
            )
            result[step_key] = None
        else:
            cleaned_values: list[str] = []
            invalid_values = False
            for value in raw_ids:
                if not isinstance(value, str) or not value.strip():
                    invalid_values = True
                    continue
                cleaned_values.append(value.strip())
            if invalid_values:
                errors.append(
                    _error(
                        "invalid_source_material_identity_id",
                        pointer,
                        "Every source material identity ID must be a non-empty string",
                        source_plan_step=step_id,
                    )
                )
                result[step_key] = None
            else:
                result[step_key] = frozenset(cleaned_values)
    return result


def _structured_identity_ids(value: Any) -> set[str]:
    """Collect IDs only from explicit fields in a structured registry shape."""

    if isinstance(value, list):
        identities: set[str] = set()
        for item in value:
            identities.update(_structured_identity_ids(item))
        return identities
    if not isinstance(value, Mapping):
        return set()

    identities = set()
    for field in (
        "identity_id",
        "material_identity_id",
        "research_material_identity_id",
        *_IDENTITY_FIELDS[1:],
    ):
        identity_id = _strict_string(value.get(field))
        if identity_id:
            identities.add(identity_id)
    for field in (
        "entries",
        "materials",
        "identities",
        "material_identity_registry",
    ):
        if field in value:
            identities.update(_structured_identity_ids(value[field]))
    return identities


def _explicit_root_batch_identity_ids(batch_plan: Any) -> set[str]:
    if isinstance(batch_plan, Mapping) and isinstance(batch_plan.get("batch_plan"), list):
        batch_plan = batch_plan["batch_plan"]
    if not isinstance(batch_plan, list):
        return set()
    identities: set[str] = set()
    for record in batch_plan:
        if isinstance(record, Mapping) and record.get("is_root_batch") is True:
            identities.update(_structured_identity_ids(record))
    return identities


def _root_identity_registry(
    batch_plan: Any, errors: list[dict[str, Any]]
) -> tuple[set[str], dict[str, str]]:
    if isinstance(batch_plan, Mapping) and isinstance(batch_plan.get("batch_plan"), list):
        batch_plan = batch_plan["batch_plan"]
    if not isinstance(batch_plan, list):
        return set(), {}
    identities: set[str] = set()
    names: dict[str, str] = {}
    name_pointers: dict[str, str] = {}
    for index, record in enumerate(batch_plan):
        if not isinstance(record, Mapping) or record.get("is_root_batch") is not True:
            continue
        pointer = f"/batch_plan/{index}"
        root_ids: list[str] = []
        for field in ("material_identity_id", "research_material_identity_id"):
            identity_id = _strict_string(record.get(field))
            if identity_id:
                identities.add(identity_id)
                root_ids.append(identity_id)
        raw_names = []
        for field in ("canonical_name", "material_id"):
            if field not in record:
                continue
            value = _strict_string(record.get(field))
            if value is None:
                errors.append(
                    _error(
                        "invalid_root_canonical_name",
                        f"{pointer}/{field}",
                        "Trusted root canonical name must be a non-empty string",
                    )
                )
            else:
                raw_names.append((field, value))
        unique_names = {value for _, value in raw_names}
        if len(unique_names) > 1:
            errors.append(
                _error(
                    "conflicting_root_canonical_names",
                    pointer,
                    "Root record canonical_name and material_id disagree",
                    canonical_names=sorted(unique_names),
                )
            )
            continue
        if not unique_names:
            continue
        canonical_name = next(iter(unique_names))
        for identity_id in root_ids:
            previous = names.get(identity_id)
            if previous is not None and previous != canonical_name:
                errors.append(
                    _error(
                        "conflicting_trusted_root_names",
                        pointer,
                        "Trusted root records disagree on canonical display name",
                        material_identity_id=identity_id,
                        canonical_name=canonical_name,
                        previous_canonical_name=previous,
                        previous_pointer=name_pointers[identity_id],
                    )
                )
            else:
                names[identity_id] = canonical_name
                name_pointers[identity_id] = pointer
    return identities, names


def _all_batch_identity_ids(batch_plan: Any) -> set[str]:
    if isinstance(batch_plan, Mapping) and isinstance(batch_plan.get("batch_plan"), list):
        batch_plan = batch_plan["batch_plan"]
    if not isinstance(batch_plan, list):
        return set()
    identities: set[str] = set()
    for record in batch_plan:
        if not isinstance(record, Mapping):
            continue
        for field in ("material_identity_id", "research_material_identity_id"):
            identity_id = _strict_string(record.get(field))
            if identity_id:
                identities.add(identity_id)
    return identities


def _device_plan_slot_candidates(
    device_plan: Any,
    station_resolver: StationResolver,
    trusted_root_identities: set[str],
    known_material_identities: set[str],
    errors: list[dict[str, Any]],
) -> dict[tuple[str, int], set[str]]:
    if device_plan is None:
        return {}
    if isinstance(device_plan, Mapping) and isinstance(device_plan.get("device_plan"), list):
        device_plan = device_plan["device_plan"]
    if not isinstance(device_plan, list):
        errors.append(
            _error(
                "invalid_device_plan",
                "/device_plan",
                "device_plan must be a list when supplied for legacy enrichment",
            )
        )
        return {}
    result: dict[tuple[str, int], set[str]] = {}
    for index, step in enumerate(device_plan):
        if not isinstance(step, Mapping):
            errors.append(
                _error(
                    "invalid_plan_step",
                    f"/device_plan/{index}",
                    "Device plan step must be an object",
                )
            )
            continue
        key_values = step.get("key_values", step.get("parameters", {}))
        slots = _slot_mentions(key_values)
        if not slots:
            continue
        station = _resolve_record_station(step, station_resolver)
        if station is None:
            errors.append(
                _error(
                    "unresolved_device_plan_station",
                    f"/device_plan/{index}",
                    "Device-plan slot evidence has no resolvable canonical station",
                    slot_numbers=sorted(slots),
                )
            )
            continue
        raw_ids, has_source_ids, source_aliases_ok = _resolve_identical_alias_values(
            step,
            _SOURCE_ID_FIELDS,
            f"/device_plan/{index}",
            errors,
            field_kind="source_material_identity_ids",
        )
        if not source_aliases_ok:
            continue
        if not has_source_ids or not isinstance(raw_ids, list):
            errors.append(
                _error(
                    "invalid_source_material_identity_ids",
                    f"/device_plan/{index}",
                    "Device-plan slot evidence requires a material identity ID list",
                    station_code=station,
                    slot_numbers=sorted(slots),
                )
            )
            continue
        authorized: set[str] = set()
        invalid_ids = False
        for value in raw_ids:
            identity_id = _strict_string(value)
            if identity_id is None:
                invalid_ids = True
            else:
                authorized.add(identity_id)
        if invalid_ids or not authorized:
            errors.append(
                _error(
                    "invalid_source_material_identity_id",
                    f"/device_plan/{index}",
                    "Every device-plan authorization must be a non-empty identity string",
                    station_code=station,
                    slot_numbers=sorted(slots),
                )
            )
            continue
        unknown = authorized - known_material_identities
        if unknown:
            errors.append(
                _error(
                    "unknown_device_material_identity_id",
                    f"/device_plan/{index}",
                    "Device-plan slot evidence contains identity IDs absent from the trusted material ledger",
                    material_identity_ids=sorted(unknown),
                    station_code=station,
                    slot_numbers=sorted(slots),
                )
            )
            continue
        authorized_roots = authorized & trusted_root_identities
        if not authorized_roots:
            continue
        for slot in slots:
            result.setdefault((station, slot), set()).update(authorized_roots)
    return result


def _slot_mentions(value: Any) -> set[int]:
    slots: set[int] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            match = _PLAN_BOTTLE_KEY_RE.fullmatch(str(key))
            if match:
                slot = _parse_slot(match.group(1))
                if slot is not None:
                    slots.add(slot)
            if key == "N号原液瓶":
                entries = child if isinstance(child, list) else [child]
                for entry in entries:
                    if isinstance(entry, Mapping):
                        slot, _ = _unique_slot_alias(entry)
                        if slot is not None:
                            slots.add(slot)
            slots.update(_slot_mentions(child))
    elif isinstance(value, list):
        for item in value:
            slots.update(_slot_mentions(item))
    elif isinstance(value, str):
        for match in _PLAN_BOTTLE_TEXT_RE.finditer(value):
            slot = _parse_slot(match.group(1))
            if slot is not None:
                slots.add(slot)
    return slots


def _verbatim_identity_matches(text: str, identities: set[str]) -> set[str]:
    if not text:
        return set()
    matches: set[str] = set()
    for identity_id in identities:
        # ``\w`` is Unicode-aware under Python's default regex semantics.  An
        # ASCII-only boundary lets a CJK identity such as ``水`` match inside
        # the longer word ``去离子水溶液``, which is not verbatim token evidence.
        pattern = rf"(?<!\w){re.escape(identity_id)}(?!\w)"
        if re.search(pattern, text):
            matches.add(identity_id)
    return matches


def _resolve_record_station(
    record: Mapping[str, Any], station_resolver: StationResolver
) -> Optional[str]:
    candidates: set[str] = set()
    for field in _STATION_CODE_FIELDS:
        code = _strict_string(record.get(field))
        if code:
            resolved = _call_station_resolver(station_resolver, code)
            candidates.add(resolved or code)
    for field in _STATION_FIELDS:
        label = _strict_string(record.get(field))
        if label:
            resolved = _call_station_resolver(station_resolver, label)
            if resolved:
                candidates.add(resolved)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _call_station_resolver(
    station_resolver: StationResolver, value: str
) -> Optional[str]:
    try:
        return _resolved_code(station_resolver(value))
    except Exception:
        return None


def _resolved_code(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return _clean_string(value)
    if isinstance(value, Mapping):
        return _clean_string(value.get("code") or value.get("station_code"))
    return _clean_string(getattr(value, "code", None) or getattr(value, "station_code", None))


def _parse_slot(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    text = _clean_string(value)
    if text and text.isdigit() and int(text) > 0:
        return int(text)
    return None


def _resolve_typed_id_aliases(
    record: Mapping[str, Any],
    fields: Sequence[str],
    pointer: str,
    errors: list[dict[str, Any]],
    *,
    field_kind: str,
) -> tuple[Optional[MacroId], bool]:
    """Resolve synonymous ID fields without truthiness or type coercion.

    Every explicitly present alias participates.  Equivalent aliases must have
    the same *typed* JSON scalar identity; ``1``, ``"1"`` and ``1.0`` are
    therefore different.  Invalid or conflicting aliases are blocking, and an
    explicit invalid value never falls back to a mapping key or list position.
    """

    present = [(field, record[field]) for field in fields if field in record]
    if not present:
        return None, False

    normalized: list[tuple[str, MacroId, MacroKey]] = []
    invalid = False
    for field, value in present:
        field_path = f"{pointer}/{_pointer_token(field)}"
        try:
            resolved = normalize_macro_id(value, field_path)
        except MacroIdentityError as exc:
            invalid = True
            errors.append(
                _error(
                    f"invalid_{field_kind}_id",
                    field_path,
                    f"{field_kind} must be a non-boolean typed scalar ID",
                    actual=value,
                    identity_error=str(exc),
                )
            )
            continue
        normalized.append((field, resolved, macro_id_key(resolved, field_path)))

    if invalid or not normalized:
        return None, True
    distinct_keys = {key for _, _, key in normalized}
    if len(distinct_keys) != 1:
        errors.append(
            _error(
                f"conflicting_{field_kind}_aliases",
                pointer,
                f"Explicit {field_kind} aliases must carry one identical typed ID",
                aliases={field: value for field, value, _ in normalized},
            )
        )
        return None, True
    return copy.deepcopy(normalized[0][1]), True


def _resolve_identical_alias_values(
    record: Mapping[str, Any],
    fields: Sequence[str],
    pointer: str,
    errors: list[dict[str, Any]],
    *,
    field_kind: str,
) -> tuple[Any, bool, bool]:
    """Resolve list-valued aliases only when every explicit value is identical."""

    present = [(field, record[field]) for field in fields if field in record]
    if not present:
        return None, False, True
    first_value = present[0][1]

    def normalized_string_set(value: Any) -> Optional[frozenset[str]]:
        if not isinstance(value, list):
            return None
        if any(not isinstance(item, str) or not item.strip() for item in value):
            return None
        return frozenset(item.strip() for item in value)

    first_set = normalized_string_set(first_value)
    conflicts = False
    for _, value in present[1:]:
        value_set = normalized_string_set(value)
        if first_set is not None and value_set is not None:
            if value_set != first_set:
                conflicts = True
                break
        elif value != first_value:
            conflicts = True
            break
    if conflicts:
        errors.append(
            _error(
                f"conflicting_{field_kind}_aliases",
                pointer,
                f"Explicit {field_kind} aliases must be structurally identical",
                aliases={field: value for field, value in present},
            )
        )
        return None, True, False
    return copy.deepcopy(first_value), True, True


def _first(record: Mapping[str, Any], fields: Sequence[str]) -> Any:
    for field in fields:
        if field in record:
            return record[field]
    return None


def _clean_string(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _strict_string(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _unique_text_alias(
    record: Mapping[str, Any], fields: Sequence[str], semantic_name: str
) -> tuple[Optional[str], Optional[str]]:
    values: set[str] = set()
    present = False
    invalid = False
    for field in fields:
        if field not in record:
            continue
        present = True
        value = _strict_string(record[field])
        if value is None:
            invalid = True
        else:
            values.add(value)
    if len(values) > 1:
        return None, f"ambiguous_{semantic_name}"
    if invalid:
        return None, f"invalid_{semantic_name}"
    if not present or not values:
        return None, f"missing_{semantic_name}"
    return next(iter(values)), None


def _optional_unique_text_alias(
    record: Mapping[str, Any], fields: Sequence[str], semantic_name: str
) -> tuple[Optional[str], Optional[str]]:
    if not any(field in record for field in fields):
        return None, None
    return _unique_text_alias(record, fields, semantic_name)


def _unique_slot_alias(
    record: Mapping[str, Any],
) -> tuple[Optional[int], Optional[str]]:
    slots: set[int] = set()
    present = False
    invalid = False
    for field in _SLOT_FIELDS:
        if field not in record:
            continue
        present = True
        slot = _parse_slot(record[field])
        if slot is None:
            invalid = True
        else:
            slots.add(slot)
    if len(slots) > 1:
        return None, "ambiguous_slot_number"
    if invalid:
        return None, "invalid_slot_number"
    if not present or not slots:
        return None, "missing_slot_number"
    return next(iter(slots)), None


def _optional_unique_slot_alias(
    record: Mapping[str, Any],
) -> tuple[Optional[int], Optional[str]]:
    if not any(field in record for field in _SLOT_FIELDS):
        return None, None
    return _unique_slot_alias(record)


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _error(code: str, pointer: str, message: str, **details: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "rule_id": RULE_ID,
        "code": code,
        "json_pointer": pointer,
        "message": message,
    }
    item.update(details)
    return item


__all__ = [
    "RULE_ID",
    "ReagentSlotBinding",
    "ReagentSlotIdentityRootResult",
    "ReagentSlotPlanEnrichmentResult",
    "ReagentSlotProjectionResult",
    "ReagentSlotRegistryResult",
    "ReagentSlotValidationResult",
    "build_reagent_slot_registry",
    "collect_material_identity_registry_evidence",
    "enrich_legacy_reagent_slot_plan",
    "project_reagent_slot_identities",
    "validate_reagent_slot_identity_roots",
    "validate_reagent_slot_identities",
]
