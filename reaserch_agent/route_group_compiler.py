"""Compile explicitly extracted facts within one experimental group.

This is a *proposal* compiler, not a paper verifier.  A model may identify a
fact and a field in an explicitly supplied material graph; the compiler binds
the proposed excerpt, graph claim and group scope without inventing a route,
material edge, capability, locator or quantity.  SourceVerifier later checks
the excerpt and locators against independently registered original text.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
import math
import re
from typing import Any, Mapping, Sequence

from chem_agent_contracts.route_field_basis import (
    classify_route_field_basis, controlled_state_mapping,
    is_material_port_state_path, output_state_parent_role_issue,
    state_source_locally_attributed,
)
from chem_agent_contracts.route_convention_basis import (
    canonical_state_derivation, derive_unreviewed_output_state,
    output_quantity_role_issue,
)
from chem_agent_contracts.route_source_labels import (
    SOURCE_LABEL_RULE_VERSION, RULE_SCOPED_LABEL_IDENTITY, SourceLabelContext,
    build_source_label_context, definition_site_concentration_binding,
    quantity_identity_surfaces, state_attribution_outcome, surface_anchor,
)
from chem_agent_contracts.v2 import (
    canonical_digest, evidence_contains_exact_quantity, normalize_material_state,
)

from .route_pdf_quote_binding import normalize_pdf_quote_whitespace


_PATH_PART = re.compile(r"([a-z][a-z0-9_]*)(?:\[(0|[1-9][0-9]*)\])?\Z")
_GRAPH_ROOT = re.compile(r"material_graph\[(0|[1-9][0-9]*)\]\Z")
_SOURCE_LOCATOR = re.compile(
    r"(?:lines:[1-9][0-9]*-[1-9][0-9]*|"
    r"pdf:p[1-9][0-9]*:b[1-9][0-9]*-p[1-9][0-9]*:b[1-9][0-9]*)\Z"
)
_DOCUMENT_DIGEST = re.compile(r"sha256_[0-9a-f]{64}\Z")
_MATERIAL_AMOUNT_PATH = re.compile(
    r"(material_graph\[(?:0|[1-9][0-9]*)\]"
    r"(?:\.material_(?:inputs|intermediates|outputs)\[(?:0|[1-9][0-9]*)\])?)"
    r"\.(?:quantity\.value|concentration_value)\Z"
)
_DIMENSIONLESS_PARAMETER_PATH = re.compile(
    r"material_graph\[(0|[1-9][0-9]*)\]\.parameters"
    r"\[(0|[1-9][0-9]*)\]\.value\Z"
)
_DIMENSIONLESS_CONTAINER_COUNT_PATH = re.compile(
    r"material_graph\[(0|[1-9][0-9]*)\]\.logical_containers"
    r"\[(0|[1-9][0-9]*)\]\.count\Z"
)
_DIMENSIONLESS_PARAMETER_NAMES = frozenset({
    "ph", "ph_target", "wash_count", "cycle_count", "repeat_count",
    "molar_ratio", "volume_ratio", "ratio",
})
_LOCAL_CLAUSE = re.compile(
    r"[,;，；。]|\.(?=\s|$)|\b(?:and|plus|with)\b|[、与和及]",
    re.IGNORECASE,
)
_LINK_WORDS = frozenset({
    "a", "an", "about", "added", "approximately", "are", "as", "at",
    "containing", "dissolved", "in", "is", "of", "precursor", "solution",
    "solid", "the", "to", "used", "was", "weighed", "were",
})


@dataclass(frozen=True)
class RouteGroupCompilationDiagnosticV1:
    protocol_index: int
    group_index: int
    paper_id: str
    experimental_group_id: str
    reason_code: str


@dataclass
class RouteGroupCompilationResultV1:
    protocols: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[RouteGroupCompilationDiagnosticV1] = field(default_factory=list)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _node_value(node: Any, name: str) -> Any:
    return node.get(name) if isinstance(node, Mapping) else getattr(node, name, None)


_AMOUNT_PORT_PATH = re.compile(
    r"material_graph\[([0-9]+)\]\."
    r"(material_inputs|material_intermediates|material_outputs)\[([0-9]+)\]"
)


def _port_material_for_path(graph: Any, field_path: str) -> tuple[str, str]:
    """Resolve a graph port path to (material_id, sample_id) without binding."""
    match = _AMOUNT_PORT_PATH.match(field_path)
    if match is None or not isinstance(graph, (list, tuple)):
        return "", ""
    try:
        step = graph[int(match.group(1))]
        ports = _node_value(step, match.group(2))
        port = ports[int(match.group(3))]
    except (IndexError, KeyError, TypeError, ValueError):
        return "", ""
    return _text(_node_value(port, "material_id")), _text(
        _node_value(step, "sample_id"))


def canonicalize_proposal_material_ids(
    proposal: Mapping[str, Any],
    *,
    previous_ids: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Replace model co-reference symbols with deterministic internal IDs.

    The model may propose that two ports refer to the same material using one
    local symbol. Its spelling is not an assertion from the paper. The program
    assigns an opaque ID scoped to the source document and experimental group,
    and records the transformation for the unsigned proposal audit. Evidence
    for each port's name, amount, and relationship is checked separately.
    """
    detached = deepcopy(dict(proposal))
    ref = detached.get("source_group_ref")
    graph = detached.get("material_graph")
    if not isinstance(ref, Mapping) or not isinstance(graph, list):
        return detached, []
    scope = tuple(_text(ref.get(key)) for key in (
        "paper_id", "experimental_group_id", "source_digest",
    ))
    if not all(scope):
        return detached, []
    assigned: dict[tuple[str, str], str] = {}
    audit: list[dict[str, str]] = []

    def generated_id(kind: str, symbol: str, path: str) -> str:
        key = (kind, symbol)
        generated = assigned.get(key)
        if generated is None:
            if symbol in previous_ids:
                generated = symbol
            else:
                seed = "\0".join((*scope, kind, symbol))
                prefix = "material_" if kind == "material_id" else "instance_"
                generated = prefix + sha256(seed.encode("utf-8")).hexdigest()[:24]
            assigned[key] = generated
        audit.append({
            "field_path": path,
            "id_kind": kind,
            "model_symbol": symbol,
            "generated_id": generated,
        })
        return generated

    def visit(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child_path = f"{path}.{key}"
                if key in {"material_id", "material_instance_id"} and (
                    isinstance(value, str) and value.strip()
                ):
                    node[key] = generated_id(key, value.strip(), child_path)
                elif (key.endswith("material_instance_ids")
                      and isinstance(value, list)):
                    node[key] = [
                        generated_id("material_instance_id", item.strip(),
                                     f"{child_path}[{index}]")
                        if isinstance(item, str) and item.strip() else item
                        for index, item in enumerate(value)
                    ]
                else:
                    visit(value, child_path)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, f"{path}[{index}]")

    visit(graph, "material_graph")
    return detached, audit


