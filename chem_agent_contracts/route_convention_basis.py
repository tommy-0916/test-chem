"""Exact, source-scoped proof for a state inherited through one material edge.

This does not review chemistry or create a material relation. It only checks a
relation already declared in a route graph against the versioned convention
resource and two independently locatable paper facts. The caller must still
verify those quotations against the original experimental group.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .route_field_basis import (
    affirmative_material_operation_span, controlled_state_mapping,
    split_rule_pattern_matches, state_source_locally_attributed,
)
from .route_retained_object import (
    POST_OPERATION_RETAINED_OBJECT_RULE_ID,
    POST_OPERATION_RETAINED_OBJECT_SCHEMA,
)
from .v2 import normalize_material_state


_RESOURCE = Path(__file__).resolve().parents[1] / "chem_resources" / "chemistry_conventions" / "conventions.json"
_OUTPUT_STATE = re.compile(
    r"material_graph\[(0|[1-9][0-9]*)\]\.material_outputs"
    r"\[(0|[1-9][0-9]*)\]\.state\Z"
)
_INPUT_STATE = re.compile(
    r"material_graph\[(0|[1-9][0-9]*)\]\."
    r"(material_inputs|material_intermediates)"
    r"\[(0|[1-9][0-9]*)\]\.state\Z"
)
_OUTPUT_QUANTITY = re.compile(
    r"material_graph\[(0|[1-9][0-9]*)\]\.material_outputs"
    r"\[(0|[1-9][0-9]*)\]\.quantity\.value\Z"
)
_SPLIT_COUNT_UNITS = frozenset({
    "part", "parts", "portion", "portions", "fraction", "fractions",
    "aliquot", "aliquots",
})
_CONCENTRATION_UNITS = frozenset({"M", "mM", "mol/L", "mmol/L", "mol l-1", "mmol l-1"})
_RULE_EVENTS = {
    "SPLIT_V1": ("split_same_material", "split_material", "split_from_parent"),
    "TRANSFER_V1": ("process_same_material", "transfer_material", "transfer_of"),
}
_STATE_CHANGE_RULE_EVENTS = ("state_change", "transform_material", "state_change_of")
_INHERITANCE_RULE_ID = "PARENT_OUTPUT_STATE_INHERITANCE_V1"
_INHERITANCE_RULE_VERSION = "1.0.0"
_LIQUID_EXCERPT_TOKENS = ("water", "solvent", "aqua", "水", "溶剂")


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


def is_concentration_unit(unit: Any) -> bool:
    """Whether a unit string denotes a concentration in the controlled set."""
    return isinstance(unit, str) and unit.strip() in _CONCENTRATION_UNITS


def output_quantity_role_issue(field_path: str, graph: Sequence[Any], unit: str) -> bool:
    """A split count or concentration cannot be one output material's amount."""
    match = _OUTPUT_QUANTITY.fullmatch(field_path)
    if match is None:
        return False
    if is_concentration_unit(unit):
        return True
    # A count of portions describes cardinality of an operation. It does not
    # specify how much material each output contains, even if the proposal
    # omitted its relation graph entirely.
    return unit.strip().casefold() in _SPLIT_COUNT_UNITS


def _rule_is_state_change(rule: Mapping[str, Any]) -> bool:
    """A rule derives a new state when it declares so by any of its fields."""
    if rule.get("event_kind") == "state_change":
        return True
    if rule.get("output_states") != ["same_as_input"]:
        return True
    return _mapping(rule.get("lineage_effect")).get("relation_type") == "state_change_of"


def _state_change_rule_shape_ok(rule: Mapping[str, Any]) -> bool:
    preconditions = rule.get("preconditions")
    outputs = rule.get("output_states")
    retained = rule.get("retained_output")
    return bool(
        isinstance(preconditions, Mapping)
        and isinstance(preconditions.get("operation_patterns"), list)
        and isinstance(preconditions.get("intent_patterns"), list)
        and isinstance(rule.get("allowed_input_states"), list)
        and rule.get("allowed_input_states")
        and isinstance(outputs, list)
        and isinstance(retained, str)
        and retained
        and retained in outputs
        and _mapping(rule.get("lineage_effect")).get("relation_type") == "state_change_of"
    )


def _rule_liquid_participation(rule: Mapping[str, Any]) -> Mapping[str, Any]:
    premise = _mapping(rule.get("liquid_participation"))
    return premise if premise.get("required") is True else {}


def _step_has_liquid_participation(
    inputs: Sequence[Mapping[str, Any]], operation_excerpt: str,
    liquid_states: Sequence[str] = ("solution",),
    excerpt_tokens: Sequence[str] = _LIQUID_EXCERPT_TOKENS,
    operation_patterns: Sequence[str] = (),
) -> bool:
    """Whether a liquid takes part: a liquid-state input port or named liquid."""
    liquid = {_text(state) for state in liquid_states if _text(state)} or {"solution"}
    for port in inputs:
        state = _text(_mapping(port).get("state"))
        if state and normalize_material_state(state) in liquid:
            return True
    if operation_patterns:
        # The named medium must govern an affirmed mention of this rule's
        # operation in its own clause, not just appear in the excerpt.
        return _liquid_medium_for_operation(
            operation_excerpt, operation_patterns, excerpt_tokens,
        )
    return _liquid_medium_in_excerpt(operation_excerpt, excerpt_tokens)


def _rule_resource() -> tuple[dict[str, Mapping[str, Any]], str]:
    try:
        raw = _RESOURCE.read_bytes()
        payload = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return {}, ""
    if (not isinstance(payload, dict)
            or payload.get("schema") != "chemistry-conventions/v1"
            or not isinstance(payload.get("rules"), list)):
        return {}, ""
    rules: dict[str, Mapping[str, Any]] = {}
    for rule in payload["rules"]:
        if (not isinstance(rule, dict)
                or not isinstance(rule.get("rule_id"), str)
                or rule.get("numeric_generation_allowed") is not False
                or not isinstance(rule.get("version"), str)):
            continue
        rule_id = rule["rule_id"]
        if rule_id in _RULE_EVENTS:
            if rule.get("output_states") != ["same_as_input"]:
                continue
        elif not (_rule_is_state_change(rule) and _state_change_rule_shape_ok(rule)):
            continue
        rules[rule_id] = rule
    return rules, "sha256_" + sha256(raw).hexdigest()


def _evidence_id(paper_id: str, group_id: str, fact_id: str) -> str:
    return "route_fact_" + sha256(
        f"{paper_id}\0{group_id}\0{fact_id}".encode("utf-8")
    ).hexdigest()[:24]


def _parse_retained_object_record(
    resolver: Any, field_path: str,
) -> tuple[Mapping[str, Any] | None, str, bool]:
    """Resolve and SHAPE-parse a post-operation retained-object record.

    Returns ``(record, issue, invalid)``: the shape-parsed record, or the
    resolver's own issue string when no record exists for the field, or
    ``invalid=True`` when a record was returned but is not shaped like a
    post-operation-retained-object/v1 record (schema version, rule id,
    output state path present, retained object non-empty).  Parsing alone
    grants ZERO authority: ``_cross_check_retained_object_record`` must
    also verify the record content against the graph and facts before any
    rule may stand on it.
    """
    raw, issue = resolver(field_path)
    if raw is None:
        return None, _text(issue), False
    record = _mapping(raw)
    output = _mapping(record.get("output"))
    valid = bool(
        record
        and record.get("schema_version") == POST_OPERATION_RETAINED_OBJECT_SCHEMA
        and _text(record.get("rule_id")) == POST_OPERATION_RETAINED_OBJECT_RULE_ID
        and _text(output.get("state_path"))
        and _text(record.get("retained_object"))
    )
    return (record if valid else None), "", not valid


