"""Produce unsigned literal-fact receipts before independent route review.

The caller must obtain the group inventory from signed PDF enumeration. This
module rechecks only the association's scope and literal excerpts against that
inventory. It neither authenticates PDF acquisition nor decides group roles,
chemical equivalence, capability completeness, or execution authorization.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import re
from typing import Any

from chem_agent_contracts.route_field_basis import (
    controlled_state_mapping, is_material_port_state_path,
    output_state_parent_role_issue, state_source_locally_attributed,
)
from chem_agent_contracts.route_convention_basis import (
    convention_fact_evidence_by_id, derive_unreviewed_input_state,
    derive_unreviewed_output_state, verify_bound_output_state,
)
from chem_agent_contracts.route_retained_object import (
    build_retained_object_resolver,
)
from chem_agent_contracts.route_source_labels import (
    SOURCE_LABEL_RULE_VERSION, RULE_SCOPED_LABEL_IDENTITY, SourceLabelContext,
    build_source_label_context, competing_quantity_identity_surfaces,
    definition_site_concentration_binding,
    quantity_identity_surfaces, state_attribution_outcome, surface_anchor,
)

from .route_group_compiler import (
    _port_material_for_path, _scoped_claim, classify_route_field_basis,
    dimensionless_labeled_value_verified, dimensionless_numeric_field_pending,
    literal_quantity_present, material_identity_for_amount_path,
    output_quantity_role_issue,
    quantity_has_local_attribution,
)
from .route_state_proof_dag import (
    STATE_PROOF_DAG_DERIVATION, build_verified_state_proof_dags,
    dag_entry_derivation, is_input_state_path,
)
from .route_pdf_group_proposals import PdfGroupProposalAssociationResultV1
from .route_pdf_groups import PdfExperimentalGroupV1
from .route_pdf_quote_binding import (
    bind_pdf_quote, normalize_pdf_quote_whitespace,
)
from .route_pdf_verification_context import verification_context_reason


_DIGEST = re.compile(r"sha256_[0-9a-f]{64}\Z")
_SOURCE_KEYS = frozenset({"source_document", "section", "locator", "source_digest"})
_FACT_SOURCE_KEYS = frozenset({
    "paper_id", "experimental_group_id", "section", "locator", "source_digest",
})
_PROTOCOL_KEYS = frozenset({
    "paper_id", "experimental_group_id", "group_role", "role_hint", "source",
    "route_facts", "target", "route_signature", "material_graph",
    "required_capabilities",
})
_FACT_KEYS = frozenset({
    "fact_id", "field_path", "value", "unit", "excerpt", "required", "source",
    "verification_excerpt",
})


@dataclass(frozen=True)
class PdfGroupFactReceiptBudgetV1:
    max_groups: int = 32
    max_blocks: int = 1024
    max_facts: int = 1024
    max_total_chars: int = 300_000
    max_proposal_chars: int = 300_000

    def __post_init__(self) -> None:
        for name in (
            "max_groups", "max_blocks", "max_facts", "max_total_chars",
            "max_proposal_chars",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class PdfGroupLiteralStatusV1:
    paper_id: str
    experimental_group_id: str
    source_digest: str
    group_locator: str
    role_hint: str
    status: str
    verified_field_paths: tuple[str, ...]
    reason_codes: tuple[str, ...]
    derived_state_field_paths: tuple[str, ...] = ()
    # G1: state paths accepted on a dual-verified state-proof-dag/v1
    # multi-hop proof, kept distinct from flat single-hop derivations.
    dag_proven_state_field_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class PdfGroupFactReceiptV1:
    schema_version: str
    verification_mode: str
    method: str
    status: str
    reason_codes: tuple[str, ...]
    group_results: tuple[PdfGroupLiteralStatusV1, ...]
    review_work_orders: tuple[dict[str, Any], ...]


def _work_order(
    group: PdfExperimentalGroupV1, *, role_hint: str, status: str,
    field_paths: Sequence[str], reasons: Sequence[str],
) -> dict[str, Any]:
    scope = group.source_scope
    return {
        "schema_version": "pdf_group_independent_review_work_order_v1",
        "paper_id": scope.paper_id,
        "experimental_group_id": scope.experimental_group_id,
        "section": scope.section,
        "group_locator": scope.locator,
        "source_digest": scope.source_digest,
        "role_hint_untrusted": role_hint,
        "proposed_field_paths": list(field_paths),
        "literal_check_status": status,
        "literal_reason_codes": list(reasons),
        "pending_checks": [
            "trusted_group_role_assignment",
            "trusted_capability_mapping_if_route_group",
            "independent_chemical_semantics_review",
            "execution_authorization_separate",
        ],
        "human_chemical_review_completed": False,
        "execution_authorized": False,
    }


def _blocked(
    reason: str, groups: Sequence[PdfExperimentalGroupV1] = (),
) -> PdfGroupFactReceiptV1:
    group_results = tuple(PdfGroupLiteralStatusV1(
        paper_id=group.source_scope.paper_id,
        experimental_group_id=group.source_scope.experimental_group_id,
        source_digest=group.source_scope.source_digest,
        group_locator=group.source_scope.locator,
        role_hint="", status="blocked", verified_field_paths=(),
        reason_codes=(reason,),
    ) for group in groups)
    return PdfGroupFactReceiptV1(
        schema_version="pdf_group_fact_receipt_v1",
        verification_mode="automated_unsigned",
        method="signed_inventory_scope_and_literal_quote_check_v1",
        status="blocked", reason_codes=(reason,), group_results=group_results,
        review_work_orders=tuple(_work_order(
            group, role_hint="", status="blocked", field_paths=(),
            reasons=(reason,),
        ) for group in groups),
    )


def _key(group: PdfExperimentalGroupV1) -> tuple[str, str, str]:
    scope = group.source_scope
    return scope.paper_id, scope.experimental_group_id, scope.source_digest


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _validate_inventory(
    groups: Sequence[PdfExperimentalGroupV1], budget: PdfGroupFactReceiptBudgetV1,
) -> str:
    if not groups:
        return "enumerated_group_inventory_empty"
    if len(groups) > budget.max_groups:
        return "group_budget_exceeded"
    seen: set[tuple[str, str, str]] = set()
    names: set[tuple[str, str]] = set()
    block_count = 0
    total_chars = 0
    for group in groups:
        if not isinstance(group, PdfExperimentalGroupV1):
            return "enumerated_group_invalid"
        scope = group.source_scope
        key = _key(group)
        if not all(key) or not _DIGEST.fullmatch(scope.source_digest) or (
            not _text(scope.section) or not _text(scope.locator)
            or not _text(group.source_document) or not group.blocks
        ):
            return "enumerated_group_invalid"
        if key in seen or key[:2] in names:
            return "enumerated_group_duplicate"
        seen.add(key)
        names.add(key[:2])
        locators: set[str] = set()
        for block in group.blocks:
            if not _text(block.locator) or not _text(block.text):
                return "enumerated_block_invalid"
            if block.locator in locators:
                return "enumerated_block_duplicate"
            locators.add(block.locator)
            block_count += 1
            total_chars += len(block.text)
            if block_count > budget.max_blocks:
                return "block_budget_exceeded"
            if total_chars > budget.max_total_chars:
                return "source_char_budget_exceeded"
    return ""


def _proposal_key(protocol: Mapping[str, Any]) -> tuple[str, str, str] | None:
    source = protocol.get("source")
    if not isinstance(source, Mapping):
        return None
    key = (
        _text(protocol.get("paper_id")),
        _text(protocol.get("experimental_group_id")),
        _text(source.get("source_digest")),
    )
    return key if all(key) else None


def _state_derivation_proof(
    fact: Mapping[str, Any], facts: Sequence[Mapping[str, Any]],
    graph: Any, scope: Any, *,
    retained_object_resolver: Any = None,
    dag_proofs: Any = None,
) -> dict[str, Any] | None:
    """Output derivation first, then verified input-state inheritance.

    An inherited state is accepted only when the recomputed proof verifies via
    ``verify_bound_output_state`` and the fact value equals the proof target;
    anything else returns ``None`` so the existing literal gates stay in charge.

    Only after BOTH flat single-hop derivations fail, a G1 multi-hop proof is
    consulted: ``dag_proofs`` is the per-group output of
    ``build_verified_state_proof_dags`` (built and dual-verified from THIS
    receipt's current signed blocks).  An entry is accepted only through the
    ``dag_entry_derivation`` guard — dual-empty verify issues, genuine
    state-proof-dag/v1 schema, verdict consistency — and the returned record
    is marked ``derivation="state_proof_dag_v1"``, never disguised as a
    paper-literal or flat derivation.  For input/intermediate state paths the
    fact value must equal the DAG root claim's target state, mirroring the
    flat inheritance acceptance.  Diagnostic (3E) records are rejected by the
    guard; no capability token is minted or forwarded on this path.
    """
    field_path = _text(fact.get("field_path"))
    if not field_path.endswith(".state"):
        return None
    graph_list = graph if isinstance(graph, list) else []
    derived, _issue = derive_unreviewed_output_state(
        graph_list, facts, field_path,
        paper_id=scope.paper_id,
        experimental_group_id=scope.experimental_group_id,
        source_digest=scope.source_digest,
        retained_object_resolver=retained_object_resolver,
    )
    if derived is not None:
        return derived
    inherited, _inherit_issue = derive_unreviewed_input_state(
        graph_list, facts, field_path, fact.get("source"),
        retained_object_resolver=retained_object_resolver,
    )
    if (inherited is not None
            and _text(fact.get("value")) == _text(inherited.get("target_state"))
            and not verify_bound_output_state(
                inherited, graph_list,
                convention_fact_evidence_by_id(
                    facts, paper_id=scope.paper_id,
                    experimental_group_id=scope.experimental_group_id),
                paper_id=scope.paper_id,
                experimental_group_id=scope.experimental_group_id,
                source_digest=scope.source_digest,
                retained_object_resolver=retained_object_resolver,
            )):
        return inherited
    # Both flat single-hop derivations failed: consult the G1 multi-hop
    # state-proof DAG (built and dual-verified from the current signed
    # blocks at the receipt layer, never from a stored artifact).
    if isinstance(dag_proofs, Mapping):
        derivation = dag_entry_derivation(dag_proofs.get(field_path))
        if derivation is not None:
            if (is_input_state_path(field_path)
                    and _text(fact.get("value"))
                    != _text(derivation.get("target_state"))):
                return None
            return derivation
    return None


def _literal_fact_reason(
    fact: Mapping[str, Any], group: PdfExperimentalGroupV1,
    blocks: Sequence[tuple[str, str]],
    *, graph: Any, facts: Sequence[Mapping[str, Any]],
    label_context: SourceLabelContext | None = None,
    binding_sink: dict[str, Any] | None = None,
    retained_object_resolver: Any = None,
    dag_proofs: Any = None,
    acceptance_sink: dict[str, Any] | None = None,
) -> str:
    if set(fact) - _FACT_KEYS:
        return "fact_authority_field_forbidden"
    source = fact.get("source")
    if not isinstance(source, Mapping) or set(source) != _FACT_SOURCE_KEYS:
        return "fact_source_scope_invalid"
    scope = group.source_scope
    if any((source.get(name) != expected for name, expected in (
        ("paper_id", scope.paper_id),
        ("experimental_group_id", scope.experimental_group_id),
        ("section", scope.section),
        ("source_digest", scope.source_digest),
    ))):
        return "fact_source_scope_mismatch"
    locator = source.get("locator")
    possible_locators = {
        f"{blocks[first][0].split('-')[0]}-{blocks[last][0].split('-')[-1]}"
        for first in range(len(blocks))
        # Three quoted prose blocks may straddle one parser-marked caption.
        # The binder below still rejects four ordinary prose blocks.
        for last in range(first, min(first + 4, len(blocks)))
    }
    if not isinstance(locator, str) or locator not in possible_locators:
        return "fact_block_outside_group"
    located_excerpt = fact.get("excerpt")
    binding, quote_issue = bind_pdf_quote(
        blocks, located_excerpt,
        caption_block_locators={block.locator for block in group.blocks if block.caption},
    )
    if quote_issue:
        return quote_issue
    assert binding is not None
    if locator != binding.locator:
        return "fact_source_locator_mismatch"
    # The full verification context must bind to this same group and contain
    # the located excerpt before it may back any semantic check.
    context_issue = verification_context_reason(
        fact, blocks,
        {block.locator for block in group.blocks if block.caption},
    )
    if context_issue:
        return context_issue
    # Semantic verification reads the full original quotation when a bounded
    # trim shortened the located excerpt; location stays with the short one.
    excerpt = fact.get("verification_excerpt") or located_excerpt
    if not _text(fact.get("fact_id")) or not _text(fact.get("field_path")):
        return "fact_identity_missing"
    if classify_route_field_basis(_text(fact.get("field_path"))) == "generated_id":
        return "fact_generated_id_paper_fact_forbidden"
    value = fact.get("value")
    unit = fact.get("unit", "")
    if not isinstance(unit, str):
        return "fact_unit_invalid"
    field_path = _text(fact.get("field_path"))
    # Flat single-hop derivations first — exactly the pre-G1 order.  The G1
    # multi-hop DAG is consulted ONLY where this function would otherwise
    # fail a ``.state`` fact (see _dag_rescued below), so a fact the
    # literal gates accept on their own keeps its literal classification.
    derived = _state_derivation_proof(
        fact, facts, graph, scope,
        retained_object_resolver=retained_object_resolver,
    )
    dag_derivation: dict[str, Any] | None = None
    dag_checked = False

    def _dag_rescued() -> bool:
        """Consult the dual-verified proof DAG for a failing ``.state`` gate.

        The DAG entry was built and dual-verified from THIS receipt's
        current signed blocks (never from a stored artifact); the
        ``dag_entry_derivation`` guard rejects verdict forgeries, cross-path
        substitutions, and 3E diagnostic records.  Consultation is lazy and
        cached: it happens only when a state-literal gate (or the
        string-value excerpt requirement below) has actually failed, so a
        fact the literal gates accept on their own is never relabeled.

        A rescue lifts ONLY the state-literal-evidence requirement — the
        parent-role/graph-path/controlled-mapping/attribution
        (semantic_binding_pending) gates and the "value must appear in the
        excerpt" string check — exactly the flat-derived gate exemptions (a
        proven state is not re-demanded as a verbatim quote).  The
        independent value-type and unit checks below still run after a
        rescue; the acceptance sink is written only at the final successful
        exit, so a rescued fact that later fails an independent check is
        reported with that reason and never classified dag_proven.  For
        input/intermediate paths the fact value must equal the DAG root
        claim's target state, mirroring flat inheritance.
        """
        nonlocal dag_derivation, dag_checked
        if not field_path.endswith(".state") or not isinstance(
                dag_proofs, Mapping):
            return False
        if not dag_checked:
            dag_checked = True
            candidate = _state_derivation_proof(
                fact, facts, graph, scope,
                retained_object_resolver=retained_object_resolver,
                dag_proofs=dag_proofs,
            )
            if (candidate is not None and isinstance(candidate, Mapping)
                    and candidate.get("derivation")
                    == STATE_PROOF_DAG_DERIVATION):
                dag_derivation = dict(candidate)
        return dag_derivation is not None

    if is_material_port_state_path(field_path):
        if derived is None:
            state_issue = ""
            if output_state_parent_role_issue(field_path, graph, value, excerpt):
                state_issue = "parent_state_not_child_evidence"
            else:
                scoped = _scoped_claim(
                    graph if isinstance(graph, list) else [], {}, field_path,
                )
                if scoped is None:
                    state_issue = "fact_graph_path_missing"
                else:
                    _mapping, mapping_issue = controlled_state_mapping(
                        field_path, value, scoped[0],
                    )
                    if mapping_issue:
                        state_issue = mapping_issue
                    else:
                        owner = scoped[1]
                        context = label_context
                        if context is None:
                            context = build_source_label_context(
                                graph if isinstance(graph, list) else [], facts,
                            )
                        outcome, binding = state_attribution_outcome(
                            value, excerpt, field_path,
                            graph if isinstance(graph, list) else [], context,
                        )
                        if outcome == "pending":
                            state_issue = "semantic_binding_pending"
                        elif outcome == "binding":
                            if binding_sink is not None:
                                binding_sink["binding"] = binding
                        elif (not isinstance(owner, Mapping)
                                or not state_source_locally_attributed(
                                    value, excerpt, owner.get("name"),
                                )):
                            state_issue = "semantic_binding_pending"
            # A DAG rescue waives only these state-literal gates; execution
            # continues into the independent value/unit checks below.
            if state_issue and not _dag_rescued():
                return state_issue
    if isinstance(value, bool):
        return "fact_value_type_unverifiable"
    if isinstance(value, (int, float)):
        if output_quantity_role_issue(field_path, graph, unit):
            return "fact_quantity_role_mismatch"
        if not unit.strip():
            if not dimensionless_numeric_field_pending(field_path, graph):
                return "fact_numeric_unit_missing"
            return (
                ""
                if dimensionless_labeled_value_verified(
                    value, excerpt, field_path, graph)
                else "dimensionless_semantic_pending"
            )
        if not literal_quantity_present(excerpt, value, unit):
            return "fact_quantity_not_in_excerpt"
        identity, identity_required = material_identity_for_amount_path(
            _text(fact.get("field_path")), graph, facts,
        )
        context = label_context
        if context is None:
            context = build_source_label_context(
                graph if isinstance(graph, list) else [], facts,
            )
        surfaces = quantity_identity_surfaces(
            _text(fact.get("field_path")),
            graph if isinstance(graph, list) else [], context,
        )
        match_sink: dict[str, Any] = {}
        if quantity_has_local_attribution(
            excerpt, value, unit, identity=identity,
            identity_required=identity_required, identity_surfaces=surfaces,
            match_sink=match_sink,
        ):
            surface = match_sink.get("identity_surface", "")
            if (surface and surface != identity.strip()
                    and binding_sink is not None):
                binding_sink["binding"] = {
                    "schema_version": "source-label-binding/v1",
                    "rule_version": SOURCE_LABEL_RULE_VERSION,
                    "rule_id": RULE_SCOPED_LABEL_IDENTITY,
                    "label": surface.casefold(),
                    "source_surface": surface,
                    "entity_material_id": _port_material_for_path(
                        graph if isinstance(graph, list) else [],
                        _text(fact.get("field_path")),
                    )[0],
                    "mention_anchor": surface_anchor(
                        _text(fact.get("field_path")),
                        graph if isinstance(graph, list) else [],
                        context, surface, excerpt),
                }
        else:
            binding = definition_site_concentration_binding(
                excerpt, value, unit, surfaces,
                competing_surfaces=competing_quantity_identity_surfaces(
                    _text(fact.get("field_path")),
                    graph if isinstance(graph, list) else [], context,
                ),
            )
            if binding is None:
                return "fact_quantity_attribution_unresolved"
            binding["entity_material_id"] = _port_material_for_path(
                graph if isinstance(graph, list) else [],
                _text(fact.get("field_path")),
            )[0]
            anchor = surface_anchor(
                _text(fact.get("field_path")),
                graph if isinstance(graph, list) else [],
                context, binding["source_surface"], excerpt,
            )
            if anchor:
                binding["mention_anchor"] = anchor
            if binding_sink is not None:
                binding_sink["binding"] = binding
    elif isinstance(value, str):
        if unit.strip():
            return "fact_unit_non_numeric"
        literal = normalize_pdf_quote_whitespace(value)
        normalized_excerpt = normalize_pdf_quote_whitespace(excerpt)
        if derived is None and dag_derivation is None and (not literal or re.search(
            rf"(?<!\w){re.escape(literal)}(?!\w)", normalized_excerpt,
        ) is None):
            string_issue = (
                "semantic_binding_pending"
                if classify_route_field_basis(_text(fact.get("field_path")))
                == "controlled_mapping"
                else "fact_value_not_in_excerpt"
            )
            if not _dag_rescued():
                return string_issue
    else:
        return "fact_value_type_unverifiable"
    # Every check passed.  Only now does a DAG rescue become an acceptance:
    # a rescued fact that failed any independent check above returned its
    # reason without touching the sink, so it is never classified dag_proven.
    if dag_derivation is not None and acceptance_sink is not None:
        acceptance_sink["state_proof_dag"] = dict(dag_derivation)
    return ""


def produce_pdf_group_fact_receipt(
    enumerated_groups: Sequence[PdfExperimentalGroupV1],
    association: PdfGroupProposalAssociationResultV1 | Sequence[Mapping[str, Any]],
    *,
    signed_inventory_verified: bool,
    budget: PdfGroupFactReceiptBudgetV1 = PdfGroupFactReceiptBudgetV1(),
) -> PdfGroupFactReceiptV1:
    """Check only source-scoped literals and queue all semantic trust decisions.

    ``signed_inventory_verified`` is an assertion by the trusted caller that
    these exact groups came from ``enumerate_attested_pdf_experimental_groups``.
    It does not let this receipt assert human review or sign anything. Every
    enumerated group must have exactly one associated protocol; invalid or
    partial association blocks the whole receipt.
    """
    if not isinstance(budget, PdfGroupFactReceiptBudgetV1):
        raise TypeError("budget must be PdfGroupFactReceiptBudgetV1")
    if signed_inventory_verified is not True:
        return _blocked("signed_group_inventory_not_verified")
    if not isinstance(enumerated_groups, Sequence) or isinstance(
        enumerated_groups, (str, bytes, bytearray)
    ):
        return _blocked("enumerated_group_inventory_invalid")
    inventory_issue = _validate_inventory(enumerated_groups, budget)
    if inventory_issue:
        return _blocked(inventory_issue)
    if isinstance(association, PdfGroupProposalAssociationResultV1):
        if association.diagnostics:
            return _blocked("proposal_association_has_diagnostics", enumerated_groups)
        protocols = association.protocols
    else:
        protocols = association
    if not isinstance(protocols, Sequence) or isinstance(
        protocols, (str, bytes, bytearray)
    ):
        return _blocked("group_proposals_invalid", enumerated_groups)
    if len(protocols) != len(enumerated_groups):
        return _blocked("group_proposal_coverage_incomplete", enumerated_groups)
    try:
        proposal_chars = len(json.dumps(
            protocols, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ))
    except (TypeError, ValueError, OverflowError, RecursionError):
        return _blocked("group_proposals_not_json", enumerated_groups)
    if proposal_chars > budget.max_proposal_chars:
        return _blocked("proposal_char_budget_exceeded", enumerated_groups)
    known = {_key(group): group for group in enumerated_groups}
    proposals: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    total_facts = 0
    for protocol in protocols:
        if not isinstance(protocol, Mapping):
            return _blocked("group_proposal_invalid", enumerated_groups)
        if set(protocol) - _PROTOCOL_KEYS:
            return _blocked("group_proposal_authority_field_forbidden", enumerated_groups)
        key = _proposal_key(protocol)
        if key not in known:
            return _blocked("group_proposal_scope_not_enumerated", enumerated_groups)
        if key in proposals:
            return _blocked("group_proposal_duplicate", enumerated_groups)
        proposals[key] = protocol
        facts = protocol.get("route_facts", [])
        if not isinstance(facts, list):
            return _blocked("group_route_facts_invalid", enumerated_groups)
        total_facts += len(facts)
        if total_facts > budget.max_facts:
            return _blocked("fact_budget_exceeded", enumerated_groups)

    group_results: list[PdfGroupLiteralStatusV1] = []
    work_orders: list[dict[str, Any]] = []
    overall_reasons: list[str] = []
    for group in enumerated_groups:
        scope = group.source_scope
        protocol = proposals[_key(group)]
        source = protocol.get("source")
        reasons: list[str] = []
        if not isinstance(source, Mapping) or set(source) != _SOURCE_KEYS or any((
            source.get(name) != expected for name, expected in (
                ("source_document", group.source_document),
                ("section", scope.section),
                ("locator", scope.locator),
                ("source_digest", scope.source_digest),
            )
        )):
            reasons.append("group_source_scope_mismatch")
        role_hint = protocol.get("role_hint", "")
        if not isinstance(role_hint, str):
            reasons.append("group_role_hint_invalid")
            role_hint = ""
        blocks = [(block.locator, block.text) for block in group.blocks]
        facts = protocol.get("route_facts", [])
        graph = protocol.get("material_graph")
        # The retained-object resolver is rebuilt LIVE from this group's
        # signed blocks for every receipt; a stored record is never read.
        retained_object_resolver = (
            build_retained_object_resolver(
                graph, facts if isinstance(facts, list) else (),
                blocks,
                [block.locator for block in group.blocks if block.caption],
            )
            if isinstance(graph, list) else None
        )
        label_context = build_source_label_context(
            graph if isinstance(graph, list) else [],
            facts if isinstance(facts, list) else [],
        )
        # G1: multi-hop state-proof DAGs are built and dual-verified ONCE per
        # group from THIS receipt's current signed blocks (never from a
        # stored artifact or an extraction-stage carry-over), then consulted
        # per fact only after both flat single-hop derivations fail.
        dag_proofs = (
            build_verified_state_proof_dags(
                graph, facts,
                paper_id=scope.paper_id,
                experimental_group_id=scope.experimental_group_id,
                source_digest=scope.source_digest,
                blocks=blocks,
                caption_block_locators=[
                    block.locator for block in group.blocks if block.caption
                ],
            )
            if isinstance(graph, list) and isinstance(facts, list) else None
        )
        # One quoted evidence item may support several distinct field paths.
        # Reusing its ID is valid only when both its literal quote and source
        # scope are unchanged, matching the route compiler's binding rule.
        fact_ids: dict[str, tuple[str, Any]] = {}
        field_paths: set[str] = set()
        verified_paths: list[str] = []
        derived_paths: list[str] = []
        dag_proven_paths: list[str] = []
        for index, fact in enumerate(facts):
            if not isinstance(fact, Mapping):
                reasons.append(f"fact[{index}]:fact_invalid")
                continue
            fact_id = _text(fact.get("fact_id"))
            field_path = _text(fact.get("field_path"))
            identity = (_text(fact.get("excerpt")), fact.get("source"))
            if field_path in field_paths:
                reasons.append(f"fact[{index}]:fact_identity_duplicate")
                continue
            if fact_id in fact_ids and fact_ids[fact_id] != identity:
                reasons.append(f"fact[{index}]:fact_id_conflict")
                continue
            fact_ids[fact_id] = identity
            field_paths.add(field_path)
            acceptance: dict[str, Any] = {}
            reason = _literal_fact_reason(
                fact, group, blocks, graph=protocol.get("material_graph"),
                facts=facts, label_context=label_context,
                retained_object_resolver=retained_object_resolver,
                dag_proofs=dag_proofs,
                acceptance_sink=acceptance,
            )
            if reason:
                reasons.append(f"fact[{index}]:{reason}")
            elif acceptance.get("state_proof_dag") is not None:
                # Rescued by a dual-verified state-proof DAG after the flat
                # derivations and literal gates failed: classified apart
                # from both literal-verified and flat-derived paths.
                dag_proven_paths.append(field_path)
            else:
                derived = _state_derivation_proof(
                    fact, facts, protocol.get("material_graph", []), scope,
                    retained_object_resolver=retained_object_resolver,
                )
                if derived is not None:
                    derived_paths.append(field_path)
                else:
                    verified_paths.append(field_path)
        if not facts and not reasons:
            status = "no_facts_to_check_pending_role"
        else:
            status = "literal_facts_verified_pending_review" if not reasons else "blocked"
        if reasons:
            overall_reasons.append("group_literal_check_failed")
        group_results.append(PdfGroupLiteralStatusV1(
            paper_id=scope.paper_id,
            experimental_group_id=scope.experimental_group_id,
            source_digest=scope.source_digest,
            group_locator=scope.locator,
            role_hint=role_hint.strip(),
            status=status,
            verified_field_paths=tuple(verified_paths),
            reason_codes=tuple(reasons),
            derived_state_field_paths=tuple(derived_paths),
            dag_proven_state_field_paths=tuple(dag_proven_paths),
        ))
        work_orders.append(_work_order(
            group, role_hint=role_hint.strip(), status=status,
            field_paths=[
                _text(fact.get("field_path")) for fact in facts
                if isinstance(fact, Mapping)
            ],
            reasons=reasons,
        ))
    return PdfGroupFactReceiptV1(
        schema_version="pdf_group_fact_receipt_v1",
        verification_mode="automated_unsigned",
        method="signed_inventory_scope_and_literal_quote_check_v1",
        status=("blocked" if overall_reasons else (
            "literal_checks_completed_pending_review"
            if any(not protocol.get("route_facts") for protocol in proposals.values())
            else "literal_facts_verified_pending_review"
        )),
        reason_codes=tuple(dict.fromkeys(overall_reasons)),
        group_results=tuple(group_results),
        review_work_orders=tuple(work_orders),
    )


__all__ = [
    "PdfGroupFactReceiptBudgetV1", "PdfGroupLiteralStatusV1",
    "PdfGroupFactReceiptV1", "produce_pdf_group_fact_receipt",
]