def literal_quantity_present(excerpt: Any, value: Any, unit: Any) -> bool:
    """Malformed untrusted numbers or units abstain instead of crashing."""
    try:
        return evidence_contains_exact_quantity(excerpt, value, unit)
    except (OverflowError, ValueError, TypeError, AttributeError):
        return False


def dimensionless_numeric_field_pending(field_path: str, graph: Any) -> bool:
    """Recognize explicit dimensionless graph fields without approving a value.

    No literal verifier for pH, repetition counts, or ratios exists yet. A
    recognized unitless field therefore gets a distinct pending reason, while
    material quantities and unrecognized numeric parameters still require a
    physical unit. The field path and parameter name, never the number alone,
    select this branch.
    """
    parameter = _DIMENSIONLESS_PARAMETER_PATH.fullmatch(field_path)
    container_count = _DIMENSIONLESS_CONTAINER_COUNT_PATH.fullmatch(field_path)
    if parameter is None and container_count is None:
        return False
    match = parameter or container_count
    assert match is not None
    try:
        step = graph[int(match.group(1))]
        if parameter is not None:
            node = step["parameters"][int(match.group(2))]
            return (
                isinstance(node, Mapping)
                and _text(node.get("name")).casefold()
                in _DIMENSIONLESS_PARAMETER_NAMES
                and isinstance(node.get("unit", ""), str)
                and not node.get("unit", "").strip()
            )
        node = step["logical_containers"][int(match.group(2))]
        return isinstance(node, Mapping) and type(node.get("count")) is int
    except (IndexError, KeyError, TypeError, ValueError):
        return False


def material_identity_for_amount_path(
    field_path: str, graph: Any = None,
    facts: Sequence[Mapping[str, Any]] = (),
) -> tuple[str, bool]:
    """Return an exact proposed material label, never a chemical synonym.

    The boolean says whether this is a material amount that must have a
    resolvable literal identity. A sibling name fact can supply the label
    before the graph is compiled; conflicting names deliberately abstain.
    """
    match = _MATERIAL_AMOUNT_PATH.fullmatch(field_path)
    if match is None:
        return "", False
    prefix = match.group(1)
    port_match = re.search(
        r"\.material_(inputs|intermediates|outputs)\[([0-9]+)\]\Z",
        prefix,
    )
    label_paths = {prefix + ".name"}
    if port_match is None:
        label_paths.add(prefix + ".reagent_or_object")
    fact_names = {
        _text(fact.get("value")) for fact in facts
        if isinstance(fact, Mapping)
        and _text(fact.get("field_path")) in label_paths
        and _text(fact.get("value"))
    }
    graph_name = ""
    try:
        step_index = int(prefix.split("[", 1)[1].split("]", 1)[0])
        step = graph[step_index]
        node = step
        if port_match is not None:
            ports = _node_value(step, "material_" + port_match.group(1))
            node = ports[int(port_match.group(2))]
        graph_name = _text(_node_value(node, "name")) if port_match else (
            _text(_node_value(node, "reagent_or_object"))
            or _text(_node_value(node, "name"))
        )
    except (IndexError, KeyError, TypeError, ValueError, AttributeError):
        pass
    labels = fact_names | ({graph_name} if graph_name else set())
    required = port_match is not None or bool(labels)
    return (next(iter(labels)), required) if len(labels) == 1 else ("", required)


def quantity_has_local_attribution(
    excerpt: str, value: int | float, unit: str, *, identity: str = "",
    identity_required: bool = False, identity_surfaces: Sequence[str] = (),
    match_sink: dict[str, Any] | None = None,
) -> bool:
    """Check a literal amount belongs to one local clause and named entity.

    Exact value/unit presence alone would permit a sentence such as
    ``A 1 mmol and B 2 mmol`` to support the false claim ``A = 2 mmol``.
    This is intentionally conservative: ambiguous coordinated amounts are
    deferred to independent review, without deriving chemistry or synonyms.
    ``identity_surfaces`` carries the entity's verified label spellings
    (including case variants from its definition-site mentions); the scoped
    label rule never merges labels that differ beyond case and whitespace.
    """
    if not literal_quantity_present(excerpt, value, unit):
        return False
    if identity_required and not identity:
        return False
    normalized = re.sub(r"\s+", " ", excerpt.replace("−", "-").replace("–", "-").replace("µ", "u"))
    normalized_unit = unit.strip().replace("µ", "u")
    unit_pattern = r"\s*".join(
        re.escape(piece) for piece in re.split(r"\s+", normalized_unit)
    )
    amount_pattern = re.compile(
        rf"(?<![\w.])([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
        rf"\s*{unit_pattern}(?![\w/])",
        re.IGNORECASE,
    )
    clauses = [part.strip() for part in _LOCAL_CLAUSE.split(normalized) if part.strip()]
    amount_matches = [list(amount_pattern.finditer(part)) for part in clauses]
    try:
        amounts_by_clause = [
            [float(match.group(1)) for match in matches]
            for matches in amount_matches
        ]
    except (ValueError, OverflowError):
        return False
    if identity:
        spellings = [identity.strip(), *identity_surfaces]
        seen: set[str] = set()
        alternatives = []
        for spelling in spellings:
            candidate = re.sub(r"\s+", " ", spelling.replace("µ", "u")).strip()
            if candidate and candidate not in seen:
                seen.add(candidate)
                alternatives.append(re.escape(candidate))
        identity_pattern = re.compile(
            rf"(?<!\w)(?:{'|'.join(alternatives)})(?!\w)"
        )
        labeled = [
            (part, numbers, matches, list(identity_pattern.finditer(part)))
            for part, numbers, matches in zip(
                clauses, amounts_by_clause, amount_matches,
            )
            if numbers and identity_pattern.search(part)
        ]
        if len(labeled) != 1 or len(labeled[0][1]) != 1 or len(labeled[0][3]) != 1:
            return False
        _, numbers, matches, identities = labeled[0]
        amount, material = matches[0], identities[0]
        between = (
            labeled[0][0][material.end():amount.start()]
            if material.end() <= amount.start()
            else labeled[0][0][amount.end():material.start()]
        )
        if any(word.casefold() not in _LINK_WORDS for word in re.findall(
            r"[^\W\d_]\w*", between,
        )):
            return False
        if match_sink is not None:
            match_sink["identity_surface"] = material.group()
        return math.isclose(
            numbers[0], float(value), rel_tol=1e-12, abs_tol=1e-12,
        )
    amounts = [number for numbers in amounts_by_clause for number in numbers]
    return len(amounts) == 1 and math.isclose(
        amounts[0], float(value), rel_tol=1e-12, abs_tol=1e-12,
    )


