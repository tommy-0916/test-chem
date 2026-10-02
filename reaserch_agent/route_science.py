"""Run existing Research scientific gates against a proposed route graph.

This adapter does not validate a paper or upgrade provenance.  It only reports
which candidate fields can be tied to the typed material graph and which
existing Research checks passed.  Source authentication is a separate receipt.
"""

from __future__ import annotations

import re
import json
from typing import Any

from chem_agent_contracts.route_candidate import RouteCandidateV1, SourceLabelBindingV1
from chem_agent_contracts.route_field_basis import (
    is_material_port_state_path, state_source_locally_attributed,
    output_state_parent_role_issue,
    verify_controlled_state_mapping,
)
from chem_agent_contracts.route_convention_basis import (
    is_convention_inheritance_proof, output_quantity_role_issue,
    verify_bound_output_state,
)
from chem_agent_contracts.route_source_labels import (
    build_source_label_context, state_attribution_outcome,
)
from chem_agent_contracts.v2 import ScientificCompletenessV2, canonical_digest

from .state import ResearchAgentState, ResearchEvent
from .workflow import ResearchAgent


_INDEXED_NAME = re.compile(r"^([a-z][a-z0-9_]*)(?:\[(0|[1-9][0-9]*)\])?$")
_GRAPH_ROOT = re.compile(r"^material_graph\[(0|[1-9][0-9]*)\]$")
_PORT_FIELD = re.compile(
    r"^(material_graph\[(?:0|[1-9][0-9]*)\]\."
    r"material_(?:inputs|intermediates|outputs)\[(?:0|[1-9][0-9]*)\])\."
    r"(.+)$"
)


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


def _paper_field_bound_to_port(
    candidate: RouteCandidateV1,
    steps: list[dict[str, Any]],
    field: Any,
    owner_provenance: dict[str, Any],
    fields_by_path: dict[str, Any],
) -> bool:
    """Allow a separate paper fact for a port field anchored by its name fact.

    A port has one V2 provenance object, whereas its name, state and quantity
    can come from separate literal facts.  The port provenance anchors the
    entity name.  Each other fact still needs its own bound excerpt, scope and
    field-level verification before RouteDecision can admit it.
    """
    match = _PORT_FIELD.fullmatch(field.field_path)
    if match is None or match.group(2) == "name":
        return False
    name_path = match.group(1) + ".name"
    name_claim = fields_by_path.get(name_path)
    name_resolved = _graph_field(steps, name_path)
    if (
        name_claim is None or name_claim.status != "supported"
        or name_claim.provenance is None or name_resolved is None
        or not _same_value(name_claim.value, name_resolved[0])
        or name_claim.provenance.model_dump(mode="json", exclude_none=True)
        != owner_provenance
    ):
        return False
    provenance = field.provenance
    scope = field.source_scope
    route_scope = candidate.source_scope
    if (
        provenance is None or provenance.kind != "paper"
        or owner_provenance.get("kind") != "paper"
        or scope is None or route_scope is None
        or (scope.paper_id, scope.experimental_group_id, scope.source_digest)
        != (route_scope.paper_id, route_scope.experimental_group_id,
            route_scope.source_digest)
        or field.evidence_id != provenance.reference
    ):
        return False
    evidence = next(
        ((index, item) for index, item in enumerate(candidate.evidence_bundle)
         if item.evidence_id == provenance.reference),
        None,
    )
    if evidence is None:
        return False
    index, item = evidence
    return (
        provenance.source_path == f"evidence_bundle.items[{index}].excerpt"
        and bool(provenance.excerpt.strip())
        and provenance.excerpt in item.excerpt
        and provenance.source_digest == canonical_digest(item.excerpt)
    )


def _port_name_paper_anchor(
    candidate: RouteCandidateV1, steps: list[dict[str, Any]], field_path: str,
    owner_provenance: dict[str, Any], fields_by_path: dict[str, Any],
) -> bool:
    """A derived state may share a port whose entity name is paper backed."""
    match = _PORT_FIELD.fullmatch(field_path)
    if match is None or match.group(2) != "state":
        return False
    name_path = match.group(1) + ".name"
    name_field = fields_by_path.get(name_path)
    resolved = _graph_field(steps, name_path)
    scope = candidate.source_scope
    return bool(
        name_field is not None and name_field.status == "supported"
        and name_field.provenance is not None
        and name_field.provenance.kind == "paper"
        and name_field.evidence_id == name_field.provenance.reference
        and any(item.evidence_id == name_field.evidence_id
                for item in candidate.evidence_bundle)
        and scope is not None and name_field.source_scope is not None
        and (name_field.source_scope.paper_id,
             name_field.source_scope.experimental_group_id,
             name_field.source_scope.source_digest)
        == (scope.paper_id, scope.experimental_group_id, scope.source_digest)
        and resolved is not None
        and name_field.value == resolved[0]
        and name_field.provenance.model_dump(mode="json", exclude_none=True)
        == owner_provenance
    )


