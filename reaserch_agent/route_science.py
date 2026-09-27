"""Run existing Research scientific gates against a proposed route graph.

This adapter does not validate a paper or upgrade provenance.  It only reports
which candidate fields can be tied to the typed material graph and which
existing Research checks passed.  Source authentication is a separate receipt.
"""

from __future__ import annotations

import re
from typing import Any

from chem_agent_contracts.route_candidate import RouteCandidateV1
from chem_agent_contracts.v2 import ScientificCompletenessV2, canonical_digest

from .state import ResearchAgentState, ResearchEvent
from .workflow import ResearchAgent


_INDEXED_NAME = re.compile(r"^([a-z][a-z0-9_]*)(?:\[(0|[1-9][0-9]*)\])?$")
_GRAPH_ROOT = re.compile(r"^material_graph\[(0|[1-9][0-9]*)\]$")


def _signature_field(candidate: RouteCandidateV1, field_path: str) -> Any | None:
    """Resolve a signature claim without treating the claim as source proof."""

    chunks = field_path.split(".")
    if len(chunks) < 2 or chunks[0] != "route_signature":
        return None
    node: Any = candidate.route_signature.model_dump(mode="json")
    for chunk in chunks[1:]:
        match = _INDEXED_NAME.fullmatch(chunk)
        if match is None or not isinstance(node, dict):
            return None
        node = node.get(match.group(1))
        if node is None:
            return None
        if match.group(2) is not None:
            index = int(match.group(2))
            if not isinstance(node, list) or index >= len(node):
                return None
            node = node[index]
    return node


def _graph_field(
    steps: list[dict[str, Any]], field_path: str
) -> tuple[Any, dict[str, Any], int] | None:
    """Resolve only explicit indexed graph paths, retaining the fact owner."""
    chunks = field_path.split(".")
    root = _GRAPH_ROOT.fullmatch(chunks[0]) if chunks else None
    if root is None or len(chunks) < 2:
        return None
    step_index = int(root.group(1))
    if step_index >= len(steps):
        return None
    node: Any = steps[step_index]
    owner: dict[str, Any] = node
    for chunk in chunks[1:]:
        match = _INDEXED_NAME.fullmatch(chunk)
        if match is None or match.group(1) == "provenance" or not isinstance(node, dict):
            return None
        key = match.group(1)
        if key not in node:
            return None
        node = node[key]
        if match.group(2) is not None:
            index = int(match.group(2))
            if not isinstance(node, list) or index >= len(node):
                return None
            node = node[index]
        if isinstance(node, dict) and isinstance(node.get("provenance"), dict):
            owner = node
    provenance = owner.get("provenance")
    if not isinstance(provenance, dict):
        return None
    return node, provenance, step_index


def _same_value(actual: Any, claimed: Any) -> bool:
    # bool compares equal to 0/1 in Python; a scientific quantity must not.
    if isinstance(actual, bool) or isinstance(claimed, bool):
        return isinstance(actual, bool) and isinstance(claimed, bool) and actual is claimed
    return actual == claimed


def _numeric_graph_fields(steps: list[dict[str, Any]]) -> list[tuple[str, Any, str]]:
    """Enumerate science-bearing numeric leaves, including free-form V2 fields.

    Only a step's ordinal ``sequence`` is structural metadata.  In particular,
    containers, concentrations, allocations and quantity_requirements are not
    exempt from evidence merely because their values are nested below a
    material relation or a free-form dict.
    """
    found: list[tuple[str, Any, str]] = []

    def walk(value: Any, path: str, unit: str = "") -> None:
        if isinstance(value, dict):
            next_unit = value.get("unit") if isinstance(value.get("unit"), str) else unit
            for key, child in value.items():
                if key == "provenance" or (
                    key == "sequence" and re.fullmatch(r"material_graph\[[0-9]+\]", path)
                ):
                    continue
                child_unit = (
                    str(value.get("concentration_unit") or "")
                    if key == "concentration_value" else next_unit
                )
                walk(child, f"{path}.{key}", child_unit)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]", unit)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            found.append((path, value, unit))
        elif (
            isinstance(value, str) and re.search(r"\d", value)
            and re.search(r"(?:\.value|_value)(?:\[[0-9]+\])?$", path)
        ):
            # ScientificParameterV2.value is intentionally Any, so a number
            # serialized as "20" must not evade the same coverage rule.
            found.append((path, value, unit))

    for index, step in enumerate(steps):
        walk(step, f"material_graph[{index}]")
    return found


