"""Protocol definitions and same-group protocol references (narrow proof class).

A paper may DEFINE a named protocol once — "a centrifugation−redispersion
protocol using deionized water three times, which was the first
centrifugation−redispersion protocol" — and later REFERENCE it by name and
ordinal — "after a second centrifugation−redispersion protocol one time".
This module is the contract-level machinery for exactly that pattern and
nothing else:

- ``extract_protocol_definition`` deterministically extracts a
  ``protocol-definition/v1`` record from a definition phrase.  The record's
  closed slot set is ``operation_sequence`` (ordered ``segment_identity`` /
  ``segment_order`` pairs decomposed from the compound protocol name, the
  same decomposition the typed graph uses for composite operations such as
  graph[5]'s ``ms5-seg-centrifugation`` / ``ms5-seg-redispersion``) and
  ``liquid_medium`` (the medium named by the ``using`` clause that governs
  the protocol mention syntactically).  Provenance annotations (definition
  evidence id, ordinal anchor, locator/char span, group scope) accompany the
  slots.  There is NO execution-count slot anywhere: "three times" is
  invocation-local and is recorded only as an annotation on a resolution
  record, never on the definition.  ``validate_protocol_definition``
  rejects ANY key outside the closed set — result/object slots such as
  ``retained_object``, ``output_state``, ``material_instance_identity``,
  ``execution_count``, ``inter_segment_material_flow`` and friends are
  forbidden outright, so a protocol reference can never smuggle "first run
  produced X ⇒ second run produces X" into a proof.

- ``resolve_protocol_reference`` resolves a reference phrase against the
  candidate definition mentions of ONE pinned experimental-group scope
  (paper id + group id + source digest): normalized protocol-name equality
  (U+2212/dash variants, case, whitespace), exactly one in-scope definition
  mention (zero → ``protocol_reference_unresolved``, two or more →
  ``protocol_reference_ambiguous``), the definition must textually precede
  the reference (projected character offsets), and the ordinal evidence
  must be consistent (the reference ordinal strictly follows the
  definition's anchor ordinal, else ``protocol_reference_ordinal_mismatch``;
  a definition named only outside the pinned scope fails
  ``protocol_reference_scope_mismatch``).  A nearer non-definition medium
  phrase ("dispersed in 30 mL of water and aged") is never a candidate:
  only mentions that extract as definitions count.  Several facts quoting
  the SAME definition sentence dedupe to one mention.  The resolution
  record (``protocol-reference-proof/v1``) is explicit and traceable:
  definition evidence id + reference evidence id + both ordinals + both
  invocation-local execution counts + group scope + the content digest of
  the definition it stands on.

The typed proof-DAG layer (``route_proof_dag``) turns one resolution record
into a ``protocol_reference`` node; the flat engine
(``route_convention_basis``) accepts only the resulting
``_VerifiedLiquidMedium`` capability token at the liquid-participation
gate.  Nothing here discharges retained-object, output-state, or
material-identity premises, and nothing resolves across groups.
"""

from __future__ import annotations

from hashlib import sha256
import json
import re
from typing import Any, Mapping, Sequence

from .route_convention_basis import _LIQUID_EXCERPT_TOKENS

PROTOCOL_DEFINITION_SCHEMA = "protocol-definition/v1"
PROTOCOL_REFERENCE_PROOF_SCHEMA = "protocol-reference-proof/v1"
PROTOCOL_REFERENCE_RULE_ID = "PROTOCOL_REFERENCE_V1"
PROTOCOL_REFERENCE_RULE_VERSION = "1.0.0"

_OPERATION_PATH = re.compile(
    r"material_graph\[(0|[1-9][0-9]*)\]\.operation\Z")

# The closed slot set + provenance annotations of a protocol-definition/v1
# record.  Anything else — result/object slots, execution counts, material
# flow — is rejected by ``validate_protocol_definition``.
_DEFINITION_KEYS = frozenset({
    "schema_version",
    "protocol_name",
    "operation_sequence",
    "liquid_medium",
    "definition_evidence_id",
    "definition_ordinal_anchor",
    "paper_id",
    "experimental_group_id",
    "source_digest",
    "locator",
    "char_span",
})
_DEFINITION_REQUIRED = _DEFINITION_KEYS - {"locator", "char_span"}