def _scoped_claim(
    graph: list[Any], signature: Mapping[str, Any], path: str,
) -> tuple[Any, Mapping[str, Any] | None, str] | None:
    """Resolve an exact graph or route-signature field, without fuzzy matching."""
    chunks = path.split(".")
    match = _GRAPH_ROOT.fullmatch(chunks[0]) if chunks else None
    if len(chunks) < 2:
        return None
    if match is None:
        if chunks[0] != "route_signature":
            return None
        node: Any = signature
        owner: Mapping[str, Any] | None = None
    else:
        step_index = int(match.group(1))
        if step_index >= len(graph) or not isinstance(graph[step_index], Mapping):
            return None
        node = graph[step_index]
        owner = node
    unit = ""
    for chunk in chunks[1:]:
        item = _PATH_PART.fullmatch(chunk)
        if item is None or item.group(1) == "provenance" or not isinstance(node, Mapping):
            return None
        key = item.group(1)
        if key not in node:
            return None
        if key == "concentration_value":
            unit = _text(node.get("concentration_unit"))
        elif key == "value":
            unit = _text(node.get("unit"))
        node = node[key]
        if item.group(2) is not None:
            index = int(item.group(2))
            if not isinstance(node, list) or index >= len(node):
                return None
            node = node[index]
        if isinstance(node, Mapping) and isinstance(node.get("provenance"), Mapping):
            owner = node
    return node, owner, unit


def _required_qualitative_paths(
    graph: list[Any], signature: Mapping[str, Any],
) -> tuple[set[str], set[str]]:
    """Paper-derived chemistry must have literal same-group quotations.

    Structural IDs, graph relations and device capability selectors are
    checked structurally, not against prose. This demands a literal quote for
    the route's defining signature and each paper-claimed operation, material
    name, and state. A synonym is not promoted into evidence here.
    """
    signature_paths: set[str] = set()
    for key in ("route_family", "target_transformation", "endpoint_state"):
        if _text(signature.get(key)):
            signature_paths.add(f"route_signature.{key}")
    for key in ("precursor_roles", "reagent_roles", "operations", "control_modes"):
        items = signature.get(key)
        if isinstance(items, list):
            signature_paths.update(
                f"route_signature.{key}[{index}]"
                for index, value in enumerate(items) if _text(value)
            )
    transitions = signature.get("phase_transitions")
    if isinstance(transitions, list):
        for index, transition in enumerate(transitions):
            if isinstance(transition, Mapping):
                for key in ("before_state", "after_state", "confidence"):
                    if _text(transition.get(key)):
                        signature_paths.add(
                            f"route_signature.phase_transitions[{index}].{key}"
                        )

    graph_paths: set[str] = set()
    for step_index, step in enumerate(graph):
        if not isinstance(step, Mapping):
            continue
        prefix = f"material_graph[{step_index}]"
        for key in ("operation", "reagent_or_object"):
            if _text(step.get(key)):
                graph_paths.add(f"{prefix}.{key}")
        for port_kind in ("material_inputs", "material_intermediates", "material_outputs"):
            ports = step.get(port_kind)
            if not isinstance(ports, list):
                continue
            for port_index, port in enumerate(ports):
                if not isinstance(port, Mapping):
                    continue
                # Name and state are required for every material port.  The
                # requirement must not depend on whether the model happened
                # to fill a non-empty value: an unresolved state surfaces
                # here as an explicit required-fact dependency instead of
                # vanishing from the coverage check.
                graph_paths.add(f"{prefix}.{port_kind}[{port_index}].name")
                graph_paths.add(f"{prefix}.{port_kind}[{port_index}].state")
    return signature_paths, graph_paths


def _label_distinguishing_core(name: str) -> str:
    """The label residue that identifies a material, with role words removed.

    Controlled state tokens (``solution``) and stage/batch words (``the
    reaction``) describe a role or a stage, not an identity. A name whose
    residue is empty is a bare referential stage label; any non-empty residue
    is identity-bearing and must agree across every alias of one entity.
    Raw words are matched case-insensitively, but single-letter articles are
    stripped only in lowercase: the ``A`` of ``solution A`` is a designation,
    not an article, and remains part of the core.
    """
    residue = []
    for raw in re.findall(r"[^\W\d_]\w*", name):
        word = raw.casefold()
        if word in _GENERIC_LABEL_WORDS and (
                word not in {"a", "an", "the"} or raw.islower()):
            continue
        if normalize_material_state(word) != "unknown":
            continue
        residue.append(word)
    return " ".join(residue)


_GENERIC_LABEL_WORDS = frozenset({
    "a", "an", "the", "of", "batch", "mixture", "precursor", "product",
    "reaction", "stage",
})


