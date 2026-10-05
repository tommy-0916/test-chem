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
import json
from pathlib import Path
import re
from statistics import median
from typing import Any, Mapping

from chem_agent_contracts.route_candidate import RouteCandidateV1
from chem_agent_contracts.route_convention_basis import verify_bound_output_state
from chem_agent_contracts.route_field_basis import (
    is_material_port_state_path, output_state_parent_role_issue,
    state_source_locally_attributed,
)
from chem_agent_contracts.route_inventory_basis import (
    RESOLVED, load_inventory_resource, resolve_external_input_states,
)
from chem_agent_contracts.route_source_labels import (
    RULE_SCOPED_LABEL_IDENTITY, SOURCE_LABEL_RULE_VERSION,
    build_source_label_context, competing_quantity_identity_surfaces,
    definition_site_concentration_binding,
    quantity_identity_surfaces, state_attribution_outcome, surface_anchor,
)
from chem_agent_contracts.v2 import canonical_digest, normalize_material_state

from .route_source import (
    RouteSourceVerificationV1,
    _graph_paper_issue,
    _paper_graph_claims,
)
from .route_group_compiler import (
    _port_material_for_path, literal_quantity_present,
    material_identity_for_amount_path, quantity_has_local_attribution,
)
from .route_pdf_quote_binding import (
    bind_pdf_quote, normalize_pdf_quote_whitespace,
)
from .route_pdf_verification_context import MAX_VERIFICATION_CONTEXT_BLOCKS


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
    y1: float
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


# A folio (printed page number such as "S 75") is recognized by six factors
# combined, never by a subset: (1) a standalone folio-shaped short line in
# the bottom band of the page, (2) a consistent vertical position across
# pages, (3) a number that advances in lockstep with the page order, (4) a
# consistent horizontal anchor (the line's center x agrees across pages),
# (5) body isolation inside its block (the candidate's original PDF block
# contains no other non-empty text line), and (6) body isolation across the
# block neighborhood (no other text line on the page overlaps the candidate
# horizontally while standing within normal body line spacing of it, above
# or below -- sole occupancy of a block does not by itself prove detachment
# from body text).  Nothing is deleted by digit count, font size, or a fixed
# page position; a single lookalike line (a chart tick, a table total, a
# measured value under a label) never qualifies, because the cross-page and
# isolation factors cannot hold for it.  When context is doubtful the line
# is kept: keeping content is always the safe failure.
_FOLIO_FORM = re.compile(r"([A-Za-z]{0,2})\s*([0-9]{1,4})\Z")
_FOLIO_BAND = 0.90
_FOLIO_MIN_PAGES = 3
_FOLIO_Y_TOLERANCE = 4.0
_FOLIO_X_TOLERANCE = 4.0
# The neighborhood proximity scale is derived from the candidate's own font
# metrics: one standard single line height (1.2 em).  A horizontally
# overlapping neighbor whose glyph-box gap to the candidate is smaller than
# that scale stands no further away than the next line of a single-spaced
# paragraph, so the candidate sits inside body flow and is not a folio.
_FOLIO_BODY_GAP_FACTOR = 1.2


def _has_body_neighbor(candidate: _PdfLine, lines: list[_PdfLine]) -> bool:
    """True when another text line on the page overlaps the candidate
    horizontally and stands within body line spacing of it (either side).

    The vertical glyph-box gap is compared against one standard line height
    of the candidate's own font size; touching or vertically intersecting
    boxes count as zero gap.  Only the gap scale comes from font metrics --
    no text content, coordinate, or page position is consulted.
    """
    scale = candidate.body_size or candidate.heading_size
    threshold = _FOLIO_BODY_GAP_FACTOR * scale
    for other in lines:
        if other is candidate:
            continue
        if other.x0 >= candidate.x1 or candidate.x0 >= other.x1:
            continue  # no horizontal overlap
        if other.y0 >= candidate.y1:
            gap = other.y0 - candidate.y1
        elif other.y1 <= candidate.y0:
            gap = candidate.y0 - other.y1
        else:
            gap = 0.0  # the glyph boxes touch or intersect vertically
        if gap < threshold:
            return True
    return False


