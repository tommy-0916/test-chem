"""Post-operation retained-object source relation.

This module derives a SOURCE relation (schema
``post-operation-retained-object/v1``): whether a naming sentence that
FOLLOWS an operation in the signed paper text binds a retained-object noun
to exactly one of that step's output labels (e.g. "The precipitates were
labeled as LDH seeds" binds "precipitates" to the output named
"LDH seeds").

It performs no chemistry: it never maps the retained object to a material
state and never picks a convention rule.  The convention layer
(``route_convention_basis``) may consume a validated record as source
evidence; the canonical output state stays convention-derived, and a
conflict between the record and any rule family's endpoint stays pending
(ambiguous), never an override.

English-only naming/discard patterns in Round 2; Chinese C1 aliases are
explicitly out of scope.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Sequence

POST_OPERATION_RETAINED_OBJECT_SCHEMA = "post-operation-retained-object/v1"
POST_OPERATION_RETAINED_OBJECT_RULE_ID = "POST_OPERATION_RETAINED_OBJECT_V1"
POST_OPERATION_RETAINED_OBJECT_RULE_VERSION = "1.0.0"

# "<object> was|were labeled|labelled|named|called|denoted as <label>" with
# the label terminated by punctuation or end of text.  An occurrence only
# counts when the captured label equals the output's source label exactly
# (case-sensitive); the caller compares, this pattern only captures.
_NAMING_PATTERN = re.compile(
    r"\b(?P<object>[A-Za-z][\w-]*)\s+"
    r"(?:was|were)\s+"
    r"(?:labeled|labelled|named|called|denoted)\s+as\s+"
    r"(?P<label>[A-Za-z][\w .\-]*?)(?=\s*[,;.]|$)",
    flags=re.IGNORECASE,
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return {}


def _items(value: Any) -> list[Mapping[str, Any]]:
    return [_mapping(item) for item in value] if isinstance(value, (list, tuple)) else []


def _discard_patterns(object_surface: str) -> tuple[re.Pattern, re.Pattern]:
    """Predications that mark the captured object itself as discarded."""
    escaped = re.escape(object_surface)
    return (
        re.compile(
            rf"\b(?:the\s+)?{escaped}\s+(?:was|were)\s+"
            rf"(?:discarded|disposed|thrown\s+away|removed)\b",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"\b(?:discarded|disposed\s+of|threw\s+away)\s+"
            rf"(?:the\s+)?{escaped}\b",
            flags=re.IGNORECASE,
        ),
    )


def _normalize_object_surface(surface: str) -> str:
    """Deterministic trivial normalization of the captured object noun.

    Casefold, drop a leading "the ", and singularize ONE trailing "s" of a
    regular plural: the "s" is dropped when the letter before it is a
    consonant ("pellets" -> "pellet") or the "e" of a consonant+"es"
    plural ("precipitates" -> "precipitate").  Anything else is kept as
    written; no lemmatizer, no vocabulary lookup.
    """
    text = surface.strip().casefold()
    if text.startswith("the "):
        text = text[4:].strip()
    if len(text) > 1 and text.endswith("s"):
        stem = text[:-1]
        if stem and (
            stem[-1] not in "aeiou"
            or (len(stem) > 1 and stem[-1] == "e" and stem[-2] not in "aeiou")
        ):
            text = stem
    return text


class _ExcerptSpanResolver:
    """Callable excerpt -> (first_block_index, last_block_index) or None.

    Wraps ``bind_pdf_quote`` over the supplied signed group blocks; an
    excerpt that is missing, ambiguous, or over-span resolves to None.
    Results are cached per excerpt.  ``blocks`` is exposed for the
    same-block textual-ordering tie-break below.
    """

    def __init__(self, blocks: Sequence[Any], caption_block_locators: Sequence[str]):
        from reaserch_agent.route_pdf_quote_binding import bind_pdf_quote

        self._bind = bind_pdf_quote
        self.blocks = [(locator, text) for locator, text in blocks]
        self._captions = tuple(caption_block_locators or ())
        self._cache: dict[str, tuple[int, int] | None] = {}

    def __call__(self, excerpt: str) -> tuple[int, int] | None:
        key = excerpt if isinstance(excerpt, str) else ""
        if key not in self._cache:
            binding, _issue = self._bind(
                self.blocks, key, caption_block_locators=self._captions,
            )
            self._cache[key] = (
                (binding.first_block_index, binding.last_block_index)
                if binding is not None else None
            )
        return self._cache[key]


def build_excerpt_span_resolver(
    blocks: Sequence[Any], caption_block_locators: Sequence[str] = (),
) -> Callable[[str], tuple[int, int] | None]:
    """Resolve an excerpt to its first/last block indexes, or None."""
    return _ExcerptSpanResolver(blocks, caption_block_locators)


def _naming_follows_operation_in_block(
    blocks: Sequence[tuple[str, str]], shared_index: int,
    operation_excerpt: str, naming_excerpt: str,
) -> bool:
    """Tie-break when both excerpts touch the same physical block.

    PDF block seams can split a sentence boundary ("...protocol. The" /
    "precipitates were labeled ..."), so a block-index-only comparison
    would report the naming sentence as not following the operation even
    when it textually does.  Re-project the blocks exactly like the direct
    binding path (single-space joins of whitespace-normalized text) and
    accept only when the operation quote's last character in the shared
    block precedes the naming quote's first character there.  A naming
    quote that textually precedes the operation quote is never admitted.
    """
    from reaserch_agent.route_pdf_quote_binding import (
        _match_coordinates, _project, normalize_pdf_quote_whitespace,
    )

    normalized = [normalize_pdf_quote_whitespace(text) for _, text in blocks]
    indexes = list(range(len(blocks)))
    source, starts = _project(indexes, normalized, [" "] * (len(indexes) - 1))
    operation_quote = normalize_pdf_quote_whitespace(operation_excerpt)
    naming_quote = normalize_pdf_quote_whitespace(naming_excerpt)
    if not operation_quote or not naming_quote:
        return False
    operation_ends = [
        starts[last] + end_offset
        for first, _start, last, end_offset in _match_coordinates(
            source, operation_quote, starts, indexes,
        )
        if last == shared_index
    ]
    naming_starts = [
        starts[first] + start_offset
        for first, start_offset, _last, _end in _match_coordinates(
            source, naming_quote, starts, indexes,
        )
        if first == shared_index
    ]
    return any(
        operation_end < naming_start
        for operation_end in operation_ends
        for naming_start in naming_starts
    )


def derive_post_operation_retained_object(
    graph: Sequence[Any], facts: Sequence[Any], step_index: int, *,
    span_of: Callable[[str], tuple[int, int] | None],
) -> tuple[dict | None, str]:
    """Derive the post-operation retained-object record for one graph step.

    Returns ``(record, "")`` on success or ``(None, issue)``.  Guards, in
    order: the operation quote must bind; the naming sentence must bind
    its object to exactly one output label; the object must not be
    predicated as discarded; the naming must follow the operation; and no
    other modeled operation may intervene between them.
    """
    steps = [_mapping(step) for step in graph] if isinstance(graph, (list, tuple)) else []
    if not isinstance(step_index, int) or not 0 <= step_index < len(steps):
        return None, "retained_object_operation_span_missing"
    step = steps[step_index]
    by_path: dict[str, Mapping[str, Any]] = {}
    for raw in facts or []:
        fact = _mapping(raw)
        path = _text(fact.get("field_path"))
        if path and path not in by_path:
            by_path[path] = fact

    operation_fact = by_path.get(f"material_graph[{step_index}].operation")
    operation_excerpt = _text(operation_fact.get("excerpt")) if operation_fact else ""
    operation_span = span_of(operation_excerpt) if operation_excerpt else None
    if operation_fact is None or operation_span is None:
        return None, "retained_object_operation_span_missing"

    outputs = _items(step.get("material_outputs"))
    matches: list[tuple[int, Mapping[str, Any], tuple[int, int], re.Match]] = []
    for output_index, port in enumerate(outputs):
        name_fact = by_path.get(
            f"material_graph[{step_index}].material_outputs[{output_index}].name"
        )
        label = _text(name_fact.get("value")) if name_fact else ""
        naming_excerpt = _text(name_fact.get("excerpt")) if name_fact else ""
        if name_fact is None or not label:
            continue
        naming_span = span_of(naming_excerpt)
        if naming_span is None:
            continue
        for occurrence in _NAMING_PATTERN.finditer(naming_excerpt):
            if occurrence.group("label").strip() == label:
                matches.append((output_index, name_fact, naming_span, occurrence))
    if len({match[0] for match in matches}) != 1:
        # No output bound, or two different outputs both bound: unresolved.
        return None, "retained_object_output_binding_unresolved"
    output_index, name_fact, naming_span, occurrence = matches[0]
    label = _text(name_fact.get("value"))
    naming_excerpt = _text(name_fact.get("excerpt"))
    object_surface = occurrence.group("object")

    if any(pattern.search(naming_excerpt)
           for pattern in _discard_patterns(object_surface)):
        return None, "retained_object_discarded"

    if naming_span[0] < operation_span[1]:
        return None, "retained_object_mention_precedes_operation"
    if naming_span[0] == operation_span[1]:
        # Same physical block: accept only when the naming quote textually
        # follows the operation quote inside it (see the tie-break above).
        blocks = getattr(span_of, "blocks", None)
        if not blocks or not _naming_follows_operation_in_block(
            blocks, naming_span[0], operation_excerpt, naming_excerpt,
        ):
            return None, "retained_object_mention_precedes_operation"

    for other_index, other in enumerate(steps):
        if other_index == step_index:
            continue
        other_fact = by_path.get(f"material_graph[{other_index}].operation")
        other_excerpt = _text(other_fact.get("excerpt")) if other_fact else ""
        other_span = span_of(other_excerpt) if other_excerpt else None
        if other_span is None:
            continue
        if operation_span[1] < other_span[0] < naming_span[0]:
            return None, "retained_object_intervening_operation"

    port = _mapping(outputs[output_index])
    record = {
        "schema_version": POST_OPERATION_RETAINED_OBJECT_SCHEMA,
        "rule_id": POST_OPERATION_RETAINED_OBJECT_RULE_ID,
        "rule_version": POST_OPERATION_RETAINED_OBJECT_RULE_VERSION,
        "step_index": step_index,
        "macro_step_id": _text(step.get("macro_step_id")),
        "operation_fact_id": _text(operation_fact.get("fact_id")),
        "output": {
            "state_path": (
                f"material_graph[{step_index}]"
                f".material_outputs[{output_index}].state"
            ),
            "name_path": (
                f"material_graph[{step_index}]"
                f".material_outputs[{output_index}].name"
            ),
            "output_index": output_index,
            "material_instance_id": _text(port.get("material_instance_id")),
            "material_id": _text(port.get("material_id")),
            "label": label,
        },
        "retained_object_surface": object_surface,
        "retained_object": _normalize_object_surface(object_surface),
        "naming_fact_id": _text(name_fact.get("fact_id")),
        "naming_excerpt": naming_excerpt,
        "operation_span": [operation_span[0], operation_span[1]],
        "naming_span": [naming_span[0], naming_span[1]],
    }
    return record, ""


__all__ = [
    "POST_OPERATION_RETAINED_OBJECT_SCHEMA",
    "POST_OPERATION_RETAINED_OBJECT_RULE_ID",
    "POST_OPERATION_RETAINED_OBJECT_RULE_VERSION",
    "build_excerpt_span_resolver",
    "derive_post_operation_retained_object",
]
