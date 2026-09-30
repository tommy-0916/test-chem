"""Source resolution for external-input material states.

Fixed evidence order for a material port's state:

1. the paper states it — bound by the existing paper-fact gates;
2. an approved inventory / material-spec record (this module) uniquely
   binds the concrete material and its verified supply form;
3. neither exists — the state stays an explicit unresolved dependency in
   the unreviewed draft; no default is guessed and formal publication keeps
   blocking until it resolves.

Inventory records are real sources, not chemistry conventions: they are
carried as ``inventory_record`` evidence with the resource digest, never as
``paper_explicit`` and never as ``chemistry_convention``.  Eligibility does
not depend on the model's ``material_origin`` claim — that claim is never
proof of inventory: any first-appearance input without upstream production
evidence may be resolved from an approved record.  A port that shows
upstream evidence but claims ``external_inventory`` is refused by this
path — re-labeling an intermediate product as inventory cannot bypass
upstream state requirements.
"""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from .v2 import normalize_material_state

INVENTORY_SCHEMA = "material-inventory/v1"
_INVENTORY_RESOURCE = (
    Path(__file__).resolve().parents[1]
    / "chem_resources" / "material_inventory" / "v1.json"
)

RESOLVED = "resolved"
SPEC_MISMATCH = "spec_mismatch"
SOURCE_MISSING = "source_missing"
LINEAGE_BLOCKED = "lineage_blocked"
AMBIGUOUS = "ambiguous"

# An operation that expresses dissolving the named material is explicit
# evidence about the required supply form: a stock record supplied as a
# solution contradicts it.  A bare amount unit (e.g. mmol) never proves a
# form by itself.
_DISSOLVING_VERB = re.compile(r"dissol\w*", re.IGNORECASE)
_SOLVENT_INTRODUCTION = re.compile(r"\b(?:in|into|using|with)\b", re.IGNORECASE)
_PASSIVE_AUXILIARY = re.compile(r"\b(?:was|were|is|are)\b", re.IGNORECASE)
_OTHER_CLAUSE = re.compile(
    r"[,;\n!?]|(?<!\d)\.(?!\d)|\b(?:then|after|before)\b", re.IGNORECASE,
)


def _requires_non_solution_form(
    facts: Sequence[Any], name: str, step_index: int,
    step_operation: str = "",
) -> bool:
    """Whether source text explicitly says this step dissolves the material."""
    if not name.strip():
        return False
    name_pattern = re.compile(
        rf"(?<!\w){re.escape(name.strip())}(?!\w)", re.IGNORECASE,
    )
    prefix = f"material_graph[{step_index}]."
    blobs = [step_operation] if step_operation else []
    for fact in facts:
        path = _entry_text(fact, "field_path")
        if not path.startswith(prefix):
            continue
        excerpt = _entry_text(fact, "excerpt")
        if excerpt:
            blobs.append(excerpt)
    for blob in blobs:
        for match in name_pattern.finditer(blob):
            before = blob[max(0, match.start() - 160):match.start()]
            preceding = list(_DISSOLVING_VERB.finditer(before))
            if preceding:
                between = before[preceding[-1].end():]
                # In "dissolve NaOH in water", water is the solvent, not a
                # solute required to arrive in non-solution form.
                if _SOLVENT_INTRODUCTION.search(between):
                    continue
                if not _OTHER_CLAUSE.search(between):
                    return True
            after = blob[match.end():match.end() + 160]
            following = _DISSOLVING_VERB.search(after)
            if following:
                between = after[:following.start()]
                if (_PASSIVE_AUXILIARY.search(between)
                        and not _SOLVENT_INTRODUCTION.search(between)
                        and not _OTHER_CLAUSE.search(between)):
                    return True
    return False


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _entry_text(record: Any, key: str) -> str:
    if isinstance(record, Mapping):
        return _text(record.get(key))
    return _text(getattr(record, key, None))


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def load_inventory_resource(
    path: str | Path | None = None,
) -> tuple[list[Mapping[str, Any]], str]:
    """Read the versioned inventory bytes; empty items are a valid state."""
    resource = Path(path) if path is not None else _INVENTORY_RESOURCE
    try:
        raw = resource.read_bytes()
        payload = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return [], ""
    if (not isinstance(payload, Mapping)
            or payload.get("schema") != INVENTORY_SCHEMA
            or not isinstance(payload.get("items"), list)):
        return [], ""
    items = [item for item in payload["items"] if isinstance(item, Mapping)]
    return items, "sha256_" + sha256(raw).hexdigest()