def _cross_check_retained_object_record(
    record: Mapping[str, Any], graph: Sequence[Any], facts: Sequence[Any],
    field_path: str,
) -> bool:
    """Verify a parsed record's CONTENT against the graph and the facts.

    A retained-object record has zero authority of its own: it is trusted
    only when every claim it makes is reproduced by the typed graph and
    the route facts at this layer.  Checked, in order: the output state
    path is exactly the field being proven; the graph output port at that
    path exists and the record's instance id, material id and label equal
    the port's; the output name fact carries the record's
    ``output_name_fact_id`` (which must equal ``naming_fact_id``) and the
    record's naming excerpt; the step's operation fact carries the
    record's ``operation_fact_id``; and ``step_index``/``macro_step_id``
    match the graph step.  Any mismatch rejects the record.
    """
    output = _mapping(record.get("output"))
    if _text(output.get("state_path")) != field_path:
        return False
    match = _OUTPUT_STATE.fullmatch(field_path)
    if match is None:
        return False
    step_index, output_index = int(match.group(1)), int(match.group(2))
    if not isinstance(graph, (list, tuple)) or step_index >= len(graph):
        return False
    step = _mapping(graph[step_index])
    outputs = _items(step.get("material_outputs"))
    if output_index >= len(outputs):
        return False
    port = _mapping(outputs[output_index])
    if (output.get("output_index") != output_index
            or _text(output.get("material_instance_id"))
            != _text(port.get("material_instance_id"))
            or _text(output.get("material_id")) != _text(port.get("material_id"))
            or _text(output.get("label")) != _text(port.get("name"))):
        return False
    if (record.get("step_index") != step_index
            or _text(record.get("macro_step_id"))
            != _text(step.get("macro_step_id"))):
        return False
    by_path: dict[str, Mapping[str, Any]] = {}
    for raw in facts or []:
        fact = _mapping(raw)
        path = _text(fact.get("field_path"))
        if path and path not in by_path:
            by_path[path] = fact
    name_fact = by_path.get(_text(output.get("name_path")))
    if name_fact is None:
        return False
    if (_text(name_fact.get("fact_id"))
            != _text(record.get("output_name_fact_id"))
            or _text(record.get("output_name_fact_id"))
            != _text(record.get("naming_fact_id"))
            or _text(name_fact.get("excerpt"))
            != _text(record.get("naming_excerpt"))):
        return False
    operation_fact = by_path.get(f"material_graph[{step_index}].operation")
    if operation_fact is None:
        return False
    return _text(operation_fact.get("fact_id")) == _text(
        record.get("operation_fact_id"))


def _literal_in_quote(value: str, quote: str) -> bool:
    """Locate a proposed operation word inside its own quote.

    ASCII words keep word-boundary matching.  CJK text has no word
    boundaries, so a value with a non-ASCII edge binds by plain occurrence;
    without this a Chinese operation word could never bind its own quote.
    """
    if not value or not quote:
        return False
    left = r"(?<!\w)" if value[0].isascii() else ""
    right = r"(?!\w)" if value[-1].isascii() else ""
    return bool(re.search(
        left + re.escape(value) + right, quote, flags=re.IGNORECASE,
    ))


_NEGATION_PREFIX_EN = re.compile(
    r"\b(?:not|never|no|without|wasn't|weren't|isn't|aren't"
    r"|didn't|doesn't|don't|won't)\s*$",
    flags=re.IGNORECASE,
)
_NEGATION_PREFIX_ZH = re.compile(r"(?:未|没有|没|不|无|非)$")
_SUBSTRATE_SUFFIX_EN = re.compile(
    r"^[A-Za-z]{0,6}\s+(?:onto|upon|over|on)\b", flags=re.IGNORECASE,
)
_SUBSTRATE_SUFFIX_ZH = re.compile(r"^(?:到|于|在)[^。，,;；]{0,8}?(?:上|表面)")
_NEGATION_WINDOW = 25
_CLAUSE_BOUNDARY = re.compile(r"[.!?;。；！？]")
# Event-scope boundary: a coordinated clause with its own new subject and
# finite predicate (", and the catalyst ink was prepared …") is a different
# event — its medium never wets the first clause's operation.  Coordinated
# predicates of one subject (", and redispersed in water", ", and aged for
# 20 h") carry no new subject/auxiliary pair and do not split.
_COORDINATED_CLAUSE_BOUNDARY = re.compile(
    r",\s+and\s+(?="
    r"(?:(?:the|a|an|this|that|these|those|all|each|both|it|he|she|we|they)\s+)?"
    r"[A-Za-z][\w-]*(?:\s+[A-Za-z][\w-]*){0,3}?\s+"
    r"(?:was|were|is|are|has|have|had|been|will|would|could|should|may"
    r"|might|must|shall|underwent|undergoes|undergo)\b"
    r")",
    flags=re.IGNORECASE,
)
_ALTERNATIVE_PREFIX_EN = re.compile(
    r"\b(?:instead\s+of|rather\s+than|in\s+lieu\s+of|as\s+opposed\s+to)"
    r"(?:\s+(?:being|be|getting|get))?\s*$",
    flags=re.IGNORECASE,
)
_ALTERNATIVE_PREFIX_ZH = re.compile(r"(?:而不是|而非|而不是被)$")
_ALTERNATIVE_WINDOW = 48
_ATTEMPT_PREFIX_EN = re.compile(
    r"\b(?:attempt(?:ed|s|ing)?|tried|trying|sought|seeking)\s+to\b",
    flags=re.IGNORECASE,
)
_ATTEMPT_PREFIX_ZH = re.compile(r"(?:试图|尝试|妄图)")
_FAILURE_SUFFIX_EN = re.compile(
    r"^\s*(?:,?\s*but\s+)?(?:failed|was\s+unsuccessful|were\s+unsuccessful"
    r"|unsuccessfully|without\s+success|in\s+vain|to\s+no\s+avail)\b",
    flags=re.IGNORECASE,
)
_FAILURE_SUFFIX_ZH = re.compile(r"(?:失败|未果|未遂)")


def _operation_assertion_polarity(
    haystack: str, token_start: int, match_end: int,
) -> str:
    """How the surrounding clause asserts one operation mention.

    Returns one of ``"negated"``, ``"alternative"``, ``"attempt_failed"``,
    or ``"affirmed"``.  The clause is the text between the sentence
    boundaries around the mention.  A mention the paper negates ("was not
    redispersed", "未重新分散"), names only as the rejected half of an
    alternative ("dried instead of being redispersed", "而不是重新分散"), or
    reports as a failed attempt ("attempted to redisperse … but failed",
    "重新分散失败") is not the affirmed operation a convention rule proves.
    """
    text = haystack if isinstance(haystack, str) else ""
    if not text or not 0 <= token_start <= match_end <= len(text):
        return "affirmed"
    clause_start = 0
    for boundary in _CLAUSE_BOUNDARY.finditer(text, 0, token_start):
        clause_start = boundary.end()
    clause_end = len(text)
    forward = _CLAUSE_BOUNDARY.search(text, match_end)
    if forward is not None:
        clause_end = forward.start()
    # Assertion words scope the whole Latin token, not the matched stem
    # ("redispersed but failed", not "redisperse" + "d but failed").
    token_end = match_end
    while (token_end < len(text)
           and text[token_end].isascii()
           and (text[token_end].isalnum() or text[token_end] == "_")):
        token_end += 1
    prefix = text[clause_start:token_start]
    suffix = text[token_end:clause_end]
    window = text[max(0, token_start - _NEGATION_WINDOW):token_start]
    if (_NEGATION_PREFIX_EN.search(window)
            or _NEGATION_PREFIX_ZH.search(window)):
        return "negated"
    if (_ALTERNATIVE_PREFIX_EN.search(prefix[-_ALTERNATIVE_WINDOW:])
            or _ALTERNATIVE_PREFIX_ZH.search(prefix)):
        return "alternative"
    if (_ATTEMPT_PREFIX_EN.search(prefix)
            or _ATTEMPT_PREFIX_ZH.search(prefix)
            or _FAILURE_SUFFIX_EN.match(suffix)
            or _FAILURE_SUFFIX_ZH.search(suffix)):
        return "attempt_failed"
    return "affirmed"


