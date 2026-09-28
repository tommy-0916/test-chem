"""Verification mode for route fields; independent of evidence provenance."""

from __future__ import annotations

import re
import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from .v2 import normalize_material_state


_STATE_RESOURCE = Path(__file__).resolve().parents[1] / "chem_resources" / "material_states" / "v1.json"
_SAFE_STATE_ALIASES = frozenset({
    "dry solid", "干粉", "干燥固体", "干燥粉末", "粉末", "悬浊液", "悬浊",
    "溶液", "澄清溶液", "上清液", "上清", "滤液", "气体",
    "solution", "suspension", "powder", "supernatant", "filtrate", "gas",
})
_PORT_STATE_PATH = re.compile(
    r"material_graph\[(0|[1-9][0-9]*)\]\."
    r"(material_inputs|material_intermediates|material_outputs)"
    r"\[(0|[1-9][0-9]*)\]\.state\Z"
)
_STATE_LINK_WORDS = frozenset({"a", "an", "as", "in", "is", "of", "the", "was"})
_OUTPUT_STATE_PATH = re.compile(
    r"material_graph\[(0|[1-9][0-9]*)\]\.material_outputs"
    r"\[(0|[1-9][0-9]*)\]\.state\Z"
)
_PASSIVE_SPLIT = re.compile(
    r"\s*(?:\([^()]{0,100}\)\s*)?(?:was|were)\s+"
    r"(?!not\b|never\b)(?:split(?:\s+into)?|divided\s+into)\b",
    re.IGNORECASE,
)
_PASSIVE_TRANSFER = re.compile(
    r"\s*(?:\([^()]{0,100}\)\s*)?(?:was|were)\s+"
    r"(?!not\b|never\b)transferred\b",
    re.IGNORECASE,
)
_PASSIVE_PARENT_SPLIT = re.compile(
    r"\s*(?:\([^()]{0,100}\)\s*)?"
    r"(?:was|were)(?:n't|\s+(?:not|never))?\s+"
    r"(?:split(?:\s+into)?|divided\s+into)\b",
    re.IGNORECASE,
)
_PASSIVE_PARENT_TRANSFER = re.compile(
    r"\s*(?:\([^()]{0,100}\)\s*)?"
    r"(?:was|were)(?:n't|\s+(?:not|never))?\s+transferred\b",
    re.IGNORECASE,
)
_SPLIT_STEP_OPERATION = re.compile(
    r"\b(?:split|splits|splitting|divided\s+into)\b|分装|分液|分成|分配",
    re.IGNORECASE,
)
_TRANSFER_STEP_OPERATION = re.compile(
    r"\b(?:transfer|transfers|transferred|transferring)\b|转移|移液|移取|换瓶",
    re.IGNORECASE,
)


_GENERATED_MATERIAL_ID_PATH = re.compile(
    r"material_graph\[(?:0|[1-9][0-9]*)\]"
    r"(?:\.[a-z][a-z0-9_]*(?:\[(?:0|[1-9][0-9]*)\])?)*"
    r"\.(?:material_(?:instance_)?id|[a-z_]*material_instance_ids"
    r"\[(?:0|[1-9][0-9]*)\])\Z"
)
_CONTROLLED_STATE_PATH = re.compile(
    r"(?:material_graph\[(?:0|[1-9][0-9]*)\]"
    r"\.material_(?:inputs|intermediates|outputs)"
    r"\[(?:0|[1-9][0-9]*)\]\.state|"
    r"route_signature\.(?:endpoint_state|phase_transitions"
    r"\[(?:0|[1-9][0-9]*)\]\.(?:before_state|after_state)))\Z"
)
_CONTROLLED_OPERATION_PATH = re.compile(
    r"(?:material_graph\[(?:0|[1-9][0-9]*)\]\.operation|"
    r"route_signature\.operations\[(?:0|[1-9][0-9]*)\])\Z"
)
_CONTROLLED_MATERIAL_NAME_PATH = re.compile(
    r"material_graph\[(?:0|[1-9][0-9]*)\]\."
    r"material_(?:inputs|intermediates|outputs)"
    r"\[(?:0|[1-9][0-9]*)\]\.name\Z"
)