_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}
_ORDINAL_SURFACE = (
    r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth"
    r"|[0-9]{1,2}(?:st|nd|rd|th))"
)
_DASH_VARIANTS = ("−", "‐", "‑", "–", "—")
_PROTOCOL_NAME = (
    r"([A-Za-z][\w]*(?:[-‐‑–—−][\w]+)*\s+protocol)"
)
# A definition mention: the protocol name immediately governed by a
# "using <medium>" clause (syntactic scope — the medium modifies the
# protocol phrase), the medium phrase ending at the invocation-local
# execution count or a clause boundary.
_DEFINITION_MENTION = re.compile(
    r"\b" + _PROTOCOL_NAME + r"\s+using\s+(.+?)(?=[,.;:!?。；]|$)",
    flags=re.IGNORECASE,
)
_MEDIUM_COUNT = re.compile(
    r"^(.*?)(?:\s+((?:[0-9]+|one|two|three|four|five|six|seven|eight|nine"
    r"|ten)\s+times?))?\s*$",
    flags=re.IGNORECASE,
)
# The definition's ordinal anchor: "which was the first <same name>".
_ORDINAL_ANCHOR = re.compile(
    r"\bwhich\s+was\s+the\s+(" + _ORDINAL_SURFACE + r")\s+"
    + _PROTOCOL_NAME + r"\b",
    flags=re.IGNORECASE,
)
_REFERENCE_VALUE = re.compile(
    r"^(?:(" + _ORDINAL_SURFACE + r")\s+)?(.+?\bprotocol)\s*\.?\s*$",
    flags=re.IGNORECASE,
)
_REFERENCE_MENTION = re.compile(
    r"\b(" + _ORDINAL_SURFACE + r")\s+" + _PROTOCOL_NAME + r"\b",
    flags=re.IGNORECASE,
)
_INVOCATION_COUNT = re.compile(
    r"^\s*((?:[0-9]+|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"\s+times?)\b",
    flags=re.IGNORECASE,
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical(payload: Any) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def normalize_protocol_name(name: Any) -> str:
    """Normalize a protocol name for equality: dash variants (U+2212 minus,
    figure/en/em dashes) fold to ASCII hyphen, case folds, whitespace
    collapses, and spaces around hyphens are removed.
    """
    text = _text(name)
    for dash in _DASH_VARIANTS:
        text = text.replace(dash, "-")
    text = " ".join(text.split()).casefold()
    return re.sub(r"\s*-\s*", "-", text)


def _ordinal_value(surface: str) -> int | None:
    word = _text(surface).casefold()
    if word in _ORDINAL_WORDS:
        return _ORDINAL_WORDS[word]
    match = re.match(r"^([0-9]{1,2})(?:st|nd|rd|th)?$", word)
    return int(match.group(1)) if match is not None else None


def _ordinal_word(surface: str) -> str:
    """The recorded ordinal surface: the word form for word/digit input."""
    value = _ordinal_value(surface)
    for word, number in _ORDINAL_WORDS.items():
        if number == value:
            return word
    return _text(surface).casefold()


def _segment_sequence(normalized_name: str) -> list[dict]:
    """Compound-name decomposition into ordered segment identities.

    "centrifugation-redispersion protocol" decomposes into
    ``segment_identity`` "centrifugation" (order 1) then "redispersion"
    (order 2) — the same identities the typed graph's composite operation
    segments carry (``ms5-seg-centrifugation`` / ``ms5-seg-redispersion``).
    """
    stem = normalized_name
    if stem.endswith(" protocol"):
        stem = stem[: -len(" protocol")]
    return [
        {"segment_identity": identity, "segment_order": index + 1}
        for index, identity in enumerate(
            part for part in (part.strip() for part in stem.split("-"))
            if part
        )
    ]


def _medium_has_liquid_token(medium: str) -> bool:
    blob = medium.casefold()
    return any(_text(token).casefold() in blob
               for token in _LIQUID_EXCERPT_TOKENS if _text(token))