def _affirmative_pattern_match(
    pattern: str, text: str, *, reject_substrate: bool = False,
) -> bool:
    """Whether a convention pattern occurs affirmed in the evidence text.

    Patterns are stems and match by substring, but an occurrence counts
    only when the paper affirms it: an occurrence scoped by an adjacent
    English or Chinese negation ("was not redispersed", "未重新分散"), named
    only as the rejected half of an alternative ("dried instead of being
    redispersed"), or reported as a failed attempt ("attempted to redisperse
    … but failed") does not count, and with ``reject_substrate`` an
    occurrence governed by a surface preposition ("dispersed onto carbon
    paper", "分散到…上") is a coating/deposition mention, never the bulk
    operation a state-change convention rule proves.
    """
    stem = _text(pattern)
    haystack = _text(text)
    if not stem or not haystack:
        return False
    for match in re.finditer(re.escape(stem.casefold()), haystack.casefold()):
        # A stem may match inside a larger Latin word ("disperse" inside
        # "redispersed"); assertion polarity scopes the whole word, not the
        # stem.
        token_start = match.start()
        while (token_start > 0
               and haystack[token_start - 1].isascii()
               and (haystack[token_start - 1].isalnum()
                    or haystack[token_start - 1] == "_")):
            token_start -= 1
        if (_operation_assertion_polarity(haystack, token_start, match.end())
                != "affirmed"):
            continue
        if reject_substrate:
            suffix = haystack[match.end():match.end() + 24]
            if (_SUBSTRATE_SUFFIX_EN.match(suffix)
                    or _SUBSTRATE_SUFFIX_ZH.match(suffix)):
                continue
        return True
    return False


def _liquid_medium_in_excerpt(
    excerpt: str, excerpt_tokens: Sequence[str],
) -> bool:
    """Whether the excerpt names a liquid as the dispersing medium.

    Token presence alone is not participation: "a water bath" only heats,
    "a water-free medium" excludes water, "脱水" removes water, and "rinsed
    with water" washes a surface — none of them disperses.  English counts
    only a governed medium phrase ("redispersed in water", "dispersed into
    deionized water"); Chinese counts 于水/…水中 while excluding 脱水 and
    水浴.
    """
    text = _text(excerpt)
    if not text:
        return False
    for token in excerpt_tokens:
        liquid = _text(token)
        if not liquid:
            continue
        if liquid.isascii():
            medium = re.compile(
                # Bounded medium phrase: an optional quantity + volume unit
                # (+ optional "of") — "in 30 mL of deionized water" — then
                # up to three modifier words before the liquid token.
                rf"\b(?:in|into)\s+"
                rf"(?:(?:[~≈]|ca\.|about|approx\.?)?\s*\d+(?:\.\d+)?\s*"
                rf"(?:mL|ml|µL|μL|uL|L|liters?|litres?|cc)\s+"
                rf"(?:of\s+)?)?"
                rf"(?:[\w.%µ/-]+\s+){{0,3}}?"
                rf"{re.escape(liquid)}\b(?![\s-]*bath)"
                rf"(?!\s*[-–—](?:free|less)\b)",
                flags=re.IGNORECASE,
            )
            if medium.search(text):
                return True
            continue
        for match in re.finditer(re.escape(liquid), text):
            before = text[match.start() - 1:match.start()]
            after = text[match.end():match.end() + 1]
            if before in ("脱", "除", "无") or after == "浴":
                continue
            if before == "于" or after == "中":
                return True
    return False


def _liquid_medium_for_operation(
    excerpt: str, operation_patterns: Sequence[str],
    excerpt_tokens: Sequence[str],
) -> bool:
    """Whether an affirmed operation mention's own clause names the medium.

    The medium must belong to the operation's clause, not merely to the
    excerpt: "The solid was redispersed. The reactor was washed in water."
    never wets the redispersion, and neither does a coordinated clause with
    its own new subject and predicate ("…was redispersed, and the catalyst
    ink was prepared in water.").  Coordinated predicates of the same
    subject ("…was stirred, and redispersed in water.") stay one segment.
    Only affirmed, non-substrate mentions bind a clause (a negated,
    alternative, failed-attempt, or surface-governed mention is not the
    bulk operation).
    """
    text = _text(excerpt)
    if not text:
        return False
    boundaries = sorted(
        (boundary.start(), boundary.end())
        for boundary in (
            list(_CLAUSE_BOUNDARY.finditer(text))
            + list(_COORDINATED_CLAUSE_BOUNDARY.finditer(text))
        )
    )
    segments: list[tuple[int, int]] = []
    segment_start = 0
    for boundary_start, boundary_end in boundaries:
        if boundary_start < segment_start:
            continue
        segments.append((segment_start, boundary_start))
        segment_start = boundary_end
    segments.append((segment_start, len(text)))
    for pattern in operation_patterns or []:
        stem = _text(pattern)
        if not stem:
            continue
        for match in re.finditer(re.escape(stem.casefold()), text.casefold()):
            token_start = match.start()
            while (token_start > 0
                   and text[token_start - 1].isascii()
                   and (text[token_start - 1].isalnum()
                        or text[token_start - 1] == "_")):
                token_start -= 1
            if (_operation_assertion_polarity(text, token_start, match.end())
                    != "affirmed"):
                continue
            suffix = text[match.end():match.end() + 24]
            if (_SUBSTRATE_SUFFIX_EN.match(suffix)
                    or _SUBSTRATE_SUFFIX_ZH.match(suffix)):
                continue
            segment = next(
                (span for span in segments
                 if span[0] <= match.start() < span[1]),
                None,
            )
            if (segment is not None
                    and _liquid_medium_in_excerpt(
                        text[segment[0]:segment[1]], excerpt_tokens)):
                return True
    return False