def classify_route_field_basis(field_path: str) -> str:
    """Select a proof method without asserting provenance or approval.

    The current proposal compiler admits source literals and generated IDs.
    Controlled terms without a verified mapping remain pending. Convention,
    derived and runtime values retain their existing separate gates. Device
    SOP is an evidence source class, not a field verification mode.
    """
    if _GENERATED_MATERIAL_ID_PATH.fullmatch(field_path):
        return "generated_id"
    if (_CONTROLLED_STATE_PATH.fullmatch(field_path)
            or _CONTROLLED_MATERIAL_NAME_PATH.fullmatch(field_path)
            or _CONTROLLED_OPERATION_PATH.fullmatch(field_path)):
        return "controlled_mapping"
    return "paper_literal"


def controlled_state_requires_mapping(field_path: str, value: Any) -> bool:
    """A literal alias cannot silently become a different V2 state token."""
    return bool(
        _CONTROLLED_STATE_PATH.fullmatch(field_path)
        and isinstance(value, str)
        and (
            normalize_material_state(value) != value
            or normalize_material_state(value) == "unknown"
        )
    )


def is_material_port_state_path(field_path: str) -> bool:
    return _PORT_STATE_PATH.fullmatch(field_path) is not None


def state_source_locally_attributed(
    source_value: Any, excerpt: Any, material_name: Any,
) -> bool:
    """Require one literal state phrase to refer locally to this port's name.

    This admits only an explicit short phrase in the same excerpt. Repeated
    mentions, another material between the name and state, and contextual
    references remain pending for semantic review; proximity alone is not
    treated as evidence of a material's state.
    """
    if any(not isinstance(value, str) or not value.strip()
           for value in (source_value, excerpt, material_name)):
        return False
    source_matches = list(re.finditer(
        rf"(?<!\w){re.escape(source_value.strip())}(?!\w)", excerpt,
        flags=re.IGNORECASE,
    ))
    name_matches = list(re.finditer(
        rf"(?<!\w){re.escape(material_name.strip())}(?!\w)", excerpt,
    ))
    if len(source_matches) != 1 or len(name_matches) != 1:
        return False
    source_match, name_match = source_matches[0], name_matches[0]
    if (source_match.start() >= name_match.start()
            and source_match.end() <= name_match.end()):
        return True
    between = (
        excerpt[name_match.end():source_match.start()]
        if name_match.end() <= source_match.start() else
        excerpt[source_match.end():name_match.start()]
        if source_match.end() <= name_match.start() else None
    )
    return bool(
        between is not None
        and not re.search(r"[^\w\s]", between)
        and all(
            word.casefold() in _STATE_LINK_WORDS
            for word in re.findall(r"[^\W\d_]\w*", between)
        )
    )


def split_rule_pattern_matches(pattern: str, text: str) -> bool:
    """Match listed English split forms at word boundaries, never a stem."""
    if not isinstance(pattern, str) or not isinstance(text, str) or not pattern:
        return False
    if all(character.isascii() and (character.isalpha() or character.isspace())
           for character in pattern):
        expression = re.compile(
            rf"(?<!\w){re.escape(pattern).replace(r'\ ', r'\s+')}(?!\w)",
            flags=re.IGNORECASE,
        )
        return any(not re.search(
            r"\b(?:not|never|wasn't|weren't)\s*$",
            text[max(0, match.start() - 25):match.start()],
            flags=re.IGNORECASE,
        ) for match in expression.finditer(text))
    return pattern in text