def _upstream_evidence(graph: Sequence[Any], step_index: int, port: Mapping) -> bool:
    """Whether the port's identity was produced upstream in this graph."""
    if _text(port.get("material_origin")) == "upstream_output":
        return True
    if isinstance(port.get("parent_output_refs"), list) and port.get("parent_output_refs"):
        return True
    material_id = _text(port.get("material_id"))
    name = _norm(_text(port.get("name")))
    for earlier in graph[:step_index]:
        if not isinstance(earlier, Mapping):
            continue
        for kind in ("material_intermediates", "material_outputs"):
            ports = earlier.get(kind, [])
            if not isinstance(ports, list):
                continue
            for candidate in ports:
                if not isinstance(candidate, Mapping):
                    continue
                if material_id and _text(candidate.get("material_id")) == material_id:
                    return True
                if name and _norm(_text(candidate.get("name"))) == name and name:
                    return True
    return False


def _concentration_spec(record: Mapping) -> tuple[float, str] | None:
    """A complete, finite concentration; absent or invalid specs prove nothing."""
    value = record.get("concentration_value")
    unit = _text(record.get("concentration_unit"))
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not unit:
        return None
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return None
    if math.isfinite(numeric) and numeric > 0:
        return numeric, unit
    return None


def _has_concentration_requirement(port: Mapping) -> bool:
    # Null defaults do not assert a concentration. A value or a unit alone
    # still expresses an incomplete requirement, which must not disappear.
    return (port.get("concentration_value") is not None
            or bool(_text(port.get("concentration_unit"))))


