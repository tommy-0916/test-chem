"""Verification mode for route fields; independent of evidence provenance."""

from __future__ import annotations

import re
import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

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