def material_id_graph_issue(graph: Sequence[Any]) -> str:
    """Check internal ID references without pretending the IDs are prose.

    A paper-backed port name establishes the material entity separately. An
    opaque ID must be nonempty and quantity and relation references must
    resolve to declared ports. One ID may carry several port names in a step
    only when every name is a bare referential stage label (its distinguishing
    core is empty) AND continuity evidence says they are one batch: every
    diverse name must appear in the same ID's ports of a neighbouring step,
    unless every diverse port declares one shared instance ID. Empty cores
    alone never prove identity; the paper refers to one batch as ``the
    reaction``, ``the solution``, then ``the suspension`` while its state
    evolves, and forcing a single spelling would rewrite the source. Any name
    with a distinguishing core (``Solution A`` vs ``Solution B``) must keep
    the same core across every alias of the ID in the step, or the conflict
    stands. Upstream input references must identify an earlier output with
    the same material identity. A concrete instance ID cannot define batches
    in two sample arms. V2 checks state transitions and full cross-step
    lineage; the per-mention attribution gates bind every label's mentions to
    one entity.
    """
    instance_samples: dict[str, set[str]] = {}
    outputs_by_ref: dict[tuple[str, str], list[tuple[Any, str]]] = {}
    for step in graph:
        if not isinstance(step, Mapping):
            continue
        step_id = _text(step.get("macro_step_id"))
        sequence = step.get("sequence")
        outputs = step.get("material_outputs", []) or []
        if not isinstance(outputs, list):
            return "route_group_material_graph_invalid"
        for port in outputs:
            if not isinstance(port, Mapping):
                continue
            instance_id = _text(port.get("material_instance_id"))
            if step_id and instance_id:
                outputs_by_ref.setdefault((step_id, instance_id), []).append(
                    (sequence, _text(port.get("material_id")))
                )
    parent_consumers: set[tuple[str, str]] = set()
    input_names_by_step: dict[int, dict[str, set[str]]] = {}
    output_names_by_step: dict[int, dict[str, set[str]]] = {}
    for step_index, step in enumerate(graph):
        if not isinstance(step, Mapping):
            continue
        for port_kind, store in (
            ("material_inputs", input_names_by_step),
            ("material_intermediates", input_names_by_step),
            ("material_intermediates", output_names_by_step),
            ("material_outputs", output_names_by_step),
        ):
            ports = step.get(port_kind, []) or []
            if not isinstance(ports, list):
                continue
            by_id = store.setdefault(step_index, {})
            for port in ports:
                if not isinstance(port, Mapping):
                    continue
                material_id = _text(port.get("material_id"))
                material_name = re.sub(
                    r"\s+", " ", _text(port.get("name"))).casefold()
                if material_id and material_name:
                    by_id.setdefault(material_id, set()).add(material_name)
    for step_index, step in enumerate(graph):
        if not isinstance(step, Mapping):
            continue
        sample_id = _text(step.get("sample_id"))
        step_ids: set[str] = set()
        names_by_id: dict[str, set[str]] = {}
        raw_names_by_id: dict[str, set[str]] = {}
        instances_of_id: dict[str, set[str]] = {}
        instance_ids: dict[str, set[str]] = {}
        for port_kind in (
            "material_inputs", "material_intermediates", "material_outputs",
        ):
            ports = step.get(port_kind, [])
            if not isinstance(ports, list):
                continue
            instance_ids[port_kind] = set()
            for port in ports:
                if not isinstance(port, Mapping):
                    continue
                material_id = _text(port.get("material_id"))
                material_name = _text(port.get("name"))
                if not material_id or not material_name:
                    return "route_group_material_identity_missing"
                instance_id = _text(port.get("material_instance_id"))
                if instance_id:
                    if not sample_id:
                        return "route_group_material_instance_scope_missing"
                    instance_samples.setdefault(instance_id, set()).add(sample_id)
                    instance_ids[port_kind].add(instance_id)
                    instances_of_id.setdefault(material_id, set()).add(instance_id)
                else:
                    # A port without an instance ID cannot join a
                    # physical-continuity argument for its material.
                    instances_of_id.setdefault(material_id, set()).add("")
                step_ids.add(material_id)
                names_by_id.setdefault(material_id, set()).add(
                    re.sub(r"\s+", " ", material_name).casefold()
                )
                # Keep the original spelling: single-letter designations
                # (the ``A`` of ``solution A``) are recognized by case.
                raw_names_by_id.setdefault(material_id, set()).add(
                    re.sub(r"\s+", " ", material_name)
                )
                if port_kind == "material_inputs":
                    origin = _text(port.get("material_origin"))
                    parent_refs = port.get("parent_output_refs", []) or []
                    if not isinstance(parent_refs, list):
                        return "route_group_parent_output_reference_invalid"
                    if (origin == "upstream_output") != bool(parent_refs):
                        return "route_group_parent_output_reference_invalid"
                    for parent_ref in parent_refs:
                        if not isinstance(parent_ref, Mapping):
                            return "route_group_parent_output_reference_invalid"
                        parent_key = (
                            _text(parent_ref.get("macro_step_id")),
                            _text(parent_ref.get("material_instance_id")),
                        )
                        if not all(parent_key):
                            return "route_group_parent_output_reference_invalid"
                        matches = outputs_by_ref.get(parent_key, [])
                        if len(matches) != 1:
                            return "route_group_parent_output_reference_missing"
                        parent_sequence, parent_material_id = matches[0]
                        sequence = step.get("sequence")
                        if (type(parent_sequence) is not int
                                or type(sequence) is not int
                                or parent_sequence >= sequence):
                            return "route_group_parent_output_reference_not_earlier"
                        if parent_material_id != material_id:
                            return "route_group_parent_output_material_id_mismatch"
                        if parent_key in parent_consumers:
                            return "route_group_parent_output_reference_reused"
                        parent_consumers.add(parent_key)
        if any(len(names) != 1 for names in names_by_id.values()):
            # Bare stage labels may share one ID only when continuity
            # evidence says they are one batch: every diverse name must be
            # anchored in the same ID's ports of a neighbouring step (its
            # earlier outputs or later inputs), unless every diverse port
            # declares one shared instance ID. Empty cores alone never
            # prove identity, and any distinguishing core present must be
            # the same one for every alias of the ID in this step.
            cores = {
                _label_distinguishing_core(name)
                for material_id, names in raw_names_by_id.items()
                if len(names) != 1
                for name in names
                if _label_distinguishing_core(name)
            }
            if len(cores) > 1:
                return "route_group_material_id_identity_conflict"
            for material_id, names in names_by_id.items():
                if len(names) == 1:
                    continue
                declared = instances_of_id.get(material_id, set())
                continuous = len(declared) == 1 and "" not in declared
                if continuous:
                    continue
                anchored = True
                for name in names:
                    earlier = any(
                        name in output_names_by_step.get(earlier_index, {}).get(
                            material_id, set())
                        for earlier_index in range(step_index)
                    )
                    later = any(
                        name in input_names_by_step.get(later_index, {}).get(
                            material_id, set())
                        for later_index in range(step_index + 1, len(graph))
                    )
                    if not (earlier or later):
                        anchored = False
                        break
                if not anchored:
                    return "route_group_material_id_identity_conflict"
        relations = step.get("material_relations", []) or []
        if not isinstance(relations, list):
            return "route_group_material_relations_invalid"
        allowed_inputs = (instance_ids.get("material_inputs", set())
                          | instance_ids.get("material_intermediates", set()))
        allowed_outputs = (instance_ids.get("material_intermediates", set())
                           | instance_ids.get("material_outputs", set()))
        for relation in relations:
            if not isinstance(relation, Mapping):
                return "route_group_material_relations_invalid"
            for key, declared in (
                ("input_material_instance_ids", allowed_inputs),
                ("output_material_instance_ids", allowed_outputs),
            ):
                references = relation.get(key, [])
                if (not isinstance(references, list)
                        or any(not _text(item) for item in references)):
                    return "route_group_material_relation_reference_invalid"
                if any(item not in declared for item in references):
                    return "route_group_material_relation_reference_missing"
        requirements = step.get("quantity_requirements", []) or []
        if not isinstance(requirements, list):
            return "route_group_quantity_requirements_invalid"
        for requirement in requirements:
            if not isinstance(requirement, Mapping):
                continue
            referenced = _text(requirement.get("material_id"))
            if referenced and referenced not in step_ids:
                return "route_group_material_id_reference_missing"
    if any(len(samples) != 1 for samples in instance_samples.values()):
        return "route_group_material_instance_scope_conflict"
    return ""