def _inventory_resolved_state_field(field: Any) -> bool:
    """An external-input state bound from an approved inventory record.

    Structural verification only; the resource bytes are re-verified at the
    source-audit layer.  The graph-state equality and controlled-vocabulary
    checks around this exemption still apply.
    """
    provenance = field.provenance
    if provenance is None or provenance.kind != "inventory":
        return False
    if provenance.evidence_class != "inventory_record":
        return False
    if not provenance.reference.strip() or not provenance.excerpt.strip():
        return False
    return re.fullmatch(r"sha256_[0-9a-f]{64}", provenance.source_digest) is not None


def _verified_inherited_state_field(
    candidate: RouteCandidateV1, steps: list[dict[str, Any]], field: Any,
    fields_by_path: dict[str, Any], proof: dict[str, Any], *,
    retained_object_resolver: Any = None,
) -> bool:
    """Bind an inherited input state to its separately verified parent output."""
    provenance = field.provenance
    scope = candidate.source_scope
    if (proof.get("field_path") != field.field_path
            or proof.get("target_state") != field.value
            or proof.get("rule_id") != provenance.inference_rule
            or provenance.reference != proof.get("parent_evidence_id")
            or field.controlled_mapping is not None
            or field.unit != ""):
        return False
    parent_field = fields_by_path.get(proof.get("parent_state_path"))
    if (parent_field is None or parent_field.status != "supported"
            or parent_field.provenance is None
            or parent_field.provenance.kind != "paper"
            or parent_field.provenance.evidence_class != "paper_explicit"
            or parent_field.evidence_id != proof.get("parent_evidence_id")
            or parent_field.provenance.reference != parent_field.evidence_id
            or parent_field.source_scope is None
            or (parent_field.source_scope.paper_id,
                parent_field.source_scope.experimental_group_id,
                parent_field.source_scope.source_digest)
            != (scope.paper_id, scope.experimental_group_id, scope.source_digest)):
        return False
    if parent_field.value != proof.get("parent_source_value"):
        return False
    resolved_parent = _graph_field(steps, proof["parent_state_path"])
    if resolved_parent is None or verify_controlled_state_mapping(
        proof["parent_state_path"], parent_field.value, resolved_parent[0],
        parent_field.controlled_mapping.model_dump(mode="json")
        if parent_field.controlled_mapping is not None else None,
    ):
        return False
    evidence_by_id = {item.evidence_id: item for item in candidate.evidence_bundle}
    return not verify_bound_output_state(
        proof, steps, evidence_by_id,
        paper_id=scope.paper_id,
        experimental_group_id=scope.experimental_group_id,
        source_digest=scope.source_digest,
        retained_object_resolver=retained_object_resolver,
    )