def passive_material_operation_span(
    excerpt: str, material_name: str, operation: str,
) -> tuple[int, int] | None:
    """Locate a passive split/transfer whose grammatical subject is material."""
    if not isinstance(excerpt, str) or not isinstance(material_name, str):
        return None
    names = list(re.finditer(
        rf"(?<!\w){re.escape(material_name.strip())}(?!\w)", excerpt,
        flags=re.IGNORECASE,
    )) if material_name.strip() else []
    if len(names) != 1:
        return None
    predicate = _PASSIVE_SPLIT if operation == "split" else (
        _PASSIVE_TRANSFER if operation == "transfer" else None
    )
    if predicate is None:
        return None
    verb = predicate.match(excerpt, names[0].end())
    return (names[0].start(), verb.end()) if verb is not None else None


def _parent_role_passive_span(
    excerpt: str, material_name: str, operation: str,
) -> tuple[int, int] | None:
    """A negated operation still describes the parent's pre-operation state."""
    if not isinstance(material_name, str) or not material_name.strip():
        return None
    mentions = list(re.finditer(
        rf"(?<!\w){re.escape(material_name.strip())}(?!\w)", excerpt,
        flags=re.IGNORECASE,
    ))
    if len(mentions) != 1:
        return None
    predicate = (_PASSIVE_PARENT_SPLIT if operation == "split" else
                 _PASSIVE_PARENT_TRANSFER if operation == "transfer" else None)
    verb = predicate.match(excerpt, mentions[0].end()) if predicate else None
    return (mentions[0].start(), verb.end()) if verb is not None else None


def _active_material_operation_span(
    excerpt: str, material_name: str, operation: str,
    *, allow_modifier_words: bool = True,
) -> tuple[int, int] | None:
    if operation == "split":
        verb = r"(?:split|splits)"
        boundary = r"\binto\b"
    elif operation == "transfer":
        verb = r"(?:transfer|transfers|transferred)"
        boundary = r"\b(?:to|into)\b"
    else:
        return None
    modifiers = r"(?:[\w-]+\s+){0,3}" if allow_modifier_words else ""
    expression = re.compile(
        rf"(?<!\w){verb}\s+(?:the\s+)?{modifiers}"
        rf"{re.escape(material_name.strip())}(?!\w)",
        flags=re.IGNORECASE,
    )
    matches = list(expression.finditer(excerpt))
    if len(matches) != 1:
        return None
    match = matches[0]
    remainder = excerpt[match.end():match.end() + 100]
    end = re.search(boundary, remainder, flags=re.IGNORECASE)
    if end is not None and not re.search(r"[,;.]", remainder[:end.start()]):
        return match.start(), match.end() + end.start()
    return match.start(), match.end()


def affirmative_material_operation_span(
    excerpt: str, material_name: str, operation: str,
) -> tuple[int, int] | None:
    """Bind an affirmative split/transfer to its material, not a nearby word."""
    passive = passive_material_operation_span(excerpt, material_name, operation)
    if passive is not None:
        return passive
    active = _active_material_operation_span(
        excerpt, material_name, operation, allow_modifier_words=False,
    )
    if active is None:
        return None
    clause = re.split(r"[.;:]", excerpt[:active[0]])[-1][-60:]
    if re.search(
        r"\b(?:not|never|without)\b|\b(?:didn't|doesn't|don't|won't)\b",
        clause, flags=re.IGNORECASE,
    ):
        return None
    return active