def _validated_folio_lines(
    pages: list[tuple[float, list[_PdfLine]]],
) -> set[tuple[int, int, int]]:
    """Return (page, original_block, original_line) keys of validated folios.

    A page contributes a candidate only when it carries exactly one
    folio-shaped line in the bottom band; pages with zero or several
    candidates contribute none.  A candidate must be the only non-empty text
    line of its original block, and no other line on the page may overlap it
    horizontally within body line spacing (a value sharing a block with its
    label, or sitting right next to it, is body text, never a folio).  The
    candidate set is accepted only when every candidate shares one
    alphabetic prefix form, the numbers advance exactly with the page order,
    and the vertical positions and horizontal anchors each agree within
    tolerance -- otherwise the set is empty and nothing is stripped.
    """
    candidates: dict[int, tuple[str, int, float, float, _PdfLine]] = {}
    for _width, lines in pages:
        block_line_counts: dict[tuple[int, int], int] = {}
        for line in lines:
            key = (line.page, line.original_block)
            block_line_counts[key] = block_line_counts.get(key, 0) + 1
        found = []
        for line in lines:
            if line.y0 <= line.page_height * _FOLIO_BAND:
                continue
            if block_line_counts.get((line.page, line.original_block), 0) != 1:
                continue
            match = _FOLIO_FORM.fullmatch(line.text)
            if match is not None:
                found.append((
                    match.group(1).casefold(), int(match.group(2)),
                    line.y0, (line.x0 + line.x1) / 2, line,
                ))
        if len(found) == 1:
            prefix, number, y0, x_center, line = found[0]
            if not _has_body_neighbor(line, lines):
                candidates[line.page] = (prefix, number, y0, x_center, line)
    if len(candidates) < _FOLIO_MIN_PAGES:
        return set()
    ordered_pages = sorted(candidates)
    if len({candidates[page][0] for page in ordered_pages}) != 1:
        return set()
    numbered = [(page, candidates[page][1]) for page in ordered_pages]
    if any(
        later_number - number != later_page - page
        for (page, number), (later_page, later_number)
        in zip(numbered, numbered[1:])
    ):
        return set()
    anchor = median(candidates[page][2] for page in ordered_pages)
    if any(
        abs(candidates[page][2] - anchor) > _FOLIO_Y_TOLERANCE
        for page in ordered_pages
    ):
        return set()
    x_anchor = median(candidates[page][3] for page in ordered_pages)
    if any(
        abs(candidates[page][3] - x_anchor) > _FOLIO_X_TOLERANCE
        for page in ordered_pages
    ):
        return set()
    return {
        (line.page, line.original_block, line.original_line)
        for line in (candidates[page][4] for page in ordered_pages)
    }


def _line_debug(line: _PdfLine, width: float) -> dict[str, Any]:
    return {
        "page": line.page,
        "text": line.text,
        "x0": round(line.x0, 1),
        "x1": round(line.x1, 1),
        "y0": round(line.y0, 1),
        "page_width": round(width, 1),
    }