def _proof_for_evidence(
    graph: Sequence[Any], field_path: str, *,
    parent_source_value: str, parent_excerpt: str,
    operation_value: str, operation_excerpt: str,
    parent_evidence_id: str, operation_evidence_id: str,
    paper_id: str, experimental_group_id: str, source_digest: str,
    retained_object_record: Mapping[str, Any] | None = None,
    retained_object_issue: str = "",
    parent_state_proven: bool = False,
) -> tuple[dict[str, str] | None, str]:
    """Recompute one exact proof; no candidate-provided rule identifier is used.

    ``parent_state_proven`` is the proof-DAG hook: a composed proof graph sets
    it only after the parent state premise carries its own verified node, so
    the flat literal parent gate is skipped.  The default keeps legacy
    behavior byte-identical.
    """
    match = _OUTPUT_STATE.fullmatch(field_path)
    if match is None:
        return None, "semantic_binding_pending"
    step_index, output_index = int(match.group(1)), int(match.group(2))
    if step_index >= len(graph):
        return None, "convention_graph_path_missing"
    step = _mapping(graph[step_index])
    outputs, inputs = _items(step.get("material_outputs")), _items(step.get("material_inputs"))
    if output_index >= len(outputs):
        return None, "convention_graph_path_missing"
    child = outputs[output_index]
    target_state = _text(child.get("state"))
    child_id = _text(child.get("material_instance_id"))
    child_material_id = _text(child.get("material_id"))
    if not all((target_state, child_id, child_material_id)):
        return None, "convention_material_identity_missing"
    if not all((paper_id, experimental_group_id, source_digest,
                parent_evidence_id, operation_evidence_id)):
        return None, "convention_source_scope_missing"
    if not _literal_in_quote(operation_value, operation_excerpt):
        return None, "convention_operation_fact_unbound"
    if _text(step.get("operation")) != operation_value:
        return None, "convention_operation_mismatch"

    relations = [relation for relation in _items(step.get("material_relations"))
                 if child_id in relation.get("output_material_instance_ids", [])]
    if len(relations) != 1:
        return None, "convention_material_relation_missing_or_ambiguous"
    relation = relations[0]
    parent_ids = relation.get("input_material_instance_ids")
    if not isinstance(parent_ids, list) or len(parent_ids) != 1:
        return None, "convention_parent_relation_ambiguous"
    parent_id = _text(parent_ids[0])
    parents = [(index, port) for index, port in enumerate(inputs)
               if _text(port.get("material_instance_id")) == parent_id]
    if len(parents) != 1:
        return None, "convention_parent_material_missing"
    input_index, parent = parents[0]
    if _text(parent.get("material_id")) != child_material_id:
        return None, "convention_parent_state_or_identity_mismatch"
    parent_state = _text(parent.get("state"))
    if _text(parent.get("material_origin")) == "upstream_output":
        references = parent.get("parent_output_refs")
        if not isinstance(references, list) or len(references) != 1:
            return None, "convention_upstream_reference_missing"
        reference = _mapping(references[0])
        upstream = [
            port for earlier in graph[:step_index]
            for port in _items(_mapping(earlier).get("material_outputs"))
            if (_text(_mapping(earlier).get("macro_step_id"))
                == _text(reference.get("macro_step_id"))
                and _text(port.get("material_instance_id"))
                == _text(reference.get("material_instance_id")))
        ]
        # The declared upstream output is this input port carried forward, so
        # its state must equal the parent input state exactly.  For a
        # same-state rule that is also the child state checked below.
        if (len(upstream) != 1
                or _text(upstream[0].get("material_id")) != child_material_id
                or _text(upstream[0].get("state")) != parent_state):
            return None, "convention_upstream_reference_mismatch"
    parent_state_path = f"material_graph[{step_index}].material_inputs[{input_index}].state"
    if not parent_state_proven:
        _state_mapping, issue = controlled_state_mapping(
            parent_state_path, parent_source_value, parent.get("state"),
        )
        if issue or not state_source_locally_attributed(
            parent_source_value, parent_excerpt, parent.get("name"),
        ):
            return None, "convention_parent_state_unverified"

    relation_id = _text(relation.get("relation_id"))
    segment_id = _text(relation.get("source_operation_ref"))
    segments = [segment for segment in _items(step.get("operation_segments"))
                if _text(segment.get("segment_id")) == segment_id]
    if not relation_id or not segment_id or len(segments) != 1:
        return None, "convention_operation_segment_missing"
    from .v2 import MaterialOperationSegmentV2, MaterialRelationV2

    try:
        MaterialRelationV2.model_validate(relation, strict=True)
        MaterialOperationSegmentV2.model_validate(segments[0], strict=True)
    except (TypeError, ValueError):
        return None, "convention_material_relation_or_segment_invalid"
    relation_provenance = _mapping(relation.get("provenance"))
    segment_provenance = _mapping(segments[0].get("provenance"))
    if any(
        provenance.get("kind") != "paper"
        or _text(provenance.get("reference")) != operation_evidence_id
        for provenance in (relation_provenance, segment_provenance)
    ):
        return None, "convention_operation_evidence_missing"

    rules, resource_digest = _rule_resource()
    operation_blob = operation_value.casefold()
    matched_families = set()
    for rule_id, rule in rules.items():
        if rule_id in _RULE_EVENTS:
            continue
        family_patterns = _mapping(rule.get("preconditions")).get("operation_patterns") or []
        if any(_text(pattern).casefold() in operation_blob
               for pattern in family_patterns if _text(pattern)):
            matched_families.add(_text(rule.get("operation")) or rule_id)
    if len(matched_families) > 1 and retained_object_record is None:
        # The step declares several distinct state-change operations (e.g.
        # "centrifugation−redispersion"): without a validated post-operation
        # retained-object source record a composite operation may not be
        # proven by picking the one rule whose endpoint the proposal likes.
        # The source relation's own blocker (a preceding mention, an
        # intervening operation, ...) surfaces when one was recorded.
        return None, retained_object_issue or "convention_rule_not_applicable_or_ambiguous"
    eligible: list[Mapping[str, Any]] = []
    record_matched_rule_ids: set[str] = set()
    liquid_denied = False
    for rule_id, rule in rules.items():
        if rule_id in _RULE_EVENTS:
            event_kind, material_effect, lineage_type = _RULE_EVENTS[rule_id]
            state_change_rule = False
        else:
            event_kind, material_effect, lineage_type = _STATE_CHANGE_RULE_EVENTS
            state_change_rule = True
        if (relation.get("event_kind") != event_kind
                or segments[0].get("material_effect") != material_effect
                or _mapping(rule.get("lineage_effect")).get("relation_type") != lineage_type):
            continue
        relation_output_ids = relation.get("output_material_instance_ids", [])
        if state_change_rule:
            if len(relation_output_ids) != 1:
                continue
        elif rule_id == "TRANSFER_V1" and len(relation_output_ids) != 1:
            continue
        preconditions = _mapping(rule.get("preconditions"))
        intent_blob = (operation_value + " " + operation_excerpt).casefold()
        patterns = preconditions.get("operation_patterns", [])
        intents = preconditions.get("intent_patterns", [])
        allowed_states = preconditions.get("input_states", [])
        pattern_matches = (
            split_rule_pattern_matches if rule_id == "SPLIT_V1"
            else lambda pattern, text: _text(pattern).casefold() in text
        )
        if (not isinstance(patterns, list) or not isinstance(intents, list)
                or not isinstance(allowed_states, list)
                or not any(pattern_matches(pattern, operation_blob)
                           for pattern in patterns if _text(pattern))):
            continue
        accepts_record = bool(
            state_change_rule
            and retained_object_record is not None
            and preconditions.get("accept_post_operation_retained_object") is True
        )
        if state_change_rule:
            # The paper must affirm this operation and its intent: mentions
            # scoped by an adjacent negation, and operation mentions
            # governed by a surface preposition, are not the bulk
            # state-change operation this rule proves.
            if not any(
                _affirmative_pattern_match(
                    pattern, operation_excerpt, reject_substrate=True,
                )
                for pattern in patterns if _text(pattern)
            ):
                continue
            # A rule declaring accept_post_operation_retained_object takes
            # the post-operation naming record as the intent/object source
            # relation; the verbatim intent phrase is not required then.
            if (not accepts_record
                    and not any(
                        _affirmative_pattern_match(pattern, operation_excerpt)
                        for pattern in intents if _text(pattern)
                    )):
                continue
        elif not any(pattern_matches(pattern, intent_blob)
                     for pattern in intents if _text(pattern)):
            continue
        rule_inputs = rule.get("allowed_input_states", [])
        if not isinstance(rule_inputs, list):
            continue
        if state_change_rule:
            declared_output = _text(rule.get("retained_output"))
            if (target_state != declared_output
                    or parent_state not in allowed_states
                    or parent_state not in rule_inputs):
                continue
            retained_objects = preconditions.get("retained_object_patterns", [])
            if isinstance(retained_objects, list) and retained_objects:
                if accepts_record:
                    # The source relation discharges the object premise: the
                    # rule's object patterns match the naming record's
                    # normalized retained object, never the operation quote.
                    if not any(
                        split_rule_pattern_matches(
                            pattern,
                            _text(retained_object_record.get("retained_object")),
                        )
                        for pattern in retained_objects if _text(pattern)
                    ):
                        continue
                    record_matched_rule_ids.add(_text(rule.get("rule_id")))
                elif not any(
                    split_rule_pattern_matches(pattern, operation_excerpt)
                    for pattern in retained_objects if _text(pattern)
                ):
                    # A retained/affected-object rule must find its object
                    # affirmed in the operation evidence, never in a negated
                    # mention.
                    continue
            liquid_premise = _rule_liquid_participation(rule)
            if liquid_premise:
                liquid_states = liquid_premise.get("liquid_states", ["solution"])
                excerpt_tokens = liquid_premise.get("excerpt_tokens")
                tokens = tuple(excerpt_tokens) if isinstance(excerpt_tokens, list) and excerpt_tokens else _LIQUID_EXCERPT_TOKENS
                if not _step_has_liquid_participation(
                    inputs, operation_excerpt, liquid_states, tokens,
                    operation_patterns=patterns,
                ):
                    liquid_denied = True
                    continue
        else:
            # Same-state inheritance: the child keeps the verified parent state.
            if (parent_state != target_state
                    or target_state not in allowed_states
                    or target_state not in rule_inputs):
                continue
        eligible.append(rule)
    if len(matched_families) > 1:
        # Composite operation with a validated source record: only
        # record-consistent rules survive.  A rule with retained-object
        # patterns is consistent only when the record object matched them;
        # a rule without them is consistent only when the record object
        # normalizes to exactly its retained endpoint (fail-closed: an
        # unmapped object normalizes to "unknown" and matches nothing).
        # A conflict between the record and a family endpoint stays
        # pending; nothing may stand on segment order or one family's
        # premises alone.
        record_object = _text(retained_object_record.get("retained_object"))
        consistent: list[Mapping[str, Any]] = []
        for rule in eligible:
            rule_objects = _mapping(rule.get("preconditions")).get(
                "retained_object_patterns",
            )
            if isinstance(rule_objects, list) and rule_objects:
                if _text(rule.get("rule_id")) in record_matched_rule_ids:
                    consistent.append(rule)
            elif normalize_material_state(record_object) == _text(
                rule.get("retained_output")
            ):
                consistent.append(rule)
        eligible = consistent
    if not eligible and liquid_denied:
        return None, "convention_liquid_participation_missing"
    if len(eligible) != 1:
        return None, "convention_rule_not_applicable_or_ambiguous"
    rule = eligible[0]
    lineage = _mapping(step.get("lineage_relation"))
    expected_lineage_type = (
        _RULE_EVENTS[rule["rule_id"]][2] if rule["rule_id"] in _RULE_EVENTS
        else _STATE_CHANGE_RULE_EVENTS[2]
    )
    if (lineage.get("relation_type") != expected_lineage_type
            or lineage.get("parent_material_instance_ids") != [parent_id]
            or lineage.get("child_material_instance_ids")
            != relation.get("output_material_instance_ids")):
        return None, "convention_lineage_relation_mismatch"
    if rule["rule_id"] in _RULE_EVENTS:
        operation_kind = "split" if rule["rule_id"] == "SPLIT_V1" else "transfer"
        if affirmative_material_operation_span(
            operation_excerpt, _text(parent.get("name")), operation_kind,
        ) is None:
            # A transferred vessel, electron, or unrelated material is not proof
            # that this exact parent material was transferred or split.
            return None, "convention_operation_material_attribution_unresolved"
    if rule["rule_id"] == "SPLIT_V1":
        child_ids = relation.get("output_material_instance_ids", [])
        resolved = [port for port in outputs
                    if _text(port.get("material_instance_id")) in child_ids]
        declared_parts = re.findall(
            r"\b([1-9][0-9]*)\s+(?:parts|fractions|portions)\b",
            operation_excerpt, flags=re.IGNORECASE,
        )
        if (len(child_ids) < 2 or len(set(child_ids)) != len(child_ids)
                or len(resolved) != len(child_ids)
                or len(declared_parts) != 1
                or int(declared_parts[0]) != len(child_ids)
                or any(_text(port.get("material_id")) != child_material_id
                       or _text(port.get("state")) != target_state
                       for port in resolved)):
            return None, "convention_split_children_incomplete"
    proof = {
        "schema_version": "route-convention-state/v1",
        "field_path": field_path,
        "target_state": target_state,
        "parent_state_path": parent_state_path,
        "parent_source_value": parent_source_value,
        "parent_evidence_id": parent_evidence_id,
        "operation_path": f"material_graph[{step_index}].operation",
        "operation_evidence_id": operation_evidence_id,
        "relation_id": relation_id,
        "parent_instance_id": parent_id,
        "child_instance_id": child_id,
        "rule_id": _text(rule.get("rule_id")),
        "rule_version": _text(rule.get("version")),
        "resource_digest": resource_digest,
        "paper_id": paper_id,
        "experimental_group_id": experimental_group_id,
        "source_digest": source_digest,
    }
    if _text(rule.get("rule_id")) in record_matched_rule_ids:
        # The winning rule stood on the post-operation retained-object
        # source relation; the proof names that relation and its evidence,
        # including the source-binding spans and fact ids the record was
        # recomputed from.  Verification re-derives the record from the
        # live resolver and demands exact dict equality, so any change to
        # the source text or facts invalidates the proof.
        proof["retained_object_rule_id"] = _text(
            retained_object_record.get("rule_id"),
        )
        proof["retained_object_rule_version"] = _text(
            retained_object_record.get("rule_version"),
        )
        proof["retained_object"] = _text(retained_object_record.get("retained_object"))
        proof["retained_object_evidence_id"] = _evidence_id(
            paper_id, experimental_group_id,
            _text(retained_object_record.get("naming_fact_id")),
        )
        proof["retained_object_output_name_fact_id"] = _text(
            retained_object_record.get("output_name_fact_id"),
        )
        proof["retained_object_operation_locator"] = _text(
            retained_object_record.get("operation_locator"),
        )
        proof["retained_object_operation_span"] = json.dumps(
            _mapping(retained_object_record).get("operation_span") or [],
            separators=(",", ":"),
        )
        proof["retained_object_naming_locator"] = _text(
            retained_object_record.get("naming_locator"),
        )
        proof["retained_object_naming_span"] = json.dumps(
            _mapping(retained_object_record).get("naming_span") or [],
            separators=(",", ":"),
        )
        operation_char_span = _mapping(retained_object_record).get(
            "operation_char_span") or []
        proof["retained_object_operation_char_span"] = (
            f"{operation_char_span[0]}:{operation_char_span[1]}"
            if len(operation_char_span) == 2 else ""
        )
        naming_char_span = _mapping(retained_object_record).get(
            "naming_char_span") or []
        proof["retained_object_naming_char_span"] = (
            f"{naming_char_span[0]}:{naming_char_span[1]}"
            if len(naming_char_span) == 2 else ""
        )
    return proof, ""