def _provenance_records(steps: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Enumerate the same claim-bearing positions as the V2 package gate."""
    records: list[tuple[str, dict[str, Any]]] = []
    for step_index, step in enumerate(steps):
        claims: list[tuple[str, Any]] = [("provenance", step.get("provenance"))]
        for collection in (
            "parameters", "quantity_requirements", "material_inputs",
            "material_intermediates", "material_outputs", "material_relations",
            "operation_segments", "material_applicability",
        ):
            for item_index, item in enumerate(step.get(collection, []) or []):
                if isinstance(item, dict):
                    claims.append((
                        f"{collection}[{item_index}].provenance",
                        item.get("provenance"),
                    ))
        records.extend(
            (f"material_graph[{step_index}].{location}", provenance)
            for location, provenance in claims
            if isinstance(provenance, dict)
        )
    return records


def _paper_scalar_issues(
    records: list[tuple[str, dict[str, Any]]], candidate: RouteCandidateV1
) -> list[str]:
    """Check the excerpt digest contract, including parameter provenance.

    Research's material provenance helper does not currently visit parameter
    provenance; the V2 package gate does.  Keep this small check aligned with
    the package scalar rule without treating a document digest as an excerpt
    digest or independently authenticating the source document.
    """
    items = {item.evidence_id: (index, item)
             for index, item in enumerate(candidate.evidence_bundle)}
    issues: list[str] = []
    for location, provenance in records:
        if provenance.get("kind") != "paper":
            continue
        binding = items.get(str(provenance.get("reference") or ""))
        if binding is None:
            issues.append(f"paper_evidence_reference_unbound:{location}")
            continue
        index, item = binding
        if provenance.get("source_path") != f"evidence_bundle.items[{index}].excerpt":
            issues.append(f"paper_excerpt_source_path_mismatch:{location}")
        excerpt = str(provenance.get("excerpt") or "")
        if not excerpt or excerpt not in item.excerpt:
            issues.append(f"paper_excerpt_unbound:{location}")
        if provenance.get("source_digest") != canonical_digest(item.excerpt):
            issues.append(f"paper_excerpt_digest_mismatch:{location}")
    return issues


def _provenance_class_issues(
    records: list[tuple[str, dict[str, Any]]]
) -> list[str]:
    """Do not let a source kind self-assign a stronger evidence class."""
    expected = {
        "paper": "paper_explicit",
        "device_skill": "device_sop",
        "runtime": "runtime_measurement",
        "agent_inferred": "chemistry_convention",
        "user": None,
        "manual_revision": None,
    }
    issues: list[str] = []
    for location, provenance in records:
        kind = provenance.get("kind")
        evidence_class = provenance.get("evidence_class")
        required = expected.get(kind)
        if evidence_class is not None and evidence_class != required:
            issues.append(f"provenance_evidence_class_mismatch:{location}")
    return issues


def audit_route_candidate_science(
    candidate: RouteCandidateV1,
    *,
    agent: ResearchAgent | None = None,
) -> dict[str, Any]:
    """Return a trusted, fail-closed science fragment for RouteValidationReceiptV1.

    Every Phase 1–3 method operates on a copy of the candidate's MacroStepV2
    graph.  The adapter never treats a field's own ``supported`` claim or a
    nearby measurement step as verification of its source or runtime path.
    """
    result: dict[str, Any] = {
        "scientific_completeness": None,
        "scientific_gate_issues": [],
        "audited_field_paths": [],
        "verified_graph_step_ids": [],
        "verified_runtime_resolution_fields": [],
        "verified_convention_field_paths": [],
    }
    if not isinstance(candidate, RouteCandidateV1):
        result["scientific_gate_issues"].append("route_candidate_invalid")
        return result
    if not candidate.material_graph:
        return result

    evaluator = agent or ResearchAgent(
        model=object(), use_llm=False, enable_memory=False,
        enable_online_literature=False, enable_web_search=False,
        contract_version="v2",
    )
    if evaluator._contract_version != "v2":
        return result

    # Pydantic has already validated every MacroStepV2.  Work on JSON copies:
    # convention expansion annotates the plan, never the candidate itself.
    steps = [step.model_dump(mode="json", exclude_none=True) for step in candidate.material_graph]
    issues: list[str] = []
    step_ids = [step.macro_step_id for step in candidate.material_graph]
    if len(set(step_ids)) != len(step_ids):
        issues.append("material_graph_duplicate_step_id")
    if [step.sequence for step in candidate.material_graph] != list(range(1, len(steps) + 1)):
        issues.append("material_graph_sequence_invalid")
    if len({step.macro_action_id for step in candidate.material_graph}) != 1:
        issues.append("material_graph_macro_action_mismatch")
    graph_operations = [step.operation for step in candidate.material_graph]
    signature_operations = candidate.route_signature.operations
    composite_unmapped = any(
        len({segment.source_operation_ref for segment in step.operation_segments}) > 1
        for step in candidate.material_graph
    )
    operation_mapping_pending = composite_unmapped or not signature_operations
    operation_mismatch = graph_operations != signature_operations
    if operation_mismatch and not operation_mapping_pending:
        issues.append("route_signature_graph_operations_mismatch")
    structure_pending = (
        not steps[0].get("material_inputs")
        or not steps[-1].get("material_outputs")
        or not candidate.evidence_matrix
    )
    records = _provenance_records(steps)
    issues.extend(_provenance_class_issues(records))
    paper_records = [value for _location, value in records if value.get("kind") == "paper"]
    paper_ids = {str(value.get("reference") or "") for value in paper_records}
    paper_ids.update(
        field.provenance.reference
        for field in candidate.evidence_matrix
        if field.provenance is not None and field.provenance.kind == "paper"
    )
    bundle_by_id = {item.evidence_id: item for item in candidate.evidence_bundle}
    source_pending = (
        not candidate.evidence_bundle
        or any(value.get("kind") == "user" for _location, value in records)
        or any(field.provenance is not None and field.provenance.kind == "user"
               for field in candidate.evidence_matrix)
        or any(
            paper_id not in bundle_by_id
            or bundle_by_id[paper_id].verification_status
            not in evaluator._evidence_identity_statuses()
            or bundle_by_id[paper_id].full_text_status not in {"parsed", "local_parsed"}
            for paper_id in paper_ids
        )
    )

    try:
        expansion = evaluator._expand_chemistry_conventions(steps)
        bare_issues = evaluator._v2_bare_agent_inferred_issues(steps)
        graph_issues = evaluator._v2_material_graph_issues(steps, expansion)
        contract_issues: list[str] = []
        for index, step in enumerate(steps, start=1):
            # The historical Research quality checker calls these V2 logical
            # containers ``container_requirements``.  This is an exact alias
            # of the typed MacroStepV2 field, not a newly inferred container.
            quality_step = {**step, "container_requirements": step.get("logical_containers", [])}
            contract_issues.extend(evaluator._v2_material_contract_issues(
                index, quality_step,
                allowed_device_capability_ids=set(candidate.required_capabilities),
            ))
            contract_issues.extend(evaluator._v2_quantity_requirement_issues(index, quality_step))
            contract_issues.extend(evaluator._v2_macro_step_quality_issues(index, quality_step))
        provenance_issues: list[str] = []
        if not source_pending:
            # The state is scoped to this candidate only.  This checks
            # Research's existing publication provenance rule; trusted source
            # authenticity remains the independent SourceVerifier's job.
            state = ResearchAgentState(
                event=ResearchEvent(event_type="route_candidate", query=""),
                contract_version="v2",
                current_evidence_bundle={
                    "query": "",
                    "results": [
                        {**item.model_dump(mode="json", exclude_none=True),
                         "evidence_excerpt": item.excerpt}
                        for item in candidate.evidence_bundle
                    ],
                },
            )
            for index, step in enumerate(steps, start=1):
                provenance_issues.extend(
                    evaluator._v2_material_provenance_issues(index, step, state)
                )
            provenance_issues.extend(_paper_scalar_issues(records, candidate))
        audit = ScientificCompletenessV2.model_validate(
            evaluator._scientific_completeness_audit(steps, graph_issues)
        )
    except (TypeError, ValueError, KeyError, AttributeError):
        # Evaluation failure is missing verification, not evidence that this
        # candidate violates a scientific rule.  The selector leaves it
        # unresolved when the completeness and graph receipts are absent.
        result["scientific_gate_issues"] = list(dict.fromkeys(issues))
        return result

    if not source_pending:
        result["scientific_completeness"] = audit
    issues.extend(f"bare_agent_inferred:{item}" for item in bare_issues)
    issues.extend(f"material_graph:{item}" for item in graph_issues)
    issues.extend(f"material_contract:{item}" for item in contract_issues)
    issues.extend(f"material_provenance:{item}" for item in provenance_issues)
    for key in (
        "required_unsupported", "unresolved_runtime_dependency",
        "scientific_fidelity_risk_requiring_review",
    ):
        if audit.counts.get(key, 0):
            issues.append(f"scientific_completeness:{key}={audit.counts[key]}")
    if audit.broken_material_lineage:
        issues.append(f"scientific_completeness:broken_material_lineage={audit.broken_material_lineage}")

    graph_verified = (
        not source_pending and not structure_pending
        and not operation_mapping_pending and not operation_mismatch
        and not graph_issues and not contract_issues and not provenance_issues
        and not any(issue.startswith("material_graph_") for issue in issues)
    )
    if graph_verified:
        result["verified_graph_step_ids"] = step_ids

    applied_rules = {
        (record.get("step_index"), record.get("rule_id"))
        for record in expansion
    }
    for field in candidate.evidence_matrix:
        if field.status == "runtime_pending" and field.required:
            # Existing Phase 3 searches for any earlier measurement.  The V2
            # route graph does not yet link that measurement to this exact
            # evidence-matrix field, so no runtime field can be certified here.
            continue
        if field.status != "supported":
            # RouteDecision maps unsupported to rejection and unknown to
            # unresolved.  This adapter must not flatten those meanings.
            continue
        if field.field_path.startswith("route_signature."):
            signature_value = _signature_field(candidate, field.field_path)
            if signature_value is None or field.provenance is None:
                continue
            if not _same_value(signature_value, field.value):
                issues.append(f"evidence_matrix_signature_mismatch:{field.field_path}")
                continue
            # SourceVerifier independently checks this literal quote against
            # the original experimental group.  This adapter only confirms
            # that the evidence-matrix claim equals the proposed signature.
            if not source_pending:
                result["audited_field_paths"].append(field.field_path)
            continue
        resolved = _graph_field(steps, field.field_path)
        if resolved is None or field.provenance is None:
            continue
        value, owner_provenance, step_index = resolved
        claimed_provenance = field.provenance.model_dump(mode="json", exclude_none=True)
        if not _same_value(value, field.value) or owner_provenance != claimed_provenance:
            issues.append(f"evidence_matrix_graph_mismatch:{field.field_path}")
            continue
        if not source_pending:
            result["audited_field_paths"].append(field.field_path)
        if field.provenance.kind == "agent_inferred":
            is_convention_state = bool(re.fullmatch(
                r"material_graph\[[0-9]+\]\.material_outputs\[[0-9]+\]\.state",
                field.field_path,
            ))
            verified = (
                graph_verified and is_convention_state
                and field.provenance.evidence_class == "chemistry_convention"
                and (step_index, field.provenance.inference_rule) in applied_rules
            )
            if verified:
                result["verified_convention_field_paths"].append(field.field_path)

    # A graph can otherwise smuggle a new number through a valid-looking step
    # provenance while the evidence_matrix audits only one unrelated fact.
    # Missing coverage is unresolved; an explicit contradictory matrix claim
    # is a known integrity failure.  Both prevent RouteDecision admission.
    field_by_path = {field.field_path: field for field in candidate.evidence_matrix}
    audited_paths = set(result["audited_field_paths"])
    numeric_pending = False
    for path, _value, unit in _numeric_graph_fields(steps):
        field = field_by_path.get(path)
        if field is None:
            numeric_pending = True
            continue
        if field.status != "supported":
            issues.append(f"numeric_graph_field_not_supported:{path}")
            continue
        if path not in audited_paths:
            if field.provenance is None or source_pending:
                numeric_pending = True
            # A supplied but mismatched value or owner was already recorded
            # above as evidence_matrix_graph_mismatch.
            continue
        if unit and field.unit != unit:
            issues.append(f"numeric_graph_unit_mismatch:{path}")
            result["audited_field_paths"].remove(path)
    if numeric_pending:
        result["scientific_completeness"] = None
        result["verified_graph_step_ids"] = []
    if operation_mapping_pending:
        result["scientific_completeness"] = None
        result["verified_graph_step_ids"] = []

    result["scientific_gate_issues"] = list(dict.fromkeys(issues))
    return result


__all__ = ["audit_route_candidate_science"]