def _parse_reference(
    reference_value: str, reference_excerpt: str,
) -> tuple[str, str, str] | None:
    """(ordinal, normalized name, execution count) for a reference phrase.

    The ordinal + protocol name come from the reference value when it
    parses (the value is the model's normalized operation string, already
    bound literally to its quote by the engine); otherwise they come from
    an ordinal+governed mention inside the excerpt.  The invocation-local
    execution count ("one time") is read off the excerpt after the name.
    """
    value = _text(reference_value)
    excerpt = _text(reference_excerpt)
    ordinal = ""
    name = ""
    parsed = _REFERENCE_VALUE.match(value) if value else None
    if parsed is not None:
        ordinal = _text(parsed.group(1))
        name = normalize_protocol_name(parsed.group(2))
    if not name:
        mention = _REFERENCE_MENTION.search(excerpt)
        if mention is None:
            return None
        ordinal = _text(mention.group(1))
        name = normalize_protocol_name(mention.group(2))
    count = ""
    mention = _REFERENCE_MENTION.search(excerpt)
    if mention is not None:
        tail = _INVOCATION_COUNT.match(excerpt[mention.end():])
        if tail is not None:
            count = " ".join(_text(tail.group(1)).split())
    if not name:
        return None
    return (_ordinal_word(ordinal) if ordinal else ""), name, count


def protocol_definition_digest(definition: Mapping[str, Any]) -> str:
    """Content digest of a whole protocol-definition/v1 record."""
    return "sha256_" + sha256(
        _canonical(dict(definition)).encode("utf-8")
    ).hexdigest()


def validate_protocol_definition(record: Any) -> str:
    """Closed-schema validation; ``""`` when the record is acceptable.

    Any key outside the closed slot+provenance set is rejected — including
    every forbidden result/object slot (``retained_object``,
    ``inter_segment_material_flow``, ``output_state``,
    ``material_instance_identity``, ``execution_count``,
    ``segment_input_material``, ``segment_output_material``,
    ``retained_phase``, ``inter_segment_flow``, ``derived_state_transition``)
    — so the definition boundary lives in the schema, not in docs.
    """
    record = _mapping(record)
    if not record:
        return "protocol_definition_invalid"
    unknown = [key for key in record if key not in _DEFINITION_KEYS]
    if unknown:
        return "protocol_definition_unknown_key"
    if any(not _text(record.get(key))
           for key in sorted(_DEFINITION_REQUIRED - {"operation_sequence"})):
        return "protocol_definition_missing_key"
    if record.get("schema_version") != PROTOCOL_DEFINITION_SCHEMA:
        return "protocol_definition_invalid"
    sequence = record.get("operation_sequence")
    if (not isinstance(sequence, list) or not sequence
            or any(not isinstance(segment, Mapping) for segment in sequence)):
        return "protocol_definition_invalid"
    orders = [segment.get("segment_order") for segment in sequence]
    if (orders != list(range(1, len(sequence) + 1))
            or any(not _text(segment.get("segment_identity"))
                   for segment in sequence)):
        return "protocol_definition_invalid"
    if _ordinal_value(_text(record.get("definition_ordinal_anchor"))) is None:
        return "protocol_definition_invalid"
    locator = record.get("locator")
    char_span = record.get("char_span")
    if locator is not None and not isinstance(locator, str):
        return "protocol_definition_invalid"
    if char_span is not None and (
        not isinstance(char_span, list)
        or (char_span and (len(char_span) != 2
                           or any(not isinstance(offset, int)
                                  for offset in char_span)))
    ):
        return "protocol_definition_invalid"
    return ""


