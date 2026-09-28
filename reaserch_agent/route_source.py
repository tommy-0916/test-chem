"""Verify route evidence against an immutable, local text source.

The caller supplies a trusted paper-ID-to-path index and its allowed root.  A
candidate, model output, or evidence-bundle status must never supply that
index.  The verifier reads the source bytes once, checks their digest, and
requires exact section, experimental-group, line, and excerpt matches.

Only UTF-8 Markdown-style text is supported.  PDF text extraction, generated
JSON summaries, and unstructured text without group headings remain
unverified rather than receiving guessed locators or route signatures.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Iterator, Mapping

from pydantic import BaseModel, ValidationError

from chem_agent_contracts.route_candidate import (
    ExperimentalGroupScopeV1,
    RouteCandidateV1,
    RouteSignatureV1,
)
from chem_agent_contracts.route_convention_basis import verify_bound_output_state
from chem_agent_contracts.route_field_basis import output_state_parent_role_issue
from chem_agent_contracts.v2 import canonical_digest, evidence_contains_exact_quantity


_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_LINE_LOCATOR = re.compile(r"lines:([1-9][0-9]*)-([1-9][0-9]*)\Z")
_SIGNATURE_OPEN = "```chem-agent-route-signature-v1"
_SIGNATURE_CLOSE = "```"
_MAX_SOURCE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class RouteSourceVerificationV1:
    source_scope_verified: bool = False
    verified_evidence_ids: tuple[str, ...] = ()
    verified_field_paths: tuple[str, ...] = ()
    source_route_signature: RouteSignatureV1 | None = None
    document_digest: str = ""
    source_path: str = ""
    reasons: tuple[str, ...] = ()


def _failure(reason: str) -> RouteSourceVerificationV1:
    return RouteSourceVerificationV1(reasons=(reason,))


def _locator_range(locator: str, line_count: int) -> tuple[int, int] | None:
    match = _LINE_LOCATOR.fullmatch(locator)
    if match is None:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    return (start, end) if start <= end <= line_count else None


def _group_span(
    lines: list[str], scope: ExperimentalGroupScopeV1
) -> tuple[int, int] | None:
    """Return the sole exact group heading and its body, within a named section.

    Any next heading closes the group.  This intentionally rejects nested
    subsections until an independently verified group parser can distinguish
    them from a different experimental arm.
    """

    ancestors: list[tuple[int, str]] = []
    matches: list[int] = []
    headings: list[int] = []
    for line_number, line in enumerate(lines, start=1):
        match = _HEADING.fullmatch(line)
        if match is None:
            continue
        level, title = len(match.group(1)), match.group(2).strip()
        headings.append(line_number)
        while ancestors and ancestors[-1][0] >= level:
            ancestors.pop()
        if title == scope.experimental_group_id and any(
            ancestor_title == scope.section for _, ancestor_title in ancestors
        ):
            matches.append(line_number)
        ancestors.append((level, title))
    if len(matches) != 1:
        return None
    start = matches[0]
    end = next((line - 1 for line in headings if line > start), len(lines))
    return start, end


def _source_signature(
    lines: list[str], group_start: int, group_end: int
) -> tuple[RouteSignatureV1 | None, tuple[int, int] | None, str | None]:
    """Read a machine-readable signature annotation embedded in this group.

    Ordinary Methods prose cannot establish all RouteSignatureV1 roles and
    transitions deterministically. The annotation alone is not source proof;
    verify_route_source additionally requires quoted per-component facts.
    """

    openings = [
        line_number
        for line_number in range(group_start + 1, group_end + 1)
        if lines[line_number - 1].strip() == _SIGNATURE_OPEN
    ]
    if not openings:
        return None, None, None
    if len(openings) != 1:
        return None, None, "source_signature_ambiguous"
    opening = openings[0]
    closing = next(
        (
            line_number
            for line_number in range(opening + 1, group_end + 1)
            if lines[line_number - 1].strip() == _SIGNATURE_CLOSE
        ),
        None,
    )
    if closing is None:
        return None, None, "source_signature_unclosed"
    try:
        raw = json.loads("\n".join(lines[opening:closing - 1]))
        signature = RouteSignatureV1.model_validate(raw, strict=True)
    except (TypeError, ValueError, ValidationError):
        return None, (opening, closing), "source_signature_invalid"
    return signature, (opening, closing), None


def _signature_claims(signature: RouteSignatureV1) -> dict[str, str]:
    """Every chemical signature component needs a quoted group-level fact.

    The embedded JSON block alone may be a local annotation. It cannot turn
    a proposed route into independently quoted paper evidence.
    """

    claims = {
        "route_signature.route_family": signature.route_family,
        "route_signature.target_transformation": signature.target_transformation,
        "route_signature.endpoint_state": signature.endpoint_state,
    }
    for key in ("precursor_roles", "reagent_roles", "operations", "control_modes"):
        for index, value in enumerate(getattr(signature, key)):
            claims[f"route_signature.{key}[{index}]"] = value
    for index, transition in enumerate(signature.phase_transitions):
        for key in ("before_state", "after_state", "confidence"):
            claims[f"route_signature.phase_transitions[{index}].{key}"] = getattr(
                transition, key
            )
    return claims


def _field_source_issue(
    candidate: RouteCandidateV1,
    field_index: int,
    lines: list[str],
    group_start: int,
    group_end: int,
    signature_span: tuple[int, int] | None,
    document_digest: str,
) -> str | None:
    field = candidate.evidence_matrix[field_index]
    scope = candidate.source_scope
    field_scope = field.source_scope
    provenance = field.provenance
    assert scope is not None
    if field_scope is None or (
        field_scope.paper_id != scope.paper_id
        or field_scope.experimental_group_id != scope.experimental_group_id
        or field_scope.section != scope.section
        or field_scope.source_digest != document_digest
    ):
        return "field_source_scope_mismatch"
    field_range = _locator_range(field_scope.locator, len(lines))
    if field_range is None or not (
        group_start < field_range[0] <= field_range[1] <= group_end
    ):
        return "field_locator_outside_group"
    if signature_span is not None and not (
        field_range[1] < signature_span[0]
        or field_range[0] > signature_span[1]
    ):
        return "field_locator_in_signature_annotation"
    if provenance is None or provenance.kind != "paper" or (
        provenance.evidence_class != "paper_explicit"
    ):
        return "field_paper_provenance_missing"
    if not field.evidence_id or provenance.reference != field.evidence_id:
        return "field_evidence_reference_mismatch"
    binding = next(
        (
            (index, item)
            for index, item in enumerate(candidate.evidence_bundle)
            if item.evidence_id == field.evidence_id
        ),
        None,
    )
    if binding is None:
        return "field_evidence_item_missing"
    evidence_index, item = binding
    if provenance.source_path != f"evidence_bundle.items[{evidence_index}].excerpt":
        return "field_provenance_source_path_mismatch"
    if provenance.source_digest != canonical_digest(item.excerpt):
        return "field_provenance_excerpt_digest_mismatch"
    excerpt = provenance.excerpt
    source_span = "\n".join(lines[field_range[0] - 1 : field_range[1]])
    if not excerpt.strip() or excerpt not in source_span or excerpt not in item.excerpt:
        return "field_excerpt_not_in_source_group"
    if field.value is None:
        return "field_value_missing"
    if output_state_parent_role_issue(
        field.field_path, candidate.material_graph, field.value, excerpt,
    ):
        return "parent_state_not_child_evidence"
    if field.unit:
        if isinstance(field.value, bool) or not isinstance(field.value, (int, float)):
            return "field_quantity_not_numeric"
        if not evidence_contains_exact_quantity(excerpt, field.value, field.unit):
            return "field_quantity_not_in_excerpt"
    elif isinstance(field.value, str):
        literal = field.value.strip()
        if not literal or re.search(
            rf"(?<!\w){re.escape(literal)}(?!\w)", excerpt
        ) is None:
            return "field_value_not_in_excerpt"
    else:
        # Unitary booleans and structured/derived values need a separate,
        # explicit verifier; they cannot be proven by loose text membership.
        return "field_value_type_unverifiable"
    return None


def _group_prose_segments(
    lines: list[str], group_start: int, group_end: int,
    signature_span: tuple[int, int] | None,
) -> tuple[str, ...]:
    """Keep signature annotations out of the experimental prose evidence."""

    if signature_span is None:
        return ("\n".join(lines[group_start:group_end]),)
    opening, closing = signature_span
    return (
        "\n".join(lines[group_start:opening - 1]),
        "\n".join(lines[closing:group_end]),
    )


def _in_group_prose(excerpt: str, segments: tuple[str, ...]) -> bool:
    return bool(excerpt.strip() and any(excerpt in segment for segment in segments))


def _paper_graph_claims(
    value: object, path: str,
) -> Iterator[tuple[str, Mapping[str, object]]]:
    """Find paper provenance at every nested graph node, including raw dicts.

    MacroStepV2.quantity_requirements and expected_return may contain plain
    dictionaries, while ports, relations, segments, and applicability use
    nested models.  Traversing the complete model dump covers both forms.
    """

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if isinstance(child, BaseModel):
                child = child.model_dump(mode="python")
            if key == "provenance" and isinstance(child, Mapping):
                if child.get("kind") == "paper":
                    yield child_path, child
            else:
                yield from _paper_graph_claims(child, child_path)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from _paper_graph_claims(child, f"{path}[{index}]")


def _graph_paper_issue(
    provenance: Mapping[str, object],
    evidence_by_id: Mapping[str, tuple[int, str]],
    prose_segments: tuple[str, ...],
) -> str | None:
    evidence_id = str(provenance.get("reference") or "")
    binding = evidence_by_id.get(evidence_id)
    if binding is None:
        return "graph_paper_evidence_item_missing"
    index, item_excerpt = binding
    if provenance.get("evidence_class") != "paper_explicit":
        return "graph_paper_evidence_class_mismatch"
    if provenance.get("source_path") != f"evidence_bundle.items[{index}].excerpt":
        return "graph_paper_source_path_mismatch"
    if provenance.get("source_digest") != canonical_digest(item_excerpt):
        return "graph_paper_excerpt_digest_mismatch"
    excerpt = str(provenance.get("excerpt") or "")
    if not excerpt.strip() or excerpt not in item_excerpt:
        return "graph_paper_excerpt_bundle_mismatch"
    if not _in_group_prose(excerpt, prose_segments):
        return "graph_paper_excerpt_outside_group"
    return None


def verify_route_source(
    candidate: RouteCandidateV1,
    *,
    source_paths: Mapping[str, str | Path],
    source_root: str | Path,
) -> RouteSourceVerificationV1:
    """Verify one paper-group candidate against a trusted local source index.

    `source_paths` is built by trusted ingestion/registry code, never copied
    from a candidate or LLM output.  A `source_route_signature` is returned
    only if a valid `chem-agent-route-signature-v1` block is already present
    inside the verified group of that trusted text file.
    """

    scope = candidate.source_scope
    if candidate.origin != "paper_experimental_group" or scope is None:
        return _failure("paper_experimental_group_scope_required")
    source_name = source_paths.get(scope.paper_id)
    if source_name is None:
        return _failure("paper_id_not_in_trusted_index")
    try:
        trusted_root = Path(source_root).resolve(strict=True)
        source_path = Path(source_name).resolve(strict=True)
        if not trusted_root.is_dir() or not source_path.is_file():
            return _failure("source_not_regular_file")
        if not source_path.is_relative_to(trusted_root):
            return _failure("source_outside_trusted_root")
        if source_path.suffix.lower() not in {".txt", ".md"}:
            return _failure("source_format_not_locatable")
        if source_path.stat().st_size > _MAX_SOURCE_BYTES:
            return _failure("source_too_large")
        raw = source_path.read_bytes()
        if len(raw) > _MAX_SOURCE_BYTES:
            return _failure("source_too_large")
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeError, ValueError, TypeError):
        return _failure("source_unavailable_or_invalid_utf8")
    digest = "sha256_" + hashlib.sha256(raw).hexdigest()
    if scope.source_digest != digest:
        return _failure("source_document_digest_mismatch")
    if not scope.section.strip() or not scope.experimental_group_id.strip():
        return _failure("source_section_or_group_missing")
    lines = text.splitlines()
    group = _group_span(lines, scope)
    if group is None:
        return _failure("experimental_group_not_unique_in_section")
    group_start, group_end = group
    if _locator_range(scope.locator, len(lines)) != group:
        return _failure("experimental_group_locator_mismatch")

    source_signature, signature_span, signature_issue = _source_signature(
        lines, group_start, group_end
    )
    prose_segments = _group_prose_segments(
        lines, group_start, group_end, signature_span
    )
    reasons: list[str] = []
    if signature_issue:
        reasons.append(signature_issue)
    verified_fields: list[str] = []
    verified_items: set[str] = set()
    referenced_items: set[str] = set()
    field_invalid = False
    for index, field in enumerate(candidate.evidence_matrix):
        if field.status != "supported" or field.provenance is None or (
            field.provenance.kind != "paper"
        ):
            continue
        if field.evidence_id:
            referenced_items.add(field.evidence_id)
        issue = _field_source_issue(
            candidate,
            index,
            lines,
            group_start,
            group_end,
            signature_span,
            digest,
        )
        if issue:
            reasons.append(f"{issue}:{field.field_path}")
            field_invalid = True
            continue
        verified_fields.append(field.field_path)
        verified_items.add(field.evidence_id)
    fields_by_path = {field.field_path: field for field in candidate.evidence_matrix}
    for field in candidate.evidence_matrix:
        provenance = field.provenance
        if (provenance is None or provenance.kind != "agent_inferred"
                or not provenance.derivation):
            continue
        try:
            proof = json.loads(provenance.derivation)
        except (TypeError, ValueError):
            proof = None
        if (not isinstance(proof, dict)
                or proof.get("schema_version") != "route-convention-state/v1"):
            continue
        support_ok = True
        for path_key, evidence_key in (
            ("parent_state_path", "parent_evidence_id"),
            ("operation_path", "operation_evidence_id"),
        ):
            support = fields_by_path.get(proof.get(path_key))
            if (support is None or support.field_path not in verified_fields
                    or support.evidence_id != proof.get(evidence_key)
                    or support.evidence_id not in verified_items):
                support_ok = False
        if (field.status != "supported" or field.field_path != proof.get("field_path")
                or field.value != proof.get("target_state")
                or field.evidence_id or not support_ok
                or verify_bound_output_state(
                    proof, candidate.material_graph,
                    {item.evidence_id: item for item in candidate.evidence_bundle},
                    paper_id=scope.paper_id,
                    experimental_group_id=scope.experimental_group_id,
                    source_digest=scope.source_digest,
                )):
            reasons.append(f"convention_state_support_unverified:{field.field_path}")
            field_invalid = True
    evidence_by_id = {
        item.evidence_id: (index, item.excerpt)
        for index, item in enumerate(candidate.evidence_bundle)
    }
    graph_invalid = False
    for step_index, step in enumerate(candidate.material_graph):
        for claim_path, provenance in _paper_graph_claims(
            step, f"material_graph[{step_index}]"
        ):
            evidence_id = str(provenance.get("reference") or "")
            if evidence_id:
                referenced_items.add(evidence_id)
            issue = _graph_paper_issue(
                provenance, evidence_by_id, prose_segments
            )
            if issue:
                graph_invalid = True
                reasons.append(f"{issue}:{claim_path}")
            else:
                verified_items.add(evidence_id)
    item_invalid = False
    for evidence_id in sorted(referenced_items):
        binding = evidence_by_id.get(evidence_id)
        if binding is None or not _in_group_prose(binding[1], prose_segments):
            item_invalid = True
            reasons.append(f"evidence_item_excerpt_not_in_group:{evidence_id}")
    if source_signature is not None:
        fields_by_path = {
            field.field_path: field for field in candidate.evidence_matrix
        }
        verified_paths = set(verified_fields)
        for path, expected_value in _signature_claims(source_signature).items():
            field = fields_by_path.get(path)
            if (
                field is None or field.status != "supported"
                or field.value != expected_value or path not in verified_paths
            ):
                reasons.append(f"source_signature_field_evidence_missing:{path}")
                source_signature = None
                break
    if field_invalid or graph_invalid or item_invalid:
        return RouteSourceVerificationV1(
            document_digest=digest,
            source_path=str(source_path),
            reasons=tuple(sorted(set(reasons))),
        )
    return RouteSourceVerificationV1(
        source_scope_verified=True,
        verified_evidence_ids=tuple(sorted(verified_items)),
        verified_field_paths=tuple(sorted(verified_fields)),
        source_route_signature=source_signature,
        document_digest=digest,
        source_path=str(source_path),
        reasons=tuple(sorted(set(reasons))),
    )


__all__ = ["RouteSourceVerificationV1", "verify_route_source"]
