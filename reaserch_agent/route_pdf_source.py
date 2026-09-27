"""Verify PDF/SI route excerpts at deterministic page and text-block locators.

The caller supplies a trusted paper-ID-to-PDF-path index.  The original PDF
bytes, not extracted text or candidate metadata, determine the document hash.
Normalized text blocks provide reproducible locators of the form
``pdf:p1:b3-p2:b4`` (one-based pages and blocks).  The normalization splits
bold paragraph prefixes from prose and restores two-column reading order.
Exact blocks with heading typography delimit a section and a group.

This verifies that quoted facts occur within that group in the *local file*.
PDF parsing cannot establish that the file is publisher primary literature,
that an extracted procedure is chemically sound, or every role in a route
signature.  The source route signature is therefore always absent; a separate
reviewed, independently verifiable signature source is required to select a
route for planning.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
from statistics import median
from typing import Mapping

from chem_agent_contracts.route_candidate import RouteCandidateV1
from chem_agent_contracts.v2 import canonical_digest

from .route_source import (
    RouteSourceVerificationV1,
    _graph_paper_issue,
    _paper_graph_claims,
)
from .route_group_compiler import (
    literal_quantity_present, material_identity_for_amount_path,
    quantity_has_local_attribution,
)
from .route_pdf_quote_binding import (
    bind_pdf_quote, normalize_pdf_quote_whitespace,
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
    caption: bool = False


@dataclass(frozen=True)
class _PdfLine:
    page: int
    original_block: int
    original_line: int
    page_height: float
    x0: float
    x1: float
    y0: float
    text: str
    heading: str
    body: str
    heading_size: float
    body_size: float


def _is_bold_span(span: Mapping[str, object]) -> bool:
    return (
        "bold" in str(span.get("font") or "").lower()
        or "semibold" in str(span.get("font") or "").lower()
        or bool(int(span.get("flags") or 0) & 16)
    )


def _line_parts(spans: list[dict]) -> tuple[str, str, float, float]:
    # A decorative section marker can have a much larger font than the
    # heading itself.  It cannot create a heading without a bold text span.
    start = 0
    if len(spans) > 1 and str(spans[0].get("text") or "").strip() in {"■", "●", "▪"}:
        start = 1
    relevant = spans[start:]
    bold_end = 0
    while bold_end < len(relevant):
        if _is_bold_span(relevant[bold_end]):
            bold_end += 1
            continue
        # PDF equations often use a separate regular-font glyph for a
        # minus sign inside an otherwise bold heading.
        glyph = str(relevant[bold_end].get("text") or "").strip()
        if (glyph and len(glyph) <= 2 and bold_end + 1 < len(relevant)
                and _is_bold_span(relevant[bold_end + 1])):
            bold_end += 1
            continue
        break
    prefix = "".join(str(span["text"]) for span in relevant[:bold_end]).strip()
    tail = "".join(str(span["text"]) for span in relevant[bold_end:]).strip()
    if prefix and (not tail or prefix.endswith((".", ":"))):
        return (
            prefix, tail,
            max((float(span.get("size") or 0) for span in relevant[:bold_end]), default=0),
            max((float(span.get("size") or 0) for span in relevant[bold_end:]), default=0),
        )
    return "", "".join(str(span["text"]) for span in spans).strip(), 0, max(
        (float(span.get("size") or 0) for span in spans), default=0,
    )


def _two_column_page(lines: list[_PdfLine], width: float) -> bool:
    middle = width / 2
    left = sum(line.x1 < middle - 2 for line in lines)
    right = sum(line.x0 > middle + 2 for line in lines)
    return left >= 8 and right >= 8


def _ordered_page_lines(
    lines: list[_PdfLine], width: float,
) -> tuple[list[_PdfLine] | None, str | None]:
    if not _two_column_page(lines, width):
        return sorted(lines, key=lambda line: (
            line.y0, line.x0, line.original_block, line.original_line,
        )), None
    middle = width / 2
    # A left-column line may cross the center by a few points because of PDF
    # extraction. A genuine spanning line must cover both column interiors.
    interior = width * 0.15
    spanning: list[_PdfLine] = []
    columns: list[tuple[int, _PdfLine]] = []
    for line in lines:
        if line.x0 < middle - interior and line.x1 > middle + interior:
            spanning.append(line)
            continue
        center = (line.x0 + line.x1) / 2
        if abs(center - middle) <= 2:
            return None, "pdf_column_layout_ambiguous"
        columns.append((0 if center < middle else 1, line))

    barriers = sorted(spanning, key=lambda line: (
        line.y0, line.x0, line.original_block, line.original_line,
    ))
    barrier_y = [line.y0 for line in barriers]
    if any(right - left <= 3 for left, right in zip(barrier_y, barrier_y[1:])):
        return None, "pdf_column_layout_ambiguous"
    bands: list[list[tuple[int, _PdfLine]]] = [[] for _ in range(len(barriers) + 1)]
    for column, line in columns:
        # Near-identical top coordinates represent overlapping rows, not a
        # reliable before/after relation around the spanning text. Ordinary
        # consecutive PDF lines can have overlapping glyph boxes, so compare
        # their row origins rather than their font bounding boxes.
        band = bisect_left(barrier_y, line.y0)
        if ((band < len(barrier_y) and abs(barrier_y[band] - line.y0) <= 3)
                or (band > 0 and abs(barrier_y[band - 1] - line.y0) <= 3)):
            return None, "pdf_column_layout_ambiguous"
        bands[band].append((column, line))

    ordered: list[_PdfLine] = []
    for index, band in enumerate(bands):
        # Each band follows normal newspaper order: top-to-bottom in the left
        # column, then top-to-bottom in the right column. A spanning row stays
        # at the boundary instead of being moved to the beginning of the page.
        ordered.extend(line for _, line in sorted(band, key=lambda item: (
            item[0], item[1].y0, item[1].x0,
            item[1].original_block, item[1].original_line,
        )))
        if index < len(barriers):
            ordered.append(barriers[index])
    return ordered, None


def _continues_heading(previous: _PdfLine, current: _PdfLine) -> bool:
    return (
        previous.page == current.page
        and previous.original_block == current.original_block
        and current.original_line == previous.original_line + 1
        and bool(previous.heading) and not previous.body
        and bool(current.heading)
        and not previous.heading.endswith((".", ":"))
        and (previous.heading.endswith(("=", ",", ";", "(", "−", "-"))
             or len(previous.heading) >= 45)
    )


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
    pages: list[tuple[float, list[_PdfLine]]] = []
    chars = 0
    line_count = 0
    try:
        with fitz.open(stream=raw, filetype="pdf") as document:
            if document.needs_pass:
                return None, "pdf_requires_password"
            if len(document) > _MAX_PAGES:
                return None, "pdf_too_many_pages"
            for page_number, page in enumerate(document, start=1):
                lines_on_page: list[_PdfLine] = []
                for original_block, item in enumerate(page.get_text("dict", sort=True).get("blocks", [])):
                    if item.get("type") != 0:
                        continue
                    for original_line, line in enumerate(item.get("lines", [])):
                        spans = [span for span in line.get("spans", []) if str(span.get("text") or "")]
                        if not spans:
                            continue
                        text = "".join(str(span["text"]) for span in spans).strip()
                        if not text:
                            continue
                        chars += len(text)
                        line_count += 1
                        if line_count > _MAX_BLOCKS or chars > _MAX_TEXT_CHARS:
                            return None, "pdf_extracted_text_too_large"
                        heading, body, heading_size, body_size = _line_parts(spans)
                        x0, y0, x1, _ = line.get("bbox", (0, 0, 0, 0))
                        lines_on_page.append(_PdfLine(
                            page=page_number,
                            original_block=original_block,
                            original_line=original_line,
                            page_height=page.rect.height,
                            x0=float(x0), x1=float(x1), y0=float(y0),
                            text=text, heading=heading, body=body,
                            heading_size=heading_size, body_size=body_size,
                        ))
                pages.append((page.rect.width, lines_on_page))
    except Exception:
        # PDF parsers have several backend-specific errors; none may turn into
        # an affirmative source receipt.
        return None, "pdf_unreadable"
    if not any(lines for _, lines in pages):
        return None, "pdf_no_extractable_text"
    # Repeated margin text is page furniture, not an experimental boundary.
    margin_pages: dict[str, set[int]] = {}
    for _, lines in pages:
        for line in lines:
            if line.y0 < line.page_height * 0.07 or line.y0 > line.page_height * 0.94:
                margin_pages.setdefault(" ".join(line.text.casefold().split()), set()).add(line.page)
    for width, lines in pages:
        number = 0
        previous: _PdfLine | None = None
        content_lines: list[_PdfLine] = []
        for line in lines:
            margin = line.y0 < line.page_height * 0.07 or line.y0 > line.page_height * 0.94
            repeated = len(margin_pages.get(" ".join(line.text.casefold().split()), set())) >= 2
            folio = line.y0 > line.page_height * 0.94 and bool(re.fullmatch(r"\d{3,}", line.text))
            if margin and (repeated or folio):
                continue
            content_lines.append(line)
        ordered, order_issue = _ordered_page_lines(content_lines, width)
        if ordered is None:
            return None, order_issue or "pdf_column_layout_ambiguous"
        for line in ordered:
            if previous is not None and _continues_heading(previous, line):
                last = blocks[-1]
                blocks[-1] = _PdfBlock(
                    page=last.page, number=last.number,
                    text=last.text + " " + line.heading,
                    font_size=max(last.font_size, line.heading_size), bold=True,
                    caption=last.caption,
                )
            elif line.heading:
                number += 1
                blocks.append(_PdfBlock(
                    line.page, number, line.heading, line.heading_size, True,
                    _caption_text(line.heading),
                ))
            if line.body:
                number += 1
                continued_caption = (
                    previous is not None
                    and previous.original_block == line.original_block
                    and bool(blocks) and blocks[-1].caption
                    and not line.heading
                )
                blocks.append(_PdfBlock(
                    line.page, number, line.body, line.body_size, False,
                    _caption_text(line.heading)
                    or _caption_text(line.body)
                    or continued_caption,
                ))
            if len(blocks) > _MAX_BLOCKS:
                return None, "pdf_extracted_text_too_large"
            previous = line
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


def _caption_text(text: str) -> bool:
    return bool(re.match(r"^(?:figure|fig\.?|table|scheme)\s*[sS]?\d+\b", text, re.I))


def _caption(block: _PdfBlock) -> bool:
    return block.caption or _caption_text(block.text)


def _group_boundary(
    blocks: list[_PdfBlock], group_index: int, section_end: int,
    body_size: float,
) -> int:
    # Some journals set subsection labels and prose at the same point size.
    # A peer needs heading typography; unstyled text closes the group only
    # when it is visibly larger than the section's ordinary prose.
    size = blocks[group_index].font_size
    return next((
        index for index in range(group_index + 1, section_end)
        if (not _caption(blocks[index])
            and _heading(blocks[index]) and blocks[index].font_size >= size - 0.1)
        or (not _heading(blocks[index])
            and not _caption(blocks[index])
            and blocks[index].font_size >= size - 0.1
            and blocks[index].font_size > body_size + 0.5)
    ), section_end)


def _section_body_size(blocks: list[_PdfBlock], start: int, end: int) -> float:
    sizes = [
        block.font_size for block in blocks[start:end]
        if not _heading(block) and not _caption(block)
    ]
    return float(median(sizes)) if sizes else 0


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
        if not _caption(blocks[index])
        and blocks[index].font_size >= section_size - 0.1
    ), len(blocks))
    groups = [
        index for index in range(section_index + 1, section_end)
        if blocks[index].text == group_id and _heading(blocks[index])
        and not _caption(blocks[index])
        and blocks[index].font_size < section_size - 0.1
    ]
    if len(groups) != 1:
        return None
    group_index = groups[0]
    group_size = blocks[group_index].font_size
    group_end = _group_boundary(
        blocks, group_index, section_end,
        _section_body_size(blocks, section_index + 1, section_end),
    )
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
    quote_blocks = _quote_blocks(blocks, group_range)
    binding, quote_issue = bind_pdf_quote(
        quote_blocks, excerpt,
        caption_block_locators=_quote_caption_locators(blocks, group_range),
    )
    if (
        quote_issue or binding is None
        or binding.locator != field_scope.locator
        or not excerpt.strip()
        or normalize_pdf_quote_whitespace(excerpt)
        not in normalize_pdf_quote_whitespace(item.excerpt)
    ):
        return "field_excerpt_not_in_source_group"
    if field.value is None:
        return "field_value_missing"
    if field.unit:
        if isinstance(field.value, bool) or not isinstance(field.value, (int, float)):
            return "field_quantity_not_numeric"
        if not literal_quantity_present(excerpt, field.value, field.unit):
            return "field_quantity_not_in_excerpt"
        identity, identity_required = material_identity_for_amount_path(
            field.field_path, candidate.material_graph,
        )
        if not quantity_has_local_attribution(
            excerpt, field.value, field.unit, identity=identity,
            identity_required=identity_required,
        ):
            return "field_quantity_attribution_unresolved"
    elif isinstance(field.value, str):
        literal = normalize_pdf_quote_whitespace(field.value)
        normalized_excerpt = normalize_pdf_quote_whitespace(excerpt)
        if not literal or re.search(
            rf"(?<!\w){re.escape(literal)}(?!\w)", normalized_excerpt,
        ) is None:
            return "field_value_not_in_excerpt"
    else:
        return "field_value_type_unverifiable"
    return None


def _quote_blocks(
    blocks: list[_PdfBlock], group_range: tuple[int, int],
) -> list[tuple[str, str]]:
    # The group heading identifies the scope but is not experimental prose.
    return [
        (f"pdf:p{block.page}:b{block.number}-p{block.page}:b{block.number}", block.text)
        for block in blocks[group_range[0] + 1:group_range[1] + 1]
    ]


def _quote_caption_locators(
    blocks: list[_PdfBlock], group_range: tuple[int, int],
) -> set[str]:
    """Identify captions from parsed PDF bytes, never proposal metadata."""
    return {
        f"pdf:p{block.page}:b{block.number}-p{block.page}:b{block.number}"
        for block in blocks[group_range[0] + 1:group_range[1] + 1]
        if _caption(block)
    }


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
    quote_blocks = _quote_blocks(blocks, group)
    caption_locators = _quote_caption_locators(blocks, group)
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
            graph_excerpt = str(provenance.get("excerpt") or "")
            # Reuse the provenance/bundle checks, then perform PDF-specific
            # layout binding within this group.  The text-source verifier
            # checks a raw substring and cannot resolve split PDF blocks.
            issue = _graph_paper_issue(
                provenance, evidence_by_id, (graph_excerpt,),
            )
            if issue is None:
                _binding, quote_issue = bind_pdf_quote(
                    quote_blocks, graph_excerpt,
                    caption_block_locators=caption_locators,
                )
                if quote_issue:
                    issue = "graph_paper_excerpt_outside_group"
            if issue:
                invalid = True
                reasons.append(f"{issue}:{claim_path}")
            else:
                verified_ids.add(evidence_id)
    for evidence_id in sorted(referenced_ids):
        binding = evidence_by_id.get(evidence_id)
        if binding is None or bind_pdf_quote(
            quote_blocks, binding[1], caption_block_locators=caption_locators,
        )[1]:
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