def extract_protocol_definition(
    excerpt: str, *,
    evidence_id: str = "", locator: str = "",
    char_span: Sequence[int] = (), paper_id: str = "",
    experimental_group_id: str = "", source_digest: str = "",
) -> tuple[dict | None, str]:
    """Extract one protocol-definition/v1 record from a definition phrase.

    Returns ``(record, "")`` or ``(None, issue)`` with issue one of
    ``protocol_definition_not_present`` (no governed ``using`` mention),
    ``protocol_definition_medium_missing`` (the governed medium names no
    liquid), or ``protocol_definition_anchor_missing`` (no "which was the
    <ordinal> <name>" anchor).  A non-definition medium phrase ("dispersed
    in 30 mL of water and aged") yields no record and is never a candidate.
    """
    text = _text(excerpt)
    if not text:
        return None, "protocol_definition_not_present"
    mention = _DEFINITION_MENTION.search(text)
    if mention is None:
        return None, "protocol_definition_not_present"
    split = _MEDIUM_COUNT.match(mention.group(2))
    medium = " ".join(_text(split.group(1)).split()) if split else ""
    if not medium or not _medium_has_liquid_token(medium):
        return None, "protocol_definition_medium_missing"
    name = normalize_protocol_name(mention.group(1))
    anchor = _ORDINAL_ANCHOR.search(text)
    if (anchor is None
            or normalize_protocol_name(anchor.group(2)) != name):
        return None, "protocol_definition_anchor_missing"
    record = {
        "schema_version": PROTOCOL_DEFINITION_SCHEMA,
        "protocol_name": name,
        "operation_sequence": _segment_sequence(name),
        "liquid_medium": medium,
        "definition_evidence_id": _text(evidence_id),
        "definition_ordinal_anchor": _ordinal_word(anchor.group(1)),
        "paper_id": _text(paper_id),
        "experimental_group_id": _text(experimental_group_id),
        "source_digest": _text(source_digest),
        "locator": _text(locator),
        "char_span": [int(offset) for offset in char_span][:2] if char_span else [],
    }
    issue = validate_protocol_definition(record)
    if issue:
        return None, issue
    return record, ""


