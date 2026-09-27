"""Bounded, group-scoped revisions of unsigned PDF route proposals.

This producer can ask for a new proposal for a failing group. It preserves the
original model output, every previously required field path, and already
literal-verified facts. It does not create a receipt or admit a route.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from hashlib import sha256
import json
import re
from typing import Any

from .route_group_compiler import (
    _numeric_leaves, _required_qualitative_paths, _scoped_claim,
)
from .route_pdf_groups import PdfExperimentalGroupV1
from .route_pdf_local_diagnostics import assess_pdf_group_proposal_fields


_PROPOSAL_KEYS = frozenset({
    "source_group_ref", "role_hint", "target", "route_signature",
    "material_graph", "route_facts",
})
_FACT_KEYS = frozenset({
    "fact_id", "field_path", "value", "unit", "excerpt", "block_locator",
    "required",
})
PDF_LOCAL_REPAIR_VERSION = "pdf_group_local_repair/v1"
_GRAPH_STEP_PATH = re.compile(r"material_graph\[([0-9]+)\]\Z")


def _digest(value: Any) -> str:
    return "sha256_" + sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _key(proposal: Mapping[str, Any]) -> tuple[str, str, str] | None:
    ref = proposal.get("source_group_ref")
    if not isinstance(ref, Mapping) or set(ref) != {
        "paper_id", "experimental_group_id", "source_digest",
    }:
        return None
    values = tuple(ref.get(name) for name in (
        "paper_id", "experimental_group_id", "source_digest",
    ))
    return values if all(isinstance(item, str) and item for item in values) else None


def _claim(proposal: Mapping[str, Any], path: str) -> Any:
    graph = proposal.get("material_graph")
    signature = proposal.get("route_signature")
    resolved = _scoped_claim(
        graph if isinstance(graph, list) else [],
        signature if isinstance(signature, Mapping) else {},
        path,
    )
    if resolved is None:
        return None
    value, owner, unit = resolved
    return value, unit, owner.get("provenance") if isinstance(owner, Mapping) else None


def _graph_required_paths(proposal: Mapping[str, Any]) -> set[str]:
    graph = proposal.get("material_graph")
    signature = proposal.get("route_signature")
    graph = graph if isinstance(graph, list) else []
    signature = signature if isinstance(signature, Mapping) else {}
    sig_paths, graph_paths = _required_qualitative_paths(graph, signature)
    return sig_paths | graph_paths | _numeric_leaves(graph)


def _changed_paths(before: Any, after: Any, path: str) -> set[str]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        changes: set[str] = set()
        for key in set(before) | set(after):
            child_path = f"{path}.{key}" if path else str(key)
            if key not in before or key not in after:
                changes.add(child_path)
            else:
                changes.update(_changed_paths(
                    before[key], after[key], child_path,
                ))
        return changes
    if isinstance(before, list) and isinstance(after, list):
        changes = set()
        for index in range(max(len(before), len(after))):
            child_path = f"{path}[{index}]"
            if index >= len(before) or index >= len(after):
                changes.add(child_path)
            else:
                changes.update(_changed_paths(
                    before[index], after[index], child_path,
                ))
        return changes
    return {path} if before != after else set()


def _coverage_rows(
    proposals: Sequence[Any], passing_slots: Sequence[tuple[int, int]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    passed = set(passing_slots)
    for proposal_index, proposal in enumerate(proposals):
        if not isinstance(proposal, Mapping):
            continue
        facts = proposal.get("route_facts")
        if not isinstance(facts, list):
            continue
        declared = {
            fact.get("field_path") for fact in facts
            if isinstance(fact, Mapping) and fact.get("required") is True
            and isinstance(fact.get("field_path"), str)
        }
        literal_passed = {
            fact.get("field_path") for fact_index, fact in enumerate(facts)
            if (proposal_index, fact_index) in passed
            and isinstance(fact, Mapping)
        }
        graph_required = _graph_required_paths(proposal)
        rows.append({
            "proposal_index": proposal_index,
            "source_group_ref": deepcopy(proposal.get("source_group_ref")),
            "declared_required_count": len(declared),
            "declared_literal_passed_count": len(declared & literal_passed),
            "graph_required_count": len(graph_required),
            "graph_required_with_fact_count": len(graph_required & declared),
            "missing_graph_required_paths": sorted(graph_required - declared),
        })
    return rows


def _revised_group_issue(
    original: Mapping[str, Any], revised: Any,
    passing_indexes: Sequence[int],
) -> str:
    if not isinstance(revised, Mapping) or set(revised) - _PROPOSAL_KEYS:
        return "local_revision_proposal_invalid"
    if _key(revised) != _key(original):
        return "local_revision_group_scope_changed"
    before = original.get("route_facts")
    after = revised.get("route_facts")
    if not isinstance(before, list) or not isinstance(after, list):
        return "local_revision_facts_invalid"
    if any(not isinstance(fact, Mapping) or set(fact) - _FACT_KEYS
           for fact in after):
        return "local_revision_fact_authority_field_forbidden"

    original_required: dict[str, Mapping[str, Any]] = {}
    for fact in before:
        if not isinstance(fact, Mapping):
            return "local_revision_original_fact_invalid"
        path = fact.get("field_path")
        if not isinstance(path, str) or not path or path in original_required:
            return "local_revision_original_field_paths_invalid"
        # The existing compiler requires every supplied route fact to be
        # required=true. Revision cannot reduce the initial coverage.
        original_required[path] = fact
    revised_paths: dict[str, Mapping[str, Any]] = {}
    for fact in after:
        path = fact.get("field_path")
        if not isinstance(path, str) or not path or path in revised_paths:
            return "local_revision_field_paths_duplicate_or_invalid"
        revised_paths[path] = fact
    if not set(original_required).issubset(revised_paths):
        return "local_revision_required_field_dropped"
    if any(revised_paths[path].get("required") is not True
           for path in original_required):
        return "local_revision_required_field_dropped"
    original_graph_required = _graph_required_paths(original)
    if not original_graph_required.issubset(_graph_required_paths(revised)):
        return "local_revision_required_graph_path_dropped"
    if not original_graph_required.issubset(revised_paths):
        return "local_revision_required_graph_fact_missing"
    if not _graph_required_paths(revised).issubset(revised_paths):
        return "local_revision_required_graph_fact_missing"

    failing_paths = {
        fact["field_path"] for index, fact in enumerate(before)
        if index not in passing_indexes
    }
    allowed_changes = set(failing_paths)
    for path in failing_paths:
        if path.endswith(".quantity.value"):
            allowed_changes.add(path[:-len("value")] + "unit")
        elif path.endswith(".concentration_value"):
            allowed_changes.add(
                path[:-len("concentration_value")] + "concentration_unit"
            )
    for field in ("material_graph", "route_signature", "target"):
        changed = _changed_paths(original.get(field), revised.get(field), field)
        for path in changed:
            step_match = _GRAPH_STEP_PATH.fullmatch(path)
            appended_step = (
                field == "material_graph"
                and isinstance(original.get(field), list)
                and isinstance(revised.get(field), list)
                and step_match is not None
                and int(step_match.group(1)) >= len(original[field])
                and any(item.endswith(".operation") for item in failing_paths)
                and isinstance(revised[field][int(step_match.group(1))], Mapping)
                and revised[field][int(step_match.group(1))].get("sample_id")
                in {
                    step.get("sample_id") for step in original[field]
                    if isinstance(step, Mapping) and step.get("sample_id")
                }
            )
            if path not in allowed_changes and not appended_step:
                return "local_revision_unrelated_claim_changed"
    if revised.get("role_hint", "") != original.get("role_hint", ""):
        return "local_revision_role_hint_changed"

    for index in passing_indexes:
        if not 0 <= index < len(before):
            return "local_revision_passing_fact_missing"
        prior = before[index]
        path = prior["field_path"]
        updated = revised_paths[path]
        # The model's locator was already non-authoritative and is recomputed.
        prior_body = {key: value for key, value in prior.items()
                      if key != "block_locator"}
        updated_body = {key: value for key, value in updated.items()
                        if key != "block_locator"}
        if prior_body != updated_body:
            return "local_revision_passing_fact_changed"
        if _claim(original, path) != _claim(revised, path):
            return "local_revision_passing_graph_claim_changed"
    return ""


def revise_pdf_group_proposals_locally(
    groups: Sequence[PdfExperimentalGroupV1],
    original_proposals: Sequence[Mapping[str, Any]],
    invoke_json: Callable[[str], Mapping[str, Any]],
    build_group_prompt: Callable[[Sequence[PdfExperimentalGroupV1]], str],
    *,
    max_repair_groups: int,
    max_prompt_chars: int,
    max_response_chars: int,
    check_required_graph_facts: bool = False,
) -> tuple[list[Any], dict[str, Any]]:
    """Try at most one revision per failing group, then reassess the full batch."""
    baseline = deepcopy(list(original_proposals))
    assessment = assess_pdf_group_proposal_fields(
        groups, baseline,
        check_required_graph_facts=check_required_graph_facts,
    )
    report: dict[str, Any] = {
        "status": "unchanged", "original_proposals_digest": _digest(baseline),
        "initial_issues": deepcopy(assessment.issues), "revisions": [],
        "parser_version": assessment.located.parser_version,
        "binding_version": assessment.located.binding_version,
        "production_version": assessment.located.production_version,
        "repair_version": PDF_LOCAL_REPAIR_VERSION,
        "check_required_graph_facts": check_required_graph_facts,
        "coverage_before": _coverage_rows(
            baseline, assessment.passing_fact_slots,
        ),
    }
    if not assessment.issues or max_repair_groups == 0:
        report["final_issues"] = deepcopy(assessment.issues)
        report["final_proposals_digest"] = _digest(baseline)
        report["coverage_after"] = deepcopy(report["coverage_before"])
        return baseline, report

    # A missing/duplicate group or malformed proposal is not a field-level
    # repair opportunity. The strict association reports it without a guess.
    if len(baseline) != len(groups) or any(
        not isinstance(proposal, Mapping) or _key(proposal) is None
        for proposal in baseline
    ) or any(
        item["fact_index"] < 0
        and item["reason_code"] != "required_graph_fact_missing"
        for item in assessment.issues
    ):
        report["status"] = "structural_blocker"
        report["final_issues"] = deepcopy(assessment.issues)
        report["final_proposals_digest"] = _digest(baseline)
        report["coverage_after"] = deepcopy(report["coverage_before"])
        return baseline, report

    known = {
        (group.source_scope.paper_id, group.source_scope.experimental_group_id,
         group.source_scope.source_digest): group for group in groups
    }
    if len(known) != len(groups) or {_key(item) for item in baseline} != set(known):
        report["status"] = "group_coverage_blocker"
        report["final_issues"] = deepcopy(assessment.issues)
        report["final_proposals_digest"] = _digest(baseline)
        report["coverage_after"] = deepcopy(report["coverage_before"])
        return baseline, report

    final = deepcopy(baseline)
    for proposal_index in assessment.group_issue_indexes[:max_repair_groups]:
        original = baseline[proposal_index]
        group = known[_key(original)]
        issues = [item for item in assessment.issues
                  if item["proposal_index"] == proposal_index]
        passing_indexes = [fact_index for pidx, fact_index
                           in assessment.passing_fact_slots
                           if pidx == proposal_index]
        entry: dict[str, Any] = {
            "proposal_index": proposal_index,
            "source_group_ref": deepcopy(original["source_group_ref"]),
            "initial_issues": deepcopy(issues),
            "original_group_digest": _digest(original),
            "original_required_field_paths": sorted(
                fact.get("field_path", "") for fact in original["route_facts"]
                if isinstance(fact, Mapping) and fact.get("required") is True
            ),
            "passing_fact_indexes": passing_indexes,
            "group_layout_digest": next((
                record["group_layout_digest"]
                for record in assessment.located.resolutions
                if record["proposal_index"] == proposal_index
            ), ""),
        }
        if any(item["reason_code"] == "fact_graph_path_missing"
               for item in issues):
            # A route fact outside the compiler's graph/signature namespace
            # cannot be safely remapped while preserving the original field.
            entry["reason_code"] = "local_revision_original_graph_path_invalid"
            report["revisions"].append(entry)
            continue
        prompt = (
            build_group_prompt((group,))
            + "\nLOCAL REVISION: The following is the previous UNSIGNED proposal "
              "for this exact group and its failed field diagnostics. Return "
              "one complete revised proposal in the same proposals envelope. "
              "Keep every original required field_path and every already "
              "passing fact unchanged. Revise only failed facts and their "
              "related graph or signature claims. For every "
              "required_graph_fact_missing path, add a required=true fact "
              "for that exact existing graph or signature leaf; if the PDF "
              "does not support it, leave the proposal unresolved. Use atomic source-literal "
              "facts: for string values unit is the empty string; for numeric "
              "values quote the exact number, unit and material attribution. "
              "Use only this group's source text. If a claim has no support, "
              "leave it unresolved rather than inventing evidence. This is "
              "unreviewed output; do not assert approval or source identity.\n"
            + "Previous proposal (JSON):\n"
            + json.dumps(original, ensure_ascii=False, separators=(",", ":"))
            + "\nFailed fields (JSON):\n"
            + json.dumps(issues, ensure_ascii=False, separators=(",", ":"))
        )
        entry["prompt_chars"] = len(prompt)
        if len(prompt) > max_prompt_chars:
            entry["reason_code"] = "local_revision_prompt_char_budget_exceeded"
            report["revisions"].append(entry)
            continue
        try:
            envelope = invoke_json(prompt)
        except Exception:
            entry["reason_code"] = "local_revision_model_invocation_failed"
            report["revisions"].append(entry)
            continue
        try:
            response_chars = len(json.dumps(
                envelope, ensure_ascii=False, separators=(",", ":"),
                allow_nan=False,
            ))
        except (TypeError, ValueError, OverflowError):
            entry["reason_code"] = "local_revision_envelope_invalid"
            report["revisions"].append(entry)
            continue
        entry["response_chars"] = response_chars
        if response_chars > max_response_chars:
            entry["reason_code"] = "local_revision_response_char_budget_exceeded"
            report["revisions"].append(entry)
            continue
        entry["raw_revision"] = deepcopy(envelope)
        if (not isinstance(envelope, Mapping) or set(envelope) != {"proposals"}
              or not isinstance(envelope.get("proposals"), list)
              or len(envelope["proposals"]) != 1):
            entry["reason_code"] = "local_revision_envelope_invalid"
        else:
            revised = envelope["proposals"][0]
            entry["reason_code"] = _revised_group_issue(
                original, revised, passing_indexes,
            )
            if not entry["reason_code"]:
                final[proposal_index] = deepcopy(revised)
                entry["status"] = "merged_unreviewed"
                entry["revised_group_digest"] = _digest(revised)
                prior_facts = {
                    fact["field_path"]: fact for fact in original["route_facts"]
                }
                next_facts = {
                    fact["field_path"]: fact for fact in revised["route_facts"]
                }
                entry["changed_field_paths"] = sorted(
                    path for path in set(prior_facts) | set(next_facts)
                    if prior_facts.get(path) != next_facts.get(path)
                )
                entry["changed_non_fact_paths"] = sorted(set().union(*(
                    _changed_paths(original.get(field), revised.get(field), field)
                    for field in ("material_graph", "route_signature", "target")
                )))
        report["revisions"].append(entry)

    report["status"] = "reassessed_unreviewed"
    final_assessment = assess_pdf_group_proposal_fields(
        groups, final,
        check_required_graph_facts=check_required_graph_facts,
    )
    report["final_issues"] = deepcopy(final_assessment.issues)
    report["final_proposals_digest"] = _digest(final)
    report["coverage_after"] = _coverage_rows(
        final, final_assessment.passing_fact_slots,
    )
    report["groups_attempted"] = len(report["revisions"])
    report["groups_merged"] = sum(
        item.get("status") == "merged_unreviewed"
        for item in report["revisions"]
    )
    report["groups_budget_skipped"] = max(
        0, len(assessment.group_issue_indexes) - max_repair_groups,
    )
    return final, report


__all__ = ["revise_pdf_group_proposals_locally"]