def output_state_parent_role_issue(
    field_path: str, graph: Sequence[Any], source_value: Any, excerpt: Any,
) -> bool:
    """A parent's state mention in a split/transfer sentence is not a child fact.

    A separately quoted, explicit child-state sentence remains eligible for
    the existing paper-literal check. This predicate supplies no child state.
    """
    match = _OUTPUT_STATE_PATH.fullmatch(field_path)
    if (match is None or not isinstance(source_value, str)
            or not isinstance(excerpt, str)):
        return False
    if not isinstance(graph, (list, tuple)):
        return False
    step_index, output_index = int(match.group(1)), int(match.group(2))
    if step_index >= len(graph):
        return False
    step = graph[step_index]
    operation_value = (step.get("operation") if isinstance(step, Mapping)
                       else getattr(step, "operation", None))
    operation_text = operation_value if isinstance(operation_value, str) else ""
    relevant = bool(
        _SPLIT_STEP_OPERATION.search(operation_text)
        or _TRANSFER_STEP_OPERATION.search(operation_text)
    )
    if not relevant:
        outputs = (step.get("material_outputs") if isinstance(step, Mapping)
                   else getattr(step, "material_outputs", None))
        if isinstance(outputs, (list, tuple)) and output_index < len(outputs):
            output = outputs[output_index]
            child_id = (output.get("material_instance_id")
                        if isinstance(output, Mapping)
                        else getattr(output, "material_instance_id", None))
            relations = (step.get("material_relations")
                         if isinstance(step, Mapping)
                         else getattr(step, "material_relations", None))
            if isinstance(relations, (list, tuple)) and child_id:
                for relation in relations:
                    event = (relation.get("event_kind")
                             if isinstance(relation, Mapping)
                             else getattr(relation, "event_kind", None))
                    ids = (relation.get("output_material_instance_ids")
                           if isinstance(relation, Mapping)
                           else getattr(relation, "output_material_instance_ids", None))
                    if (event in {"split_same_material", "process_same_material"}
                            and isinstance(ids, (list, tuple)) and child_id in ids):
                        relevant = True
                        break
    if not relevant:
        return False
    state_mentions = list(re.finditer(
        rf"(?<!\w){re.escape(source_value.strip())}(?!\w)", excerpt,
        flags=re.IGNORECASE,
    )) if source_value.strip() else []
    if len(state_mentions) != 1:
        return False
    # The quote itself can establish that its state word names the material
    # *before* a split/transfer. Do not let an incorrect or missing graph
    # input disguise that grammatical role as paper evidence for a child.
    for operation in ("split", "transfer"):
        span = _parent_role_passive_span(excerpt, source_value, operation)
        if span is not None and state_mentions[0].end() <= span[1]:
            return True
        span = _active_material_operation_span(excerpt, source_value, operation)
        if (span is not None and span[0] <= state_mentions[0].start()
                and state_mentions[0].end() <= span[1]):
            return True
    inputs = (step.get("material_inputs") if isinstance(step, Mapping)
              else getattr(step, "material_inputs", None))
    if not isinstance(inputs, (list, tuple)):
        return False
    for raw_parent in inputs:
        parent = raw_parent if isinstance(raw_parent, Mapping) else {
            "name": getattr(raw_parent, "name", ""),
            "state": getattr(raw_parent, "state", ""),
        }
        name = parent.get("name")
        possible_parent_subjects = []
        if state_source_locally_attributed(source_value, excerpt, name):
            possible_parent_subjects.append(name)
        # A proposal may use a normalized parent name while the paper calls
        # it only by its state ("the suspension"). The state word is still
        # the grammatical parent, not literal evidence for every child.
        parent_state = parent.get("state")
        if (isinstance(parent_state, str)
                and parent_state.casefold() == source_value.casefold()):
            possible_parent_subjects.append(source_value)
        for subject in possible_parent_subjects:
            for operation in ("split", "transfer"):
                span = _parent_role_passive_span(excerpt, subject, operation)
                if span is not None and state_mentions[0].end() <= span[1]:
                    return True
                span = _active_material_operation_span(excerpt, subject, operation)
                if (span is not None and span[0] <= state_mentions[0].start()
                        and state_mentions[0].end() <= span[1]):
                    return True
    return False


def _state_resource() -> tuple[dict[str, str], str]:
    """Read the versioned rule bytes, not the permissive V2 fallback cache."""
    try:
        raw = _STATE_RESOURCE.read_bytes()
        payload = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return {}, ""
    if (not isinstance(payload, dict)
            or payload.get("schema") != "material-states/v1"
            or not isinstance(payload.get("aliases"), dict)):
        return {}, ""
    aliases = payload["aliases"]
    if any(not isinstance(key, str) or not isinstance(value, str)
           for key, value in aliases.items()):
        return {}, ""
    return aliases, "sha256_" + sha256(raw).hexdigest()