def derive_unreviewed_output_state(
    graph: Sequence[Any], facts: Sequence[Any], field_path: str, *,
    paper_id: str, experimental_group_id: str, source_digest: str,
    retained_object_resolver: Any = None,
    parent_state_proven: bool = False,
) -> tuple[dict[str, str] | None, str]:
    """Produce a proof only from existing graph edges and proposed source facts.

    ``parent_state_proven`` is set only by the typed proof-DAG layer after the
    parent premise carries its own verified node; the default is the legacy
    literal parent gate.
    """
    by_path: dict[str, Mapping[str, Any]] = {}
    for raw in facts:
        fact = _mapping(raw)
        path = _text(fact.get("field_path"))
        if not path or path in by_path:
            return None, "convention_fact_path_missing_or_duplicate"
        by_path[path] = fact
    child_fact = by_path.get(field_path)
    match = _OUTPUT_STATE.fullmatch(field_path)
    if child_fact is None or match is None:
        return None, "semantic_binding_pending"
    retained_object_record: Mapping[str, Any] | None = None
    retained_object_issue = ""
    if retained_object_resolver is not None:
        retained_object_record, retained_object_issue, invalid_record = (
            _parse_retained_object_record(retained_object_resolver, field_path)
        )
        if invalid_record or (
            retained_object_record is not None
            and not _cross_check_retained_object_record(
                retained_object_record, graph, facts, field_path,
            )
        ):
            # A record that fails shape parsing or whose content does not
            # match the graph and facts at this layer is rejected outright:
            # a record has zero authority of its own.
            return None, "retained_object_output_binding_unresolved"
    step_index, output_index = int(match.group(1)), int(match.group(2))
    try:
        step = _mapping(graph[step_index])
        child = _items(step.get("material_outputs"))[output_index]
    except (IndexError, TypeError):
        return None, "convention_graph_path_missing"
    if _text(child_fact.get("value")) != _text(child.get("state")):
        return None, "convention_child_state_value_mismatch"
    operation_path = f"material_graph[{step_index}].operation"
    operation_fact = by_path.get(operation_path)
    if operation_fact is None:
        return None, "convention_operation_fact_missing"
    if (_text(child_fact.get("excerpt")) != _text(operation_fact.get("excerpt"))
            or child_fact.get("source") != operation_fact.get("source")):
        return None, "convention_child_quote_not_operation_quote"
    relation_candidates = [relation for relation in _items(step.get("material_relations"))
                           if _text(child.get("material_instance_id"))
                           in relation.get("output_material_instance_ids", [])]
    if len(relation_candidates) != 1:
        return None, "convention_material_relation_missing_or_ambiguous"
    parent_ids = relation_candidates[0].get("input_material_instance_ids")
    if not isinstance(parent_ids, list) or len(parent_ids) != 1:
        return None, "convention_parent_relation_ambiguous"
    parent_ports = [(index, port) for index, port in enumerate(_items(step.get("material_inputs")))
                    if _text(port.get("material_instance_id")) == _text(parent_ids[0])]
    if len(parent_ports) != 1:
        return None, "convention_parent_material_missing"
    parent_state_path = f"material_graph[{step_index}].material_inputs[{parent_ports[0][0]}].state"
    parent_fact = by_path.get(parent_state_path)
    if parent_fact is None:
        return None, "convention_parent_state_fact_missing"
    support_sources = [_mapping(fact.get("source"))
                       for fact in (child_fact, parent_fact, operation_fact)]
    # Before quote association the unsigned model proposal has only one
    # source_group_ref. This is a proof candidate, never a verified locator.
    if any(support_sources):
        if not all(support_sources):
            return None, "convention_source_scope_mismatch"
        for source in support_sources:
            if (source.get("paper_id") != paper_id
                    or source.get("experimental_group_id") != experimental_group_id
                    or source.get("source_digest") != source_digest):
                return None, "convention_source_scope_mismatch"
    operation_id = _text(operation_fact.get("fact_id"))
    parent_id = _text(parent_fact.get("fact_id"))
    if not operation_id or not parent_id or not _text(child_fact.get("fact_id")):
        return None, "convention_support_fact_identity_missing"
    operation_evidence_id = _evidence_id(paper_id, experimental_group_id, operation_id)
    relation = relation_candidates[0]
    segment_id = _text(relation.get("source_operation_ref"))
    segments = [segment for segment in _items(step.get("operation_segments"))
                if _text(segment.get("segment_id")) == segment_id]
    if len(segments) != 1:
        return None, "convention_operation_segment_missing"
    # Unsigned graph placeholders are not paper evidence yet. The compiler
    # replaces them only after the cited literal operation fact is checked.
    for node in (relation, segments[0]):
        provenance = _mapping(node.get("provenance"))
        if provenance.get("kind") != "paper" or provenance.get("reference") != f"fact:{operation_id}":
            return None, "convention_operation_evidence_missing"
    prepared = json.loads(json.dumps(graph))
    prepared_step = prepared[step_index]
    for node in (*prepared_step.get("material_relations", []),
                 *prepared_step.get("operation_segments", [])):
        provenance = node.get("provenance")
        if isinstance(provenance, dict) and provenance.get("reference") == f"fact:{operation_id}":
            provenance["reference"] = operation_evidence_id
    return _proof_for_evidence(
        prepared, field_path,
        parent_source_value=_text(parent_fact.get("value")),
        parent_excerpt=_text(parent_fact.get("excerpt")),
        operation_value=_text(operation_fact.get("value")),
        operation_excerpt=_text(operation_fact.get("excerpt")),
        parent_evidence_id=_evidence_id(paper_id, experimental_group_id, parent_id),
        operation_evidence_id=operation_evidence_id,
        paper_id=paper_id, experimental_group_id=experimental_group_id,
        source_digest=source_digest,
        retained_object_record=retained_object_record,
        retained_object_issue=retained_object_issue,
        parent_state_proven=parent_state_proven,
    )


