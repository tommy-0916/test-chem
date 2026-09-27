"""Verify PDF/SI route excerpts at deterministic page and text-block locators.

The caller supplies a trusted paper-ID-to-PDF-path index.  The original PDF
bytes, not extracted text or candidate metadata, determine the document hash.
PyMuPDF's sorted text blocks provide reproducible locators of the form
``pdf:p1:b3-p2:b4`` (one-based pages and text blocks).  Exact text blocks with
distinct heading typography delimit a section and an experimental group.

This verifies that quoted facts occur within that group in the *local file*.
PDF parsing cannot establish that the file is publisher primary literature,
that an extracted procedure is chemically sound, or every role in a route
signature.  The source route signature is therefore always absent; a separate
reviewed, independently verifiable signature source is required to select a
route for planning.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
from typing import Mapping

from chem_agent_contracts.route_candidate import RouteCandidateV1
from chem_agent_contracts.v2 import canonical_digest, evidence_contains_exact_quantity

from .route_source import (
    RouteSourceVerificationV1,
    _graph_paper_issue,
    _in_group_prose,
    _paper_graph_claims,
)


_LOCATOR = re.compile(r"pdf:p([1-9][0-9]*):b([1-9][0-9]*)-p([1-9][0-9]*):b([1-9][0-9]*)\Z")
_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_MAX_PAGES = 250
_MAX_BLOCKS = 10000
_MAX_TEXT_CHARS = 1000000


@dataclass(frozen=True)
class _PdfBlock:
    page: int
    number: int
    text: str
    font_size: float
    bold: bool


def _failure(reason: str) -> RouteSourceVerificationV1:
    return RouteSourceVerificationV1(reasons=(reason,))


def _read_pdf_blocks(raw: bytes) -> tuple[list[_PdfBlock] | None, str | None]:
    try:
        import fitz
    except ImportError:
        return None, "pdf_parser_unavailable"
    if not raw.startswith(b"%PDF-"):
        return None, "source_not_pdf"
    blocks: list[_PdfBlock] = []
    chars = 0
    try:
        with fitz.open(stream=raw, filetype="pdf") as document:
            if document.needs_pass:
                return None, "pdf_requires_password"
            if len(document) > _MAX_PAGES:
                return None, "pdf_too_many_pages"
            for page_number, page in enumerate(document, start=1):
                text_number = 0
                for item in page.get_text("dict", sort=True).get("blocks", []):
                    if item.get("type") != 0:
                        continue
                    lines: list[str] = []
                    sizes: list[float] = []
                    bold_spans = 0
                    for line in item.get("lines", []):
                        spans = [span for span in line.get("spans", []) if str(span.get("text") or "")]
                        if not spans:
                            continue
                        lines.append("".join(str(span["text"]) for span in spans))
                        sizes.extend(float(span.get("size") or 0) for span in spans)
                        bold_spans += sum(
                            "bold" in str(span.get("font") or "").lower()
                            or bool(int(span.get("flags") or 0) & 16)
                            for span in spans
                        )
                    text = "\n".join(lines).strip()
                    if not text:
                        continue
                    text_number += 1
                    chars += len(text)
                    if len(blocks) >= _MAX_BLOCKS or chars > _MAX_TEXT_CHARS:
                        return None, "pdf_extracted_text_too_large"
                    blocks.append(_PdfBlock(
                        page=page_number,
                        number=text_number,
                        text=text,
                        font_size=max(sizes, default=0),
                        bold=bold_spans > 0,
                    ))
    except Exception:
        # PDF parsers have several backend-specific errors; none may turn into
        # an affirmative source receipt.
        return None, "pdf_unreadable"
    if not blocks:
        return None, "pdf_no_extractable_text"
    return blocks, None


def _locator_range(locator: str, blocks: list[_PdfBlock]) -> tuple[int, int] | None:
    match = _LOCATOR.fullmatch(locator)
    if match is None:
        return None
    start = (int(match.group(1)), int(match.group(2)))
    end = (int(match.group(3)), int(match.group(4)))
    positions = {(block.page, block.number): index for index, block in enumerate(blocks)}
    if start not in positions or end not in positions:
        return None
    first, last = positions[start], positions[end]
    return (first, last) if first <= last else None


def _heading(block: _PdfBlock) -> bool:
    # A standalone bold heading is the minimal typography signal used to
    # distinguish labels from prose mentions.  Ambiguous PDFs abstain.
    return block.bold and "\n" not in block.text


def _group_range(
    blocks: list[_PdfBlock], section: str, group_id: str,
) -> tuple[int, int] | None:
    sections = [
        index for index, block in enumerate(blocks)
        if block.text == section and _heading(block)
    ]
    if len(sections) != 1:
        return None
    section_index = sections[0]
    section_size = blocks[section_index].font_size
    section_end = next((
        index for index in range(section_index + 1, len(blocks))
        if blocks[index].font_size >= section_size - 0.1
    ), len(blocks))
    groups = [
        index for index in range(section_index + 1, section_end)
        if blocks[index].text == group_id and _heading(blocks[index])
        and blocks[index].font_size < section_size - 0.1
    ]
    if len(groups) != 1:
        return None
    group_index = groups[0]
    group_size = blocks[group_index].font_size
    group_end = next((
        index for index in range(group_index + 1, section_end)
        if blocks[index].font_size >= group_size - 0.1
    ), section_end)
    if group_end <= group_index + 1:
        return None
    return group_index, group_end - 1


def _field_issue(
    candidate: RouteCandidateV1,
    field_index: int,
    blocks: list[_PdfBlock],
    group_range: tuple[int, int],
    digest: str,
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
        or field_scope.source_digest != digest
    ):
        return "field_source_scope_mismatch"
    field_range = _locator_range(field_scope.locator, blocks)
    if field_range is None or not (
        group_range[0] < field_range[0] <= field_range[1] <= group_range[1]
    ):
        return "field_locator_outside_group"
    if provenance is None or provenance.kind != "paper" or (
        provenance.evidence_class != "paper_explicit"
    ):
        return "field_paper_provenance_missing"
    if not field.evidence_id or provenance.reference != field.evidence_id:
        return "field_evidence_reference_mismatch"
    binding = next((
        (index, item) for index, item in enumerate(candidate.evidence_bundle)
        if item.evidence_id == field.evidence_id
    ), None)
    if binding is None:
        return "field_evidence_item_missing"
    item_index, item = binding
    if provenance.source_path != f"evidence_bundle.items[{item_index}].excerpt":
        return "field_provenance_source_path_mismatch"
    if provenance.source_digest != canonical_digest(item.excerpt):
        return "field_provenance_excerpt_digest_mismatch"
    excerpt = provenance.excerpt
    span = "\n".join(block.text for block in blocks[field_range[0]:field_range[1] + 1])
    if not excerpt.strip() or excerpt not in span or excerpt not in item.excerpt:
        return "field_excerpt_not_in_source_group"
    if field.value is None:
        return "field_value_missing"
    if field.unit:
        if isinstance(field.value, bool) or not isinstance(field.value, (int, float)):
            return "field_quantity_not_numeric"
        if not evidence_contains_exact_quantity(excerpt, field.value, field.unit):
            return "field_quantity_not_in_excerpt"
    elif isinstance(field.value, str):
        literal = field.value.strip()
        if not literal or re.search(rf"(?<!\w){re.escape(literal)}(?!\w)", excerpt) is None:
            return "field_value_not_in_excerpt"
    else:
        return "field_value_type_unverifiable"
    return None


def verify_route_pdf_source(
    candidate: RouteCandidateV1,
    *,
    source_paths: Mapping[str, str | Path],
    source_root: str | Path,
) -> RouteSourceVerificationV1:
    """Check one candidate against a registered local PDF, never its own path."""

    scope = candidate.source_scope
    if candidate.origin != "paper_experimental_group" or scope is None:
        return _failure("paper_experimental_group_scope_required")
    source_name = source_paths.get(scope.paper_id)
    if source_name is None:
        return _failure("paper_id_not_in_trusted_index")
    try:
        root = Path(source_root).resolve(strict=True)
        path = Path(source_name).resolve(strict=True)
        if not root.is_dir() or not path.is_file():
            return _failure("source_not_regular_file")
        if not path.is_relative_to(root):
            return _failure("source_outside_trusted_root")
        if path.suffix.lower() != ".pdf":
            return _failure("source_format_not_pdf")
        if path.stat().st_size > _MAX_SOURCE_BYTES:
            return _failure("source_too_large")
        raw = path.read_bytes()
        if len(raw) > _MAX_SOURCE_BYTES:
            return _failure("source_too_large")
    except (OSError, ValueError, TypeError):
        return _failure("source_unavailable")
    digest = "sha256_" + sha256(raw).hexdigest()
    if scope.source_digest != digest:
        return _failure("source_document_digest_mismatch")
    if not scope.section.strip() or not scope.experimental_group_id.strip():
        return _failure("source_section_or_group_missing")
    blocks, parse_issue = _read_pdf_blocks(raw)
    if blocks is None:
        return _failure(parse_issue or "pdf_unreadable")
    group = _group_range(blocks, scope.section, scope.experimental_group_id)
    if group is None:
        return _failure("experimental_group_not_unique_in_section")
    if _locator_range(scope.locator, blocks) != group:
        return _failure("experimental_group_locator_mismatch")
    prose = ("\n".join(block.text for block in blocks[group[0] + 1:group[1] + 1]),)
    reasons: list[str] = []
    verified_fields: list[str] = []
    verified_ids: set[str] = set()
    referenced_ids: set[str] = set()
    invalid = False
    for index, field in enumerate(candidate.evidence_matrix):
        if field.status != "supported" or field.provenance is None or field.provenance.kind != "paper":
            continue
        if field.evidence_id:
            referenced_ids.add(field.evidence_id)
        issue = _field_issue(candidate, index, blocks, group, digest)
        if issue:
            reasons.append(f"{issue}:{field.field_path}")
            invalid = True
        else:
            verified_fields.append(field.field_path)
            verified_ids.add(field.evidence_id)
    evidence_by_id = {
        item.evidence_id: (index, item.excerpt)
        for index, item in enumerate(candidate.evidence_bundle)
    }
    for step_index, step in enumerate(candidate.material_graph):
        for claim_path, provenance in _paper_graph_claims(
            step, f"material_graph[{step_index}]"
        ):
            evidence_id = str(provenance.get("reference") or "")
            if evidence_id:
                referenced_ids.add(evidence_id)
            issue = _graph_paper_issue(provenance, evidence_by_id, prose)
            if issue:
                invalid = True
                reasons.append(f"{issue}:{claim_path}")
            else:
                verified_ids.add(evidence_id)
    for evidence_id in sorted(referenced_ids):
        binding = evidence_by_id.get(evidence_id)
        if binding is None or not _in_group_prose(binding[1], prose):
            invalid = True
            reasons.append(f"evidence_item_excerpt_not_in_group:{evidence_id}")
    if invalid:
        return RouteSourceVerificationV1(
            document_digest=digest,
            source_path=str(path),
            reasons=tuple(sorted(set(reasons))),
        )
    return RouteSourceVerificationV1(
        source_scope_verified=True,
        verified_evidence_ids=tuple(sorted(verified_ids)),
        verified_field_paths=tuple(sorted(verified_fields)),
        # A PDF prose locator does not prove every claimed route role.
        source_route_signature=None,
        document_digest=digest,
        source_path=str(path),
    )


__all__ = ["verify_route_pdf_source"]
