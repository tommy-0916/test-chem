"""Deterministic repair of a collapsed split-event representation.

A source split (``divided into n parts``) may be represented by an empty
output list — the program constructs n children from the located operation —
or by explicit child ports covering every part.  A revision that collapses
the event into ONE output port carrying the part count as a material
quantity breaks the typed structure and changes the quantity's role.  This
repair undoes exactly that mistake on a detached copy:

* the wrong port and its numeric facts move to an audit row with the
  reason; nothing is silently deleted, and coverage is recomputed from the
  repaired graph by the normal gates;
* the source operation, its unique parent material, and the verified part
  count are preserved, so the existing structure constructor rebuilds the
  children in the current scope with fresh instance IDs;
* downstream references to the removed port are NOT re-pointed to the
  split parent: a whole-batch split with no stated remainder makes the
  children the only post-split material, so re-consuming the parent here
  would double-book it against the children.  The executable instance
  binding is withdrawn (the port keeps its source-backed name/state) and
  the reference is reported unresolved — no child is silently selected,
  and no merge of the children is assumed.  The only defensible merge is
  one the source itself states (e.g. an explicit collection sentence),
  which downstream model topology may still assert for review.

The part count always comes from the source event text; nothing here
hardcodes a task, a paper, or a number.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Mapping

from .route_pdf_material_structure import _id as _structured_child_id

_COUNT_UNITS = frozenset({
    "part", "parts", "portion", "portions", "fraction", "fractions",
    "aliquot", "aliquots",
})
_SPLIT_COUNT = re.compile(
    r"\bdivided\s+into\s+([1-9][0-9]*)\s+"
    r"(?:parts|portions|fractions|aliquots)\b",
    re.IGNORECASE,
)
_PORT_LISTS = ("material_inputs", "material_intermediates", "material_outputs")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def split_count_from_operation(operation: Any) -> int | None:
    """The verified part count of a split event in its source operation."""
    match = _SPLIT_COUNT.search(_text(operation))
    return int(match.group(1)) if match else None


def repair_split_event_representation(
    proposal: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Undo count-as-quantity split ports; keep everything else intact.

    Returns ``(repaired_proposal, audit_rows, unresolved_rows)``.  The
    repaired proposal is a detached copy; every removal and withdrawal is
    recorded.  Unresolved rows name references whose deterministic target
    does not exist (revoked split children in downstream ports, relation
    refs into revoked ports); they stay explicit for the gates and the
    review to judge.
    """
    repaired = deepcopy(dict(proposal))
    graph = repaired.get("material_graph")
    facts = repaired.get("route_facts")
    if not isinstance(graph, list) or not isinstance(facts, list):
        return repaired, [], []
    audit: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    for step_index, step in enumerate(graph):
        if not isinstance(step, Mapping):
            continue
        count = split_count_from_operation(step.get("operation"))
        if count is None:
            continue
        outputs = step.get("material_outputs")
        if not isinstance(outputs, list):
            continue
        wrong = [
            (port_index, port) for port_index, port in enumerate(outputs)
            if isinstance(port, Mapping)
            and _text((port.get("quantity") or {}).get("unit")
                      if isinstance(port.get("quantity"), Mapping)
                      else "").casefold() in _COUNT_UNITS
        ]
        if not wrong:
            continue
        inputs = [p for p in step.get("material_inputs", []) or []
                  if isinstance(p, Mapping)]
        parents = {_text(p.get("material_instance_id")) for p in inputs}
        parents.discard("")
        if len(parents) != 1:
            unresolved.append({
                "step_index": step_index,
                "reason": ("split step does not declare exactly one parent "
                           "instance; cannot repair deterministically"),
            })
            continue
        parent_instance = next(iter(parents))

        removed_instances: set[str] = set()
        for port_index, port in reversed(wrong):
            instance_id = _text(port.get("material_instance_id"))
            audit.append({
                "kind": "split_count_quantity_revoked",
                "step_index": step_index,
                "field_path": f"material_graph[{step_index}]."
                              f"material_outputs[{port_index}]",
                "removed_port": deepcopy(dict(port)),
                "part_count": count,
                "reason": ("a part count expressed as one output material "
                           "quantity cannot expand into the verified number "
                           "of children and changes the quantity role; the "
                           "count lives in the source operation and the "
                           "constructed children"),
            })
            if instance_id:
                removed_instances.add(instance_id)
            outputs.pop(port_index)

        kept_facts = []
        for fact in facts:
            path = _text(fact.get("field_path")) if isinstance(fact, Mapping) else ""
            if any(path.startswith(f"material_graph[{step_index}]"
                                   f".material_outputs[{pi}].")
                   for pi, _port in wrong):
                audit.append({
                    "kind": "split_count_fact_revoked",
                    "field_path": path,
                    "removed_fact": deepcopy(fact),
                    "reason": "fact targeted the revoked count-as-quantity port",
                })
                continue
            kept_facts.append(fact)
        facts[:] = kept_facts

        if not removed_instances:
            continue
        candidate_children = _constructed_child_ids(repaired, step_index, count)
        for later_index in range(step_index + 1, len(graph)):
            later = graph[later_index]
            if not isinstance(later, Mapping):
                continue
            for kind in _PORT_LISTS:
                for port in later.get(kind, []) or []:
                    if not isinstance(port, Mapping):
                        continue
                    if _text(port.get("material_instance_id")) in removed_instances:
                        removed_ref = _text(port.get("material_instance_id"))
                        port["material_instance_id"] = None
                        audit.append({
                            "kind": "downstream_reference_withdrawn",
                            "step_index": later_index,
                            "field_path": f"material_graph[{later_index}].{kind}",
                            "from_instance": removed_ref,
                            "split_parent_instance": parent_instance,
                            "candidate_child_instances": candidate_children,
                            "part_count": count,
                            "reason": ("the revoked port's instance no longer "
                                       "exists; the split children are the only "
                                       "post-split material and the parent stays "
                                       "in lineage only; the executable binding "
                                       "is withdrawn rather than re-pointed to "
                                       "the parent (no double-booked parent, no "
                                       "silently selected child, no assumed "
                                       "merge)"),
                        })
                        unresolved.append({
                            "step_index": later_index,
                            "reason": (f"{kind} port referenced revoked split "
                                       f"port instance {removed_ref!r}; "
                                       "downstream processing target stays "
                                       "unresolved pending review (candidate "
                                       "set: the constructed split children; "
                                       "merge only if the source states one)"),
                        })
            for relation in later.get("material_relations", []) or []:
                if not isinstance(relation, Mapping):
                    continue
                for key in ("input_material_instance_ids",
                            "output_material_instance_ids"):
                    refs = relation.get(key)
                    if isinstance(refs, list) and any(
                            _text(r) in removed_instances for r in refs):
                        unresolved.append({
                            "step_index": later_index,
                            "reason": (f"relation {key} references a revoked "
                                       "split port; no deterministic child "
                                       "mapping exists"),
                        })
    return repaired, audit, unresolved


def _constructed_child_ids(
    proposal: Mapping[str, Any], step_index: int, count: int,
) -> list[str]:
    """The child instance IDs the typed constructor will generate here.

    Same deterministic scheme as the structure constructor (sha256 over
    scope/kind/step-id/ordinal); after this repair the split step's outputs
    are empty, so the constructor generates exactly these IDs.  They name
    the candidate set for the withdrawn downstream reference; selecting one
    or merging them is a reviewed decision, never this repair's.  An empty
    list means the scope was unusable and the row names only parent+count.
    """
    ref = proposal.get("source_group_ref")
    graph = proposal.get("material_graph") or []
    step = graph[step_index] if step_index < len(graph) else None
    if not isinstance(ref, Mapping) or not isinstance(step, Mapping):
        return []
    scope = tuple(_text(ref.get(key)) for key in (
        "paper_id", "experimental_group_id", "source_digest"))
    step_id = _text(step.get("macro_step_id"))
    if not all(scope) or not step_id:
        return []
    return [_structured_child_id(scope, "child_local", step_id, ordinal)
            for ordinal in range(count)]


__all__ = [
    "repair_split_event_representation",
    "split_count_from_operation",
]