def is_convention_inheritance_proof(proof: Any) -> bool:
    """Whether a proof record denotes parent-output state inheritance."""
    return isinstance(proof, Mapping) and proof.get("rule_id") == _INHERITANCE_RULE_ID


def convention_fact_evidence_by_id(
    facts: Sequence[Any], *, paper_id: str, experimental_group_id: str,
) -> dict[str, Mapping[str, Any]]:
    """Key raw route facts by the evidence id a convention proof binds to."""
    evidence: dict[str, Mapping[str, Any]] = {}
    for raw in facts or []:
        fact = _mapping(raw)
        fact_id = _text(fact.get("fact_id"))
        if fact_id:
            evidence[_evidence_id(paper_id, experimental_group_id, fact_id)] = fact
    return evidence


def _resolve_parent_output(
    graph: Sequence[Any], step_index: int, reference: Mapping[str, Any],
) -> tuple[int, int, Mapping[str, Any]] | tuple[None, None, None]:
    """Resolve one declared parent output ref to an earlier output port."""
    ref_step = _text(reference.get("macro_step_id"))
    ref_instance = _text(reference.get("material_instance_id"))
    if not ref_step or not ref_instance:
        return None, None, None
    upstream = [
        (index, output_index, port)
        for index, earlier in enumerate(graph[:step_index])
        for output_index, port in enumerate(_items(_mapping(earlier).get("material_outputs")))
        if (_text(_mapping(earlier).get("macro_step_id")) == ref_step
            and _text(port.get("material_instance_id")) == ref_instance)
    ]
    if len(upstream) != 1:
        return None, None, None
    return upstream[0]