def controlled_state_mapping(
    field_path: str, source_value: Any, graph_value: Any,
) -> tuple[dict[str, str] | None, str]:
    """Verify a narrow lexical state normalization, never a phase inference.

    An empty mapping means the source already spells the canonical token.
    Unknown, phase-retention, and context-sensitive terms remain pending.
    The source literal must be checked against the same-group PDF separately.
    """
    if _PORT_STATE_PATH.fullmatch(field_path) is None:
        return None, "semantic_binding_pending"
    if not isinstance(source_value, str) or not isinstance(graph_value, str):
        return None, "semantic_binding_pending"
    source, graph = source_value.strip(), graph_value.strip()
    aliases, resource_digest = _state_resource()
    if not aliases or not source or not graph:
        return None, "semantic_binding_pending"
    canonical_graph = normalize_material_state(graph)
    if source == canonical_graph and source in set(aliases.values()) and source != "unknown":
        return None, ""
    alias_key = source if source in aliases else source.casefold()
    if alias_key not in _SAFE_STATE_ALIASES:
        return None, "semantic_binding_pending"
    target = aliases.get(alias_key)
    if (target is None or target == "unknown" or target != canonical_graph):
        return None, "semantic_binding_pending"
    return {
        "schema_version": "controlled-state-mapping/v1",
        "source_value": source,
        "target_value": target,
        "rule_id": "material-states/v1:alias:" + alias_key,
        "rule_version": "material-states/v1",
        "resource_digest": resource_digest,
    }, ""


def verify_controlled_state_mapping(
    field_path: str, source_value: Any, graph_value: Any,
    record: Any,
) -> str:
    expected, issue = controlled_state_mapping(field_path, source_value, graph_value)
    if issue:
        return issue
    if expected is None:
        return "" if record is None else "semantic_binding_pending"
    if not isinstance(record, Mapping) or dict(record) != expected:
        return "semantic_binding_pending"
    return ""


def canonicalize_unreviewed_state_facts(
    proposal: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Produce source-literal state facts in an unsigned, detached proposal.

    It only corrects the case of an *existing* proposed source expression
    when that expression occurs once next to this port's literal name. It
    never selects a different state word merely because one occurs somewhere
    in the same excerpt. This audit is not source or chemical review.
    """
    detached = deepcopy(dict(proposal))
    graph, facts = detached.get("material_graph"), detached.get("route_facts")
    if not isinstance(graph, list) or not isinstance(facts, list):
        return detached, []
    rows: list[dict[str, Any]] = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        path = fact.get("field_path")
        match = _PORT_STATE_PATH.fullmatch(path) if isinstance(path, str) else None
        if match is None:
            continue
        try:
            port = graph[int(match.group(1))][match.group(2)][int(match.group(3))]
            graph_value = port["state"]
        except (KeyError, IndexError, TypeError):
            continue
        source_value, excerpt = fact.get("value"), fact.get("excerpt")
        if (not isinstance(graph_value, str) or not isinstance(excerpt, str)
                or not isinstance(source_value, str) or not source_value.strip()
                or not isinstance(port, Mapping)):
            continue
        target = normalize_material_state(graph_value)
        if target == "unknown":
            continue
        source_matches = list(re.finditer(
            rf"(?<!\w){re.escape(source_value.strip())}(?!\w)", excerpt,
            flags=re.IGNORECASE,
        ))
        if len(source_matches) != 1:
            continue
        source_match = source_matches[0]
        source = source_match.group()
        name = port.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        if not state_source_locally_attributed(source, excerpt, name):
            continue
        mapping, issue = controlled_state_mapping(path, source, target)
        if issue:
            continue
        if source != source_value or (mapping is not None and target != graph_value):
            rows.append({
                "field_path": path, "original_fact_value": source_value,
                "source_value": source, "target_value": target,
                "mapping": mapping, "status": "unsigned_lexical_normalization",
            })
            fact["value"] = source
            if mapping is not None:
                port["state"] = target
    return detached, rows