def _verified_convention_state_field(
    candidate: RouteCandidateV1, steps: list[dict[str, Any]], field: Any,
    fields_by_path: dict[str, Any], *,
    retained_object_resolver: Any = None,
) -> bool:
    """Bind a derived state to exact, separately source-verified paper fields."""
    provenance = field.provenance
    scope = candidate.source_scope
    if (provenance is None or provenance.kind != "agent_inferred"
            or provenance.evidence_class != "chemistry_convention"
            or field.evidence_id or scope is None):
        return False
    try:
        proof = json.loads(provenance.derivation)
    except (TypeError, ValueError):
        return False
    if (not isinstance(proof, dict)
            or proof.get("schema_version") != "route-convention-state/v1"):
        return False
    if is_convention_inheritance_proof(proof):
        return _verified_inherited_state_field(
            candidate, steps, field, fields_by_path, proof,
            retained_object_resolver=retained_object_resolver,
        )
    if (proof.get("field_path") != field.field_path
            or proof.get("target_state") != field.value
            or proof.get("rule_id") != provenance.inference_rule
            or provenance.reference != proof.get("operation_evidence_id")
            or field.controlled_mapping is not None
            or field.unit != ""):
        return False
    for path_key, evidence_key in (
        ("parent_state_path", "parent_evidence_id"),
        ("operation_path", "operation_evidence_id"),
    ):
        support = fields_by_path.get(proof.get(path_key))
        if (support is None or support.status != "supported"
                or support.provenance is None
                or support.provenance.kind != "paper"
                or support.provenance.evidence_class != "paper_explicit"
                or support.evidence_id != proof.get(evidence_key)
                or support.provenance.reference != support.evidence_id
                or support.source_scope is None
                or (support.source_scope.paper_id,
                    support.source_scope.experimental_group_id,
                    support.source_scope.source_digest)
                != (scope.paper_id, scope.experimental_group_id, scope.source_digest)):
            return False
    parent_field = fields_by_path[proof["parent_state_path"]]
    if parent_field.value != proof.get("parent_source_value"):
        return False
    resolved_parent = _graph_field(steps, proof["parent_state_path"])
    if resolved_parent is None or verify_controlled_state_mapping(
        proof["parent_state_path"], parent_field.value, resolved_parent[0],
        parent_field.controlled_mapping.model_dump(mode="json")
        if parent_field.controlled_mapping is not None else None,
    ):
        return False
    operation_field = fields_by_path[proof["operation_path"]]
    resolved_operation = _graph_field(steps, proof["operation_path"])
    if (resolved_operation is None
            or operation_field.value != resolved_operation[0]):
        return False
    evidence_by_id = {item.evidence_id: item for item in candidate.evidence_bundle}
    return not verify_bound_output_state(
        proof, steps, evidence_by_id,
        paper_id=scope.paper_id,
        experimental_group_id=scope.experimental_group_id,
        source_digest=scope.source_digest,
        retained_object_resolver=retained_object_resolver,
    )


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
    retained_object_resolver: Any = None,
) -> dict[str, Any]:
    """Return a trusted, fail-closed science fragment for RouteValidationReceiptV1.

    Every Phase 1–3 method operates on a copy of the candidate's MacroStepV2
    graph.  The adapter never treats a field's own ``supported`` claim or a
    nearby measurement step as verification of its source or runtime path.

    ``retained_object_resolver`` stays ``None`` at every current call site:
    the compiled candidate retains only hashed evidence ids, not the raw
    route-fact ids a live retained-object record binds, so no resolver can
    be rebuilt at this layer and retained-object proofs remain rejected
    here (fail-closed).
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
    fields_by_path = {field.field_path: field for field in candidate.evidence_matrix}
    label_context = build_source_label_context(
        candidate.material_graph, candidate.evidence_matrix,
    )
    for field in candidate.evidence_matrix:
        if field.status == "runtime_pending" and field.required:
            # Completeness audits scheduled material returns. The V2 route
            # graph still has no verified binding from a return to this exact
            # evidence-matrix field, so this adapter cannot certify it.
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
        field_value = field.value
        convention_state_verified = False
        if is_material_port_state_path(field.field_path):
            if field.provenance.kind == "agent_inferred":
                convention_state_verified = _verified_convention_state_field(
                    candidate, steps, field, fields_by_path,
                    retained_object_resolver=retained_object_resolver,
                )
                if not convention_state_verified:
                    issues.append(
                        f"evidence_matrix_convention_state_invalid:{field.field_path}"
                    )
                    continue
            else:
                if (field.provenance.kind == "paper"
                        and output_state_parent_role_issue(
                            field.field_path, steps, field.value,
                            field.provenance.excerpt,
                        )):
                    issues.append(
                        f"evidence_matrix_parent_state_reused_for_output:{field.field_path}"
                    )
                    continue
                mapping = field.controlled_mapping
                issue = verify_controlled_state_mapping(
                    field.field_path, field.value, value,
                    mapping.model_dump(mode="json") if mapping is not None else None,
                )
                if issue:
                    issues.append(
                        f"evidence_matrix_controlled_mapping_invalid:{field.field_path}"
                    )
                    continue
                if field.provenance.kind == "paper":
                    outcome, binding = state_attribution_outcome(
                        field.value, field.provenance.excerpt, field.field_path,
                        candidate.material_graph, label_context,
                    )
                    if outcome == "pending":
                        # Ambiguous attribution is pending, not an audited fact.
                        # An engaged association failure never reopens through
                        # the legacy matcher at this independent audit boundary.
                        continue
                    if field.source_label_binding is not None:
                        expected_binding = (
                            SourceLabelBindingV1.model_validate(binding).model_dump(
                                mode="json", exclude_none=True,
                            ) if binding is not None else None
                        )
                        if field.source_label_binding.model_dump(
                            mode="json", exclude_none=True,
                        ) != expected_binding:
                            issues.append(
                                "evidence_matrix_source_label_binding_mismatch:"
                                + field.field_path
                            )
                            continue
                    if outcome == "legacy":
                        name = _graph_field(
                            steps, field.field_path.rsplit(".", 1)[0] + ".name",
                        )
                        if (name is None or not state_source_locally_attributed(
                            field.value, field.provenance.excerpt, name[0],
                        )):
                            continue
                if mapping is not None:
                    field_value = mapping.target_value
        claimed_provenance = field.provenance.model_dump(mode="json", exclude_none=True)
        if not _same_value(value, field_value) or (
            owner_provenance != claimed_provenance
            and not (convention_state_verified and _port_name_paper_anchor(
                candidate, steps, field.field_path,
                owner_provenance, fields_by_path,
            ))
            and not _paper_field_bound_to_port(
                candidate, steps, field, owner_provenance, fields_by_path,
            )
            and not _inventory_resolved_state_field(field)
        ):
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
                is_convention_state
                and field.provenance.evidence_class == "chemistry_convention"
                and (
                    (convention_state_verified and not source_pending)
                    or (graph_verified and not field.provenance.derivation
                        and (step_index, field.provenance.inference_rule) in applied_rules)
                )
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
        if output_quantity_role_issue(path, steps, unit):
            issues.append(f"numeric_graph_quantity_role_mismatch:{path}")
            result["audited_field_paths"] = [
                audited for audited in result["audited_field_paths"]
                if audited != path
            ]
            continue
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