def resolve_external_input_states(
    graph: Sequence[Any],
    inventory_items: Sequence[Mapping[str, Any]] = (),
    facts: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    """Classify every material port's state source.

    First-appearance input ports with no upstream production evidence are
    eligible regardless of the model's ``material_origin`` claim (the claim
    itself is never proof of inventory).  Each record names the port, the
    required information, and — when resolved — the exact record that
    provides it.  Conclusions are reported separately: ``source_missing``
    (no record), ``spec_mismatch`` (a record contradicts the material form
    or operation constraint), ``ambiguous`` (several records cannot be
    distinguished), and ``lineage_blocked``.
    """
    records: list[dict[str, Any]] = []
    for step_index, step in enumerate(graph):
        if not isinstance(step, Mapping):
            continue
        for port_kind in ("material_inputs", "material_intermediates", "material_outputs"):
            ports = step.get(port_kind, [])
            if not isinstance(ports, list):
                continue
            for port_index, port in enumerate(ports):
                if not isinstance(port, Mapping):
                    continue
                path = (f"material_graph[{step_index}].{port_kind}"
                        f"[{port_index}].state")
                origin = _text(port.get("material_origin"))
                upstream = _upstream_evidence(graph, step_index, port)
                if upstream:
                    if origin == "external_inventory":
                        records.append({
                            "field_path": path,
                            "material_id": _text(port.get("material_id")),
                            "name": _text(port.get("name")),
                            "origin": origin,
                            "status": LINEAGE_BLOCKED,
                            "reason": ("port claims external_inventory but has "
                                       "upstream production evidence; resolve the "
                                       "state from upstream, not from inventory"),
                        })
                    # Upstream-sourced ports keep their existing obligations
                    # (paper facts or lineage); this path is not inventory
                    # business.
                    continue
                if port_kind != "material_inputs":
                    continue
                base = {
                    "field_path": path,
                    "material_id": _text(port.get("material_id")),
                    "name": _text(port.get("name")),
                    "origin": origin or "external_input",
                }
                name = _norm(base["name"])
                candidates = [
                    item for item in inventory_items
                    if _norm(_text(item.get("material_name"))) == name and name
                ]
                if not candidates:
                    records.append({
                        **base,
                        "status": SOURCE_MISSING,
                        "reason": ("no approved inventory, material-spec, or "
                                   "candidate supply-spec record matches this "
                                   "external input"),
                    })
                    continue
                requires_non_solution = _requires_non_solution_form(
                    facts, base["name"], step_index,
                    _text(step.get("operation")),
                )
                requires_concentration = _has_concentration_requirement(port)
                port_spec = _concentration_spec(port)
                compatible = [
                    item for item in candidates
                    if normalize_material_state(_text(item.get("supply_form")))
                    != "unknown"
                    and not (
                        requires_non_solution
                        and normalize_material_state(_text(item.get("supply_form")))
                        == "solution"
                    )
                    and (
                        not requires_concentration
                        or (port_spec is not None
                            and _concentration_spec(item) == port_spec)
                    )
                ]
                chosen: Mapping[str, Any] | None = None
                if len(compatible) == 1:
                    chosen = compatible[0]
                if chosen is None:
                    if not compatible:
                        records.append({
                            **base,
                            "status": SPEC_MISMATCH,
                            "reason": ("no matching record proves a controlled "
                                       "supply form compatible with the explicit "
                                       "operation and concentration requirements; "
                                       "dissolving requires a non-solution form, "
                                       "and declared concentrations require an "
                                       "equal finite value and unit"),
                            "candidates": [_text(item.get("item_id"))
                                           for item in candidates],
                        })
                    else:
                        records.append({
                            **base,
                            "status": AMBIGUOUS,
                            "reason": ("several records match and cannot be "
                                       "distinguished from the declared input"),
                            "candidates": [_text(item.get("item_id"))
                                           for item in candidates],
                        })
                    continue
                supply = _text(chosen.get("supply_form"))
                token = normalize_material_state(supply)
                if token == "unknown":
                    records.append({
                        **base,
                        "status": SPEC_MISMATCH,
                        "reason": ("the record's supply form is not expressible "
                                   "in the controlled state vocabulary"),
                        "candidates": [_text(chosen.get("item_id"))],
                    })
                    continue
                constraints = [
                    _text(item) for item in chosen.get("operation_constraints", [])
                    if isinstance(item, str)
                ] if isinstance(chosen.get("operation_constraints"), list) else []
                records.append({
                    **base,
                    "status": RESOLVED,
                    "item_id": _text(chosen.get("item_id")),
                    "supply_form": supply,
                    "state": token,
                    "state_mapping": (
                        f"material-states/v1 alias: {supply} -> {token}"),
                    "record": _text(chosen.get("record")),
                    "operation_constraints": constraints,
                    "candidate": bool(chosen.get("candidate_supply_spec")),
                    "reason": ("unique compatible record for this external "
                               "input" + (" (candidate supply spec, stock not "
                                          "verified)" if chosen.get("candidate_supply_spec")
                                          else "")),
                })
    return records


def apply_inventory_resolutions(
    graph: Sequence[Any],
    resolutions: Sequence[Mapping[str, Any]],
) -> list[Any]:
    """Fill resolved external-input states on a detached graph copy."""
    detached = deepcopy(list(graph))
    for record in resolutions:
        if record.get("status") != RESOLVED:
            continue
        match = re.fullmatch(
            r"material_graph\[([0-9]+)\]\."
            r"(material_inputs|material_intermediates|material_outputs)"
            r"\[([0-9]+)\]\.state", _text(record.get("field_path")),
        )
        if match is None:
            continue
        try:
            port = detached[int(match.group(1))][match.group(2)][int(match.group(3))]
        except (IndexError, KeyError, TypeError):
            continue
        if isinstance(port, dict):
            port["state"] = record["state"]
    return detached


def verified_resolutions(
    resolutions: Sequence[Mapping[str, Any]],
    inventory_items: Sequence[Mapping[str, Any]],
    inventory_digest: str,
    register: str = "material-inventory/v1",
) -> dict[str, dict[str, Any]]:
    """Resolution records whose item still exists in the named register.

    A record only verifies against the register it cites: a candidate
    supply-spec never verifies against approved inventory bytes and vice
    versa.
    """
    by_id: dict[str, Mapping[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for item in inventory_items:
        item_id = _text(item.get("item_id"))
        if not item_id:
            continue
        if item_id in by_id:
            duplicate_ids.add(item_id)
        by_id[item_id] = item
    verified: dict[str, dict[str, Any]] = {}
    for record in resolutions:
        if record.get("status") != RESOLVED:
            continue
        if (_text(record.get("register")) or "material-inventory/v1") != register:
            continue
        item_id = _text(record.get("item_id"))
        if item_id in duplicate_ids:
            continue
        item = by_id.get(item_id)
        if item is None or not inventory_digest:
            continue
        cited = _text(
            record.get("register_digest") or record.get("resource_digest"))
        if cited != inventory_digest:
            continue
        token = normalize_material_state(_text(item.get("supply_form")))
        if token == "unknown" or token != record.get("state"):
            continue
        # Older compact records omit the name. When a resolver record carries
        # source details, none may contradict the item identified by its digest.
        if ("name" in record and
                _norm(_text(record.get("name")))
                != _norm(_text(item.get("material_name")))):
            continue
        if ("supply_form" in record and
                _norm(_text(record.get("supply_form")))
                != _norm(_text(item.get("supply_form")))):
            continue
        if ("record" in record and
                _text(record.get("record")) != _text(item.get("record"))):
            continue
        verified[_text(record.get("field_path"))] = {
            "item_id": _text(item.get("item_id")),
            "supply_form": _text(item.get("supply_form")),
            "record": _text(item.get("record")),
            "resource_digest": inventory_digest,
        }
    return verified


__all__ = [
    "AMBIGUOUS",
    "INVENTORY_SCHEMA",
    "LINEAGE_BLOCKED",
    "RESOLVED",
    "SOURCE_MISSING",
    "SPEC_MISMATCH",
    "apply_inventory_resolutions",
    "load_inventory_resource",
    "resolve_external_input_states",
    "verified_resolutions",
]