def _resolve_fact_provenance(
    graph: Sequence[Any], *, paper_id: str, experimental_group_id: str,
    operation_evidence_id: str,
) -> list[Any]:
    """Rewrite ``fact:<id>`` operation placeholders to their bound evidence id.

    At receipt/compile time a graph still carries unsigned ``fact:`` references;
    a saved proof binds only evidence ids.  A placeholder is rewritten only when
    its fact id deterministically hashes to the operation evidence id the proof
    claims, so no unrelated reference is touched.
    """
    prepared = json.loads(json.dumps([_mapping(step) for step in graph]))
    for step in prepared:
        if not isinstance(step, dict):
            continue
        for node in (*step.get("material_relations", []),
                     *step.get("operation_segments", [])):
            if not isinstance(node, dict):
                continue
            provenance = node.get("provenance")
            reference = (provenance.get("reference")
                         if isinstance(provenance, dict) else None)
            if (isinstance(reference, str) and reference.startswith("fact:")
                    and _evidence_id(paper_id, experimental_group_id,
                                     reference[len("fact:"):]) == operation_evidence_id):
                provenance["reference"] = operation_evidence_id
    return prepared


def _inheritance_proof_for_evidence(
    graph: Sequence[Any], field_path: str, *,
    parent_source_value: str, parent_excerpt: str, parent_evidence_id: str,
    operation_excerpt: str, operation_evidence_id: str,
    paper_id: str, experimental_group_id: str, source_digest: str,
    retained_object_resolver: Any = None,
    facts: Sequence[Any] | None = None,
) -> tuple[dict[str, str] | None, str]:
    """Recompute one input-state inheritance proof; never guess missing refs."""
    match = _INPUT_STATE.fullmatch(field_path)
    if match is None:
        return None, "semantic_binding_pending"
    step_index, collection, port_index = (
        int(match.group(1)), match.group(2), int(match.group(3)),
    )
    if step_index >= len(graph):
        return None, "convention_graph_path_missing"
    step = _mapping(graph[step_index])
    ports = _items(step.get(collection))
    if port_index >= len(ports):
        return None, "convention_graph_path_missing"
    port = ports[port_index]
    target_state = _text(port.get("state"))
    child_instance = _text(port.get("material_instance_id"))
    material_id = _text(port.get("material_id"))
    if not all((target_state, child_instance, material_id)):
        return None, "convention_material_identity_missing"
    if not all((paper_id, experimental_group_id, source_digest,
                parent_source_value, parent_evidence_id)):
        return None, "convention_source_scope_missing"
    if _text(port.get("material_origin")) != "upstream_output":
        return None, "convention_upstream_reference_missing"
    references = port.get("parent_output_refs")
    if not isinstance(references, list) or len(references) != 1:
        return None, "convention_upstream_reference_missing"
    parent_step_index, parent_output_index, parent_port = _resolve_parent_output(
        graph, step_index, _mapping(references[0]),
    )
    if parent_port is None:
        return None, "convention_upstream_reference_mismatch"
    parent_state = _text(parent_port.get("state"))
    parent_instance = _text(parent_port.get("material_instance_id"))
    parent_state_path = (
        f"material_graph[{parent_step_index}]"
        f".material_outputs[{parent_output_index}].state"
    )
    if (not parent_state
            or parent_state != target_state
            or parent_instance != child_instance
            or _text(parent_port.get("material_id")) != material_id):
        return None, "convention_parent_state_or_identity_mismatch"
    record = {
        "schema_version": "route-convention-state/v1",
        "field_path": field_path,
        "target_state": target_state,
        "parent_state_path": parent_state_path,
        "parent_instance_id": parent_instance,
        "child_instance_id": child_instance,
        "rule_id": _INHERITANCE_RULE_ID,
        "rule_version": _INHERITANCE_RULE_VERSION,
        "resource_digest": "",
        "paper_id": paper_id,
        "experimental_group_id": experimental_group_id,
        "source_digest": source_digest,
    }
    if operation_evidence_id:
        # The parent output state is itself convention-derived; re-derive that
        # proof first.  The record then binds the grandparent state evidence
        # exactly like the parent proof did.
        parent_record: Mapping[str, Any] | None = None
        parent_record_issue = ""
        if retained_object_resolver is not None:
            # A composite parent operation may stand on the post-operation
            # retained-object source relation; re-parse that record for
            # the parent output state path before re-deriving its proof.
            # The record has zero authority: its content must also match
            # the graph and the facts supplied at this layer.
            parent_record, parent_record_issue, invalid_record = (
                _parse_retained_object_record(
                    retained_object_resolver, parent_state_path,
                )
            )
            if invalid_record or (
                parent_record is not None
                and (
                    facts is None
                    or not _cross_check_retained_object_record(
                        parent_record, graph, facts, parent_state_path,
                    )
                )
            ):
                # No record content is ever trusted without a cross-check
                # against the graph and facts at this layer.
                return None, "convention_parent_state_unverified"
        prepared = _resolve_fact_provenance(
            graph, paper_id=paper_id, experimental_group_id=experimental_group_id,
            operation_evidence_id=operation_evidence_id,
        )
        parent_proof, parent_issue = _proof_for_evidence(
            prepared, parent_state_path,
            parent_source_value=parent_source_value,
            parent_excerpt=parent_excerpt,
            operation_value=_text(_mapping(graph[parent_step_index]).get("operation")),
            operation_excerpt=operation_excerpt,
            parent_evidence_id=parent_evidence_id,
            operation_evidence_id=operation_evidence_id,
            paper_id=paper_id, experimental_group_id=experimental_group_id,
            source_digest=source_digest,
            retained_object_record=parent_record,
            retained_object_issue=parent_record_issue,
        )
        if parent_issue or parent_proof is None:
            return None, "convention_parent_state_unverified"
        if (parent_proof["target_state"] != target_state
                or parent_proof["child_instance_id"] != parent_instance):
            return None, "convention_parent_state_or_identity_mismatch"
        record.update({
            "parent_source_value": parent_proof["parent_source_value"],
            "parent_evidence_id": parent_proof["parent_evidence_id"],
            "operation_path": parent_proof["operation_path"],
            "operation_evidence_id": parent_proof["operation_evidence_id"],
            "relation_id": parent_proof["relation_id"],
        })
        return record, ""
    # The parent output state stands on its own literal/attribution gates.
    _state_mapping, mapping_issue = controlled_state_mapping(
        parent_state_path, parent_source_value, parent_port.get("state"),
    )
    if (mapping_issue
            or not state_source_locally_attributed(
                parent_source_value, parent_excerpt, parent_port.get("name"),
            )):
        return None, "convention_parent_state_unverified"
    record.update({
        "parent_source_value": parent_source_value,
        "parent_evidence_id": parent_evidence_id,
        "operation_path": "",
        "operation_evidence_id": "",
        "relation_id": "",
    })
    return record, ""