def _numeric_leaves(graph: list[Any]) -> set[str]:
    found: set[str] = set()

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key == "provenance" or (
                    key == "sequence" and _GRAPH_ROOT.fullmatch(path)
                ):
                    continue
                walk(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            found.add(path)
        elif isinstance(value, str) and re.search(r"\d", value) and re.search(
            r"(?:\.value|_value)(?:\[[0-9]+\])?\Z", path
        ):
            found.add(path)

    for index, step in enumerate(graph):
        walk(step, f"material_graph[{index}]")
    return found


def _paper_placeholders(value: Any) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "provenance" and isinstance(child, Mapping):
                if child.get("kind") == "paper":
                    found.append(child)
            else:
                found.extend(_paper_placeholders(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_paper_placeholders(child))
    return found


def _replace_paper_placeholders(
    value: Any, provenance_by_fact_id: Mapping[str, dict[str, Any]],
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "provenance" and isinstance(child, dict) and child.get("kind") == "paper":
                fact_id = str(child["reference"])[len("fact:"):]
                value[key] = deepcopy(provenance_by_fact_id[fact_id])
            else:
                _replace_paper_placeholders(child, provenance_by_fact_id)
    elif isinstance(value, list):
        for child in value:
            _replace_paper_placeholders(child, provenance_by_fact_id)


def _fact_issue(
    raw: Any, *, paper_id: str, group_id: str, section: str,
    document_digest: str, graph: list[Any], signature: Mapping[str, Any],
    facts: Sequence[Mapping[str, Any]] = (),
    label_context: SourceLabelContext | None = None,
    binding_sink: dict[str, Any] | None = None,
) -> str:
    if not isinstance(raw, Mapping):
        return "route_fact_invalid"
    fact_id = _text(raw.get("fact_id"))
    field_path = _text(raw.get("field_path"))
    # Verification reads the full original quotation when a bounded trim
    # shortened the located excerpt; location stays with the short excerpt.
    excerpt = _text(raw.get("verification_excerpt")) or _text(raw.get("excerpt"))
    # This layer is pure and holds no PDF blocks, so it re-checks the part of
    # the source binding that does not need them: the located excerpt must be
    # literally contained in the context. Block-level binding of the context
    # happens at the association/receipt boundaries and again in the PDF
    # source verifier; each of those refuses a foreign or modified context.
    if raw.get("verification_excerpt") is not None:
        located = _text(raw.get("excerpt"))
        if (not located
                or normalize_pdf_quote_whitespace(located)
                not in normalize_pdf_quote_whitespace(excerpt)):
            return "verification_context_missing_located_excerpt"
    raw_unit = raw.get("unit", "")
    if not isinstance(raw_unit, str):
        return "route_fact_unit_invalid"
    unit = raw_unit.strip()
    source = raw.get("source")
    if not fact_id or not field_path:
        return "route_fact_identity_missing"
    if classify_route_field_basis(field_path) == "generated_id":
        return "route_fact_generated_id_paper_fact_forbidden"
    if not excerpt:
        return "route_fact_excerpt_missing"
    if raw.get("required", True) is not True:
        return "route_fact_required_must_be_true"
    if not isinstance(source, Mapping) or (
        _text(source.get("paper_id")) != paper_id
        or _text(source.get("experimental_group_id")) != group_id
        or _text(source.get("section")) != section
        or _text(source.get("source_digest")) != document_digest
    ):
        return "route_fact_experimental_group_mismatch"
    if _SOURCE_LOCATOR.fullmatch(_text(source.get("locator"))) is None:
        return "route_fact_locator_missing"
    claim = _scoped_claim(graph, signature, field_path)
    if claim is None:
        return "route_fact_graph_path_missing"
    actual, owner, graph_unit = claim
    claimed = raw.get("value")
    if isinstance(claimed, bool) or not isinstance(claimed, (int, float, str)):
        return "route_fact_value_invalid"
    if isinstance(claimed, float) and not math.isfinite(claimed):
        return "route_fact_value_invalid"
    derived_proof, _derived_issue = derive_unreviewed_output_state(
        graph, facts, field_path, paper_id=paper_id,
        experimental_group_id=group_id, source_digest=document_digest,
    ) if facts and field_path.endswith(".state") else (None, "")
    if is_material_port_state_path(field_path):
        if derived_proof is None:
            if output_state_parent_role_issue(field_path, graph, claimed, excerpt):
                return "parent_state_not_child_evidence"
            _mapping, mapping_issue = controlled_state_mapping(
                field_path, claimed, actual,
            )
            if mapping_issue:
                return mapping_issue
            context = label_context
            if context is None:
                context = build_source_label_context(graph, facts)
            outcome, binding = state_attribution_outcome(
                claimed, excerpt, field_path, graph, context,
            )
            if outcome == "pending":
                return "semantic_binding_pending"
            if outcome == "binding":
                if binding_sink is not None:
                    binding_sink["binding"] = binding
            elif (not isinstance(owner, Mapping)
                    or not state_source_locally_attributed(
                        claimed, excerpt, owner.get("name"),
                    )):
                # Only an entity the label context has never seen may still
                # pass through the legacy strictly-local matcher; a known
                # entity whose association check failed stays pending.
                return "semantic_binding_pending"
    elif isinstance(actual, bool) or actual != claimed:
        return "route_fact_graph_value_mismatch"
    if unit != graph_unit:
        return "route_fact_graph_value_mismatch"
    if isinstance(claimed, (int, float)):
        if output_quantity_role_issue(field_path, graph, unit):
            return "route_fact_quantity_role_mismatch"
        if not unit:
            return (
                "dimensionless_semantic_pending"
                if dimensionless_numeric_field_pending(field_path, graph)
                else "route_fact_numeric_unit_missing"
            )
        if not literal_quantity_present(excerpt, claimed, unit):
            return "route_fact_quantity_absent_from_excerpt"
        identity, identity_required = material_identity_for_amount_path(
            field_path, graph, facts,
        )
        context = label_context
        if context is None:
            context = build_source_label_context(graph, facts)
        surfaces = quantity_identity_surfaces(field_path, graph, context)
        match_sink: dict[str, Any] = {}
        if quantity_has_local_attribution(
            excerpt, claimed, unit, identity=identity,
            identity_required=identity_required, identity_surfaces=surfaces,
            match_sink=match_sink,
        ):
            surface = match_sink.get("identity_surface", "")
            if (surface and surface != identity.strip() and binding_sink is not None):
                binding_sink["binding"] = {
                    "schema_version": "source-label-binding/v1",
                    "rule_version": SOURCE_LABEL_RULE_VERSION,
                    "rule_id": RULE_SCOPED_LABEL_IDENTITY,
                    "label": surface.casefold(),
                    "source_surface": surface,
                    "entity_material_id": _port_material_for_path(
                        graph, field_path)[0],
                    "mention_anchor": surface_anchor(
                        field_path, graph, context, surface, excerpt),
                }
        else:
            binding = definition_site_concentration_binding(
                excerpt, claimed, unit, surfaces,
            )
            if binding is None:
                return "route_fact_quantity_attribution_unresolved"
            binding["entity_material_id"] = _port_material_for_path(
                graph, field_path)[0]
            anchor = surface_anchor(
                field_path, graph, context, binding["source_surface"], excerpt)
            if anchor:
                binding["mention_anchor"] = anchor
            if binding_sink is not None:
                binding_sink["binding"] = binding
    elif derived_proof is None and (not claimed.strip() or re.search(
        rf"(?<!\w){re.escape(claimed.strip())}(?!\w)", excerpt
    ) is None):
        return (
            "semantic_binding_pending"
            if classify_route_field_basis(field_path) == "controlled_mapping"
            else "route_fact_value_absent_from_excerpt"
        )
    return ""


def _compile_group(group: dict[str, Any], parent: Mapping[str, Any]) -> str:
    """Mutate only a detached group copy; return a diagnostic on abstention."""
    raw_facts = group.get("route_facts")
    if not isinstance(raw_facts, list) or not raw_facts:
        return "route_facts_missing_or_invalid"
    if "evidence_bundle" in group or "evidence_matrix" in group:
        return "route_fact_evidence_conflict"
    paper_id = _text(group.get("paper_id")) or _text(parent.get("paper_id"))
    group_id = _text(group.get("experimental_group_id"))
    source = group.get("source")
    if not isinstance(source, Mapping):
        return "route_group_source_missing"
    section = _text(source.get("section"))
    document_digest = _text(source.get("source_digest"))
    if (
        not paper_id or not group_id or not section
        or _DOCUMENT_DIGEST.fullmatch(document_digest) is None
    ):
        return "route_group_scope_missing"
    graph = group.get("material_graph")
    if not isinstance(graph, list) or not graph:
        return "route_group_material_graph_missing"
    # Inventory resolutions are produced upstream (before G2) by binding
    # approved inventory / material-spec records to external inputs.  This
    # compiler stays file-pure: it validates each record structurally and
    # trusts the resource digest the producer recorded; the file-based
    # re-verification happens at the source-audit layer.
    inventory_records = group.pop("inventory_resolutions", [])
    if not isinstance(inventory_records, list):
        return "route_group_inventory_resolution_invalid"
    inventory_by_path: dict[str, dict[str, str]] = {}
    for record in inventory_records:
        if not isinstance(record, Mapping):
            return "route_group_inventory_resolution_invalid"
        if record.get("status") != "resolved":
            continue
        path = _text(record.get("field_path"))
        token = _text(record.get("state"))
        item_id = _text(record.get("item_id"))
        digest = _text(
            record.get("register_digest") or record.get("resource_digest"))
        register = _text(record.get("register")) or "material-inventory/v1"
        if (not path or not token or not item_id
                or _DOCUMENT_DIGEST.fullmatch(digest) is None
                or register not in {
                    "material-inventory/v1", "candidate_supply_spec/v1"}
                or not is_material_port_state_path(path)):
            return "route_group_inventory_resolution_invalid"
        inventory_by_path[path] = {
            "item_id": item_id,
            "supply_form": _text(record.get("supply_form")),
            "record": _text(record.get("record")),
            "state": token,
            "register": register,
            "register_digest": digest,
            "candidate": bool(record.get("candidate")),
        }
    for step in graph:
        if not isinstance(step, Mapping):
            return "route_group_material_graph_invalid"
        for port_kind in ("material_inputs", "material_intermediates", "material_outputs"):
            ports = step.get(port_kind, [])
            if not isinstance(ports, list):
                return "route_group_material_graph_invalid"
            for port in ports:
                if not isinstance(port, Mapping) or _text(port.get("state")) in {"", "unknown"}:
                    return "route_group_material_state_missing"
    identity_issue = material_id_graph_issue(graph)
    if identity_issue:
        return identity_issue
    for name in ("target", "route_signature", "required_capabilities"):
        if not group.get(name):
            return f"route_group_{name}_missing"
    signature = group["route_signature"]
    if not isinstance(signature, Mapping):
        return "route_group_route_signature_invalid"

    seen_paths: set[str] = set()
    state_mapping_by_path: dict[str, dict[str, str]] = {}
    derived_state_by_path: dict[str, dict[str, str]] = {}
    excerpts_by_id: dict[str, tuple[str, Mapping[str, Any]]] = {}
    owner_fact_ids: dict[int, set[str]] = {}
    owner_nodes: dict[int, Mapping[str, Any]] = {}
    label_context = build_source_label_context(graph, raw_facts)
    label_binding_by_path: dict[str, dict[str, str]] = {}
    for raw in raw_facts:
        binding_sink: dict[str, Any] = {}
        issue = _fact_issue(
            raw, paper_id=paper_id, group_id=group_id,
            section=section, document_digest=document_digest, graph=graph,
            signature=signature, facts=raw_facts, label_context=label_context,
            binding_sink=binding_sink,
        )
        if issue:
            return issue
        field_path = raw["field_path"].strip()
        if "binding" in binding_sink:
            label_binding_by_path[field_path] = binding_sink["binding"]
        fact_id = raw["fact_id"].strip()
        scoped = _scoped_claim(graph, signature, field_path)
        if scoped is not None and is_material_port_state_path(field_path):
            derived, _ = derive_unreviewed_output_state(
                graph, raw_facts, field_path, paper_id=paper_id,
                experimental_group_id=group_id, source_digest=document_digest,
            )
            if derived is not None:
                derived_state_by_path[field_path] = derived
            else:
                state_mapping, _ = controlled_state_mapping(
                    field_path, raw["value"], scoped[0],
                )
                if state_mapping is not None:
                    state_mapping_by_path[field_path] = state_mapping
        if (scoped is not None and scoped[1] is not None
                and field_path not in derived_state_by_path):
            owner = scoped[1]
            owner_fact_ids.setdefault(id(owner), set()).add(fact_id)
            owner_nodes[id(owner)] = owner
        if field_path in seen_paths:
            return "route_fact_field_path_duplicate"
        seen_paths.add(field_path)
        earlier = excerpts_by_id.get(fact_id)
        # Formal evidence carries the same text verification used: the full
        # original quotation when a bounded trim shortened the located
        # excerpt (already containment-checked above and block-bound at the
        # association/receipt boundaries), otherwise the excerpt itself.
        evidence_excerpt = raw.get("verification_excerpt")
        if not isinstance(evidence_excerpt, str) or not evidence_excerpt.strip():
            evidence_excerpt = raw["excerpt"]
        identity = (evidence_excerpt.strip(), raw["source"])
        if earlier is not None and earlier != identity:
            return "route_fact_id_conflict"
        excerpts_by_id[fact_id] = identity

    for owner_key, owner in owner_nodes.items():
        provenance = owner.get("provenance")
        reference = _text(provenance.get("reference")) if isinstance(
            provenance, Mapping
        ) else ""
        if (
            not isinstance(provenance, Mapping)
            or provenance.get("kind") != "paper"
            or not reference.startswith("fact:")
            or reference[len("fact:"):] not in owner_fact_ids[owner_key]
        ):
            return "route_fact_graph_provenance_mismatch"

    # Canonicalize only states whose exact source phrase passed the fact
    # check above. V2 itself normalizes states on load; doing it here keeps
    # the graph and field-level mapping record synchronized before hashing.
    for path, record in state_mapping_by_path.items():
        match = re.fullmatch(
            r"material_graph\[([0-9]+)\]\."
            r"(material_inputs|material_intermediates|material_outputs)"
            r"\[([0-9]+)\]\.state", path,
        )
        assert match is not None
        graph[int(match.group(1))][match.group(2)][int(match.group(3))]["state"] = (
            record["target_value"]
        )

    if not _numeric_leaves(graph).issubset(seen_paths):
        return "route_fact_numeric_graph_claim_missing"
    signature_paths, graph_paths = _required_qualitative_paths(graph, signature)
    missing_signature = signature_paths - seen_paths
    # A verified inventory resolution discharges the paper-fact obligation
    # for an external-input state path.  The resolution must agree with the
    # graph state actually compiled.
    inventory_covered: dict[str, dict[str, str]] = {}
    for path, record in inventory_by_path.items():
        if path in seen_paths:
            continue
        scoped = _scoped_claim(graph, signature, path)
        if scoped is None or scoped[0] != record["state"]:
            return "route_group_inventory_resolution_invalid"
        inventory_covered[path] = record
    missing_graph = graph_paths - seen_paths - set(inventory_covered)
    if missing_signature:
        return "route_fact_signature_claim_missing"
    if missing_graph:
        return "route_fact_qualitative_graph_claim_missing"
    derived_fact_ids = {
        _text(raw.get("fact_id")) for raw in raw_facts
        if isinstance(raw, Mapping)
        and _text(raw.get("field_path")) in derived_state_by_path
    }
    for placeholder in _paper_placeholders(graph):
        reference = _text(placeholder.get("reference"))
        if set(placeholder) != {"kind", "reference"} or not reference.startswith("fact:"):
            return "route_fact_graph_provenance_mismatch"
        if (reference[len("fact:"):] not in excerpts_by_id
                or reference[len("fact:"):] in derived_fact_ids):
            return "route_fact_graph_reference_missing"

    evidence_bundle: list[dict[str, Any]] = []
    provenance_by_fact_id: dict[str, dict[str, Any]] = {}
    for fact_id, (excerpt, _source) in excerpts_by_id.items():
        if fact_id in derived_fact_ids:
            continue
        evidence_id = "route_fact_" + sha256(
            f"{paper_id}\0{group_id}\0{fact_id}".encode("utf-8")
        ).hexdigest()[:24]
        evidence_index = len(evidence_bundle)
        evidence_bundle.append({
            "evidence_id": evidence_id,
            "title": _text(parent.get("source_title")) or _text(group.get("source_title")),
            "verification_status": "unassessed",
            "full_text_status": "unknown",
            "excerpt": excerpt,
        })
        provenance_by_fact_id[fact_id] = {
            "kind": "paper",
            "reference": evidence_id,
            "evidence_class": "paper_explicit",
            "source_path": f"evidence_bundle.items[{evidence_index}].excerpt",
            "excerpt": excerpt,
            "source_digest": canonical_digest(excerpt),
        }

    matrix: list[dict[str, Any]] = []
    for raw in raw_facts:
        fact_id = raw["fact_id"].strip()
        source = raw["source"]
        field_path = raw["field_path"].strip()
        derivation = derived_state_by_path.get(field_path)
        field_provenance = (
            {
                "kind": "agent_inferred",
                "reference": derivation["operation_evidence_id"],
                "rationale": "same-state material lineage under a verified chemistry convention",
                "evidence_class": "chemistry_convention",
                "inference_rule": derivation["rule_id"],
                "derivation": canonical_state_derivation(derivation),
            }
            if derivation is not None else deepcopy(provenance_by_fact_id[fact_id])
        )
        matrix.append({
            "field_path": field_path,
            "value": raw["value"],
            "unit": _text(raw.get("unit")),
            "required": True,
            "status": "supported",
            "provenance": field_provenance,
            "evidence_id": "" if derivation is not None else provenance_by_fact_id[fact_id]["reference"],
            "source_scope": {
                "paper_id": paper_id,
                "experimental_group_id": group_id,
                "section": section,
                "locator": _text(source.get("locator")),
                "source_digest": document_digest,
            },
            **({"controlled_mapping": deepcopy(state_mapping_by_path[raw["field_path"].strip()])}
               if raw["field_path"].strip() in state_mapping_by_path else {}),
            **({"source_label_binding": deepcopy(label_binding_by_path[raw["field_path"].strip()])}
               if raw["field_path"].strip() in label_binding_by_path else {}),
        })
    group_scope = {
        "paper_id": paper_id,
        "experimental_group_id": group_id,
        "section": section,
        "locator": _text(source.get("locator")),
        "source_digest": document_digest,
    }
    for path, record in inventory_covered.items():
        matrix.append({
            "field_path": path,
            "value": record["state"],
            "unit": "",
            "required": True,
            "status": "supported",
            "provenance": {
                "kind": "inventory",
                "reference": record["item_id"],
                "rationale": (
                    "external-input state bound from an approved inventory "
                    "record" if record["register"] == "material-inventory/v1"
                    else "external-input state bound from a candidate supply "
                         "spec (form support only; stock NOT verified)"
                ),
                "evidence_class": "inventory_record",
                "excerpt": record["record"],
                "source_digest": record["register_digest"],
            },
            "evidence_id": "",
            "resolution_path": record["register"],
            "source_scope": deepcopy(group_scope),
        })
    _replace_paper_placeholders(graph, provenance_by_fact_id)
    group["evidence_bundle"] = evidence_bundle
    group["evidence_matrix"] = matrix
    return ""


def compile_experimental_group_protocols(
    protocols: Sequence[Mapping[str, Any]],
) -> RouteGroupCompilationResultV1:
    """Bind explicit route facts; preserve incomplete groups for discovery.

    A missing ``route_facts`` list is legacy extraction and is passed through
    untouched.  Groups that declare route facts but fail any binding check are
    also passed through unchanged, with an explicit diagnostic.  The function
    is pure with respect to the caller's protocol objects and reads no files.
    """
    result = RouteGroupCompilationResultV1()
    for protocol_index, original in enumerate(protocols):
        if not isinstance(original, Mapping):
            continue
        protocol = deepcopy(dict(original))
        groups = protocol.get("experimental_groups")
        if isinstance(groups, list):
            entries = [
                (group_index, group) for group_index, group in enumerate(groups)
                if isinstance(group, dict) and "route_facts" in group
            ]
        elif "route_facts" in protocol:
            entries = [(0, protocol)]
        else:
            entries = []
        for group_index, group in entries:
            trial = deepcopy(group)
            reason = _compile_group(trial, protocol)
            if reason:
                result.diagnostics.append(RouteGroupCompilationDiagnosticV1(
                    protocol_index=protocol_index,
                    group_index=group_index,
                    paper_id=_text(group.get("paper_id")) or _text(protocol.get("paper_id")),
                    experimental_group_id=_text(group.get("experimental_group_id")),
                    reason_code=reason,
                ))
            elif group is protocol:
                protocol = trial
            else:
                groups[group_index] = trial
        result.protocols.append(protocol)
    return result


__all__ = [
    "RouteGroupCompilationDiagnosticV1",
    "RouteGroupCompilationResultV1",
    "compile_experimental_group_protocols",
]
