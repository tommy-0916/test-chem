"""Bind literal PDF quotations across short, source-identified layout seams.

Only whitespace introduced at block boundaries and one independently marked
caption block may be ignored. Characters, numbers, punctuation, and chemistry
inside a PDF text block are never repaired or inferred here.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from itertools import product
import re
from typing import Collection, Sequence


_LOCATOR = re.compile(
    r"pdf:p([1-9][0-9]*):b([1-9][0-9]*)-p([1-9][0-9]*):b([1-9][0-9]*)\Z"
)
_MAX_QUOTE_BLOCKS = 3
_MAX_SKIPPED_CAPTIONS = 1
PDF_QUOTE_BINDING_VERSION = "route_pdf_quote_binding/v1"
# These characters explicitly join text at a PDF line/block seam. Plain digit
# or letter seams are never concatenated.
_SEAM_JOINERS = frozenset({"·", "/", "-", "–", "−"})


@dataclass(frozen=True)
class PdfQuoteBindingV1:
    locator: str
    first_block_index: int
    last_block_index: int


def normalize_pdf_quote_whitespace(text: str) -> str:
    """Collapse layout whitespace without changing literal characters."""

    return " ".join(text.split())


def _match_coordinates(
    source: str, quote: str, starts: Sequence[int],
    original_indexes: Sequence[int],
) -> set[tuple[int, int, int, int]]:
    """Map each exact occurrence to original block and in-block offsets."""

    matches: set[tuple[int, int, int, int]] = set()
    offset = 0
    while True:
        position = source.find(quote, offset)
        if position < 0:
            break
        first = bisect_right(starts, position) - 1
        end = position + len(quote) - 1
        last = bisect_right(starts, end) - 1
        if first >= 0 and last >= first:
            matches.add((
                original_indexes[first], position - starts[first],
                original_indexes[last], end - starts[last],
            ))
        offset = position + 1
    return matches


def _project(
    indexes: Sequence[int], normalized_blocks: Sequence[str],
    separators: Sequence[str],
) -> tuple[str, list[int]]:
    source_parts: list[str] = []
    starts: list[int] = []
    cursor = 0
    for ordinal, index in enumerate(indexes):
        if ordinal:
            separator = separators[ordinal - 1]
            source_parts.append(separator)
            cursor += len(separator)
        starts.append(cursor)
        source_parts.append(normalized_blocks[index])
        cursor += len(normalized_blocks[index])
    return "".join(source_parts), starts


def bind_pdf_quote(
    blocks: Sequence[tuple[str, str]], excerpt: str,
    *, asserted_block_locator: str | None = None,
    caption_block_locators: Collection[str] = (),
) -> tuple[PdfQuoteBindingV1 | None, str]:
    """Return one exact short quote span within the supplied group blocks.

    ``caption_block_locators`` must come from the trusted PDF parser, never
    from the proposal. At most one such block may be skipped, only when it
    lies strictly between the quoted prose blocks. A locator still covers the
    original continuous source span, including the skipped caption.
    """

    if not isinstance(excerpt, str) or not excerpt.strip():
        return None, "fact_excerpt_not_in_block"
    quote = normalize_pdf_quote_whitespace(excerpt)
    if not quote or not blocks:
        return None, "fact_excerpt_not_in_block"
    locators = [locator for locator, _ in blocks]
    if len(set(locators)) != len(locators) or any(
        _LOCATOR.fullmatch(locator) is None for locator in locators
    ):
        return None, "fact_block_outside_group"
    caption_locators = set(caption_block_locators)
    if caption_locators - set(locators):
        return None, "fact_block_outside_group"
    caption_indexes = {index for index, locator in enumerate(locators)
                       if locator in caption_locators}
    anchor_index: int | None = None
    if asserted_block_locator is not None:
        try:
            anchor_index = locators.index(asserted_block_locator)
        except ValueError:
            return None, "fact_block_outside_group"
        if anchor_index in caption_indexes:
            return None, "fact_block_locator_not_in_excerpt_span"

    normalized_blocks = [normalize_pdf_quote_whitespace(text) for _, text in blocks]
    if any(not text for text in normalized_blocks):
        return None, "fact_block_outside_group"
    matches: dict[tuple[int, int, int, int], set[int | None]] = {}

    # The ordinary literal path retains its ability to distinguish a quote
    # found in four or more blocks from a quote absent from the source.
    all_indexes = list(range(len(blocks)))
    direct_source, direct_starts = _project(
        all_indexes, normalized_blocks, [" "] * (len(blocks) - 1),
    )
    for match in _match_coordinates(direct_source, quote, direct_starts, all_indexes):
        if not any(index in caption_indexes for index in range(match[0], match[2] + 1)):
            matches.setdefault(match, set()).add(None)

    # Only short windows may use exceptional layout joins. A fourth content
    # block is scanned so it can be rejected with the precise span reason.
    max_window = _MAX_QUOTE_BLOCKS + _MAX_SKIPPED_CAPTIONS + 1
    for first_window in range(len(blocks)):
        for last_window in range(
            first_window, min(len(blocks), first_window + max_window),
        ):
            window = list(range(first_window, last_window + 1))
            skips: list[int | None] = [None]
            skips.extend(
                index for index in window[1:-1] if index in caption_indexes
            )
            for skipped in skips:
                indexes = [index for index in window if index != skipped]
                if len(indexes) > _MAX_QUOTE_BLOCKS + 1:
                    continue
                separator_options = [
                    (" ", "")
                    if (normalized_blocks[earlier][-1] in _SEAM_JOINERS
                        and normalized_blocks[later][0].isalnum()
                        and later == earlier + 1)
                    else (" ",)
                    for earlier, later in zip(indexes, indexes[1:])
                ]
                for separators in product(*separator_options):
                    # The ordinary projection was already checked in full.
                    if skipped is None and all(separator == " " for separator in separators):
                        continue
                    source, starts = _project(indexes, normalized_blocks, separators)
                    for match in _match_coordinates(source, quote, starts, indexes):
                        captions_in_span = {
                            index for index in range(match[0], match[2] + 1)
                            if index in caption_indexes
                        }
                        if (captions_in_span != ({skipped} if skipped is not None else set())
                                or (skipped is not None
                                    and not match[0] < skipped < match[2])):
                            continue
                        matches.setdefault(match, set()).add(skipped)

    if not matches:
        return None, "fact_excerpt_not_in_block"
    if len(matches) != 1:
        return None, "fact_excerpt_ambiguous_in_group"
    (first, _start_offset, last, _end_offset), paths = next(iter(matches.items()))
    valid_paths = {
        skipped for skipped in paths
        if last - first + 1 - int(skipped is not None) <= _MAX_QUOTE_BLOCKS
    }
    if not valid_paths:
        return None, "fact_excerpt_span_too_long"
    # An asserted anchor must itself be quoted prose. The skipped caption is
    # inside the physical range but cannot serve as a quote anchor.
    if anchor_index is not None and not first <= anchor_index <= last:
        return None, "fact_block_locator_not_in_excerpt_span"
    first_locator = _LOCATOR.fullmatch(locators[first])
    last_locator = _LOCATOR.fullmatch(locators[last])
    assert first_locator is not None and last_locator is not None
    locator = (
        f"pdf:p{first_locator.group(1)}:b{first_locator.group(2)}"
        f"-p{last_locator.group(3)}:b{last_locator.group(4)}"
    )
    return PdfQuoteBindingV1(locator, first, last), ""


__all__ = [
    "PDF_QUOTE_BINDING_VERSION", "PdfQuoteBindingV1", "bind_pdf_quote",
    "normalize_pdf_quote_whitespace",
]