def derive_unreviewed_input_state(
    graph: Sequence[Any], facts: Sequence[Any], field_path: str,
    source_scope: Mapping[str, Any], *,
    retained_object_resolver: Any = None,
) -> tuple[dict[str, str] | None, str]:
    """Prove an input/intermediate state by inheritance from a verified parent.

    The port must declare ``material_origin="upstream_output"`` with exactly
    one ``parent_output_ref`` resolving to an earlier output port whose state
    was itself verified — by the output convention derivation first, then by
    the unchanged literal/attribution gates.  Ambiguous or missing references
    return no proof (``""`` issue) so the caller's existing gates stay in
    charge; nothing is ever guessed.
    """
    scope = _mapping(source_scope)
    paper_id = _text(scope.get("paper_id"))
    experimental_group_id = _text(scope.get("experimental_group_id"))
    source_digest = _text(scope.get("source_digest"))
    match = _INPUT_STATE.fullmatch(field_path)
    if match is None:
        return None, "semantic_binding_pending"
    if not all((paper_id, experimental_group_id, source_digest)):
        return None, ""
    by_path: dict[str, Mapping[str, Any]] = {}
    for raw in facts or []:
        fact = _mapping(raw)
        path = _text(fact.get("field_path"))
        if path and path not in by_path:
            by_path[path] = fact
    fact = by_path.get(field_path)
    if fact is None:
        return None, ""
    step_index, collection, port_index = (
        int(match.group(1)), match.group(2), int(match.group(3)),
    )
    if step_index >= len(graph):
        return None, ""
    step = _mapping(graph[step_index])
    ports = _items(step.get(collection))
    if port_index >= len(ports):
        return None, ""
    port = ports[port_index]
    target_state = _text(port.get("state"))
    if _text(fact.get("value")) != target_state or not target_state:
        return None, ""
    if _text(port.get("material_origin")) != "upstream_output":
        return None, ""
    references = port.get("parent_output_refs")
    if not isinstance(references, list) or len(references) != 1:
        return None, ""
    parent_step_index, parent_output_index, parent_port = _resolve_parent_output(
        graph, step_index, _mapping(references[0]),
    )
    if parent_port is None:
        return None, ""
    parent_state = _text(parent_port.get("state"))
    if parent_state != target_state:
        return None, ""
    parent_state_path = (
        f"material_graph[{parent_step_index}]"
        f".material_outputs[{parent_output_index}].state"
    )
    parent_fact = by_path.get(parent_state_path)
    if (parent_fact is None
            or _text(parent_fact.get("value")) != parent_state
            or not _text(parent_fact.get("fact_id"))):
        return None, ""
    parent_proof, _parent_issue = derive_unreviewed_output_state(
        graph, facts, parent_state_path,
        paper_id=paper_id, experimental_group_id=experimental_group_id,
        source_digest=source_digest,
        retained_object_resolver=retained_object_resolver,
    )
    if parent_proof is not None:
        grandparent_fact = by_path.get(parent_proof["parent_state_path"])
        operation_fact = by_path.get(parent_proof["operation_path"])
        return _inheritance_proof_for_evidence(
            graph, field_path,
            parent_source_value=parent_proof["parent_source_value"],
            parent_excerpt=(_text(grandparent_fact.get("excerpt"))
                            if grandparent_fact is not None else ""),
            parent_evidence_id=parent_proof["parent_evidence_id"],
            operation_excerpt=(_text(operation_fact.get("excerpt"))
                               if operation_fact is not None else ""),
            operation_evidence_id=parent_proof["operation_evidence_id"],
            paper_id=paper_id, experimental_group_id=experimental_group_id,
            source_digest=source_digest,
            retained_object_resolver=retained_object_resolver,
            facts=facts,
        )
    return _inheritance_proof_for_evidence(
        graph, field_path,
        parent_source_value=_text(parent_fact.get("value")),
        parent_excerpt=_text(parent_fact.get("excerpt")),
        parent_evidence_id=_evidence_id(
            paper_id, experimental_group_id, _text(parent_fact.get("fact_id"))),
        operation_excerpt="", operation_evidence_id="",
        paper_id=paper_id, experimental_group_id=experimental_group_id,
        source_digest=source_digest,
    )


def verify_bound_output_state(
    proof: Any, graph: Sequence[Any], evidence_by_id: Mapping[str, Any], *,
    paper_id: str, experimental_group_id: str, source_digest: str,
    retained_object_resolver: Any = None,
) -> str:
    """Recompute a saved proof from the typed graph, evidence and rule bytes."""
    if not isinstance(proof, Mapping) or any(not isinstance(value, str)
                                             for value in proof.values()):
        return "convention_proof_invalid"
    if (proof.get("paper_id") != paper_id
            or proof.get("experimental_group_id") != experimental_group_id
            or proof.get("source_digest") != source_digest):
        return "convention_source_scope_mismatch"
    field_path = _text(proof.get("field_path"))
    if _OUTPUT_STATE.fullmatch(field_path) is not None:
        parent = _mapping(evidence_by_id.get(_text(proof.get("parent_evidence_id"))))
        operation = _mapping(evidence_by_id.get(_text(proof.get("operation_evidence_id"))))
        if not parent or not operation:
            return "convention_support_evidence_missing"
        retained_object_record: Mapping[str, Any] | None = None
        retained_object_issue = ""
        if _text(proof.get("retained_object_rule_id")):
            # A proof standing on the post-operation retained-object source
            # relation must re-resolve, re-parse and re-cross-check that
            # record against the graph and facts; proofs without
            # retained-object fields recompute without one.
            if retained_object_resolver is None:
                return "convention_support_evidence_missing"
            retained_object_record, retained_object_issue, invalid_record = (
                _parse_retained_object_record(
                    retained_object_resolver, field_path,
                )
            )
            if invalid_record:
                return "retained_object_output_binding_unresolved"
            if retained_object_record is None:
                return "convention_support_evidence_missing"
            if not _cross_check_retained_object_record(
                retained_object_record, graph,
                tuple(evidence_by_id.values()), field_path,
            ):
                return "retained_object_output_binding_unresolved"
        step_index = int(_OUTPUT_STATE.fullmatch(field_path).group(1))
        if step_index >= len(graph):
            return "convention_graph_path_missing"
        step = _mapping(graph[step_index])
        expected, issue = _proof_for_evidence(
            graph, field_path,
            parent_source_value=_text(proof.get("parent_source_value")),
            parent_excerpt=_text(parent.get("excerpt")),
            operation_value=_text(step.get("operation")),
            operation_excerpt=_text(operation.get("excerpt")),
            parent_evidence_id=_text(proof.get("parent_evidence_id")),
            operation_evidence_id=_text(proof.get("operation_evidence_id")),
            paper_id=paper_id, experimental_group_id=experimental_group_id,
            source_digest=source_digest,
            retained_object_record=retained_object_record,
            retained_object_issue=retained_object_issue,
        )
        if issue:
            return issue
        return "" if dict(proof) == expected else "convention_proof_mismatch"
    if (_INPUT_STATE.fullmatch(field_path) is not None
            and is_convention_inheritance_proof(proof)):
        parent = _mapping(evidence_by_id.get(_text(proof.get("parent_evidence_id"))))
        if not parent:
            return "convention_support_evidence_missing"
        operation_evidence_id = _text(proof.get("operation_evidence_id"))
        operation = _mapping(evidence_by_id.get(operation_evidence_id))
        if operation_evidence_id and not operation:
            return "convention_support_evidence_missing"
        expected, issue = _inheritance_proof_for_evidence(
            graph, field_path,
            parent_source_value=_text(proof.get("parent_source_value")),
            parent_excerpt=_text(parent.get("excerpt")),
            parent_evidence_id=_text(proof.get("parent_evidence_id")),
            operation_excerpt=_text(operation.get("excerpt")),
            operation_evidence_id=operation_evidence_id,
            paper_id=paper_id, experimental_group_id=experimental_group_id,
            source_digest=source_digest,
            # Forward the resolver: an inheritance proof whose parent output
            # stands on a retained-object record can only re-derive that
            # parent proof with the live record, cross-checked against the
            # same evidence facts.
            retained_object_resolver=retained_object_resolver,
            facts=tuple(evidence_by_id.values()),
        )
        if issue:
            return issue
        return "" if dict(proof) == expected else "convention_proof_mismatch"
    return "convention_proof_invalid"


def canonical_state_derivation(proof: Mapping[str, str]) -> str:
    return json.dumps(dict(proof), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


__all__ = [
    "derive_unreviewed_output_state", "derive_unreviewed_input_state",
    "verify_bound_output_state", "canonical_state_derivation",
    "output_quantity_role_issue", "is_convention_inheritance_proof",
    "convention_fact_evidence_by_id",
]
