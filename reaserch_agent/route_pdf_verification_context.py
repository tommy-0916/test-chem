"""Source binding for the full verification context of a trimmed fact.

A fact whose located excerpt was shortened carries ``verification_excerpt``
with the complete original quotation. Every consumer that runs semantic
checks on that context must first re-establish, with the consuming group's
own blocks, that:

1. the full context really occurs, uniquely, in that exact PDF group --
   under its own explicit resource budget, never the display budget, so a
   legitimate long context is not re-rejected (``max_quote_blocks``);
2. the located excerpt is literally contained in the full context.

A model proposal, a saved replay, or any other entry point may attach the
same field name; filling the field grants no verification authority. Save
and rebuild re-run these checks, so a modified or foreign context blocks
verification instead of silently degrading to the short excerpt.
"""

from __future__ import annotations

from typing import Any, Collection, Mapping, Sequence

from .route_pdf_quote_binding import (
    bind_pdf_quote, normalize_pdf_quote_whitespace,
)

# Resource cap for binding one full verification context. It is deliberately
# separate from the three-block display budget: the context exists to keep
# the whole original quotation available for verification, and re-imposing
# the display limit here would recreate the false refusals this slice
# removed. Over-cap contexts are refused explicitly, not trimmed.
MAX_VERIFICATION_CONTEXT_BLOCKS = 24


def verification_context_reason(
    fact: Mapping[str, Any],
    blocks: Sequence[tuple[str, str]],
    caption_block_locators: Collection[str] = (),
) -> str:
    """Return "" when the context is absent or fully bound; else a reason.

    The caller must pass the same group blocks the located excerpt was bound
    against, so a context from another group or document cannot bind here.
    """

    context = fact.get("verification_excerpt")
    if context is None:
        return ""
    located = fact.get("excerpt")
    if (not isinstance(context, str) or not context.strip()
            or not isinstance(located, str) or not located.strip()):
        return "verification_context_invalid"
    binding, issue = bind_pdf_quote(
        blocks, context,
        caption_block_locators=caption_block_locators,
        max_quote_blocks=MAX_VERIFICATION_CONTEXT_BLOCKS,
    )
    if issue == "fact_excerpt_not_in_block":
        return "verification_context_not_in_source_group"
    if issue == "fact_excerpt_ambiguous_in_group":
        return "verification_context_ambiguous_in_group"
    if issue == "fact_excerpt_span_too_long":
        return "verification_context_span_too_long"
    if issue or binding is None:
        return "verification_context_invalid"
    if (normalize_pdf_quote_whitespace(located)
            not in normalize_pdf_quote_whitespace(context)):
        return "verification_context_missing_located_excerpt"
    return ""


__all__ = ["MAX_VERIFICATION_CONTEXT_BLOCKS", "verification_context_reason"]