def resolve_protocol_reference(
    reference_value: str, reference_excerpt: str, *,
    reference_evidence_id: str, reference_locator: str = "",
    reference_position: int = -1,
    candidates: Sequence[Mapping[str, Any]] = (),
    paper_id: str = "", experimental_group_id: str = "",
    source_digest: str = "",
) -> tuple[dict | None, str]:
    """Resolve a protocol reference against same-group definition mentions.

    ``candidates`` are raw excerpt carriers: each a mapping with
    ``excerpt``, ``evidence_id``, ``field_path``, ``locator``,
    ``char_span`` ([start, end] projected offsets) and the carrier's own
    ``paper_id`` / ``experimental_group_id`` / ``source_digest``.  v1
    resolution rules, in order:

    1. the reference itself must parse (ordinal + protocol name), else
       ``protocol_reference_not_present``;
    2. only candidates inside the pinned group scope count — a name-matched
       definition that exists only outside the scope fails
       ``protocol_reference_scope_mismatch``;
    3. only candidates that EXTRACT as definitions count (a nearer
       non-definition medium phrase is never a candidate);
    4. normalized protocol-name equality (dash variants, case, whitespace);
    5. the definition must textually precede the reference (projected
       character offsets) — name-matched definitions that only appear later
       fail ``protocol_reference_definition_after_reference``;
    6. several facts quoting the SAME mention dedupe to one; zero remaining
       mentions fail ``protocol_reference_unresolved``, two or more
       DISTINCT mentions fail ``protocol_reference_ambiguous``;
    7. the reference ordinal must strictly follow the definition's anchor
       ordinal, else ``protocol_reference_ordinal_mismatch``.

    Returns ``(record, "")`` with an explicit, traceable
    ``protocol-reference-proof/v1`` resolution record, or ``(None, issue)``.
    """
    if not all((_text(paper_id), _text(experimental_group_id),
                _text(source_digest), _text(reference_evidence_id))):
        return None, "protocol_reference_scope_mismatch"
    parsed = _parse_reference(reference_value, reference_excerpt)
    if parsed is None:
        return None, "protocol_reference_not_present"
    reference_ordinal, name, reference_count = parsed
    if not reference_ordinal:
        return None, "protocol_reference_not_present"
    if not isinstance(reference_position, int) or reference_position < 0:
        return None, "protocol_reference_position_unknown"

    scope = (_text(paper_id), _text(experimental_group_id), _text(source_digest))
    out_of_scope_named = False
    definitions: list[tuple[dict, Mapping[str, Any]]] = []
    for raw in candidates or []:
        candidate = _mapping(raw)
        excerpt = _text(candidate.get("excerpt"))
        if not excerpt:
            continue
        candidate_scope = (
            _text(candidate.get("paper_id")),
            _text(candidate.get("experimental_group_id")),
            _text(candidate.get("source_digest")),
        )
        span = candidate.get("char_span")
        span = [int(offset) for offset in span][:2] if (
            isinstance(span, (list, tuple)) and len(span) >= 2) else []
        definition, issue = extract_protocol_definition(
            excerpt,
            evidence_id=_text(candidate.get("evidence_id")),
            locator=_text(candidate.get("locator")),
            char_span=span,
            paper_id=_text(candidate.get("paper_id")),
            experimental_group_id=_text(candidate.get("experimental_group_id")),
            source_digest=_text(candidate.get("source_digest")),
        )
        if definition is None:
            continue
        if definition["protocol_name"] != name:
            continue
        if candidate_scope != scope:
            out_of_scope_named = True
            continue
        definitions.append((definition, candidate))
    if not definitions:
        if out_of_scope_named:
            return None, "protocol_reference_scope_mismatch"
        return None, "protocol_reference_unresolved"
    # Textual precedence: the definition mention must start strictly before
    # the reference mention in the projected source text.
    preceding = [
        (definition, candidate) for definition, candidate in definitions
        if (definition.get("char_span")
            and definition["char_span"][0] < reference_position)
    ]
    if not preceding:
        return None, "protocol_reference_definition_after_reference"
    # Facts quoting the same sentence are the same mention: dedupe on
    # (normalized name, located span); the representative evidence binding
    # prefers the step operation fact, then the smallest evidence id.
    by_mention: dict[tuple, tuple[dict, Mapping[str, Any]]] = {}
    for definition, candidate in preceding:
        key = (definition["protocol_name"], tuple(definition.get("char_span") or ()))
        current = by_mention.get(key)
        rank = (
            0 if _OPERATION_PATH.fullmatch(_text(candidate.get("field_path"))) else 1,
            _text(candidate.get("evidence_id")),
        )
        if current is None or rank < (
            0 if _OPERATION_PATH.fullmatch(_text(current[1].get("field_path"))) else 1,
            _text(current[1].get("evidence_id")),
        ):
            by_mention[key] = (definition, candidate)
    if len(by_mention) != 1:
        return None, "protocol_reference_ambiguous"
    definition, _candidate = next(iter(by_mention.values()))
    definition_ordinal = _ordinal_value(definition["definition_ordinal_anchor"])
    reference_value_int = _ordinal_value(reference_ordinal)
    if (definition_ordinal is None or reference_value_int is None
            or reference_value_int <= definition_ordinal):
        return None, "protocol_reference_ordinal_mismatch"
    _mention = _DEFINITION_MENTION.search(
        _text(_candidate.get("excerpt")))
    _split = _MEDIUM_COUNT.match(_mention.group(2)) if _mention else None
    definition_count = (
        " ".join(_text(_split.group(2)).split())
        if _split is not None and _split.group(2) else ""
    )
    record = {
        "schema_version": PROTOCOL_REFERENCE_PROOF_SCHEMA,
        "rule_id": PROTOCOL_REFERENCE_RULE_ID,
        "rule_version": PROTOCOL_REFERENCE_RULE_VERSION,
        "protocol_name": name,
        "operation_sequence": json.loads(json.dumps(
            definition["operation_sequence"])),
        "liquid_medium": definition["liquid_medium"],
        "definition_evidence_id": definition["definition_evidence_id"],
        "reference_evidence_id": _text(reference_evidence_id),
        "definition_ordinal_anchor": definition["definition_ordinal_anchor"],
        "reference_ordinal": reference_ordinal,
        # Invocation-local annotations, never definition slots.
        "definition_execution_count": definition_count,
        "reference_execution_count": reference_count,
        "definition_digest": protocol_definition_digest(definition),
        "definition_locator": _text(definition.get("locator")),
        "reference_locator": _text(reference_locator),
        "paper_id": _text(paper_id),
        "experimental_group_id": _text(experimental_group_id),
        "source_digest": _text(source_digest),
    }
    return record, ""


__all__ = [
    "PROTOCOL_DEFINITION_SCHEMA", "PROTOCOL_REFERENCE_PROOF_SCHEMA",
    "PROTOCOL_REFERENCE_RULE_ID", "PROTOCOL_REFERENCE_RULE_VERSION",
    "extract_protocol_definition", "normalize_protocol_name",
    "protocol_definition_digest", "resolve_protocol_reference",
    "validate_protocol_definition",
]