def _ordered_page_lines(
    lines: list[_PdfLine], width: float,
    culprits: list[_PdfLine] | None = None,
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
            if culprits is not None:
                culprits.append(line)
            return None, "pdf_column_layout_ambiguous"
        columns.append((0 if center < middle else 1, line))

    barriers = sorted(spanning, key=lambda line: (
        line.y0, line.x0, line.original_block, line.original_line,
    ))
    for left, right in zip(barriers, barriers[1:]):
        if right.y0 - left.y0 <= 3:
            if culprits is not None:
                culprits.extend((left, right))
            return None, "pdf_column_layout_ambiguous"
    barrier_y = [line.y0 for line in barriers]
    bands: list[list[tuple[int, _PdfLine]]] = [[] for _ in range(len(barriers) + 1)]
    for column, line in columns:
        # Near-identical top coordinates represent overlapping rows, not a
        # reliable before/after relation around the spanning text. Ordinary
        # consecutive PDF lines can have overlapping glyph boxes, so compare
        # their row origins rather than their font bounding boxes.
        band = bisect_left(barrier_y, line.y0)
        if ((band < len(barrier_y) and abs(barrier_y[band] - line.y0) <= 3)
                or (band > 0 and abs(barrier_y[band - 1] - line.y0) <= 3)):
            if culprits is not None:
                culprits.append(line)
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


def _empty_parse_report() -> dict[str, Any]:
    return {"folios_stripped": [], "abstention": None}


def _read_pdf_blocks_with_report(
    raw: bytes,
) -> tuple[list[_PdfBlock] | None, str | None, dict[str, Any]]:
    try:
        import fitz
    except ImportError:
        return None, "pdf_parser_unavailable", _empty_parse_report()
    if not raw.startswith(b"%PDF-"):
        return None, "source_not_pdf", _empty_parse_report()
    blocks: list[_PdfBlock] = []
    pages: list[tuple[float, list[_PdfLine]]] = []
    chars = 0
    line_count = 0
    try:
        with fitz.open(stream=raw, filetype="pdf") as document:
            if document.needs_pass:
                return None, "pdf_requires_password", _empty_parse_report()
            if len(document) > _MAX_PAGES:
                return None, "pdf_too_many_pages", _empty_parse_report()
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
                            return None, "pdf_extracted_text_too_large", _empty_parse_report()
                        heading, body, heading_size, body_size = _line_parts(spans)
                        x0, y0, x1, y1 = line.get("bbox", (0, 0, 0, 0))
                        lines_on_page.append(_PdfLine(
                            page=page_number,
                            original_block=original_block,
                            original_line=original_line,
                            page_height=page.rect.height,
                            x0=float(x0), x1=float(x1), y0=float(y0),
                            y1=float(y1),
                            text=text, heading=heading, body=body,
                            heading_size=heading_size, body_size=body_size,
                        ))
                pages.append((page.rect.width, lines_on_page))
    except Exception:
        # PDF parsers have several backend-specific errors; none may turn into
        # an affirmative source receipt.
        return None, "pdf_unreadable", _empty_parse_report()
    if not any(lines for _, lines in pages):
        return None, "pdf_no_extractable_text", _empty_parse_report()
    report = _empty_parse_report()
    # Validated folios are page furniture below the historical margin zone;
    # the six-factor check accepts only whole-document patterns.
    folio_keys = _validated_folio_lines(pages)
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
            if (line.page, line.original_block, line.original_line) in folio_keys:
                report["folios_stripped"].append(_line_debug(line, width))
                continue
            margin = line.y0 < line.page_height * 0.07 or line.y0 > line.page_height * 0.94
            repeated = len(margin_pages.get(" ".join(line.text.casefold().split()), set())) >= 2
            folio = line.y0 > line.page_height * 0.94 and bool(re.fullmatch(r"\d{3,}", line.text))
            if margin and (repeated or folio):
                continue
            content_lines.append(line)
        culprits: list[_PdfLine] = []
        ordered, order_issue = _ordered_page_lines(content_lines, width, culprits)
        if ordered is None:
            reason = order_issue or "pdf_column_layout_ambiguous"
            report["abstention"] = {
                "reason": reason,
                "culprits": [_line_debug(line, width) for line in culprits],
            }
            return None, reason, report
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
                return None, "pdf_extracted_text_too_large", report
            previous = line
    if not blocks:
        return None, "pdf_no_extractable_text", report
    return blocks, None, report


def _read_pdf_blocks(raw: bytes) -> tuple[list[_PdfBlock] | None, str | None]:
    blocks, issue, _report = _read_pdf_blocks_with_report(raw)
    return blocks, issue


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


def _port_name_for_path(graph: Any, field_path: str) -> str:
    match = re.fullmatch(
        r"material_graph\[([0-9]+)\]\."
        r"(material_inputs|material_intermediates|material_outputs)"
        r"\[([0-9]+)\]\.state", field_path,
    )
    if match is None:
        return ""
    try:
        step = graph[int(match.group(1))]
        ports = getattr(step, match.group(2))
        port = ports[int(match.group(3))]
    except (IndexError, TypeError, AttributeError):
        return ""
    name = getattr(port, "name", "")
    return name.strip() if isinstance(name, str) else ""


def _stored_binding_issue(
    field: Any, binding: dict[str, str] | None,
) -> str | None:
    stored = getattr(field, "source_label_binding", None)
    if stored is None:
        return None
    expected = binding or {}
    comparable = {
        key: value for key, value in expected.items() if value or key != "entity_material_id"
    }
    if stored.model_dump(mode="json", exclude_none=True) != comparable:
        return "field_source_label_binding_mismatch"
    return None


def _field_issue(
    candidate: RouteCandidateV1,
    field_index: int,
    blocks: list[_PdfBlock],
    group_range: tuple[int, int],
    digest: str,
    label_context: Any = None,
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
        # Post-compile evidence carries the full verification context when a
        # bounded trim shortened the located excerpt; its re-binding uses the
        # context budget, and the stored located locator must sit inside the
        # context span -- never silently re-tested against the short text.
        max_quote_blocks=MAX_VERIFICATION_CONTEXT_BLOCKS,
    )
    context_range = (
        _locator_range(binding.locator, blocks)
        if binding is not None and not quote_issue else None
    )
    if (
        quote_issue or binding is None
        or context_range is None
        or not (group_range[0] < context_range[0] <= context_range[1] <= group_range[1])
        or not (context_range[0] <= field_range[0] <= field_range[1] <= context_range[1])
        or not excerpt.strip()
        or normalize_pdf_quote_whitespace(excerpt)
        not in normalize_pdf_quote_whitespace(item.excerpt)
    ):
        return "field_excerpt_not_in_source_group"
    if field.value is None:
        return "field_value_missing"
    if output_state_parent_role_issue(
        field.field_path, candidate.material_graph, field.value, excerpt,
    ):
        return "parent_state_not_child_evidence"
    if is_material_port_state_path(field.field_path):
        context = label_context
        if context is None:
            context = build_source_label_context(
                candidate.material_graph, candidate.evidence_matrix,
            )
        outcome, binding = state_attribution_outcome(
            field.value, excerpt, field.field_path,
            candidate.material_graph, context,
        )
        if outcome == "pending":
            return "field_state_attribution_unresolved"
        if outcome == "legacy":
            name = _port_name_for_path(candidate.material_graph, field.field_path)
            if (not name or not state_source_locally_attributed(
                    field.value, excerpt, name)):
                return "field_state_attribution_unresolved"
            binding = None
        stored_issue = _stored_binding_issue(field, binding)
        if stored_issue:
            return stored_issue
    if field.unit:
        if isinstance(field.value, bool) or not isinstance(field.value, (int, float)):
            return "field_quantity_not_numeric"
        if not literal_quantity_present(excerpt, field.value, field.unit):
            return "field_quantity_not_in_excerpt"
        context = label_context
        if context is None:
            context = build_source_label_context(
                candidate.material_graph, candidate.evidence_matrix,
            )
        identity, identity_required = material_identity_for_amount_path(
            field.field_path, candidate.material_graph, candidate.evidence_matrix,
        )
        surfaces = quantity_identity_surfaces(
            field.field_path, candidate.material_graph, context,
        )
        binding: dict[str, str] | None = None
        match_sink: dict[str, Any] = {}
        if quantity_has_local_attribution(
            excerpt, field.value, field.unit, identity=identity,
            identity_required=identity_required, identity_surfaces=surfaces,
            match_sink=match_sink,
        ):
            surface = match_sink.get("identity_surface", "")
            if surface and surface != identity.strip():
                binding = {
                    "schema_version": "source-label-binding/v1",
                    "rule_version": SOURCE_LABEL_RULE_VERSION,
                    "rule_id": RULE_SCOPED_LABEL_IDENTITY,
                    "label": surface.casefold(),
                    "source_surface": surface,
                    "entity_material_id": _port_material_for_path(
                        candidate.material_graph, field.field_path)[0],
                    "mention_anchor": surface_anchor(
                        field.field_path, candidate.material_graph, context,
                        surface, excerpt),
                }
        else:
            binding = definition_site_concentration_binding(
                excerpt, field.value, field.unit, surfaces,
                competing_surfaces=competing_quantity_identity_surfaces(
                    field.field_path, candidate.material_graph, context,
                ),
            )
            if binding is None:
                return "field_quantity_attribution_unresolved"
            binding["entity_material_id"] = _port_material_for_path(
                candidate.material_graph, field.field_path)[0]
            anchor = surface_anchor(
                field.field_path, candidate.material_graph, context,
                binding["source_surface"], excerpt,
            )
            if anchor:
                binding["mention_anchor"] = anchor
        stored_issue = _stored_binding_issue(field, binding)
        if stored_issue:
            return stored_issue
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


def _inventory_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _trusted_candidate_supply_register(
    paths: Mapping[str, str | Path], root: Path,
) -> tuple[list[Mapping[str, Any]], str]:
    """Read caller-registered spec bytes; candidate metadata is not authority."""
    name = paths.get("candidate_supply_spec/v1")
    if name is None:
        return [], ""
    try:
        path = Path(name).resolve(strict=True)
        if (not path.is_file() or not path.is_relative_to(root)
                or path.stat().st_size > _MAX_SOURCE_BYTES):
            return [], ""
        raw = path.read_bytes()
        if len(raw) > _MAX_SOURCE_BYTES:
            return [], ""
        payload = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return [], ""
    if (not isinstance(payload, dict)
            or payload.get("schema") != "candidate_supply_spec/v1"
            or not isinstance(payload.get("items"), list)):
        return [], ""
    items = payload["items"]
    if any(not isinstance(item, dict)
           or item.get("candidate_supply_spec") is not True
           or item.get("stock_verified") is not False for item in items):
        return [], ""
    return items, "sha256_" + sha256(raw).hexdigest()


def verify_route_pdf_source(
    candidate: RouteCandidateV1,
    *,
    source_paths: Mapping[str, str | Path],
    source_root: str | Path,
    inventory_register_paths: Mapping[str, str | Path] | None = None,
    retained_object_resolver: Any = None,
) -> RouteSourceVerificationV1:
    """Reopen registered PDF/spec bytes, never paths supplied by the candidate.

    Candidate supply specifications require an explicit trusted register path
    inside ``source_root``. They prove planning form only, never stock.

    Although this verifier rebuilds quote blocks live from the signed PDF
    bytes, ``retained_object_resolver`` stays ``None`` at every current call
    site: the compiled candidate retains hashed evidence ids, not the raw
    route-fact ids a live retained-object record binds, so no resolver can
    be rebuilt here and retained-object proofs remain rejected
    (fail-closed).
    """

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
    label_context = build_source_label_context(
        candidate.material_graph, candidate.evidence_matrix,
    )
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
        issue = _field_issue(
            candidate, index, blocks, group, digest, label_context,
        )
        if issue:
            reasons.append(f"{issue}:{field.field_path}")
            invalid = True
        else:
            verified_fields.append(field.field_path)
            verified_ids.add(field.evidence_id)
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
                    or support.evidence_id not in verified_ids):
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
                    retained_object_resolver=retained_object_resolver,
                )):
            reasons.append(f"convention_state_support_unverified:{field.field_path}")
            invalid = True
    inventory_items, inventory_digest = load_inventory_resource()
    registers = {"material-inventory/v1": (inventory_items, inventory_digest)}
    if any(field.resolution_path == "candidate_supply_spec/v1"
           for field in candidate.evidence_matrix):
        registers["candidate_supply_spec/v1"] = _trusted_candidate_supply_register(
            inventory_register_paths or {}, root,
        )
    graph = [step.model_dump(mode="json") for step in candidate.material_graph]
    source_facts = [
        {"field_path": field.field_path, "excerpt": field.provenance.excerpt}
        for field in candidate.evidence_matrix
        if field.field_path in verified_fields and field.provenance is not None
    ]
    for field in candidate.evidence_matrix:
        provenance = field.provenance
        if provenance is None or provenance.kind != "inventory":
            continue
        register = field.resolution_path or "material-inventory/v1"
        items, register_digest = registers.get(register, ([], ""))
        matching_items = [item for item in items
                          if _inventory_text(item.get("item_id")) == provenance.reference]
        item = matching_items[0] if len(matching_items) == 1 else None
        resolutions = {
            record["field_path"]: record
            for record in resolve_external_input_states(graph, items, source_facts)
        }
        resolution = resolutions.get(field.field_path, {})
        port_match = re.fullmatch(
            r"material_graph\[([0-9]+)\]\.material_inputs\[([0-9]+)\]\.state",
            field.field_path,
        )
        graph_state = None
        if port_match is not None:
            try:
                graph_state = candidate.material_graph[int(port_match[1])].material_inputs[int(port_match[2])].state
            except IndexError:
                pass
        if (field.status != "supported"
                or not is_material_port_state_path(field.field_path)
                or provenance.evidence_class != "inventory_record"
                or item is None
                or not register_digest
                or provenance.source_digest != register_digest
                or resolution.get("status") != RESOLVED
                or resolution.get("item_id") != provenance.reference
                or resolution.get("state") != field.value
                or graph_state != field.value
                or (register == "material-inventory/v1"
                    and (item.get("candidate_supply_spec") is True
                         or item.get("stock_verified") is False))
                or normalize_material_state(
                    _inventory_text(item.get("supply_form"))) != field.value
                or _inventory_text(item.get("record")) != provenance.excerpt.strip()):
            reasons.append(f"inventory_resolution_unverified:{field.field_path}")
            invalid = True
        else:
            verified_fields.append(field.field_path)
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
                    max_quote_blocks=MAX_VERIFICATION_CONTEXT_BLOCKS,
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
            max_quote_blocks=MAX_VERIFICATION_CONTEXT_BLOCKS,
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
